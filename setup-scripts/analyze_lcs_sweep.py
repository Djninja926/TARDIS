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
  --remaining rerun.tsv     with --list: write every trace that is NOT complete
                            (never ran, failed, or stopped part-way), in the
                            same <dataset>\\t<url> format, ready to partition
  --only traces.tsv         analyze only the traces in this list (e.g. the
                            held-out test traces from the tuning plan)
  --no-families             skip the family-weighted section
  --exclude REGEX           drop datasets matching REGEX everywhere (repeatable)

WORKLOAD TABLE (always printed): the paper-style table, mean relative miss-ratio
reduction per workload group (Wiki Upload, Wiki Text, Meta KV, ...), LRUForgive
and S3FIFOForgive at every cache size. Sampled copies of a trace are counted
once (the full copy); "sources" shows which datasets fed each row.
  --latex table.tex / --table-csv table.csv    also write it out
  --families-table          average per family first
  --keep-variants           count every copy of a trace

FAMILIES: near-identical volumes (see trace_names.py) are grouped and a
family-weighted view is printed, where each family counts once. If the two
views disagree, the trace-weighted numbers are being driven by clones.

Usage (main sweep first, later runs after it: when a cell repeats, the last wins):
  python analyze_lcs_sweep.py lcs_progress\\merged_all_nodes.csv rerun_results\\merged_all_nodes.csv ^
      p1_results\\merged_all_nodes.csv --list all_plus.tsv --diag "*_results\\forgive_diag_*.log" ^
      --latex workload_table.tex
