#!/usr/bin/env python3
"""
analyze.py - analyze sweep results.

Every comparison is a forgive policy against its own baseline:
    lruforgiveembcache vs lru          (LRUForgive)
    s3fifoforgive-embcache vs s3fifo   (S3FIFOForgive)
as relative miss-ratio reduction per trace, (base - forgive) / base * 100,
averaged. Positive = forgiveness helped.

  python analyze.py RESULTS.csv...
      Per-dataset rollups at every cache size, the per-workload table, the
      best and worst traces, and skipped/failed cells. When several files are
      given, a later file wins for the same (trace, policy, size).
  python analyze.py DEFAULT.csv... --tuned TUNED.csv...
      Default vs tuned (or borrowed) parameters on the traces present in both,
      per dataset and pooled. Add --only test_traces.tsv for held-out traces.

Options:
  --list TRACES.tsv     progress per dataset; with --remaining OUT.tsv, write
                        every incomplete trace there (ready to launch again)
  --only LIST.tsv       only traces in this list
  --exclude REGEX       drop datasets matching REGEX (repeatable)
  --byte                byte miss ratio instead of object miss ratio
  --table PREFIX        also write the workload table to PREFIX.csv and PREFIX.tex
  --merged-out OUT.csv  write all input files folded into one results file
  --diag "GLOB"         forgive rates and how often the sketch table was full,
                        from the workers' forgive_diag logs
  --gaps OUT.csv        write the per-trace reductions

WORKLOAD TABLE: the paper's Table 5 layout. Datasets are grouped into workloads
(Wiki Upload, Wiki Text, Meta KV, ...); sampled copies of a trace count once
(the copy with the most requests); "sources" lists which datasets fed each row.
"""
import argparse, csv, glob, re, statistics, sys
from collections import defaultdict

from common import BASELINE, GROUPS, PAPER, POLICIES, group_of, read_list

# (forgive policy, baseline, label). Each forgive policy is compared only with
# its own baseline (LRUF vs S3-FIFO was dropped: not a comparison we report).
PAIRS = [("lruforgiveembcache", "lru", "LRUF"),
         ("s3fifoforgive-embcache", "s3fifo", "S3FF")]
TIE = 1e-9          # |gap| at or below this counts as "no change"


def fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


BASE_POLS = ("lru", "s3fifo")
EMB_POLS = ("lruforgiveembcache", "s3fifoforgive-embcache")


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


# ---------------------------------------------------------------- workload table
# The paper-style table: mean relative miss-ratio reduction per WORKLOAD GROUP,
# LRUForgive (vs LRU) and S3FIFOForgive (vs S3-FIFO), one column pair per size.
# Groups match on the dataset name (common.GROUPS). Groups after the rule line
# are datasets the paper table does not have.
#
# VARIANTS: the CMU mirror has several copies of some traces (twitter clusters
# in full and as sample10 / sample100, alibabaBlock volumes in several
# subfolders). They share a trace name, so within a group each trace name keeps
# only the copy with the most requests (the full trace).
TABLE_COLS = (("LRUF", "LRUForgive"), ("S3FF", "S3FIFOForgive"))


