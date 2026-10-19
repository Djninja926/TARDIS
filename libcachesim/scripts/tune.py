#!/usr/bin/env python3
"""
tune.py - hyperparameter tuning for the forgive policies, in three steps.

  python tune.py plan RESULTS.csv... --list TRACES.tsv     (your machine)
      Choose the studies, their training traces and the held-out test traces,
      and split the studies across nodes. Writes tune_plan/.
  orchestrate.py --launch --run tune --plan tune_plan      (runs `tune.py run` on every node)
  python tune.py params STUDIES.csv                        (your machine)
      Turn the finished studies into tuned_params.json for the tuned sweep.

One STUDY = one (workload, cache size, forgive policy), as in the paper: the
workloads are common.GROUPS (Wiki Upload, Tencent Block, ...; a dataset outside
the list is its own workload). Optuna (TPE) searches
    lr         0.01 - 0.5    (log)
    ctx-speed  1e-7 - 1e-3   (log)
    threshold  0.3  - 0.95
to maximize the mean relative miss-ratio reduction over the policy's own
baseline on the study's training traces. The defaults (lr=0.2, ctx-speed=0.001,
threshold=0.4) are always tried first, so the best result is never worse than
the defaults on the training traces.

TRAINING TRACES. Per workload, a random third of its traces, at most TRAIN_MAX
(16); the rest are held out. The paper used a third; workloads with 48+ traces
are capped at 16 so a trial (which replays every training trace) stays
affordable. Traces with results at every size and policy are preferred, so the
same training set serves every study of the workload. A trace that appears in
several datasets of one workload (Twitter samples, block volumes in several
subfolders) counts once, as the copy with the most requests (same rule as the
analysis); the other copies are "variant" and neither train nor test.

NEAR COPIES. Some block workloads hold many near-identical volumes. A held-out
trace in the same family (common.families) as a training trace is marked
near_copy; test_far_traces.tsv leaves those out, so held-out results can be
reported with and without them.

Plan outputs (tune_plan/):
  shard_NN.txt         one JSON study per line, one file per node
  tune_split.tsv       workload, dataset, trace, family, role (train / test /
                       variant / untuned), near_copy (1 = held-out copy of a training trace)
  apply_traces.tsv     traces to rerun with tuned parameters (every trace of a tuned workload)
  test_traces.tsv      held-out traces (judge tuning on these)
  test_far_traces.tsv  held-out traces that are not near copies
  train_traces.tsv     training traces
"""
import argparse, concurrent.futures, csv, json, os, random, subprocess, sys, threading, time
from collections import defaultdict

from common import (BASELINE, FORGIVE_RATE_RE, LINE_RE, POLICIES, WSS_RE,
                    families, group_of, load_trace_stats, needs_conversion, read_list)

# ---- planning choices
TRAIN_MAX = 16               # training traces per workload (a third below 48 traces)
MIN_TRAIN, MIN_TEST = 1, 1   # per (size, policy) study
SEED = 1

