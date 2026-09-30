#!/usr/bin/env python3
"""
make_tuning_plan.py - plan the hyperparameter tuning: which studies, which
training traces, which held-out test traces, and which node runs what.

One STUDY = one (dataset, cache size, forgive policy). It searches Aryan's three
parameters (lr, ctx-speed, threshold, same ranges as his tuners) to maximize the
MEAN relative miss-ratio reduction over that policy's own baseline on the
study's training traces. Other parameters stay at their defaults.

HELD-OUT BY FAMILY. Training traces are chosen per dataset by FAMILY (see
trace_families.py): one representative per chosen family is trained on, the
rest of that family is excluded, and every other family is the test set. A
random split by trace would leak: many block volumes are near-clones, so a
"held-out" trace could be a copy of a training trace and the held-out result
would just repeat the training result.

Eligibility: a trace can train a (size, policy) study only if the sweep has a
real forgive result for it at that size (not NA_SMALL). A study needs at least
MIN_TRAIN eligible training traces and MIN_TEST eligible test traces, so the
tuned params can actually be evaluated on held-out data.

Inputs are the merged sweep CSVs (main sweep + rerun + 1% run), so plan AFTER
those finish: eligibility at a size is only known once that size has run.

Outputs (in --outdir unless noted):
  shard_NN.txt        one JSON study per line; launch with
                        orchestrate.py --run tune --worker node_tune_worker.sh
  tune_split.tsv      dataset, trace, family, role (train / excluded / test / untuned)
  apply_traces.tsv    every trace of the tuned datasets  (the tuned re-run)
  test_traces.tsv     held-out traces only               (headline results)
  train_traces.tsv    training traces only               (in-sample check)

Usage:
  python make_tuning_plan.py lcs_progress\\merged_all_nodes.csv rerun_results\\merged_all_nodes.csv ^
      p1_results\\merged_all_nodes.csv --list all_traces.tsv --nodes 8 --outdir tune_plan
"""
import argparse, csv, json, os, random, sys
from collections import defaultdict

from trace_families import load_trace_stats, families
from trace_names import read_list

