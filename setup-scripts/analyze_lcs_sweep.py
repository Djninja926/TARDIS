#!/usr/bin/env python3
"""
analyze_lcs_sweep.py - analyze the libCacheSim embedding sweep.

Reads the merged CSV from `orchestrate.py --collect` (11-column format written by
node_cachesim_worker.sh / parse_cachesim.py) and reports, per dataset and cache
size, how each forgive policy compares with its own baseline:

    lruforgiveembcache      vs  lru
    s3fifoforgive-embcache  vs  s3fifo

Sign convention (same as the CacheLib analyzer): POSITIVE = forgive is better.
    rel% = (base_miss - forgive_miss) / base_miss * 100
    pp   = (base_miss - forgive_miss) * 100

Optional:
  --list all_traces.tsv     progress per dataset (done / expected)
  --diag "dir/forgive_diag_*.log"
                            forgive rates and whether the embedding cap was
                            binding (final_emb_count == max_emb), per size
  --byte                    use byte miss ratio instead of object miss ratio
  --out gaps.csv            write per-trace gaps (default lcs_gaps.csv)

Usage:
  python analyze_lcs_sweep.py lcs_progress\\merged_all_nodes.csv ^
      --list all_traces.tsv --diag "lcs_progress\\forgive_diag_*.log"
"""
import argparse, csv, glob, re, statistics, sys
from collections import defaultdict

# (forgive policy, baseline, label). The third pair is the one a reviewer asks
# about: does LRU + forgiveness beat S3FIFO, the stronger baseline, outright?
PAIRS = [("lruforgiveembcache", "lru", "LRUF"),
         ("s3fifoforgive-embcache", "s3fifo", "S3FF"),
         ("lruforgiveembcache", "s3fifo", "LRUFvS3")]
TIE = 1e-9          # |gap| at or below this counts as "no change"


def fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def dataset_of(url):
    """Same derivation as the worker: path under cacheDatasets/, slashes -> _."""
    p = url.split("/cacheDatasets/", 1)[-1].rsplit("/", 1)[0]
    return p.replace("/", "_") or "unknown"


def pct_key(p):
    try:
        return -float(p)
    except ValueError:
        return 0.0


def fmt(x, w=8, d=3, pct=True):
    if x is None:
        return f"{'--':>{w}}"
    return f"{x:>+{w - (1 if pct else 0)}.{d}f}" + ("%" if pct else "")


def med(xs):
    return statistics.median(xs) if xs else None


