#!/usr/bin/env python3
"""
compare_tuned.py - did tuning help? Default vs tuned, on the SAME traces.

  --default   merged CSV(s) from the untuned sweeps (main + rerun + 1% run)
  --tuned     merged CSV(s) from the tuned rerun (PARAMS_FILE run)
  --only      restrict to a trace list; use test_traces.tsv from the tuning plan
              for the HEADLINE (held-out families only), train_traces.tsv to see
              the in-sample number, and compare the two to see how much of the
              gain survives on unseen traces

For every (dataset, size, policy) it pairs each trace's forgive result with its
own baseline in the same run, computes relative miss-ratio reduction (positive
= forgive better) under both configurations, and reports the mean over the
traces present in BOTH, trace-weighted and family-weighted.

Usage:
  python compare_tuned.py --default lcs_progress\\merged_all_nodes.csv rerun\\merged_all_nodes.csv ^
      p1\\merged_all_nodes.csv --tuned tuned\\merged_all_nodes.csv --only tune_plan\\test_traces.tsv
"""
import argparse, csv, statistics, sys
from collections import defaultdict

try:
    from trace_names import load_trace_stats, families
except ImportError:
    load_trace_stats = families = None

PAIRS = [("lruforgiveembcache", "lru", "LRUF"), ("s3fifoforgive-embcache", "s3fifo", "S3FF")]


from trace_names import dataset_of, trace_name


def load(paths, only):
    cells = {}
    for path in paths:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                m = r.get("miss") or ""
                if not m or m.startswith("NA") or r.get("dataset") == "dataset":
                    continue
                if only is not None and (r["dataset"], r["trace"]) not in only:
                    continue
                try:
                    cells[(r["dataset"], r["trace"], r["policy"], r["cache_pct"])] = float(m)
                except ValueError:
                    pass
    rel = {}
    for (ds, tr, pol, pct), fm in cells.items():
        for fp, bp, lab in PAIRS:
            if pol == fp and (ds, tr, bp, pct) in cells:
                bm = cells[(ds, tr, bp, pct)]
                if bm > 0:
                    rel[(ds, pct, lab, tr)] = (bm - fm) / bm * 100
    return rel


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--default", nargs="+", required=True)
    ap.add_argument("--tuned", nargs="+", required=True)
    ap.add_argument("--only")
    args = ap.parse_args()

    only = None
    if args.only:
        only = set()
        with open(args.only, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    url = line.split("\t")[-1]
                    only.add((dataset_of(url), trace_name(url)))

    d, t = load(args.default, only), load(args.tuned, only)
    fam = {}
    if families is not None:
        stats = {}
        for p in args.default:
            stats.update(load_trace_stats(p))
        fam = families(stats)

    groups = defaultdict(list)                 # (ds, pct, lab) -> [(trace, d, t)]
    for key, tv in t.items():
        if key in d:
            ds, pct, lab, tr = key
            groups[(ds, pct, lab)].append((tr, d[key], tv))
    if not groups:
        sys.exit("no traces present in both the default and tuned results")

    scope = f"only traces in {args.only}" if args.only else "all traces present in both"
    print(f"relative miss-ratio reduction vs own baseline (positive = forgive better); {scope}\n")
    print(f"{'dataset':>24} {'size':>5} {'pol':>4} | {'n':>5} {'default':>9} {'tuned':>9} "
          f"{'delta':>8} {'tuned>dflt':>10} | {'fams':>5} {'famW dflt':>9} {'famW tuned':>10}")
    for (ds, pct, lab) in sorted(groups, key=lambda k: (k[0], -float(k[1]), k[2])):
        g = groups[(ds, pct, lab)]
        dm = statistics.fmean(x[1] for x in g)
        tm = statistics.fmean(x[2] for x in g)
        better = sum(1 for x in g if x[2] > x[1] + 1e-9)
        byf = defaultdict(list)
        for tr, dv, tv in g:
            byf[fam.get((ds, tr), tr)].append((dv, tv))
        fd = statistics.fmean(statistics.fmean(v[0] for v in vs) for vs in byf.values())
        ft = statistics.fmean(statistics.fmean(v[1] for v in vs) for vs in byf.values())
        print(f"{ds:>24} {pct + '%':>5} {lab:>4} | {len(g):>5} {dm:>+8.3f}% {tm:>+8.3f}% "
              f"{tm - dm:>+7.3f} {100 * better / len(g):>9.0f}% | {len(byf):>5} {fd:>+8.3f}% {ft:>+9.3f}%")


if __name__ == "__main__":
    main()
