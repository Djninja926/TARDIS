#!/usr/bin/env python3
"""
analyze_footprint_sweep.py - analyze the multi-node footprint-sweep CSVs.

Handles the worker's schema:
  node,trace,policy,cache_pct,size_mb,hp,threads,rep,miss,tp,requests,footprint_mb,n_objects

Skips NA / NA_TOOBIG / blank miss values. Uses MEDIAN across reps.
Reports, per cache size (10% and 0.1%):
  - the forgiveness gap per trace (LRUForgive vs LRU, S3FIFOForgive vs S3FIFO)
  - an aggregate: how many traces improve/tie/regress, and the mean/median gap
Also flags per-policy coverage (how many cells were NA_TOOBIG etc.).

Usage:
  python3 analyze_footprint_sweep.py merged_all_nodes.csv
  python3 analyze_footprint_sweep.py smoke_results/merged_all_nodes.csv --csv gaps.csv
"""
import sys, csv, argparse, statistics
from collections import defaultdict

def fnum(x):
    try:
        return float(x)
    except (ValueError, TypeError):
        return None

def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csvfile")
    ap.add_argument("--csv", help="also write per-trace gaps to this CSV")
    ap.add_argument("--list", help="master trace list (full_list.tsv: dataset<TAB>url) to map each trace to its dataset")
    args = ap.parse_args()

    # optional: trace -> dataset map from the master list
    trace_ds = {}
    if args.list:
        with open(args.list, encoding="utf-8-sig") as f:
            for line in f:
                line = line.rstrip("\n").rstrip("\r")
                if not line or line.startswith("#"): continue
                parts = line.split("\t")
                if len(parts) != 2: continue
                ds, url = parts
                # trace name = basename before ".oracleGeneral" (matches the worker)
                base = url.rsplit("/", 1)[-1]
                name = base.split(".oracleGeneral")[0]
                trace_ds[name] = ds
    def dsof(tr):
        return trace_ds.get(tr, "?")

    # (trace, cache_pct, policy) -> list of miss values (across reps)
    miss = defaultdict(list)
    status_counts = defaultdict(int)
    traces = set()
    cache_pcts = set()

    with open(args.csvfile, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            tr = r["trace"]; pct = r["cache_pct"]; pol = r["policy"]
            traces.add(tr); cache_pcts.add(pct)
            raw = r["miss"]
            status_counts[raw if raw in ("NA", "NA_TOOBIG", "") else "ok"] += 1
            v = fnum(raw)
            if v is not None:
                miss[(tr, pct, pol)].append(v)

    def m(tr, pct, pol):
        return median(miss.get((tr, pct, pol), []))

    # order cache sizes: 10 (large) then 0.1 (small)
    def pct_key(p):
        try: return -float(p)
        except: return 0
    pcts = sorted(cache_pcts, key=pct_key)

    print(f"traces seen: {len(traces)}   cells: ok={status_counts['ok']} "
          f"NA={status_counts['NA']} NA_TOOBIG={status_counts['NA_TOOBIG']} blank={status_counts['']}")

    gap_rows = []  # for optional CSV: trace, cache_pct, lru, lruforgive, lru_gap, s3fifo, s3fifoforgive, s3_gap

    for pct in pcts:
        print("\n" + "=" * 78)
        print(f"CACHE SIZE = {pct}% of footprint")
        print("=" * 78)
        # per-trace gaps
        lru_gaps = []   # LRU - LRUForgive (positive = forgiveness helps)
        s3_gaps = []
        lru_improved = lru_tied = lru_regressed = 0
        s3_improved = s3_tied = s3_regressed = 0
        print(f"  {'dataset':>16} {'trace':>20} | {'LRU':>7} {'LRUF':>7} {'gap(pp)':>8} | {'S3F':>7} {'S3FF':>7} {'gap(pp)':>8}")
        for tr in sorted(traces, key=lambda t: (dsof(t), t)):
            lru = m(tr, pct, "lru"); lruf = m(tr, pct, "lruforgive")
            s3 = m(tr, pct, "s3fifo"); s3f = m(tr, pct, "s3fifoforgive")
            def s(x): return f"{x:7.4f}" if x is not None else "     --"
            lru_gap = (lru - lruf) if (lru is not None and lruf is not None) else None
            s3_gap  = (s3 - s3f)   if (s3 is not None and s3f  is not None) else None
            def g(x): return f"{x*100:+7.3f}" if x is not None else "     --"
            if lru_gap is not None or s3_gap is not None:
                print(f"  {dsof(tr):>16} {tr:>20} | {s(lru)} {s(lruf)} {g(lru_gap)} | {s(s3)} {s(s3f)} {g(s3_gap)}")
            if lru_gap is not None:
                lru_gaps.append(lru_gap)
                if lru_gap > 0.0005: lru_improved += 1
                elif lru_gap < -0.0005: lru_regressed += 1
                else: lru_tied += 1
            if s3_gap is not None:
                s3_gaps.append(s3_gap)
                if s3_gap > 0.0005: s3_improved += 1
                elif s3_gap < -0.0005: s3_regressed += 1
                else: s3_tied += 1
            gap_rows.append((tr, pct, lru, lruf, lru_gap, s3, s3f, s3_gap))

        print(f"\n  --- aggregate at {pct}% ---")
        if lru_gaps:
            # relative reductions: gap / baseline, in %
            lru_rel = [ (m(tr,pct,"lru")-m(tr,pct,"lruforgive"))/m(tr,pct,"lru")*100
                        for tr in sorted(traces)
                        if m(tr,pct,"lru") not in (None,0) and m(tr,pct,"lruforgive") is not None ]
            print(f"  LRUForgive vs LRU: n={len(lru_gaps)}  "
                  f"improved={lru_improved} tied={lru_tied} regressed={lru_regressed}")
            print(f"    absolute gap: mean={statistics.mean(lru_gaps)*100:+.3f}pp  median={statistics.median(lru_gaps)*100:+.3f}pp")
            if lru_rel:
                print(f"    RELATIVE reduction: mean={statistics.mean(lru_rel):+.3f}%  median={statistics.median(lru_rel):+.3f}%  max={max(lru_rel):+.2f}%")
        if s3_gaps:
            s3_rel = [ (m(tr,pct,"s3fifo")-m(tr,pct,"s3fifoforgive"))/m(tr,pct,"s3fifo")*100
                       for tr in sorted(traces)
                       if m(tr,pct,"s3fifo") not in (None,0) and m(tr,pct,"s3fifoforgive") is not None ]
            print(f"  S3FIFOForgive vs S3FIFO: n={len(s3_gaps)}  "
                  f"improved={s3_improved} tied={s3_tied} regressed={s3_regressed}")
            print(f"    absolute gap: mean={statistics.mean(s3_gaps)*100:+.3f}pp  median={statistics.median(s3_gaps)*100:+.3f}pp")
            if s3_rel:
                print(f"    RELATIVE reduction: mean={statistics.mean(s3_rel):+.3f}%  median={statistics.median(s3_rel):+.3f}%  max={max(s3_rel):+.2f}%")

    # ===== per-dataset rollup (only if --list provided) =====
    if trace_ds:
        for pct in pcts:
            print("\n" + "=" * 78)
            print(f"PER-DATASET rollup at {pct}% footprint (relative miss-ratio reduction)")
            print("=" * 78)
            print(f"  {'dataset':>16} | {'n':>4} {'LRUF mean%':>11} {'LRUF med%':>10} | {'S3FF mean%':>11} {'S3FF med%':>10}")
            # group traces by dataset
            by_ds = defaultdict(list)
            for tr in traces:
                by_ds[dsof(tr)].append(tr)
            for ds in sorted(by_ds):
                lru_rel = [ (m(tr,pct,"lru")-m(tr,pct,"lruforgive"))/m(tr,pct,"lru")*100
                            for tr in by_ds[ds]
                            if m(tr,pct,"lru") not in (None,0) and m(tr,pct,"lruforgive") is not None ]
                s3_rel  = [ (m(tr,pct,"s3fifo")-m(tr,pct,"s3fifoforgive"))/m(tr,pct,"s3fifo")*100
                            for tr in by_ds[ds]
                            if m(tr,pct,"s3fifo") not in (None,0) and m(tr,pct,"s3fifoforgive") is not None ]
                if not lru_rel and not s3_rel: continue
                def mm(xs): return (statistics.mean(xs), statistics.median(xs)) if xs else (float('nan'), float('nan'))
                lm, lmd = mm(lru_rel); sm, smd = mm(s3_rel)
                n = max(len(lru_rel), len(s3_rel))
                print(f"  {ds:>16} | {n:>4} {lm:>+10.3f}% {lmd:>+9.3f}% | {sm:>+10.3f}% {smd:>+9.3f}%")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["dataset","trace","cache_pct","lru","lruforgive","lru_gap_pp",
                        "s3fifo","s3fifoforgive","s3_gap_pp"])
            for tr, pct, lru, lruf, lg, s3, s3f, sg in gap_rows:
                w.writerow([dsof(tr), tr, pct,
                            f"{lru:.4f}" if lru is not None else "",
                            f"{lruf:.4f}" if lruf is not None else "",
                            f"{lg*100:.3f}" if lg is not None else "",
                            f"{s3:.4f}" if s3 is not None else "",
                            f"{s3f:.4f}" if s3f is not None else "",
                            f"{sg*100:.3f}" if sg is not None else ""])
        print(f"\nwrote per-trace gaps -> {args.csv}")

if __name__ == "__main__":
    main()