# ---- search
DEFAULTS = {"lr": 0.2, "ctx_speed": 0.001, "threshold": 0.4}
COLUMNS = ["study_id", "dataset", "datasets", "cache_pct", "policy", "baseline", "n_train",
           "n_trials", "n_complete", "default_value", "best_value", "gain_pp_of_rel",
           "best_params", "default_forgive_rate", "best_forgive_rate", "train_traces",
           "status", "seconds"]

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

    # workload -> trace name -> (dataset, url): one copy per name (most requests)
    by_wl, variants, datasets = defaultdict(dict), defaultdict(list), defaultdict(set)
    for ds, name, url in read_list(args.list):
        wl = group_of(ds, name) or ds
        datasets[wl].add(ds)
        cur = by_wl[wl].get(name)
        if cur is not None and requests.get((ds, name), 0) <= requests.get((cur[0], name), 0):
            variants[wl].append((ds, name, url))
            continue
        if cur is not None:
            variants[wl].append((cur[0], name, cur[1]))
        by_wl[wl][name] = (ds, url)

    # families within each workload, for the near-copy check
    fam = families({(wl, f"{ds}/{n}"): stats[(ds, n)]
                    for wl, tr in by_wl.items() for n, (ds, _) in tr.items() if (ds, n) in stats})

    studies, split_rows = [], []
    apply_rows, test_rows, far_rows, train_rows = [], [], [], []
    print(f"{'workload':>24} | {'traces':>6} {'train':>5} {'test':>5} {'near':>5} | studies (size:policy)")

    for wl in sorted(by_wl, key=lambda w: -len(by_wl[w])):
        traces = sorted(by_wl[wl].items())
        fam_of = {n: fam.get((wl, f"{ds}/{n}"), f"{wl}#solo:{ds}/{n}") for n, (ds, _) in traces}

        def elig(n):
            ds = by_wl[wl][n][0]
            return sum(1 for c in pcts for p in POLICIES if (ds, n, c, p) in ok)

        def write_untuned(why):
            for n, (ds, _) in traces:
                split_rows.append((wl, ds, n, fam_of[n], "untuned", ""))
            for ds, n, _ in variants[wl]:
                split_rows.append((wl, ds, n, "", "untuned", ""))
            print(f"{wl:>24} | {len(traces):>6} {'-':>5} {'-':>5} {'-':>5} | not tuned ({why})")

        pool = [n for n, _ in traces if elig(n) > 0]
        k = min(TRAIN_MAX, round(len(pool) / 3))
        if k < MIN_TRAIN or len(pool) - k < MIN_TEST:
            write_untuned(f"{len(pool)} traces with results")
            continue
        rng.shuffle(pool)
        pool.sort(key=lambda n: -elig(n))     # stable: random among the most eligible
        train = set(pool[:k])
        train_fams = {fam_of[n] for n in train}
        role = {n: "train" if n in train else "test" for n, _ in traces}
        near = {n: role[n] == "test" and fam_of[n] in train_fams for n, _ in traces}

        made = []
        for c in pcts:
            for p in POLICIES:
                tr = [n for n in sorted(train) if (by_wl[wl][n][0], n, c, p) in ok]
                te = [n for n, (ds, _) in traces if role[n] == "test" and (ds, n, c, p) in ok]
                if len(tr) < MIN_TRAIN or len(te) < MIN_TEST:
                    continue
                studies.append({
                    "id": f"{wl}|{c}|{p}", "dataset": wl, "datasets": sorted(datasets[wl]),
                    "pct": c, "frac": float(c) / 100.0,
                    "policy": p, "baseline": BASELINE[p], "trials": args.trials,
                    "seed": rng.randrange(1 << 30), "n_test": len(te),
                    "train": [{"name": n, "dataset": by_wl[wl][n][0], "url": by_wl[wl][n][1],
                               "requests": requests.get((by_wl[wl][n][0], n), 0)} for n in tr],
                })
                made.append(f"{c}:{'LRUF' if p.startswith('lru') else 'S3FF'}")
        if not made:
            write_untuned(f"no size with {MIN_TRAIN}+ train and {MIN_TEST}+ test results")
            continue

        n_test = sum(1 for v in role.values() if v == "test")
        n_near = sum(near.values())
        print(f"{wl:>24} | {len(traces):>6} {len(train):>5} {n_test:>5} {n_near:>5} | {' '.join(made)}")
        for n, (ds, u) in traces:
            split_rows.append((wl, ds, n, fam_of[n], role[n], "1" if near[n] else ""))
            apply_rows.append((ds, u))
            if role[n] == "test":
                test_rows.append((ds, u))
                if not near[n]:
                    far_rows.append((ds, u))
            else:
                train_rows.append((ds, u))
        for ds, n, u in variants[wl]:
            split_rows.append((wl, ds, n, "", "variant", ""))
            apply_rows.append((ds, u))

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
                       ("test_traces.tsv", test_rows), ("test_far_traces.tsv", far_rows),
                       ("train_traces.tsv", train_rows)):
        with open(os.path.join(args.outdir, name), "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write("\t".join(r) + "\n")

    print(f"\n{len(studies)} studies over {args.nodes} nodes; est. cost per node "
          f"(trials x training requests): " + ", ".join(f"{l / 1e9:.1f}G" for l in load))
    print(f"tuned rerun: {len(apply_rows)} traces  |  held-out test: {len(test_rows)} "
          f"({len(far_rows)} not near copies)  |  train: {len(train_rows)}")
    print(f"wrote {args.outdir}/")


# ============================================================================ run (on a node)

def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def params_str(p, sep=","):
    return f"lr={p['lr']:.6g}{sep}ctx-speed={p['ctx_speed']:.6g}{sep}threshold={p['threshold']:.6g}"


# MEMORY. A trial runs up to TUNE_PAR_TRIALS x TUNE_PAR_TRACES simulations at
# once, and with no request cap some training traces are huge (a 10% sketch
# table for a 500M-object trace is ~130 GB). Each forgive run reserves its
# estimated memory from a node-wide budget (TUNE_MEM_FRACTION of RAM, 0.8;
# use about 0.6 when a sweep runs on the same node) and waits until it fits;
# a run larger than the whole budget runs alone.
EMB_ENTRY_BYTES = 520        # per sketch-table entry
OBJ_BYTES = 100              # per unique object: the access counter (never freed) + cache metadata


def mem_total():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 1 << 50


class MemBudget:
    """Bytes reserved first come, first served (a big run is never starved)."""
    def __init__(self, total):
        self.total = self.free = total
        self.cv = threading.Condition()
        self.next_ticket = self.serving = 0

    def acquire(self, n):
        n = min(n, self.total)
        with self.cv:
            me = self.next_ticket
            self.next_ticket += 1
            self.cv.wait_for(lambda: self.serving == me and self.free >= n)
            self.free -= n
            self.serving += 1
            self.cv.notify_all()
        return n

    def release(self, n):
        with self.cv:
            self.free += n
            self.cv.notify_all()


class Runner:
    def __init__(self, cachesim, work, timeout, ignore_size=False):
        self.cachesim, self.work, self.timeout = cachesim, work, timeout
        self.ignore_size = ignore_size      # IGNORE_OBJ_SIZE=1: sizes are in objects
        self.budget = MemBudget(int(float(os.environ.get("TUNE_MEM_FRACTION", 0.8)) * mem_total()))
        self._n = 0
        self._lock = threading.Lock()

    def _tmp(self):
        with self._lock:
            self._n += 1
            return os.path.join(self.work, f".tune_out.{os.getpid()}.{self._n}")

    def run(self, trace, algo, size, eparams=None, mem=0):
        """(miss, forgive_rate or None, wss (obj, byte) or None) for one cell.
        mem: estimated bytes this run needs (waits for room in the budget)."""
        held = self.budget.acquire(mem)
        try:
            return self._run(trace, algo, size, eparams)
        finally:
            self.budget.release(held)

    def _run(self, trace, algo, size, eparams):
        out = self._tmp()
        cmd = [self.cachesim, trace, "oracleGeneral", algo, str(size), "--num-thread", "1", "--output", out]
        if eparams:
            cmd += ["--eviction-params", eparams]
        if self.ignore_size:
            cmd += ["--ignore-obj-size", "1"]
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
    datasets = ";".join(st.get("datasets") or [st["dataset"]])
    row = {"study_id": st["id"], "dataset": st["dataset"], "datasets": datasets, "cache_pct": st["pct"],
           "policy": algo, "baseline": base, "n_trials": 0, "n_complete": 0,
           "default_value": "", "best_value": "", "gain_pp_of_rel": "", "best_params": "",
           "default_forgive_rate": "", "best_forgive_rate": "", "status": "", "seconds": ""}

    # training traces: download + baseline (+ working set) once each
    train = []
    for t in st["train"]:
        ds = t.get("dataset", st["dataset"])
        path = os.path.join(cache_dir, local_name(ds, t))
        try:
            download(t["url"], path)
            key = f"{ds}|{t['name']}|{frac}|{base}|{'obj' if runner.ignore_size else 'byte'}"
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
        cap = max(1, int(cfg["emb_mult"] * frac * b["wss_obj"]))
        train.append({"name": t["name"], "path": path, "base": b["miss"],
                      "size": max(1, int(b["wss_byte"] * frac)), "cap": cap,
                      "mem": cap * EMB_ENTRY_BYTES + b["wss_obj"] * OBJ_BYTES})
    row["n_train"] = len(train)
    row["train_traces"] = ";".join(t["name"] for t in train)
    if len(train) < MIN_TRAIN:
        row["status"] = "SKIPPED_TOO_FEW_TRACES"
        row["seconds"] = f"{time.time() - t0:.0f}"
        return row
    log(f"[{st['id']}] {len(train)} training traces, {st['trials']} trials, "
        f"{cfg['par_trials']}x{cfg['par_traces']} parallel")

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=cfg["par_trials"] * cfg["par_traces"])

    def evaluate(p):
        ep = params_str(p)
        futs = [pool.submit(runner.run, t["path"], algo, t["size"],
                            f"{ep},max-emb-entries={t['cap']}", t["mem"]) for t in train]
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
    ignore = os.environ.get("IGNORE_OBJ_SIZE", "0") == "1"
    runner = Runner(args.cachesim, args.work, timeout, ignore)
    log("object sizes " + ("ignored: sizes are fractions of unique objects" if ignore
                           else "kept: sizes are fractions of the working set in bytes"))
    log(f"{len(studies)} studies in shard, {len(finished & {s['id'] for s in studies})} already done")

    for i, st in enumerate(studies):
        if st["id"] in finished:
            log(f"SKIP {st['id']} (done)")
            continue
        try:
            row = run_study(st, runner, cache_dir, base_cache, cfg, trials_log)
        except Exception as e:                      # never let one study kill the shard
            log(f"[{st['id']}] ERROR {e}")
            row = {"study_id": st["id"], "dataset": st["dataset"],
                   "datasets": ";".join(st.get("datasets") or [st["dataset"]]), "cache_pct": st["pct"],
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
        needed = {local_name(t.get("dataset", s["dataset"]), t) for s in studies[i + 1:] for t in s["train"]}
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
            # a workload's parameters apply to every dataset in it
            for ds in (r.get("datasets") or r["dataset"]).split(";"):
                table[f"{ds}|{r['cache_pct']}|{r['policy']}"] = r["best_params"].replace(";", ",")
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
    print(f"\n{len(table)} tuned entries (dataset|size|policy) -> {args.out}  "
          f"({kept_default} studies kept defaults)")
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
    p.add_argument("--trials", type=int, default=125)
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
