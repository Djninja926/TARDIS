#!/usr/bin/env python3
"""
trace_families.py - group near-identical traces ("families") within a dataset.

Why: some block datasets contain many volumes that are close to clones of one
workload (e.g. seven tencentBlock_v2 volumes with 10.4-10.9M requests, ~17GB
footprint, LRU miss 0.497-0.508 and forgive gaps of 7.1-7.3pp). Counting each
as an independent sample overstates the evidence, and a random train/test
split for tuning would leak: a "held-out" trace could be a clone of a training
trace. So:
  * the analyzer reports family counts and family-weighted means, and
  * the tuning planner splits train/test by FAMILY, never by trace.

Two traces in the same dataset are linked when ALL of these hold:
    requests within REQ_TOL (relative)
    footprint within FP_TOL (relative)     footprint = LRU 10% cache bytes / 0.1
    LRU miss at 10% within MISS_TOL (absolute)
Families are the connected components of that relation (union-find). Requiring
behavioural agreement (miss ratio), not just matching size, keeps unrelated
volumes that merely happen to be the same size from being merged.

Usage as a module:
    from trace_families import load_trace_stats, families
    stats = load_trace_stats("merged_all_nodes.csv")   # {(ds, trace): {...}}
    fam = families(stats)                              # {(ds, trace): family_id}
Standalone:
    python trace_families.py merged_all_nodes.csv [--top 10]
"""
import argparse, csv
from collections import defaultdict

REQ_TOL = 0.03
FP_TOL = 0.03
MISS_TOL = 0.02


def load_trace_stats(path, ref_pct="10", frac=0.1):
    """{(dataset, trace): {"requests", "footprint", "lru_miss"}} from the sweep CSV,
    using the LRU row at ref_pct. Traces without that row are omitted."""
    stats = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("policy") != "lru" or r.get("cache_pct") != ref_pct:
                continue
            try:
                req = int(r["requests"])
                fp = float(r["size_bytes"]) / frac
                miss = float(r["miss"])
            except (TypeError, ValueError):
                continue
            stats[(r["dataset"], r["trace"])] = {"requests": req, "footprint": fp, "lru_miss": miss}
    return stats


def families(stats, req_tol=REQ_TOL, fp_tol=FP_TOL, miss_tol=MISS_TOL):
    """{(dataset, trace): family_id}; family_id is '<dataset>#<n>', singletons included.

    Centroid clustering, NOT connected components. Pairwise linking chains:
    in a dense bucket a 3% step rule links 3.4M-request volumes to 4.6M-request
    ones through intermediates, producing 100-trace "families" that are really a
    continuum. Here a trace joins a family only if it is within tolerance of the
    family's running centroid, which bounds each family's spread to about the
    tolerance on either side."""
    per_ds = defaultdict(list)
    by_ds = defaultdict(list)
    for key, s in stats.items():
        by_ds[key[0]].append(key)

    for ds, keys in by_ds.items():
        keys.sort(key=lambda k: (stats[k]["requests"], k[1]))
        fams = []          # each: {"members": [...], "req": c, "fp": c, "miss": c}
        for k in keys:
            s = stats[k]
            best, best_d = None, None
            for fm in reversed(fams):
                if fm["req"] * (1 + req_tol) < s["requests"]:
                    break      # sorted by requests: older families are only further away
                if abs(s["requests"] - fm["req"]) > req_tol * fm["req"]:
                    continue
                if fm["fp"] > 0 and abs(s["footprint"] - fm["fp"]) > fp_tol * fm["fp"]:
                    continue
                if abs(s["lru_miss"] - fm["miss"]) > miss_tol:
                    continue
                d = abs(s["requests"] / fm["req"] - 1) + abs(s["lru_miss"] - fm["miss"])
                if best_d is None or d < best_d:
                    best, best_d = fm, d
            if best is None:
                fams.append({"members": [k], "req": s["requests"],
                             "fp": s["footprint"], "miss": s["lru_miss"]})
            else:
                m = best["members"]
                n = len(m)
                best["req"] = (best["req"] * n + s["requests"]) / (n + 1)
                best["fp"] = (best["fp"] * n + s["footprint"]) / (n + 1)
                best["miss"] = (best["miss"] * n + s["lru_miss"]) / (n + 1)
                m.append(k)
        per_ds[ds] = [sorted(fm["members"]) for fm in fams]

    out = {}
    for ds, fams in per_ds.items():
        fams.sort(key=lambda m: (-len(m), m[0][1]))
        for n, members in enumerate(fams):
            for key in members:
                out[key] = f"{ds}#{n}"
    return out


def summarize(fam):
    """{dataset: (n_traces, n_families, largest_family_size)}"""
    sizes = defaultdict(lambda: defaultdict(int))
    for (ds, _), fid in fam.items():
        sizes[ds][fid] += 1
    return {ds: (sum(f.values()), len(f), max(f.values())) for ds, f in sizes.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--top", type=int, default=5, help="show the N largest families per dataset")
    args = ap.parse_args()
    stats = load_trace_stats(args.csv)
    fam = families(stats)
    summ = summarize(fam)
    print(f"{'dataset':>24} | {'traces':>6} {'families':>8} {'largest':>7} {'in multi-trace fams':>20}")
    members = defaultdict(list)
    for key, fid in fam.items():
        members[fid].append(key[1])
    for ds in sorted(summ, key=lambda d: -summ[d][0]):
        n, nf, big = summ[ds]
        multi = sum(len(m) for fid, m in members.items() if fid.startswith(ds + "#") and len(m) > 1)
        print(f"{ds:>24} | {n:>6} {nf:>8} {big:>7} {multi:>13} ({100 * multi / n:4.1f}%)")
    if args.top:
        print()
        for ds in sorted(summ, key=lambda d: -summ[d][0]):
            fams = sorted(((fid, m) for fid, m in members.items() if fid.startswith(ds + "#")),
                          key=lambda x: -len(x[1]))
            shown = [(fid, m) for fid, m in fams[:args.top] if len(m) > 1]
            for fid, m in shown:
                s = [stats[(ds, t)] for t in m]
                print(f"  {fid:<28} size {len(m):>4}  requests {min(x['requests'] for x in s)/1e6:.2f}-"
                      f"{max(x['requests'] for x in s)/1e6:.2f}M  LRU@10% "
                      f"{min(x['lru_miss'] for x in s):.3f}-{max(x['lru_miss'] for x in s):.3f}  "
                      f"e.g. {', '.join(sorted(m)[:3])}")


if __name__ == "__main__":
    main()
