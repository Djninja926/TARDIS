#!/usr/bin/env python3
"""
analyze.py - tables from the CacheLib result files (../result/).

  python analyze.py final PERTHREAD.csv SINGLE_MANAGER.csv
      The headline (doc Section 38): LRUForgive gap vs LRU per trace, size and
      thread count for both designs; S3FIFOForgive vs S3-FIFO at 1 thread;
      throughput; t=2 is shown but is broken on every trace (doc 38.4).
  python analyze.py cells CSV...
      Any other run_matrix.sh output, or the older result files: per cell, the
      repetitions (n, mean, median, min-max) and the gap to the cell's baseline
      (lruforgive vs lru, s3fifoforgive vs s3fifo, same trace/size/threads).
      With several sizes per trace, also the size with the largest LRUForgive
      gap of at least 0.5 points (the "operating point", doc 18.1).
  python analyze.py alltraces CSV [--list ../../libcachesim/traces.tsv]
      The all-traces 1-thread sweep (all_traces_t1.csv): mean relative
      miss-ratio reduction per workload at 10% and 0.1% of the footprint,
      median over repetitions. --list maps trace names to workloads.

Gaps in points are (baseline - forgive) x 100; relative reductions are
(baseline - forgive) / baseline x 100. Positive = forgiveness helped.
"""
import csv, os, re, statistics, sys
from collections import defaultdict

PAIRS = {"lruforgive": "lru", "s3fifoforgive": "s3fifo"}
# older result files used other column names
SYNONYMS = {"branch": "label", "algo": "policy", "cache_mb": "size_mb", "hashpower": "hp",
            "t": "threads", "run": "rep", "miss_ratio": "miss", "mqps": "tp",
            "sample_rate": "rate", "recent_window": "label"}
