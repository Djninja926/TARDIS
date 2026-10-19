#!/usr/bin/env python3
"""
tune.py - hyperparameter tuning for the forgive policies, in three steps.

  python tune.py plan RESULTS.csv... --list TRACES.tsv     (your machine)
      Choose the studies, their training traces and the held-out test traces,
      and split the studies across nodes. Writes tune_plan/.
  orchestrate.py --launch --run tune --plan tune_plan      (runs `tune.py run` on every node)
  python tune.py params STUDIES.csv                        (your machine)
      Turn the finished studies into tuned_params.json for the tuned sweep.

One STUDY = one (dataset, cache size, forgive policy). Optuna (TPE) searches
    lr         0.01 - 0.5    (log)
    ctx-speed  1e-7 - 1e-3   (log)
    threshold  0.3  - 0.95
to maximize the mean relative miss-ratio reduction over the policy's own
baseline on the study's training traces. The defaults (lr=0.2, ctx-speed=0.001,
threshold=0.4) are always tried first, so the best result is never worse than
the defaults on the training traces.

HELD-OUT BY FAMILY. Training traces are one representative from each of up to
TRAIN_MAX families of near-identical traces (common.families); the rest of
those families is excluded and every other family is the test set, so a
held-out trace is never a copy of a training trace.

Plan outputs (tune_plan/):
  shard_NN.txt       one JSON study per line, one file per node
  tune_split.tsv     dataset, trace, family, role (train / excluded / test / untuned)
  apply_traces.tsv   traces to rerun with tuned parameters
  test_traces.tsv    held-out traces (judge tuning on these)
  train_traces.tsv   training traces
"""
import argparse, concurrent.futures, csv, json, os, random, subprocess, sys, threading, time
from collections import defaultdict

from common import (BASELINE, FORGIVE_RATE_RE, LINE_RE, POLICIES, WSS_RE,
                    families, load_trace_stats, needs_conversion, read_list)

# ---- planning choices (the values used for the results in ../result/)
TRAIN_MAX = 8                # training traces per dataset (one per family)
MIN_FAMILIES = 2             # fewer families: no honest held-out set, not tuned
MIN_TRAIN, MIN_TEST = 1, 1   # per (size, policy) study
MAX_TRAIN_REQUESTS = 150e6   # each trial replays every training trace
SEED = 1
# Full-size Twitter clusters are left out of the tuned rerun: several exceed the
# node's memory or the time limit. Their sampled copies are tuned and rerun.
APPLY_EXCLUDE = {"twitter"}

# ---- search
DEFAULTS = {"lr": 0.2, "ctx_speed": 0.001, "threshold": 0.4}
COLUMNS = ["study_id", "dataset", "cache_pct", "policy", "baseline", "n_train", "n_trials",
           "n_complete", "default_value", "best_value", "gain_pp_of_rel", "best_params",
           "default_forgive_rate", "best_forgive_rate", "train_traces", "status", "seconds"]

# ---- params
OFF_RATIO = 0.2              # best forgive rate below this x default: "mostly off"


def pct_order(p):
    try:
        return -float(p)
    except ValueError:
        return 0.0


# ============================================================================ plan