"""
import argparse, csv, glob, re, statistics, sys
from collections import defaultdict

from trace_names import read_list, load_trace_stats, families

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
# Groups match on the dataset name (and, for the CMU "wiki" folder, the trace
# name: wiki_2016u = upload, wiki_2019t = text). First match wins. Groups after
# the rule line are datasets the paper table does not have.
#
# VARIANTS: the CMU mirror has several copies of some traces (twitter clusters
# in full and as sample10 / sample100, alibabaBlock volumes in several
# subfolders). They share a trace name, so within a group each trace name keeps
# only the copy with the most requests (the full trace). --keep-variants keeps all.
GROUPS = [
    ("Wiki Upload",   r"^wikimedia_.*upload", None),
    ("Wiki Upload",   r"(^|_)wiki($|_)", r"u$"),
    ("Wiki Text",     r"^wikimedia_.*text", None),
    ("Wiki Text",     r"(^|_)wiki($|_)", r"t$"),
    ("Meta KV",       r"metaKV", None),
    ("Tencent Photo", r"tencentPhoto", None),
    ("Tencent Block", r"tencentBlock", None),
    ("Meta CDN",      r"metaCDN", None),
    ("Alibaba Block", r"alibabaBlock", None),
    ("Twitter",       r"twitter", None),
    ("Meta Storage",  r"metaStorage", None),
    ("MSR",           r"(^|_)msr", None),
    ("CloudPhysics",  r"cloudphysics", None),
    ("FIU",           r"(^|_)fiu", None),
    ("SYSTOR",        r"systor", None),
]
PAPER = ["Wiki Upload", "Wiki Text", "Meta KV", "Tencent Photo", "Tencent Block",
         "Meta CDN", "Alibaba Block", "Twitter"]
TABLE_COLS = (("LRUF", "LRUForgive"), ("S3FF", "S3FIFOForgive"))


def group_of(ds, trace):
    for label, dre, tre in GROUPS:
        if re.search(dre, ds) and (tre is None or re.search(tre, trace)):
            return label
    return None


def workload_table(gaps, cells, pcts, fam, args, metric):
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
        key = (grp, ds + "/" + tr) if args.keep_variants else (grp, tr)
        cur = best.get(key)
        if cur is None or reqs[(ds, tr)] > reqs[(cur, tr)]:
            best[key] = ds
    kept = {(ds, k[1].rsplit("/", 1)[-1] if args.keep_variants else k[1]): k[0]
            for k, ds in best.items()}
    dropped = sum(1 for ds, tr in traces if group_of(ds, tr)) - len(kept)
    sources = defaultdict(lambda: defaultdict(int))
    for (ds, tr), grp in kept.items():
        sources[grp][ds] += 1

    table = defaultdict(dict)                       # grp -> (pct, lab) -> (mean, n)
    for pct in pcts:
        for lab, _ in TABLE_COLS:
            per = defaultdict(lambda: defaultdict(list))
            for g in gaps:
                grp = kept.get((g["dataset"], g["trace"]))
                if grp and g["cache_pct"] == pct and g["pair"] == lab and g["rel"] is not None:
                    unit = fam.get((g["dataset"], g["trace"]), (g["dataset"], g["trace"])) \
                        if args.families_table else (g["dataset"], g["trace"])
                    per[grp][unit].append(g["rel"])
            for grp, units in per.items():
                xs = [statistics.fmean(v) for v in units.values()]
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
    print(f"WORKLOAD TABLE: average relative {metric} reduction (%), "
          f"mean over {'families' if args.families_table else 'traces'}")
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
    print(f"  n = {'families' if args.families_table else 'traces'} "
          "(LRUForgive/S3FIFOForgive when they differ, e.g. NA_SMALL on one policy)")
    print("  sources: " + "; ".join(
        f"{g}: " + ", ".join(f"{ds} ({n})" for ds, n in sorted(sources[g].items())) for g in order))
    if dropped > 0:
        print(f"  {dropped} duplicate copies of a trace (sampled versions etc.) dropped, "
              "keeping the copy with the most requests (--keep-variants keeps all)")
    if unmapped:
        print("  not in any group: " + ", ".join(f"{ds} ({n})" for ds, n in sorted(unmapped.items())))

    if args.table_csv:
        with open(args.table_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["workload", "in_paper_table", "cache_pct", "policy",
                        "mean_rel_reduction_pct", "n"])
            for grp in order:
                for pct in pcts:
                    for lab, name in TABLE_COLS:
                        v = table[grp].get((pct, lab))
                        w.writerow([grp, grp in PAPER, pct, name,
                                    f"{v[0]:.4f}" if v else "", v[1] if v else 0])
        print(f"  wrote {args.table_csv}")
    if args.latex:
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
        with open(args.latex, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        print(f"  wrote {args.latex}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="+",
                    help="one or more merged CSVs (e.g. the main sweep + the rerun + the 1%% run)")
    ap.add_argument("--list", help="all_traces.tsv, for progress per dataset")
    ap.add_argument("--diag", help='glob of forgive_diag_*.log files (quote it)')
    ap.add_argument("--pcts", default="10,1,0.1",
                    help="the worker's PCTS, in SIZES order (maps diag tag embN -> pct)")
    ap.add_argument("--byte", action="store_true", help="use byte miss ratio")
    ap.add_argument("--remaining", metavar="OUT_TSV",
                    help="with --list: write incomplete traces here for a rerun")
    ap.add_argument("--only", metavar="LIST_TSV",
                    help="restrict the analysis to traces in this list")
    ap.add_argument("--no-families", action="store_true")
    ap.add_argument("--families-table", action="store_true",
                    help="workload table: average per family of near-identical traces first")
    ap.add_argument("--top", type=int, default=10, help="top-N traces by gap to list")
    ap.add_argument("--out", default="lcs_gaps.csv")
    ap.add_argument("--keep-variants", action="store_true",
                    help="workload table: keep every copy of a trace (see WORKLOAD TABLE)")
    ap.add_argument("--exclude", action="append", default=[], metavar="REGEX",
                    help="drop datasets matching REGEX from the whole analysis (repeatable)")
    ap.add_argument("--latex", metavar="TEX", help="workload table as LaTeX (booktabs)")
    ap.add_argument("--table-csv", metavar="CSV", help="workload table as CSV")
    args = ap.parse_args()
    col = "byte_miss" if args.byte else "miss"
    only = {(d, n) for d, n, _ in read_list(args.only)} if args.only else None

    # ---------------------------------------------------------------- load
    cells = {}                                   # (ds,trace,policy,pct) -> row
    na_rows = []                                 # (key, reason); resolved after load
    seen_traces = defaultdict(set)               # ds -> {trace} with a numeric cell
    dupes = bad = 0
    for path in args.csv:
      with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
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
        want = [p.strip() for p in args.pcts.split(",") if p.strip()]
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

    # ---------------------------------------------------------------- families
    fam = {}
    if not args.no_families:
        stats = {}
        for path in args.csv:
            stats.update(load_trace_stats(path))
        fam = families(stats)
        fam_n = defaultdict(set)
        for (ds, tr), fid in fam.items():
            fam_n[ds].add(fid)
        print("\n" + "=" * 78)
        print("FAMILY-WEIGHTED rollup (each family of near-identical traces counts once)")
        print("=" * 78)
        print("  If this disagrees with the table above, clones are driving the trace-weighted numbers.")
        for pct in pcts:
            print(f"\n  at {pct}% footprint")
            hdr = f"{'dataset':>28} | {'traces':>6} {'fams':>5} |"
            for _, _, lab in PAIRS:
                hdr += f" {lab + ' famW':>12} {'better':>6} |"
            print(hdr)
            for ds in datasets:
                line = f"{ds:>28} |"
                n_tr = len({g['trace'] for g in gaps if g['dataset'] == ds and g['cache_pct'] == pct})
                if not n_tr:
                    continue
                fams_here = {fam.get((ds, g['trace']), g['trace']) for g in gaps
                             if g['dataset'] == ds and g['cache_pct'] == pct}
                line += f" {n_tr:>6} {len(fams_here):>5} |"
                for _, _, lab in PAIRS:
                    byf = defaultdict(list)
                    for g in gaps:
                        if g["dataset"] == ds and g["cache_pct"] == pct and g["pair"] == lab \
                                and g["rel"] is not None:
                            byf[fam.get((ds, g["trace"]), g["trace"])].append(g["rel"])
                    fm = [statistics.fmean(v) for v in byf.values()]
                    if fm:
                        b = sum(1 for x in fm if x > TIE)
                        line += f" {fmt(mean(fm), 12)} {100 * b / len(fm):5.0f}% |"
                    else:
                        line += f" {'--':>12} {'--':>6} |"
                print(line)

    # ---------------------------------------------------------------- workload table
    workload_table(gaps, cells, pcts, fam if not args.no_families else {}, args, metric)

    # ---------------------------------------------------------------- top traces
    for pct in pcts:
        for _, _, lab in PAIRS:
            rs = sorted((g for g in gaps if g["cache_pct"] == pct and g["pair"] == lab),
                        key=lambda g: -g["pp"])
            if not rs:
                continue
            base_lab = "base"
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
