#!/usr/bin/env python3
"""
headline.py - one-screen headline numbers for the libCacheSim sweep.

Reads the per-trace gaps file that analyze_lcs_sweep.py writes (lcs_gaps.csv)
and prints, for each cache size and pair, pooled over ALL datasets:
    traces, mean and median relative miss-ratio reduction, % better, % worse,
    and the family-weighted mean and % better (each family of near-identical
    traces counts once).
Positive = forgive policy is better than its baseline.

Usage:
  python analyze_lcs_sweep.py lcs_progress\\merged_all_nodes.csv > nul
  python headline.py lcs_gaps.csv --sweep lcs_progress\\merged_all_nodes.csv
(--sweep is the merged sweep CSV; it is only needed for the family columns.)
"""
import argparse, csv, statistics
from collections import defaultdict

TIE = 1e-9
LABELS = {"LRUF": "LRUF vs LRU", "S3FF": "S3FF vs S3-FIFO", "LRUFvS3": "LRUF vs S3-FIFO"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gaps", nargs="?", default="lcs_gaps.csv")
    ap.add_argument("--sweep", nargs="*", default=[], help="merged sweep CSV(s), for families")
    args = ap.parse_args()

    fam = {}
    if args.sweep:
        from trace_families import load_trace_stats, families
        stats = {}
        for p in args.sweep:
            stats.update(load_trace_stats(p))
        fam = families(stats)

    rel = defaultdict(list)                       # (pct, pair) -> [(ds, trace, rel)]
    with open(args.gaps, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            try:
                x = float(r["rel"])
            except (TypeError, ValueError):
                continue
            rel[(r["cache_pct"], r["pair"])].append((r["dataset"], r["trace"], x))

    def pkey(p):
        try:
            return -float(p)
        except ValueError:
            return 0.0

    pcts = sorted({p for p, _ in rel}, key=pkey)
    print(f"{'size':>6}  {'pair':<16} {'traces':>6} {'mean':>8} {'median':>8} "
          f"{'better':>7} {'worse':>6}" + (f" | {'families':>8} {'famW mean':>9} {'better':>7}" if fam else ""))
    for pct in pcts:
        for pair in ("LRUF", "S3FF", "LRUFvS3"):
            rows = rel.get((pct, pair))
            if not rows:
                continue
            xs = [x for _, _, x in rows]
            b = 100 * sum(x > TIE for x in xs) / len(xs)
            w = 100 * sum(x < -TIE for x in xs) / len(xs)
            line = (f"{pct + '%':>6}  {LABELS[pair]:<16} {len(xs):>6} {statistics.fmean(xs):>+7.2f}% "
                    f"{statistics.median(xs):>+7.2f}% {b:>6.0f}% {w:>5.0f}%")
            if fam:
                byf = defaultdict(list)
                for ds, tr, x in rows:
                    byf[fam.get((ds, tr), (ds, tr))].append(x)
                fm = [statistics.fmean(v) for v in byf.values()]
                fb = 100 * sum(x > TIE for x in fm) / len(fm)
                line += f" | {len(fm):>8} {statistics.fmean(fm):>+8.2f}% {fb:>6.0f}%"
            print(line)
        print()


if __name__ == "__main__":
    main()