DESIGNS = {"perthread", "logbackoff"}     # replicated_mode.csv, shared_mode.csv


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load(paths):
    rows = []
    for p in paths:
        with open(p, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                r = {SYNONYMS.get(k, k): v for k, v in r.items()}
                r["trace"] = (r.get("trace") or os.path.basename(p)[:-4]).replace(".oracleGeneral", "")
                r.setdefault("policy", "lruforgive")
                if r["policy"] in DESIGNS:        # older files named the LRUForgive design instead
                    r["label"], r["policy"] = r["policy"], "lruforgive"
                rows.append(r)
    return rows


def stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return len(xs), statistics.fmean(xs), statistics.median(xs), min(xs), max(xs)


# ------------------------------------------------------------------ final
def final(paths):
    miss, tp = defaultdict(list), defaultdict(list)
    for r in load(paths):
        t = int(r["threads"])
        base = round(int(r["size_mb"]) / t)          # replicated: size = base x threads
        k = (r["label"], r["trace"], r["policy"], base, t)
        miss[k].append(num(r["miss"]))
        tp[k].append(num(r["tp"]))
    labels = sorted({k[0] for k in miss})
    traces = list(dict.fromkeys(k[1] for k in miss))
    sizes = defaultdict(set)
    for k in miss:
        sizes[k[1]].add(k[3])

    def m(d, *k):
        s = stats(d.get(k, []))
        return s[1] if s else None

    f = lambda x: f"{x:7.4f}" if x is not None else "     --"
    g = lambda a, b: f"{(a - b) * 100:+6.2f}" if a is not None and b is not None else "    --"
    print("LRUForgive gap vs LRU (points; LRU from the first label), mean of repetitions")
    for tr in traces:
        for base in sorted(sizes[tr]):
            print(f"\n{tr}  base {base} MB")
            print(f"  {'t':>3} | {'LRU':>7} | " + " | ".join(f"{l[:12]:>12} {'gap':>6}" for l in labels))
            for t in (1, 2, 4, 8, 16):
                lru = next((m(miss, l, tr, "lru", base, t) for l in labels
                            if m(miss, l, tr, "lru", base, t) is not None), None)
                cells = [(m(miss, l, tr, "lruforgive", base, t)) for l in labels]
                note = "   (t=2 baseline broken, doc 38.4)" if t == 2 else ""
                print(f"  {t:>3} | {f(lru)} | " + " | ".join(f"{f(c):>12} {g(lru, c)}" for c in cells) + note)
    print("\nS3FIFOForgive gap vs S3-FIFO, 1 thread (points)")
    for tr in traces:
        for base in sorted(sizes[tr]):
            s3 = next((m(miss, l, tr, "s3fifo", base, 1) for l in labels
                       if m(miss, l, tr, "s3fifo", base, 1) is not None), None)
            cells = " | ".join(f"{l[:12]} {f(m(miss, l, tr, 's3fifoforgive', base, 1))} "
                               f"{g(s3, m(miss, l, tr, 's3fifoforgive', base, 1))}" for l in labels)
            print(f"  {tr:>12} {base:>5} MB | S3-FIFO {f(s3)} | {cells}")
    print("\nLRUForgive throughput (MQPS), operating-point size")
    for tr in traces:
        base = min(sizes[tr])
        print(f"  {tr:>12} {base:>5} MB | " + "  ".join(
            f"t={t}: " + "/".join(f"{m(tp, l, tr, 'lruforgive', base, t) or 0:.2f}" for l in labels)
            for t in (1, 4, 8, 16)) + f"   ({'/'.join(labels)})")


# ------------------------------------------------------------------ cells
def cells(paths):
    rows = load(paths)
    keycols = [c for c in ("label", "mode", "trace", "size_mb", "hp", "threads", "variant", "rate")
               if any(r.get(c) not in (None, "") for r in rows)]
    groups = defaultdict(list)
    for r in rows:
        groups[(tuple(r.get(c, "") for c in keycols), r["policy"])].append(num(r.get("miss")))
    means = {k: stats(v) for k, v in groups.items()}

    def order(item):
        (k, p), _ = item
        return [(0, num(x)) if num(x) is not None else (1, x) for x in k], p

    print(" ".join(f"{c:>10}" for c in keycols) + f" {'policy':>14} | {'n':>2} {'mean':>7} {'median':>7} "
          f"{'min':>7} {'max':>7} | gap vs base (points)")
    best = {}
    ti = keycols.index("trace") if "trace" in keycols else None
    si = keycols.index("size_mb") if "size_mb" in keycols else None
    sizes = defaultdict(set)
    for (k, p), s in sorted(means.items(), key=order):
        lead = " ".join(f"{x:>10}" for x in k) + f" {p:>14} | "
        if not s:
            print(lead + " no numeric runs")
            continue
        if ti is not None and si is not None:
            sizes[k[ti]].add(k[si])
        gap = ""
        if p in PAIRS:
            # the baseline: same cell, run without the variant; baselines carry no design label
            bk = tuple("default" if c == "variant" else x for c, x in zip(keycols, k))
            nolabel = lambda t: tuple("" if c == "label" else x for c, x in zip(keycols, t))
            bs = next((means[(c, PAIRS[p])] for c in (bk, k, nolabel(bk), nolabel(k))
                       if means.get((c, PAIRS[p]))), None)
            if bs:
                d = bs[1] - s[1]
                gap = f"{d * 100:+6.2f}"
                if p == "lruforgive" and ti is not None and si is not None and d * 100 >= 0.5:
                    if k[ti] not in best or d > best[k[ti]][1]:
                        best[k[ti]] = (k[si], d)
        n, mean, med, lo, hi = s
        print(lead + f"{n:>2} {mean:7.4f} {med:7.4f} {lo:7.4f} {hi:7.4f} | {gap}")
    multi = sorted(t for t, v in sizes.items() if len(v) > 2)
    if multi:
        print("\nOperating point (largest LRUForgive gap of at least 0.5 points):")
        for tr in multi:
            print(f"  {tr:>28}  " + (f"{best[tr][0]} MB, +{best[tr][1] * 100:.2f} points" if tr in best
                                     else "none (no meaningful gap)"))


# ------------------------------------------------------------------ all traces
def alltraces(path, listfile):
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "..", "..", "libcachesim", "scripts"))
    group = lambda name: "all"
    if listfile:
        from common import group_of, read_list
        wl = {}
        for ds, name, _ in read_list(listfile):
            wl.setdefault(name, group_of(ds, name) or ds)   # names repeated across datasets keep the first
        group = lambda name: wl.get(name, "unknown")
    reps, status = defaultdict(list), defaultdict(int)
    for r in load([path]):
        v = num(r["miss"])
        if v is None:
            status[r["miss"] or "blank"] += 1
            continue
        reps[(r["trace"], r["cache_pct"], r["policy"])].append(v)
    med = {k: statistics.median(v) for k, v in reps.items()}
    out = defaultdict(list)
    for (tr, pct, pol), fm in med.items():
        base = PAIRS.get(pol)
        bm = med.get((tr, pct, base)) if base else None
        if bm:
            rel = (bm - fm) / bm * 100
            for g in {group(tr), "ALL"}:
                out[(g, pct, pol)].append(rel)
    print(f"skipped cells by reason: {dict(status)}  (NA_TOOBIG = cache larger than 80% of RAM)")
    print(f"\n{'workload':>16} {'size':>5} {'policy':>14} | {'traces':>6} {'mean %':>7} {'median %':>8} {'helped':>7} {'hurt':>6}")
    for (g, pct, pol) in sorted(out, key=lambda k: (k[0] != "ALL", k[0], -float(k[1]), k[2])):
        xs = out[(g, pct, pol)]
        print(f"{g:>16} {pct:>4}% {pol:>14} | {len(xs):>6} {statistics.fmean(xs):>+7.2f} {statistics.median(xs):>+8.2f} "
              f"{sum(x > 0.05 for x in xs) / len(xs):>6.0%} {sum(x < -0.05 for x in xs) / len(xs):>6.0%}")


def main(a):
    if len(a) >= 3 and a[1] == "final":
        final(a[2:])
    elif len(a) >= 3 and a[1] == "cells":
        cells(a[2:])
    elif len(a) >= 3 and a[1] == "alltraces":
        lst = a[a.index("--list") + 1] if "--list" in a else None
        alltraces(a[2], lst)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
