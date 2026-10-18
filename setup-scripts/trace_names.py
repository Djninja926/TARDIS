#!/usr/bin/env python3
"""
trace_names.py - ONE rule for turning a trace URL into (dataset, trace name).

Every script that reads a trace list (analyzer, tuning planner, tuner,
comparison) uses this, and node_cachesim_worker.sh implements the same rule in
bash. If the rule ever changes, change it here AND in the worker.

  CMU mirror    .../cacheDatasets/alibabaBlock/new/1M/alibabaBlock_9.oracleGeneral.zst
                -> dataset "alibabaBlock_new_1M", name "alibabaBlock_9"
  Wikimedia     .../published/datasets/caching/2019/text/cache-t-00.gz
                -> dataset "wikimedia_2019_text", name "cache-t-00"

dataset = the path under the root, minus the filename, with "/" -> "_"
          (Wikimedia gets a "wikimedia_" prefix so it can never collide).
name    = the filename up to ".oracleGeneral"; for files that are not
          oracleGeneral (the Wikimedia TSVs), the filename with .gz / .zst /
          .tsv / .csv / .txt stripped.

Also holds the trace-FAMILY grouping (near-identical traces) used by the
analyzer, the tuning planner and compare_tuned.py.
"""
import csv
from collections import defaultdict

ROOTS = (("/cacheDatasets/", ""),
         ("/published/datasets/caching/", "wikimedia_"))
EXTS = (".gz", ".zst", ".tsv", ".csv", ".txt")


def dataset_of(url):
    for root, prefix in ROOTS:
        if root in url:
            path = url.split(root, 1)[1].rsplit("/", 1)[0]
            return prefix + path.replace("/", "_") if path else "unknown"
    return "unknown"


def trace_name(url):
    base = url.rsplit("/", 1)[-1]
    if ".oracleGeneral" in base:
        return base.split(".oracleGeneral")[0]
    changed = True
    while changed:
        changed = False
        for ext in EXTS:
            if base.endswith(ext) and len(base) > len(ext):
                base = base[: -len(ext)]
                changed = True
    return base


def needs_conversion(url):
    """True for traces that are not oracleGeneral (the Wikimedia TSV.gz files)."""
    return ".oracleGeneral" not in url.rsplit("/", 1)[-1]


def read_list(path):
    """[(dataset, name, url)] from a list of '<dataset>\\t<url>' or bare-url lines.
    The dataset is always re-derived from the URL, so every script agrees."""
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            url = line.split("\t")[-1]
            out.append((dataset_of(url), trace_name(url), url))
    return out


# ---------------------------------------------------------------- families
# (formerly trace_families.py)
#
# Why: some block datasets contain many volumes that are close to clones of one
# workload (e.g. seven tencentBlock_v2 volumes with 10.4-10.9M requests, ~17GB
# footprint, LRU miss 0.497-0.508 and forgive gaps of 7.1-7.3pp). Counting each
# as an independent sample overstates the evidence, and a random train/test
# split for tuning would leak: a "held-out" trace could be a clone of a training
# trace. So:
#   * the analyzer reports family counts and family-weighted means, and
#   * the tuning planner splits train/test by FAMILY, never by trace.
#
# A trace joins a family (same dataset) when ALL of these hold against the
# family's running centroid:
#     requests within REQ_TOL (relative)
#     footprint within FP_TOL (relative)     footprint = LRU 10% cache bytes / 0.1
#     LRU miss at 10% within MISS_TOL (absolute)
# Requiring behavioural agreement (miss ratio), not just matching size, keeps
# unrelated volumes that merely happen to be the same size from being merged.

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


if __name__ == "__main__":
    for u in ("https://ftp.pdl.cmu.edu/pub/datasets/twemcacheWorkload/cacheDatasets/alibabaBlock/new/1M/alibabaBlock_9.oracleGeneral.zst",
              "https://ftp.pdl.cmu.edu/pub/datasets/twemcacheWorkload/cacheDatasets/cloudphysics/w01.oracleGeneral.bin.zst",
              "https://analytics.wikimedia.org/published/datasets/caching/2019/text/cache-t-00.gz",
              "https://analytics.wikimedia.org/published/datasets/caching/2019/upload/cache-u-07.tsv.gz"):
        print(f"{dataset_of(u):>26}  {trace_name(u):<18} convert={needs_conversion(u)}")