def mean(xs):
    return statistics.fmean(xs) if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--list", help="all_traces.tsv, for progress per dataset")
    ap.add_argument("--diag", help='glob of forgive_diag_*.log files (quote it)')
    ap.add_argument("--pcts", default="10,0.1",
                    help="the worker's PCTS, in SIZES order (maps diag tag embN -> pct)")
    ap.add_argument("--byte", action="store_true", help="use byte miss ratio")
    ap.add_argument("--top", type=int, default=10, help="top-N traces by gap to list")
    ap.add_argument("--out", default="lcs_gaps.csv")
    args = ap.parse_args()
    col = "byte_miss" if args.byte else "miss"

    # ---------------------------------------------------------------- load
    cells = {}                                   # (ds,trace,policy,pct) -> row
    na_rows = []                                 # (key, reason); resolved after load
    seen_traces = defaultdict(set)               # ds -> {trace} with a numeric cell
    dupes = bad = 0
    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        need = {"dataset", "trace", "policy", "cache_pct", "miss", "byte_miss"}
        if not need.issubset(rd.fieldnames or []):
            sys.exit(f"ERROR: {args.csv} is not the libCacheSim sweep format "
                     f"(columns: {rd.fieldnames}). Old CacheLib CSVs need "
                     f"analyze_footprint_sweep.py instead.")
        for r in rd:
            if r.get("dataset") == "dataset":        # a repeated header line
                continue
            ds, tr = r["dataset"], r["trace"]
            k = (ds, tr, r["policy"], r["cache_pct"])
            if (r["miss"] or "").startswith("NA"):
                na_rows.append((k, r["miss"]))
                continue
            v = fnum(r[col])
            if v is None:
                bad += 1
                continue
            seen_traces[ds].add(tr)
            if k in cells:
                dupes += 1
            cells[k] = r                             # keep the last occurrence

    # Transient failures (disk, unzip, download, no working set) on a trace that
    # later produced real results are retry history, not gaps. NA_SMALL is a
    # permanent, deterministic skip and is always reported. Each (cell, reason)
    # counts once even if it appears in several node files.
    TRANSIENT = {"NA_DISK", "NA_UNZIP", "NA_DLFAIL", "NA_NOWSS"}
    na = defaultdict(lambda: defaultdict(int))
    superseded = 0
    for k, reason in set(na_rows):
        ds, tr = k[0], k[1]
        if k in cells or (reason in TRANSIENT and tr in seen_traces.get(ds, ())):
            superseded += 1
        else:
            na[ds][reason] += 1

    if not cells:
        sys.exit("no numeric cells found")

    # ---------------------------------------------------------------- pair
    gaps = []   # dict per (ds, trace, pct, pair)
    for (ds, tr, pol, pct), r in cells.items():
        for fpol, bpol, label in PAIRS:
            if pol != fpol:
                continue
            b = cells.get((ds, tr, bpol, pct))
            if not b:
                continue
            fm, bm = fnum(r[col]), fnum(b[col])
            gaps.append({
                "dataset": ds, "trace": tr, "cache_pct": pct, "pair": label,
                "base_miss": bm, "forgive_miss": fm,
                "pp": (bm - fm) * 100,
                "rel": (bm - fm) / bm * 100 if bm > 0 else None,
                "params": r.get("params", ""),
            })

    datasets = sorted({g["dataset"] for g in gaps} | set(seen_traces))
    pcts = sorted({g["cache_pct"] for g in gaps}, key=pct_key)
    metric = "byte miss ratio" if args.byte else "object miss ratio"

    print(f"loaded {len(cells)} numeric cells from {args.csv}"
          + (f"  ({dupes} duplicate cells, last kept)" if dupes else "")
          + (f"  ({superseded} failed cells later retried OK)" if superseded else "")
          + (f"  ({bad} unparsable)" if bad else ""))
    print(f"{len(gaps)} forgive-vs-baseline pairs  |  metric: {metric}  |  positive = forgive better")

    # ---------------------------------------------------------------- progress
    if args.list:
        expected = defaultdict(set)
        with open(args.list, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                url = line.split("\t")[-1]
                name = url.rsplit("/", 1)[-1].split(".oracleGeneral")[0]
                expected[dataset_of(url)].add(name)
        tot_d = tot_e = 0
        print("\n" + "=" * 78 + "\nPROGRESS (traces with a real result / expected)\n" + "=" * 78)
        for ds in sorted(expected, key=lambda d: -len(expected[d])):
            d, e = len(seen_traces.get(ds, set()) & expected[ds]), len(expected[ds])
            tot_d += d; tot_e += e
            print(f"  {ds:>28} | {d:>5} / {e:<5} {100 * d / e:5.1f}%")
        print(f"  {'TOTAL':>28} | {tot_d:>5} / {tot_e:<5} {100 * tot_d / max(1, tot_e):5.1f}%")

    # ---------------------------------------------------------------- rollups
    for pct in pcts:
        print("\n" + "=" * 78)
        print(f"PER-DATASET rollup at {pct}% footprint (relative {metric} reduction)")
        print("=" * 78)
        hdr = f"{'dataset':>28} |"
        for _, _, lab in PAIRS:
            hdr += f" {'n':>5} {lab + ' mean':>12} {lab + ' med':>11} {'better':>6} {'worse':>6} |"
        print(hdr)
        for ds in datasets:
            line = f"{ds:>28} |"
            any_row = False
            for _, _, lab in PAIRS:
                rs = [g["rel"] for g in gaps if g["dataset"] == ds and g["cache_pct"] == pct
                      and g["pair"] == lab and g["rel"] is not None]
                if rs:
                    any_row = True
                    better = sum(1 for x in rs if x > TIE)
                    worse = sum(1 for x in rs if x < -TIE)
                    line += (f" {len(rs):>5} {fmt(mean(rs), 12)} {fmt(med(rs), 11)}"
                             f" {100 * better / len(rs):5.0f}% {100 * worse / len(rs):5.0f}% |")
                else:
                    line += f" {0:>5} {'--':>12} {'--':>11} {'--':>6} {'--':>6} |"
            if any_row:
                print(line)
        # pooled line: dominated by the biggest bucket, so show it last and label it
        line = f"{'ALL (pooled, trace-weighted)':>28} |"
        for _, _, lab in PAIRS:
            rs = [g["rel"] for g in gaps if g["cache_pct"] == pct and g["pair"] == lab
                  and g["rel"] is not None]
            line += (f" {len(rs):>5} {fmt(mean(rs), 12)} {fmt(med(rs), 11)} {'':>6} {'':>6} |"
                     if rs else f" {0:>5} {'--':>12} {'--':>11} {'':>6} {'':>6} |")
        print("-" * len(hdr) + "\n" + line)

    # ---------------------------------------------------------------- top traces
    for pct in pcts:
        for _, _, lab in PAIRS:
            rs = sorted((g for g in gaps if g["cache_pct"] == pct and g["pair"] == lab),
                        key=lambda g: -g["pp"])
            if not rs:
                continue
            base_lab = "S3FIFO" if lab == "LRUFvS3" else "base"
            for title, sel in ((f"TOP {args.top}", rs[:args.top]),
                               (f"BOTTOM {args.top}", rs[::-1][:args.top])):
                if title.startswith("BOTTOM") and (not sel or sel[0]["pp"] >= 0):
                    continue                      # no regressions to show
                print(f"\n{title} by {lab} gap at {pct}% (pp = percentage points of {metric})")
                for g in sel:
                    print(f"  {g['dataset']:>24} {g['trace']:<22} {base_lab} {g['base_miss']:.4f} "
                          f"forgive {g['forgive_miss']:.4f}  {g['pp']:+.2f}pp  {fmt(g['rel'], 8, 2)}")

    # ---------------------------------------------------------------- NA summary
    if na:
        print("\n" + "=" * 78 + "\nSKIPPED / FAILED cells by reason (rows, not traces)\n" + "=" * 78)
        reasons = sorted({r for d in na.values() for r in d})
        print(f"{'dataset':>28} | " + " ".join(f"{r:>10}" for r in reasons))
        for ds in sorted(na):
            print(f"{ds:>28} | " + " ".join(f"{na[ds].get(r, 0):>10}" for r in reasons))
        if any("NA_SMALL" != r for r in reasons):
            print("  NA_SMALL is the <1000-object skip rule (expected). Anything else is a")
            print("  failure worth a look: grep the reason in the node's worker.log.")

    # ---------------------------------------------------------------- diagnostics
    if args.diag:
        files = sorted(glob.glob(args.diag))
        pct_by_idx = [p.strip() for p in args.pcts.split(",")]
        stats = defaultdict(lambda: {"cells": 0, "binding": 0, "rate": []})
        hdr_re = re.compile(r"^### (\S+)/(\S+) tag=emb(\d+)")
        lru_re = re.compile(r"\[LRUForgiveEmbCache .*?max_emb=(-?\d+|unlimited)\].*?"
                            r"forgives=\d+\(([\d.]+)%\).*?final_emb_count=(\d+)")
        s3_re = re.compile(r"\[S3FIFOForgiveEmbCache .*?max_emb=(-?\d+|unlimited)\].*?"
                           r"forgive_small=\d+\(([\d.]+)%\).*?final_emb_count=(\d+)")
        for fn in files:
            cur_pct = None
            with open(fn, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = hdr_re.match(line)
                    if m:
                        i = int(m.group(3))
                        cur_pct = pct_by_idx[i] if i < len(pct_by_idx) else f"emb{i}"
                        continue
                    for rx, lab in ((lru_re, "LRUF"), (s3_re, "S3FF")):
                        m = rx.search(line)
                        if m and cur_pct is not None:
                            s = stats[(cur_pct, lab)]
                            s["cells"] += 1
                            s["rate"].append(float(m.group(2)))
                            cap = m.group(1)
                            if cap not in ("unlimited", "-1") and int(m.group(3)) >= int(cap):
                                s["binding"] += 1
        if stats:
            print("\n" + "=" * 78 + f"\nFORGIVE DIAGNOSTICS ({len(files)} log files)\n" + "=" * 78)
            print(f"{'size':>6} {'policy':>6} | {'cells':>6} {'cap binding':>12} "
                  f"{'median forgive rate':>20}")
            for (pct, lab) in sorted(stats, key=lambda k: (pct_key(k[0]), k[1])):
                s = stats[(pct, lab)]
                print(f"{pct + '%':>6} {lab:>6} | {s['cells']:>6} "
                      f"{100 * s['binding'] / s['cells']:>11.1f}% {med(s['rate']):>19.2f}%")
            print("  cap binding = table hit 5x main capacity (final_emb_count == max_emb).")
            print("  S3FF rate is forgive_small (rescues from the small FIFO).")

    # ---------------------------------------------------------------- write
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["dataset", "trace", "cache_pct", "pair",
                                          "base_miss", "forgive_miss", "pp", "rel", "params"])
        w.writeheader()
        for g in sorted(gaps, key=lambda g: (g["dataset"], g["trace"], pct_key(g["cache_pct"]), g["pair"])):
            w.writerow(g)
    print(f"\nwrote per-trace gaps -> {args.out}")


if __name__ == "__main__":
    main()