POLICIES = ("lruforgiveembcache", "s3fifoforgive-embcache")
BASELINE = {"lruforgiveembcache": "lru", "s3fifoforgive-embcache": "s3fifo"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="+", help="merged sweep CSVs")
    ap.add_argument("--list", required=True, help="all_traces.tsv")
    ap.add_argument("--nodes", type=int, default=8)
    ap.add_argument("--outdir", default="tune_plan")
    ap.add_argument("--pcts", default="10,1,0.1", help="cache sizes to tune (percent)")
    ap.add_argument("--train-max", type=int, default=8, help="max training traces per dataset")
    ap.add_argument("--min-families", type=int, default=4,
                    help="datasets with fewer families are not tuned (no honest held-out set)")
    ap.add_argument("--min-train", type=int, default=2)
    ap.add_argument("--min-test", type=int, default=2)
    ap.add_argument("--max-train-requests", type=float, default=30e6,
                    help="training traces above this are not used (each trial replays "
                         "every training trace, so huge traces dominate tuning time)")
    ap.add_argument("--trials", type=int, default=60)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    pcts = [p.strip() for p in args.pcts.split(",") if p.strip()]
    rng = random.Random(args.seed)

    # ---- sweep results: eligibility per (ds, trace, pct, policy), requests per trace
    ok = set()                 # (ds, trace, pct, policy) with a real forgive result
    requests = {}
    stats = {}
    for path in args.csv:
        stats.update(load_trace_stats(path))
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                m = r.get("miss") or ""
                if not m or m.startswith("NA") or r.get("dataset") == "dataset":
                    continue
                key = (r["dataset"], r["trace"])
                try:
                    requests[key] = int(r["requests"])
                except (TypeError, ValueError):
                    pass
                if r["policy"] in POLICIES:
                    ok.add((r["dataset"], r["trace"], r["cache_pct"], r["policy"]))
    fam = families(stats)

    listed = read_list(args.list)
    by_ds = defaultdict(list)
    for ds, name, url in listed:
        by_ds[ds].append((name, url))

    studies, split_rows = [], []
    apply_rows, test_rows, train_rows = [], [], []
    print(f"{'dataset':>24} | {'traces':>6} {'fams':>5} {'train':>5} {'test':>5} | studies (size:policy)")

    for ds in sorted(by_ds, key=lambda d: -len(by_ds[d])):
        traces = by_ds[ds]
        url_of = dict(traces)
        fam_of = {n: fam.get((ds, n), f"{ds}#solo:{n}") for n, _ in traces}
        members = defaultdict(list)
        for n, _ in traces:
            members[fam_of[n]].append(n)

        def elig(n):
            return sum(1 for c in pcts for p in POLICIES if (ds, n, c, p) in ok)

        # one candidate representative per family: most eligible (size, policy)
        # combinations, within the request cap; ties broken at random
        cands = []
        for fid, ms in members.items():
            pool = [n for n in ms if requests.get((ds, n), 0) <= args.max_train_requests
                    and elig(n) > 0]
            if pool:
                rng.shuffle(pool)
                pool.sort(key=lambda n: -elig(n))
                cands.append((fid, pool[0]))

        n_fam = len(members)
        k = min(args.train_max, n_fam // 2, len(cands))
        if n_fam < args.min_families or k < args.min_train:
            for n, _ in traces:
                split_rows.append((ds, n, fam_of[n], "untuned"))
            why = f"only {n_fam} families" if n_fam < args.min_families else "too few eligible families"
            print(f"{ds:>24} | {len(traces):>6} {n_fam:>5} {'-':>5} {'-':>5} | not tuned ({why})")
            continue

        rng.shuffle(cands)
        cands.sort(key=lambda c: -elig(c[1]))        # prefer broadly-eligible families
        chosen = cands[:k]
        train = {rep for _, rep in chosen}
        train_fams = {fid for fid, _ in chosen}

        role = {}
        for n, _ in traces:
            if n in train:
                role[n] = "train"
            elif fam_of[n] in train_fams:
                role[n] = "excluded"           # clone of a training trace: never tested
            else:
                role[n] = "test"

        made = []
        for c in pcts:
            for p in POLICIES:
                tr = [n for n in train if (ds, n, c, p) in ok]
                te = [n for n, _ in traces if role[n] == "test" and (ds, n, c, p) in ok]
                if len(tr) < args.min_train or len(te) < args.min_test:
                    continue
                studies.append({
                    "id": f"{ds}|{c}|{p}",
                    "dataset": ds, "pct": c, "frac": float(c) / 100.0,
                    "policy": p, "baseline": BASELINE[p],
                    "trials": args.trials,
                    "seed": rng.randrange(1 << 30),
                    "n_test": len(te),
                    "train": [{"name": n, "url": url_of[n],
                               "requests": requests.get((ds, n), 0)} for n in sorted(tr)],
                })
                made.append(f"{c}:{'LRUF' if p.startswith('lru') else 'S3FF'}")

        if not made:
            for n, _ in traces:
                split_rows.append((ds, n, fam_of[n], "untuned"))
            print(f"{ds:>24} | {len(traces):>6} {n_fam:>5} {'-':>5} {'-':>5} | not tuned "
                  f"(no size with {args.min_train}+ train and {args.min_test}+ test results)")
            continue

        n_test = sum(1 for v in role.values() if v == "test")
        print(f"{ds:>24} | {len(traces):>6} {n_fam:>5} {len(train):>5} {n_test:>5} | {' '.join(made)}")
        for n, u in traces:
            split_rows.append((ds, n, fam_of[n], role[n]))
            apply_rows.append((ds, u))
            if role[n] == "test":
                test_rows.append((ds, u))
            elif role[n] == "train":
                train_rows.append((ds, u))

    if not studies:
        sys.exit("no studies could be planned (have the sweeps for these sizes finished?)")

    # ---- balance studies across nodes by estimated cost (LPT)
    for s in studies:
        s["cost"] = s["trials"] * sum(t["requests"] for t in s["train"])
    studies.sort(key=lambda s: -s["cost"])
    bins = [[] for _ in range(args.nodes)]
    load = [0] * args.nodes
    for s in studies:
        i = load.index(min(load))
        bins[i].append(s)
        load[i] += s["cost"]

    os.makedirs(args.outdir, exist_ok=True)
    for i, b in enumerate(bins):
        with open(os.path.join(args.outdir, f"shard_{i:02d}.txt"), "w", encoding="utf-8", newline="\n") as f:
            for s in b:
                f.write(json.dumps(s) + "\n")

    def write_tsv(name, rows):
        with open(os.path.join(args.outdir, name), "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write("\t".join(r) + "\n")

    write_tsv("tune_split.tsv", split_rows)
    write_tsv("apply_traces.tsv", apply_rows)
    write_tsv("test_traces.tsv", test_rows)
    write_tsv("train_traces.tsv", train_rows)

    tot = sum(load)
    print(f"\n{len(studies)} studies over {args.nodes} nodes; est. cost per node "
          f"(trials x training requests): " +
          ", ".join(f"{l / 1e9:.1f}G" for l in load))
    print(f"apply run: {len(apply_rows)} traces  |  held-out test: {len(test_rows)}  |  "
          f"train: {len(train_rows)}")
    print(f"wrote {args.outdir}/shard_NN.txt, tune_split.tsv, apply_traces.tsv, "
          f"test_traces.tsv, train_traces.tsv")
    if tot == 0:
        print("WARN: training traces have no request counts; balance is by study count only")


if __name__ == "__main__":
    main()