def plan(args):
    rng = random.Random(SEED)
    ok = set()                 # (ds, trace, pct, policy) with a real forgive result
    requests, stats, pcts = {}, {}, set()
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
                    pcts.add(r["cache_pct"])
    pcts = sorted(pcts, key=pct_order)
    fam = families(stats)

    by_ds = defaultdict(list)
    for ds, name, url in read_list(args.list):
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

        # one candidate per family: most eligible (size, policy) combinations,
        # within the request cap; ties broken at random
        cands = []
        for fid, ms in members.items():
            pool = [n for n in ms if requests.get((ds, n), 0) <= MAX_TRAIN_REQUESTS and elig(n) > 0]
            if pool:
                rng.shuffle(pool)
                pool.sort(key=lambda n: -elig(n))
                cands.append((fid, pool[0]))

        n_fam = len(members)
        k = min(TRAIN_MAX, n_fam // 2, len(cands))
        if n_fam < MIN_FAMILIES or k < MIN_TRAIN:
            for n, _ in traces:
                split_rows.append((ds, n, fam_of[n], "untuned"))
            why = f"only {n_fam} families" if n_fam < MIN_FAMILIES else "too few eligible families"
            print(f"{ds:>24} | {len(traces):>6} {n_fam:>5} {'-':>5} {'-':>5} | not tuned ({why})")
            continue

        rng.shuffle(cands)
        cands.sort(key=lambda c: -elig(c[1]))        # prefer broadly-eligible families
        chosen = cands[:k]
        train = {rep for _, rep in chosen}
        train_fams = {fid for fid, _ in chosen}
        role = {n: "train" if n in train else "excluded" if fam_of[n] in train_fams else "test"
                for n, _ in traces}

        made = []
        for c in pcts:
            for p in POLICIES:
                tr = [n for n in train if (ds, n, c, p) in ok]
                te = [n for n, _ in traces if role[n] == "test" and (ds, n, c, p) in ok]
                if len(tr) < MIN_TRAIN or len(te) < MIN_TEST:
                    continue
                studies.append({
                    "id": f"{ds}|{c}|{p}", "dataset": ds, "pct": c, "frac": float(c) / 100.0,
                    "policy": p, "baseline": BASELINE[p], "trials": args.trials,
                    "seed": rng.randrange(1 << 30), "n_test": len(te),
                    "train": [{"name": n, "url": url_of[n], "requests": requests.get((ds, n), 0)}
                              for n in sorted(tr)],
                })
                made.append(f"{c}:{'LRUF' if p.startswith('lru') else 'S3FF'}")

        if not made:
            for n, _ in traces:
                split_rows.append((ds, n, fam_of[n], "untuned"))
            print(f"{ds:>24} | {len(traces):>6} {n_fam:>5} {'-':>5} {'-':>5} | not tuned "
                  f"(no size with {MIN_TRAIN}+ train and {MIN_TEST}+ test results)")
            continue

        n_test = sum(1 for v in role.values() if v == "test")
        print(f"{ds:>24} | {len(traces):>6} {n_fam:>5} {len(train):>5} {n_test:>5} | {' '.join(made)}")
        for n, u in traces:
            split_rows.append((ds, n, fam_of[n], role[n]))
            if ds not in APPLY_EXCLUDE:
                apply_rows.append((ds, u))
            if role[n] == "test":
                test_rows.append((ds, u))
            elif role[n] == "train":
                train_rows.append((ds, u))

    if not studies:
        sys.exit("no studies could be planned (have the sweeps for these sizes finished?)")

    # balance studies across nodes by estimated cost (longest first)
    for s in studies:
        s["cost"] = s["trials"] * sum(t["requests"] for t in s["train"])
    studies.sort(key=lambda s: -s["cost"])
    bins, load = [[] for _ in range(args.nodes)], [0] * args.nodes
    for s in studies:
        i = load.index(min(load))
        bins[i].append(s)
        load[i] += s["cost"]

    os.makedirs(args.outdir, exist_ok=True)
    for i, b in enumerate(bins):
        with open(os.path.join(args.outdir, f"shard_{i:02d}.txt"), "w", encoding="utf-8", newline="\n") as f:
            for s in b:
                f.write(json.dumps(s) + "\n")
    for name, rows in (("tune_split.tsv", split_rows), ("apply_traces.tsv", apply_rows),
                       ("test_traces.tsv", test_rows), ("train_traces.tsv", train_rows)):
        with open(os.path.join(args.outdir, name), "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write("\t".join(r) + "\n")

    print(f"\n{len(studies)} studies over {args.nodes} nodes; est. cost per node "
          f"(trials x training requests): " + ", ".join(f"{l / 1e9:.1f}G" for l in load))
    print(f"tuned rerun: {len(apply_rows)} traces  |  held-out test: {len(test_rows)}  |  "
          f"train: {len(train_rows)}")
    print(f"wrote {args.outdir}/")


# ============================================================================ run (on a node)

def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def params_str(p, sep=","):
    return f"lr={p['lr']:.6g}{sep}ctx-speed={p['ctx_speed']:.6g}{sep}threshold={p['threshold']:.6g}"


class Runner:
    def __init__(self, cachesim, work, timeout):
        self.cachesim, self.work, self.timeout = cachesim, work, timeout
        self._n = 0
        self._lock = threading.Lock()

    def _tmp(self):
        with self._lock:
            self._n += 1
            return os.path.join(self.work, f".tune_out.{os.getpid()}.{self._n}")

    def run(self, trace, algo, size, eparams=None):
        """(miss, forgive_rate or None, wss (obj, byte) or None) for one cell."""
        out = self._tmp()
        cmd = [self.cachesim, trace, "oracleGeneral", algo, str(size), "--num-thread", "1", "--output", out]
        if eparams:
            cmd += ["--eviction-params", eparams]
        try:
            pr = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, errors="replace")
        finally:
            try:
                os.remove(out)
            except OSError:
                pass
        miss = None
        for line in pr.stdout.splitlines():
            m = LINE_RE.match(line.strip())
            if m:
                miss = float(m.group("miss"))
        if miss is None:
            tail = " | ".join(pr.stderr.strip().splitlines()[-2:])
            raise RuntimeError(f"cachesim rc={pr.returncode} no result for {os.path.basename(trace)} "
                               f"{algo}: {tail}")
        fr = [float(x) for x in FORGIVE_RATE_RE.findall(pr.stderr)]
        wss = None
        for w in WSS_RE.finditer(pr.stderr):
            wss = (int(w.group(3)), int(w.group(4)))
        return miss, (fr[-1] if fr else None), wss


def local_name(dataset, t):
    """oracleGeneral traces stay .zst (cachesim reads them directly); Wikimedia
    TSVs are converted once and kept as plain oracleGeneral."""
    if needs_conversion(t["url"]):
        return f"{dataset}__{t['name']}.oracleGeneral"
    return f"{dataset}__{t['name']}.oracleGeneral.zst"


def download(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    tmp = dest + ".part"
    try:
        r = subprocess.run(["wget", "-q", "-O", tmp, url])
        if r.returncode != 0 or not os.path.getsize(tmp):
            raise RuntimeError(f"download failed: {url}")
        if needs_conversion(url):
            worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "node_worker.sh")
            c = subprocess.run(["bash", worker, "--convert", tmp, dest], capture_output=True, text=True)
            if c.returncode != 0 or not os.path.exists(dest):
                raise RuntimeError(f"conversion failed: {url}: {c.stderr.strip()[-200:]}")
        else:
            os.replace(tmp, dest)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return dest


def run_study(st, runner, cache_dir, base_cache, cfg, trials_log):
    t0 = time.time()
    frac, algo, base = st["frac"], st["policy"], st["baseline"]
    row = {"study_id": st["id"], "dataset": st["dataset"], "cache_pct": st["pct"],
           "policy": algo, "baseline": base, "n_trials": 0, "n_complete": 0,
           "default_value": "", "best_value": "", "gain_pp_of_rel": "", "best_params": "",
           "default_forgive_rate": "", "best_forgive_rate": "", "status": "", "seconds": ""}

    # training traces: download + baseline (+ working set) once each
    train = []
    for t in st["train"]:
        path = os.path.join(cache_dir, local_name(st["dataset"], t))
        try:
            download(t["url"], path)
            key = f"{st['dataset']}|{t['name']}|{frac}|{base}"
            if key not in base_cache:
                # fractional size (needs a '.', or cachesim reads it as bytes)
                bm, _, wss = runner.run(path, base, f"{frac:.10f}".rstrip("0"))
                if not wss:
                    raise RuntimeError("no working-set line")
                base_cache[key] = {"miss": bm, "wss_obj": wss[0], "wss_byte": wss[1]}
            b = base_cache[key]
        except Exception as e:
            log(f"  [{st['id']}] drop {t['name']}: {e}")
            continue
        cap_objs = int(frac * b["wss_obj"])
        if cap_objs < cfg["min_cache_objs"]:
            log(f"  [{st['id']}] drop {t['name']}: cache holds ~{cap_objs} objects < {cfg['min_cache_objs']}")
            continue
        train.append({"name": t["name"], "path": path, "base": b["miss"],
                      "size": max(1, int(b["wss_byte"] * frac)),
                      "cap": max(1, int(cfg["emb_mult"] * frac * b["wss_obj"]))})
    row["n_train"] = len(train)
    row["train_traces"] = ";".join(t["name"] for t in train)
    if len(train) < 2:
        row["status"] = "SKIPPED_TOO_FEW_TRACES"
        row["seconds"] = f"{time.time() - t0:.0f}"
        return row
    log(f"[{st['id']}] {len(train)} training traces, {st['trials']} trials, "
        f"{cfg['par_trials']}x{cfg['par_traces']} parallel")

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=cfg["par_trials"] * cfg["par_traces"])

    def evaluate(p):
        ep = params_str(p)
        futs = [pool.submit(runner.run, t["path"], algo, t["size"], f"{ep},max-emb-entries={t['cap']}")
                for t in train]
        per, rates = [], []
        for t, f in zip(train, futs):
            miss, fr, _ = f.result()
            per.append((t["base"] - miss) / t["base"] * 100 if t["base"] > 0 else 0.0)
            if fr is not None:
                rates.append(fr)
        return sum(per) / len(per), per, (sum(rates) / len(rates) if rates else None)

    import optuna

    def objective(trial):
        p = {"lr": trial.suggest_float("lr", 0.01, 0.5, log=True),
             "ctx_speed": trial.suggest_float("ctx_speed", 1e-7, 1e-3, log=True),
             "threshold": trial.suggest_float("threshold", 0.3, 0.95)}
        val, per, fr = evaluate(p)
        trial.set_user_attr("per_trace", per)
        trial.set_user_attr("forgive_rate", fr)
        return val

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="maximize",
                                sampler=optuna.samplers.TPESampler(seed=st.get("seed", 1)))
    study.enqueue_trial(DEFAULTS)
    study.optimize(objective, n_trials=st["trials"], n_jobs=cfg["par_trials"],
                   catch=(RuntimeError, subprocess.TimeoutExpired))
    pool.shutdown(wait=True)

    complete = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
    row["n_trials"], row["n_complete"] = len(study.trials), len(complete)
    with open(trials_log, "a", encoding="utf-8") as f:
        for t in study.trials:
            f.write(json.dumps({"study_id": st["id"], "number": t.number, "state": t.state.name,
                                "params": t.params, "value": t.value,
                                "forgive_rate": t.user_attrs.get("forgive_rate"),
                                "per_trace": t.user_attrs.get("per_trace")}) + "\n")
    if not complete:
        row["status"] = "FAILED_ALL_TRIALS"
        row["seconds"] = f"{time.time() - t0:.0f}"
        return row

    def is_default(t):
        return all(abs(t.params.get(k, -1) - v) <= 1e-9 * max(1, abs(v)) for k, v in DEFAULTS.items())
    dflt = next((t for t in complete if is_default(t)), None)
    best = study.best_trial
    rate = lambda t: "" if t.user_attrs.get("forgive_rate") is None else f"{t.user_attrs['forgive_rate']:.4f}"
    row["best_value"] = f"{best.value:.6f}"
    row["best_params"] = params_str(best.params, sep=";")
    row["best_forgive_rate"] = rate(best)
    if dflt is not None:
        row["default_value"] = f"{dflt.value:.6f}"
        row["gain_pp_of_rel"] = f"{best.value - dflt.value:.6f}"
        row["default_forgive_rate"] = rate(dflt)
    row["status"] = "OK" if dflt is not None else "OK_NO_DEFAULT_TRIAL"
    row["seconds"] = f"{time.time() - t0:.0f}"
    log(f"[{st['id']}] default {row['default_value'] or '?'}  best {row['best_value']}  "
        f"({row['best_params']})  {row['seconds']}s")
    return row