def workload_table(gaps, cells, pcts, args, metric):
    reqs = defaultdict(float)                       # (ds, trace) -> requests
    for (ds, tr, _, _), r in cells.items():
        reqs[(ds, tr)] = max(reqs[(ds, tr)], fnum(r.get("requests")) or 0)
    traces = {(g["dataset"], g["trace"]) for g in gaps if g["rel"] is not None}
    best, unmapped = {}, defaultdict(int)
    for ds, tr in traces:
        grp = group_of(ds, tr)
        if grp is None:
            unmapped[ds] += 1
            continue
        key = (grp, tr)
        cur = best.get(key)
        if cur is None or reqs[(ds, tr)] > reqs[(cur, tr)]:
            best[key] = ds
    kept = {(ds, k[1]): k[0]
            for k, ds in best.items()}
    dropped = sum(1 for ds, tr in traces if group_of(ds, tr)) - len(kept)
    sources = defaultdict(lambda: defaultdict(int))
    for (ds, tr), grp in kept.items():
        sources[grp][ds] += 1

    table = defaultdict(dict)                       # grp -> (pct, lab) -> (mean, n)
    for pct in pcts:
        for lab, _ in TABLE_COLS:
            per = defaultdict(list)
            for g in gaps:
                grp = kept.get((g["dataset"], g["trace"]))
                if grp and g["cache_pct"] == pct and g["pair"] == lab and g["rel"] is not None:
                    per[grp].append(g["rel"])
            for grp, xs in per.items():
                table[grp][(pct, lab)] = (statistics.fmean(xs), len(xs))

    order = [g for g in PAPER if g in table] + \
            [g for g in dict.fromkeys(l for l, _, _ in GROUPS) if g in table and g not in PAPER]
    if not order:
        return
    w0 = max(len("Workload"), *(len(g) for g in order))
    head1 = f"{'':<{w0}} |" + "".join(f" {pct + '% of footprint':^31} |" for pct in pcts)
    head2 = f"{'Workload':<{w0}} |" + "".join(
        f" {'LRUForgive':>11} {'S3FIFOForgive':>13} {'n':>5} |" for _ in pcts)
    print("\n" + "=" * 78)
    print(f"WORKLOAD TABLE: average relative {metric} reduction (%), mean over traces")
    print("=" * 78)
    print(head1 + "\n" + head2 + "\n" + "-" * len(head2))
    cell = lambda v: f"{v[0]:+.2f}" if v else "--"
    for i, grp in enumerate(order):
        if i and grp not in PAPER and order[i - 1] in PAPER:
            print("-" * len(head2))
        line = f"{grp:<{w0}} |"
        for pct in pcts:
            a, b = table[grp].get((pct, "LRUF")), table[grp].get((pct, "S3FF"))
            n = f"{a[1]}/{b[1]}" if (a and b and a[1] != b[1]) else str((a or b or (0, 0))[1])
            line += f" {cell(a):>11} {cell(b):>13} {n:>5} |"
        print(line)
    print("  n = traces (LRUForgive/S3FIFOForgive when they differ, e.g. NA_SMALL on one policy)")
    print("  sources: " + "; ".join(
        f"{g}: " + ", ".join(f"{ds} ({n})" for ds, n in sorted(sources[g].items())) for g in order))
    if dropped > 0:
        print(f"  {dropped} duplicate copies of a trace (sampled versions etc.) dropped, "
              "keeping the copy with the most requests")
    if unmapped:
        print("  not in any group: " + ", ".join(f"{ds} ({n})" for ds, n in sorted(unmapped.items())))

    if args.table:
        with open(args.table + ".csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["workload", "in_paper_table", "cache_pct", "policy",
                        "mean_rel_reduction_pct", "n"])
            for grp in order:
                for pct in pcts:
                    for lab, name in TABLE_COLS:
                        v = table[grp].get((pct, lab))
                        w.writerow([grp, grp in PAPER, pct, name,
                                    f"{v[0]:.4f}" if v else "", v[1] if v else 0])
        print(f"  wrote {args.table}.csv")
        tc = lambda v: f"${v[0]:+.2f}$" if v else "--"
        out = [r"\begin{tabular}{l" + "rr" * len(pcts) + "}", r"\toprule",
               " & " + " & ".join(rf"\multicolumn{{2}}{{c}}{{{p}\% footprint}}" for p in pcts) + r" \\",
               "".join(rf"\cmidrule(lr){{{2 + 2 * i}-{3 + 2 * i}}}" for i in range(len(pcts))),
               r"\textbf{Workload} & " + " & ".join(
                   r"\textbf{LRUForgive} & \textbf{S3FIFOForgive}" for _ in pcts) + r" \\",
               r"\midrule"]
        for i, grp in enumerate(order):
            if i and grp not in PAPER and order[i - 1] in PAPER:
                out.append(r"\midrule")
            out.append(grp + " & " + " & ".join(
                f"{tc(table[grp].get((p, 'LRUF')))} & {tc(table[grp].get((p, 'S3FF')))}"
                for p in pcts) + r" \\")
        out += [r"\bottomrule", r"\end{tabular}"]
        with open(args.table + ".tex", "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        print(f"  wrote {args.table}.tex")


# ---------------------------------------------------------------- default vs tuned

def compare(args, col, only):
    """Default vs tuned (or borrowed) parameters, on traces present in both:
    per dataset, size and policy, then pooled over all datasets (ALL)."""
    pairs = [(fp, BASELINE[fp], "LRUF" if fp.startswith("lru") else "S3FF") for fp in POLICIES]

    def load(paths):
        cells = {}
        for path in paths:
            with open(path, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    m = r.get(col) or ""
                    if not m or m.startswith("NA") or r.get("dataset") == "dataset":
                        continue
                    if only is not None and (r["dataset"], r["trace"]) not in only:
                        continue
                    if any(re.search(x, r["dataset"]) for x in args.exclude):
                        continue
                    v = fnum(m)
                    if v is not None:
                        cells[(r["dataset"], r["trace"], r["policy"], r["cache_pct"])] = v
        rel = {}
        for (ds, tr, pol, pct), fm in cells.items():
            for fp, bp, lab in pairs:
                if pol == fp and (ds, tr, bp, pct) in cells:
                    bm = cells[(ds, tr, bp, pct)]
                    if bm > 0:
                        rel[(ds, pct, lab, tr)] = (bm - fm) / bm * 100
        return rel

    d, t = load(args.csv), load(args.tuned)
    groups = defaultdict(list)                 # (ds, pct, lab) -> [(trace, default, tuned)]
    for key, tv in t.items():
        if key in d:
            ds, pct, lab, tr = key
            groups[(ds, pct, lab)].append((tr, d[key], tv))
    if not groups:
        sys.exit("no traces present in both the default and tuned results")

    scope = f"only traces in {args.only}" if args.only else "all traces present in both"
    print(f"relative {'byte ' if args.byte else ''}miss-ratio reduction vs own baseline "
          f"(positive = forgive better); {scope}\n")
    hdr = (f"{'dataset':>24} {'size':>5} {'pol':>4} | {'n':>5} {'default':>9} {'tuned':>9} "
           f"{'delta':>8} {'tuned>dflt':>10} {'tuned<dflt':>10}")
    print(hdr)

    def line(name, pct, lab, g):
        dm = statistics.fmean(x[1] for x in g)
        tm = statistics.fmean(x[2] for x in g)
        better = sum(1 for x in g if x[2] > x[1] + 1e-9)
        worse = sum(1 for x in g if x[2] < x[1] - 1e-9)
        print(f"{name:>24} {pct + '%':>5} {lab:>4} | {len(g):>5} {dm:>+8.3f}% {tm:>+8.3f}% "
              f"{tm - dm:>+7.3f} {100 * better / len(g):>9.0f}% {100 * worse / len(g):>9.0f}%")

    for (ds, pct, lab) in sorted(groups, key=lambda k: (k[0], -float(k[1]), k[2])):
        line(ds, pct, lab, groups[(ds, pct, lab)])
    print("-" * len(hdr))
    pooled = defaultdict(list)
    for (ds, pct, lab), g in groups.items():
        pooled[(pct, lab)] += g
    for (pct, lab) in sorted(pooled, key=lambda k: (-float(k[0]), k[1])):
        line("ALL", pct, lab, pooled[(pct, lab)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="+", help="results CSV(s); later files win")
    ap.add_argument("--tuned", nargs="+", metavar="CSV", help="compare these against the main CSVs")
    ap.add_argument("--list", help="trace list, for progress per dataset")
    ap.add_argument("--remaining", metavar="OUT_TSV", help="with --list: write incomplete traces here")
    ap.add_argument("--only", metavar="LIST_TSV", help="only traces in this list")
    ap.add_argument("--exclude", action="append", default=[], metavar="REGEX",
                    help="drop datasets matching REGEX (repeatable)")
    ap.add_argument("--byte", action="store_true", help="byte miss ratio")
    ap.add_argument("--table", metavar="PREFIX", help="write the workload table to PREFIX.csv/.tex")
    ap.add_argument("--merged-out", metavar="CSV", help="write all inputs folded into one results file")
    ap.add_argument("--diag", metavar="GLOB", help="forgive_diag logs (quote the glob)")
    ap.add_argument("--gaps", metavar="CSV", help="write the per-trace reductions")
    args = ap.parse_args()
    col = "byte_miss" if args.byte else "miss"
    only = {(d, n) for d, n, _ in read_list(args.only)} if args.only else None
    if args.tuned:
        return compare(args, col, only)

    # ---------------------------------------------------------------- load
    cells = {}                                   # (ds,trace,policy,pct) -> row
    na_rows = []                                 # (key, reason); resolved after load
    na_row_of = {}                               # key -> the NA row itself (for --merged-out)
    fields = None
    seen_traces = defaultdict(set)               # ds -> {trace} with a numeric cell
    dupes = bad = 0
    for path in args.csv:
      with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        fields = fields or rd.fieldnames
        need = {"dataset", "trace", "policy", "cache_pct", "miss", "byte_miss"}
        if not need.issubset(rd.fieldnames or []):
            sys.exit(f"ERROR: {path} is not the libCacheSim sweep format "
                     f"(columns: {rd.fieldnames}). Old CacheLib CSVs need "
                     f"analyze_footprint_sweep.py instead.")
        for r in rd:
            if r.get("dataset") == "dataset":        # a repeated header line
                continue
            ds, tr = r["dataset"], r["trace"]
            if only is not None and (ds, tr) not in only:
                continue
            if any(re.search(x, ds) for x in args.exclude):
                continue
            k = (ds, tr, r["policy"], r["cache_pct"])
            if (r["miss"] or "").startswith("NA"):
                na_rows.append((k, r["miss"]))
                na_row_of[k] = r
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
    TRANSIENT = {"NA_DISK", "NA_UNZIP", "NA_DLFAIL", "NA_NOWSS", "NA_CONVERT", "NA_FAIL"}
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

    print(f"loaded {len(cells)} numeric cells from {', '.join(args.csv)}"
          + (f"  ({dupes} duplicate cells, last kept)" if dupes else "")
          + (f"  ({superseded} failed cells later retried OK)" if superseded else "")
          + (f"  ({bad} unparsable)" if bad else ""))
    print(f"{len(gaps)} forgive-vs-baseline pairs  |  metric: {metric}  |  positive = forgive better")

    # ---------------------------------------------------------------- progress
    if args.list:
        listed = read_list(args.list)
        if only is not None:
            listed = [x for x in listed if (x[0], x[1]) in only]
        want = sorted({k[3] for k in cells} | {k[3] for k, _ in na_rows}, key=pct_key)
        status = defaultdict(dict)                     # (ds,tr) -> {(pol,pct): ok|small}
        for (ds, tr, pol, pct) in cells:
            status[(ds, tr)][(pol, pct)] = "ok"
        for (ds, tr, pol, pct), reason in set(na_rows):
            if reason == "NA_SMALL":
                status[(ds, tr)].setdefault((pol, pct), "small")

        def complete(key):
            st = status.get(key, {})
            return (all(st.get((p, c)) == "ok" for p in BASE_POLS for c in want) and
                    all(st.get((p, c)) in ("ok", "small") for p in EMB_POLS for c in want))

        by_ds = defaultdict(list)
        for ds, name, url in listed:
            by_ds[ds].append((name, url))
        print("\n" + "=" * 78 + "\nPROGRESS  (any result / complete / expected;  complete = every "
              f"policy at {','.join(want)}% done or NA_SMALL)\n" + "=" * 78)
        tot = [0, 0, 0]
        incomplete = []
        for ds in sorted(by_ds, key=lambda d: -len(by_ds[d])):
            names = by_ds[ds]
            a = sum(1 for n, _ in names if n in seen_traces.get(ds, ()))
            c = sum(1 for n, _ in names if complete((ds, n)))
            incomplete += [(ds, u) for n, u in names if not complete((ds, n))]
            tot[0] += a; tot[1] += c; tot[2] += len(names)
            print(f"  {ds:>28} | {a:>5} {c:>5} / {len(names):<5} {100 * c / len(names):5.1f}% complete")
        print(f"  {'TOTAL':>28} | {tot[0]:>5} {tot[1]:>5} / {tot[2]:<5} "
              f"{100 * tot[1] / max(1, tot[2]):5.1f}% complete")
        if args.remaining:
            with open(args.remaining, "w", encoding="utf-8", newline="\n") as f:
                for ds, u in incomplete:
                    f.write(f"{ds}\t{u}\n")
            per = defaultdict(int)
            for ds, _ in incomplete:
                per[ds] += 1
            print(f"\n  wrote {len(incomplete)} incomplete traces -> {args.remaining}: "
                  + ", ".join(f"{d} {n}" for d, n in sorted(per.items(), key=lambda x: -x[1])))
    elif args.remaining:
        sys.exit("--remaining needs --list")

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
            if rs:
                better = sum(1 for x in rs if x > TIE)
                worse = sum(1 for x in rs if x < -TIE)
                line += (f" {len(rs):>5} {fmt(mean(rs), 12)} {fmt(med(rs), 11)}"
                         f" {100 * better / len(rs):5.0f}% {100 * worse / len(rs):5.0f}% |")
            else:
                line += f" {0:>5} {'--':>12} {'--':>11} {'':>6} {'':>6} |"
        print("-" * len(hdr) + "\n" + line)

    # ---------------------------------------------------------------- workload table
    workload_table(gaps, cells, pcts, args, metric)

    # ---------------------------------------------------------------- top traces
    for pct in pcts:
        for _, _, lab in PAIRS:
            rs = sorted((g for g in gaps if g["cache_pct"] == pct and g["pair"] == lab),
                        key=lambda g: -g["pp"])
            if not rs:
                continue
            base_lab = "base"
            for title, sel in (("TOP 10", rs[:10]), ("BOTTOM 10", rs[::-1][:10])):
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
        pct_by_idx = pcts                      # older logs only carry the size index
        stats = defaultdict(lambda: {"cells": 0, "binding": 0, "rate": []})
        # newer workers write pct= in the header; older logs only have the tag index
        hdr_re = re.compile(r"^### (\S+)/(\S+) tag=emb(\d+)\S*(?: pct=(\S+))?")
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
                        cur_pct = m.group(4) or (pct_by_idx[i] if i < len(pct_by_idx) else f"emb{i}")
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

    # ---------------------------------------------------------------- merged results file
    if args.merged_out:
        keep = dict(cells)
        for k, reason in set(na_rows):
            ds, tr = k[0], k[1]
            if k in cells or (reason in TRANSIENT and tr in seen_traces.get(ds, ())):
                continue                           # retried OK later: drop the failure row
            keep[k] = na_row_of[k]
        with open(args.merged_out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for k in sorted(keep, key=lambda k: (k[0], k[1], pct_key(k[3]), k[2])):
                w.writerow(keep[k])
        print(f"\nwrote {len(keep)} rows ({len(cells)} results, {len(keep) - len(cells)} unresolved "
              f"skip/failure rows) -> {args.merged_out}")

    # ---------------------------------------------------------------- per-trace reductions
    if not args.gaps:
        return
    with open(args.gaps, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["dataset", "trace", "cache_pct", "pair",
                                          "base_miss", "forgive_miss", "pp", "rel", "params"])
        w.writeheader()
        for g in sorted(gaps, key=lambda g: (g["dataset"], g["trace"], pct_key(g["cache_pct"]), g["pair"])):
            w.writerow(g)
    print(f"\nwrote per-trace gaps -> {args.gaps}")


if __name__ == "__main__":
    main()
