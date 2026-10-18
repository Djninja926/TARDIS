#!/usr/bin/env python3
"""
group_table.py - the paper-style table: average relative miss-ratio reduction
(%) of LRUForgive and S3FIFOForgive per workload group, one column pair per
cache size.

    rel% = (baseline_miss - forgive_miss) / baseline_miss * 100   per trace,
    then averaged over the traces in the group (positive = forgive better).
    LRUForgive is compared with LRU, S3FIFOForgive with S3-FIFO.

Input: the merged sweep CSVs (main sweep, rerun, 1% run). When the same trace
and size appear in more than one file, the LATER file wins, so list the rerun
after the main sweep.

GROUPS are matched on the dataset name (and, for the CMU "wiki" folder, the
trace name: wiki_2016u is upload, wiki_2019t is text). The first table row
group is the one in the paper; groups after the rule line are extra datasets
the paper table did not have. Datasets that match no group are listed at the
end so nothing disappears silently.

VARIANTS: the CMU mirror has several copies of some traces (twitter clusters
in full and as sample10 / sample100, alibabaBlock volumes in several
subfolders). They share a trace name, and averaging all of them would count
one workload several times. By default each (group, trace name) keeps only the
copy with the most requests, i.e. the full trace. --keep-variants turns that
off.

Usage (Windows):
  python group_table.py lcs_progress\\merged_all_nodes.csv rerun_results\\merged_all_nodes.csv ^
      p1_results\\merged_all_nodes.csv --latex group_table.tex --csv group_table.csv
Options:
  --pcts 10,1,0.1     cache sizes (columns), in this order
  --byte              byte miss ratio instead of object miss ratio
  --families          average per family of near-identical traces first
  --keep-variants     keep every copy of a trace (see VARIANTS)
  --exclude REGEX     drop datasets matching REGEX (repeatable)
  --only LIST_TSV     only traces in this list (e.g. tune_plan\\test_traces.tsv)
  --decimals 2
"""
import argparse, csv, re, statistics
from collections import defaultdict

FORGIVE = {"lruforgiveembcache": ("lru", "LRUForgive"),
           "s3fifoforgive-embcache": ("s3fifo", "S3FIFOForgive")}
POL_ORDER = ("lruforgiveembcache", "s3fifoforgive-embcache")

# (label, dataset regex, trace regex or None). First match wins. PAPER is the
# set of rows the paper's table has, in its order.
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


def group_of(ds, trace):
    for label, dre, tre in GROUPS:
        if re.search(dre, ds) and (tre is None or re.search(tre, trace)):
            return label
    return None