def run(args):
    cfg = {"emb_mult": float(os.environ.get("EMB_MULT", 5)),
           "min_cache_objs": int(os.environ.get("MIN_CACHE_OBJS", 1000)),
           "par_trials": int(os.environ.get("TUNE_PAR_TRIALS", 4)),
           "par_traces": int(os.environ.get("TUNE_PAR_TRACES", 4))}
    timeout = int(os.environ.get("CELL_TIMEOUT", 7200))
    log(f"parallel: {cfg['par_trials']} trials x {cfg['par_traces']} traces")

    os.makedirs(args.work, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    cache_dir = os.path.join(args.work, "traces")
    os.makedirs(cache_dir, exist_ok=True)
    trials_log = args.out + ".trials.jsonl"
    base_path = os.path.join(args.work, "baseline_cache.json")
    base_cache = {}
    if os.path.exists(base_path):
        with open(base_path) as f:
            base_cache = json.load(f)

    with open(args.shard, encoding="utf-8") as f:
        studies = [json.loads(l) for l in f if l.strip()]
    finished = set()
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8", newline="") as f:
            finished = {r["study_id"] for r in csv.DictReader(f)}
    runner = Runner(args.cachesim, args.work, timeout)
    log(f"{len(studies)} studies in shard, {len(finished & {s['id'] for s in studies})} already done")

    for i, st in enumerate(studies):
        if st["id"] in finished:
            log(f"SKIP {st['id']} (done)")
            continue
        try:
            row = run_study(st, runner, cache_dir, base_cache, cfg, trials_log)
        except Exception as e:                      # never let one study kill the shard
            log(f"[{st['id']}] ERROR {e}")
            row = {"study_id": st["id"], "dataset": st["dataset"], "cache_pct": st["pct"],
                   "policy": st["policy"], "baseline": st["baseline"], "status": f"ERROR {e}"[:200]}
        new = not os.path.exists(args.out)
        with open(args.out, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            if new:
                w.writeheader()
            w.writerow(row)
        with open(base_path, "w") as f:
            json.dump(base_cache, f)
        # free disk: drop traces no later study in this shard needs
        needed = {local_name(s["dataset"], t) for s in studies[i + 1:] for t in s["train"]}
        for fn in os.listdir(cache_dir):
            if fn not in needed:
                try:
                    os.remove(os.path.join(cache_dir, fn))
                except OSError:
                    pass
    log(f"ALL DONE -> {args.out}")


# ============================================================================ params

def params(args):
    """Tuned parameters per study, and a readable summary. A study keeps the
    defaults (no entry) if it did not finish OK or the best trial did not beat
    the defaults on its training traces. "mostly off" flags studies whose best
    trial forgives far less often than the defaults did: the gain there comes
    from switching forgiveness off, and should be reported that way."""
    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    rows = {}
    for path in args.csv:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("study_id") and r["study_id"] != "study_id":
                    rows[r["study_id"]] = r

    table, kept_default = {}, 0
    print(f"{'study':>52} | {'n':>2} {'default':>8} {'best':>8} {'gain':>7} | "
          f"{'fr dflt':>7} {'fr best':>7} | params")
    for sid in sorted(rows):
        r = rows[sid]
        d, b, g = num(r.get("default_value")), num(r.get("best_value")), num(r.get("gain_pp_of_rel"))
        fd, fb = num(r.get("default_forgive_rate")), num(r.get("best_forgive_rate"))
        status = r.get("status", "")
        note = ""
        use = status.startswith("OK") and b is not None and r.get("best_params") and (g is None or g > 0)
        if use:
            table[f"{r['dataset']}|{r['cache_pct']}|{r['policy']}"] = r["best_params"].replace(";", ",")
            if fd is not None and fb is not None and fd > 0 and fb < OFF_RATIO * fd:
                note = "  <- mostly off"
        else:
            kept_default += 1
            note = (f"  (default kept: {status or 'no result'})" if not status.startswith("OK")
                    else "  (default kept: no gain)")
        fmt = lambda x, w: f"{x:>+{w}.3f}" if x is not None else f"{'--':>{w}}"
        fr = lambda x: f"{x:>6.2f}%" if x is not None else f"{'--':>7}"
        print(f"{sid:>52} | {r.get('n_train') or '-':>2} {fmt(d, 8)} {fmt(b, 8)} {fmt(g, 7)} | "
              f"{fr(fd)} {fr(fb)} | {r.get('best_params', '') if use else ''}{note}")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(table, fh, indent=1, sort_keys=True)
    print(f"\n{len(table)} tuned entries -> {args.out}  ({kept_default} studies kept defaults)")
    print("values are mean relative miss-ratio reduction (%) on TRAINING traces; "
          "judge tuning on held-out traces with analyze.py --tuned ... --only test_traces.tsv")


# ============================================================================ main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="choose studies and split them across nodes")
    p.add_argument("csv", nargs="+", help="sweep results (default parameters)")
    p.add_argument("--list", required=True, help="trace list the sweep ran on")
    p.add_argument("--nodes", type=int, default=8)
    p.add_argument("--trials", type=int, default=100)
    p.add_argument("--outdir", default="tune_plan")

    r = sub.add_parser("run", help="run a shard of studies (on a node; started by node_worker.sh)")
    r.add_argument("--shard", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--work", default=os.environ.get("WORK", "/mydata/tune_work"))
    r.add_argument("--cachesim", required=True)

    q = sub.add_parser("params", help="studies CSV -> tuned_params.json")
    q.add_argument("csv", nargs="+")
    q.add_argument("--out", default="tuned_params.json")

    args = ap.parse_args()
    {"plan": plan, "run": run, "params": params}[args.cmd](args)


if __name__ == "__main__":
    main()
