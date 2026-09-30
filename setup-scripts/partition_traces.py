#!/usr/bin/env python3
"""
partition_traces.py - split the master trace list into N balanced per-node shards.

Balances by trace COUNT across N nodes. Big datasets get split across nodes;
small datasets stay whole where possible. Every trace lands in exactly ONE
shard (no overlap), so results merge cleanly by trace name.

Input:  master_trace_list.tsv  (from gen_trace_list.py: <dataset>\t<url> per line)
Output: shard_00.txt .. shard_{N-1}.txt  (one URL per line, ready for the worker)

Usage:
  python3 partition_traces.py master_trace_list.tsv 12 --outdir shards/
"""
import sys, argparse, os
from collections import defaultdict

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("master", help="master_trace_list.tsv")
    ap.add_argument("n_nodes", type=int)
    ap.add_argument("--outdir", default="shards")
    ap.add_argument("--weights-from", action="append", default=[], metavar="CSV",
                    help="merged sweep CSV(s): weight each dataset by its MEDIAN requests "
                         "per trace (in millions). Balances nodes by work instead of by "
                         "trace count; datasets still stay together.")
    ap.add_argument("--weight", action="append", default=[], metavar="DATASET=W",
                    help="cost of one trace in DATASET, in millions of requests "
                         "(overrides --weights-from; use it for datasets with no results "
                         "yet). Datasets stay together; a heavy dataset is split into "
                         "contiguous chunks over several nodes, exactly like a dataset "
                         "with many traces already is.")
    ap.add_argument("--round-robin", action="store_true",
                    help="deal traces across nodes one at a time, dataset by dataset, so "
                         "every node gets an even slice of EVERY dataset. Use this when "
                         "datasets differ a lot in per-trace cost (twitter, wiki): the "
                         "default keeps datasets together and can leave one node with "
                         "all the slow ones.")
    args = ap.parse_args()

    # read (dataset, url) rows, skip comments. utf-8-sig tolerates a UTF-8/UTF-16
    # BOM that PowerShell's > redirect adds.
    rows = []
    with open(args.master, encoding="utf-8-sig") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 2:
                continue
            rows.append((parts[0], parts[1]))

    total = len(rows)
    N = args.n_nodes
    target = total / N
    print(f"{total} traces over {N} nodes, target {target:.1f}/node", file=sys.stderr)

    # group urls by dataset (keep order stable/sorted for determinism)
    by_ds = defaultdict(list)
    for ds, url in rows:
        by_ds[ds].append(url)
    for ds in by_ds:
        by_ds[ds].sort()

    if args.round_robin:
        bins = [[] for _ in range(N)]
        per = [defaultdict(int) for _ in range(N)]
        i = 0
        # biggest datasets first so the small ones fill in the remainders evenly
        for ds, urls in sorted(by_ds.items(), key=lambda x: -len(x[1])):
            for url in urls:
                bins[i % N].append(url)
                per[i % N][ds] += 1
                i += 1
        loads = [len(b) for b in bins]
        os.makedirs(args.outdir, exist_ok=True)
        for k in range(N):
            with open(os.path.join(args.outdir, f"shard_{k:02d}.txt"), "w", newline="\n") as f:
                for url in bins[k]:
                    f.write(url + "\n")
            top = sorted(per[k].items(), key=lambda x: -x[1])[:4]
            print(f"  shard_{k:02d}: {loads[k]:>4} traces  e.g. "
                  + ", ".join(f"{d}({n})" for d, n in top), file=sys.stderr)
        assert sum(loads) == total, f"MISMATCH: wrote {sum(loads)} but had {total}"
        print(f"load range: {min(loads)}-{max(loads)} traces/node (round-robin)", file=sys.stderr)
        print(f"OK: all {total} traces assigned, no overlap", file=sys.stderr)
        return

    # build "chunks": split any dataset whose COST is larger than ~target into
    # round(cost/target) contiguous pieces; keep smaller datasets whole. Then
    # greedily pack chunks into N bins by current cost (Longest-Processing-Time).
    # cost = traces x weight; with no --weight this is exactly the trace count.
    weights = {}
    if args.weights_from:
        import csv, statistics
        req = defaultdict(dict)
        for path in args.weights_from:
            with open(path, encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    try:
                        req[r["dataset"]][r["trace"]] = int(r["requests"])
                    except (KeyError, TypeError, ValueError):
                        pass
        for ds_w, d in req.items():
            # floor of 1 (million): even a tiny trace pays download + startup
            weights[ds_w] = max(1.0, statistics.median(d.values()) / 1e6)
    for wspec in args.weight:
        ds_w, _, w = wspec.partition("=")
        try:
            weights[ds_w] = float(w)
        except ValueError:
            sys.exit(f"--weight must look like DATASET=number (got {wspec!r})")
    for wspec in args.weight:
        if wspec.partition("=")[0] not in by_ds:
            print(f"WARN: --weight {wspec}: no such dataset in {args.master}", file=sys.stderr)
    default_w = 1.0
    if weights:
        # datasets with no weight at all get the median known weight, not 1,
        # so they are neither ignored nor treated as free
        default_w = sorted(weights.values())[len(weights) // 2]
        missing = sorted(d for d in by_ds if d not in weights)
        if missing:
            print(f"WARN: no request data for {', '.join(missing)}; using {default_w:.1f}M "
                  f"per trace. Pass --weight DATASET=M to set them.", file=sys.stderr)
        for d in sorted(by_ds, key=lambda d: -len(by_ds[d]) * weights.get(d, default_w)):
            w = weights.get(d, default_w)
            print(f"    weight {d:>24}: {w:>8.1f}M requests/trace x {len(by_ds[d]):>4} "
                  f"= {w * len(by_ds[d]) / 1e3:>7.1f}B", file=sys.stderr)
    wt = lambda ds: weights.get(ds, default_w)
    total_w = sum(len(u) * wt(ds) for ds, u in by_ds.items())
    target = total_w / N
    chunks = []  # (label, [urls], weight)
    for ds, urls in sorted(by_ds.items(), key=lambda x: -len(x[1]) * wt(x[0])):
        size = len(urls)
        cost = size * wt(ds)
        if cost > target * 1.3 and size > 1:
            k = max(2, min(size, round(cost / target)))
            base = size // k
            rem = size % k
            idx = 0
            for i in range(k):
                cnt = base + (1 if i < rem else 0)
                chunks.append((f"{ds}[{i+1}/{k}]", urls[idx:idx+cnt], wt(ds)))
                idx += cnt
        else:
            chunks.append((ds, urls, wt(ds)))

    # LPT packing by cost
    chunks.sort(key=lambda c: -len(c[1]) * c[2])
    bins = [[] for _ in range(N)]
    costs = [0.0] * N
    loads = [0] * N
    assign = [[] for _ in range(N)]
    for label, urls, w in chunks:
        i = costs.index(min(costs))
        bins[i].extend(urls)
        costs[i] += len(urls) * w
        loads[i] += len(urls)
        assign[i].append(f"{label}({len(urls)})")

    os.makedirs(args.outdir, exist_ok=True)
    for i in range(N):
        path = os.path.join(args.outdir, f"shard_{i:02d}.txt")
        with open(path, "w", newline="\n") as f:
            for url in bins[i]:
                f.write(url + "\n")
        extra = f"  ~{costs[i] / 1e3:.1f}B requests" if weights else ""
        print(f"  shard_{i:02d}: {loads[i]:>4} traces{extra}  {assign[i]}", file=sys.stderr)

    print(f"load range: {min(loads)}-{max(loads)} traces/node"
          + (f", work {min(costs) / 1e3:.1f}-{max(costs) / 1e3:.1f}B requests/node"
             if weights else ""), file=sys.stderr)
    # sanity: no overlap, all accounted for
    written = sum(loads)
    assert written == total, f"MISMATCH: wrote {written} but had {total}"
    print(f"OK: all {total} traces assigned, no overlap", file=sys.stderr)

if __name__ == "__main__":
    main()