def fnum(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="+")
    ap.add_argument("--pcts", default="10,1,0.1")
    ap.add_argument("--byte", action="store_true")
    ap.add_argument("--families", action="store_true")
    ap.add_argument("--keep-variants", action="store_true")
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--only")
    ap.add_argument("--decimals", type=int, default=2)
    ap.add_argument("--csv-out", "--csv", dest="csv_out")
    ap.add_argument("--latex")
    args = ap.parse_args()
    pcts = [p.strip() for p in args.pcts.split(",") if p.strip()]
    col = "byte_miss" if args.byte else "miss"

    only = None
    if args.only:
        from trace_names import read_list
        only = {(d, n) for d, n, _ in read_list(args.only)}

    miss = {}            # (ds, trace, pct, policy) -> miss ; later files win
    reqs = {}            # (ds, trace) -> requests
    for path in args.csv:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                ds, tr = r.get("dataset", ""), r.get("trace", "")
                if any(re.search(x, ds) for x in args.exclude):
                    continue
                if only is not None and (ds, tr) not in only:
                    continue
                v = fnum(r.get(col))
                if v is None:
                    continue
                miss[(ds, tr, r.get("cache_pct", ""), r.get("policy", ""))] = v
                q = fnum(r.get("requests"))
                if q:
                    reqs[(ds, tr)] = max(reqs.get((ds, tr), 0), q)

    # per-trace relative reduction
    rel = {}             # (ds, trace, pct, fpol) -> rel%
    for (ds, tr, pct, pol), fm in miss.items():
        if pol not in FORGIVE:
            continue
        bm = miss.get((ds, tr, pct, FORGIVE[pol][0]))
        if bm is None or bm <= 0:
            continue
        rel[(ds, tr, pct, pol)] = (bm - fm) / bm * 100

    # group membership + variant dedupe
    unmapped = defaultdict(set)
    sources = defaultdict(lambda: defaultdict(set))   # group -> ds -> traces
    best = {}                                         # (group, trace) -> ds kept
    for (ds, tr) in {(k[0], k[1]) for k in rel}:
        g = group_of(ds, tr)
        if g is None:
            unmapped[ds].add(tr)
            continue
        key = (g, tr) if not args.keep_variants else (g, ds + "/" + tr)
        cur = best.get(key)
        if cur is None or reqs.get((ds, tr), 0) > reqs.get((cur, tr), 0):
            best[key] = ds
    kept = {(ds, k[1] if not args.keep_variants else k[1].split("/", 1)[1])
            for k, ds in best.items()}
    dropped = len({(k[0], k[1]) for k in rel if group_of(k[0], k[1])}) - len(kept)
    for ds, tr in kept:
        sources[group_of(ds, tr)][ds].add(tr)

    fam = {}
    if args.families:
        from trace_families import load_trace_stats, families
        stats = {}
        for p in args.csv:
            stats.update(load_trace_stats(p))
        fam = families(stats)

    # table[group][(pct, pol)] = (mean, n)
    table = defaultdict(dict)
    for g in sources:
        for pct in pcts:
            for pol in POL_ORDER:
                vals = defaultdict(list)
                for ds, trs in sources[g].items():
                    for tr in trs:
                        x = rel.get((ds, tr, pct, pol))
                        if x is not None:
                            unit = fam.get((ds, tr), (ds, tr)) if args.families else (ds, tr)
                            vals[unit].append(x)
                if vals:
                    xs = [statistics.fmean(v) for v in vals.values()]
                    table[g][(pct, pol)] = (statistics.fmean(xs), len(xs))

    order = [g for g in PAPER if g in table] + \
            [g for g in dict.fromkeys(l for l, _, _ in GROUPS) if g in table and g not in PAPER]
    d = args.decimals
    cell = lambda v: f"{v[0]:+.{d}f}" if v else "--"

    # ---- text
    unit = "families" if args.families else "traces"
    w0 = max(len("Workload"), *(len(g) for g in order)) if order else 8
    head1 = f"{'':<{w0}} |" + "".join(f" {pct + '% of footprint':^31} |" for pct in pcts)
    head2 = f"{'Workload':<{w0}} |" + "".join(
        f" {'LRUForgive':>11} {'S3FIFOForgive':>13} {'n':>5} |" for _ in pcts)
    print(f"Average relative {'byte ' if args.byte else ''}miss-ratio reduction (%), "
          f"mean over {unit}; n = {unit} (LRUForgive / S3FIFOForgive)")
    print(head1 + "\n" + head2 + "\n" + "-" * len(head2))
    for i, g in enumerate(order):
        if i and g not in PAPER and order[i - 1] in PAPER:
            print("-" * len(head2))
        line = f"{g:<{w0}} |"
        for pct in pcts:
            a, b = table[g].get((pct, POL_ORDER[0])), table[g].get((pct, POL_ORDER[1]))
            n = f"{a[1] if a else 0}/{b[1] if b else 0}" if (a and b and a[1] != b[1]) else str((a or b or (0, 0))[1])
            line += f" {cell(a):>11} {cell(b):>13} {n:>5} |"
        print(line)

    print("\nSources per row (dataset: traces kept):")
    for g in order:
        print(f"  {g}: " + ", ".join(f"{ds} ({len(t)})" for ds, t in sorted(sources[g].items())))
    if dropped > 0:
        print(f"\n{dropped} duplicate copies of a trace (same group and name, e.g. sampled "
              "versions) were dropped, keeping the copy with the most requests. "
              "--keep-variants keeps all.")
    if unmapped:
        print("\nNOT in any group (not in the table): " +
              ", ".join(f"{ds} ({len(t)})" for ds, t in sorted(unmapped.items())))

    # ---- csv
    if args.csv_out:
        with open(args.csv_out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["workload", "in_paper_table", "cache_pct", "policy", "mean_rel_reduction_pct", "n"])
            for g in order:
                for pct in pcts:
                    for pol in POL_ORDER:
                        v = table[g].get((pct, pol))
                        w.writerow([g, g in PAPER, pct, FORGIVE[pol][1],
                                    f"{v[0]:.4f}" if v else "", v[1] if v else 0])
        print(f"\nwrote {args.csv_out}")

    # ---- latex (booktabs, same layout as the paper table, one column pair per size)
    if args.latex:
        tex_cell = lambda v: f"${v[0]:+.{d}f}$" if v else "--"
        lines = [r"\begin{tabular}{l" + "rr" * len(pcts) + "}", r"\toprule",
                 " & " + " & ".join(rf"\multicolumn{{2}}{{c}}{{{p}\% footprint}}" for p in pcts) + r" \\",
                 "".join(rf"\cmidrule(lr){{{2 + 2 * i}-{3 + 2 * i}}}" for i in range(len(pcts))),
                 r"\textbf{Workload} & " + " & ".join(
                     r"\textbf{LRUForgive} & \textbf{S3FIFOForgive}" for _ in pcts) + r" \\",
                 r"\midrule"]
        for i, g in enumerate(order):
            if i and g not in PAPER and order[i - 1] in PAPER:
                lines.append(r"\midrule")
            lines.append(g + " & " + " & ".join(
                f"{tex_cell(table[g].get((p, POL_ORDER[0])))} & {tex_cell(table[g].get((p, POL_ORDER[1])))}"
                for p in pcts) + r" \\")
        lines += [r"\bottomrule", r"\end{tabular}"]
        with open(args.latex, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"wrote {args.latex}")


if __name__ == "__main__":
    main()
