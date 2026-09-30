#!/usr/bin/env python3
"""
tune_study.py - run hyperparameter-tuning studies on one node (Optuna).

Each line of the shard is one study from make_tuning_plan.py: a dataset, a
cache size, a forgive policy and its training traces. For each study:

  1. Download the training traces (kept COMPRESSED: libCacheSim reads .zst
     directly with identical results, so a study's traces fit on disk easily).
  2. Run the policy's baseline once per trace at the study's size. That gives
     the baseline miss ratio and, from cachesim's working-set log line, the
     object count used for the embedding cap.
  3. Optuna (TPE) searches Aryan's three parameters with his ranges:
         lr         0.01 - 0.5    (log)
         ctx-speed  1e-7 - 1e-3   (log)
         threshold  0.3  - 0.95
     maximizing the MEAN relative miss-ratio reduction over the baseline across
     the training traces (Aryan's objective). The defaults (lr=0.2,
     ctx-speed=0.001, threshold=0.4) are always evaluated first, so the best
     result is never worse than the defaults on the training traces.

EMBEDDING CAP. Every run passes max-emb-entries = EMB_MULT x frac x wss_obj
(5x the main cache's capacity in objects), as an absolute integer. Aryan's own
tuners pass "Nx", which the code multiplies by cache size in BYTES, making the
cap effectively unbounded; that is not reused here.

Output: one CSV row per study (resumable: finished studies are skipped), plus
<out>.trials.jsonl with every trial. The forgive rate of the default and best
trials is recorded, because a tuned result that "improves" by pushing the
threshold so high that forgiveness almost never fires is really the feature
being switched off, and should be reported as such.

Usage (normally via node_tune_worker.sh / orchestrate.py --worker node_tune_worker.sh):
  python3 tune_study.py --shard shard.txt --out results/tune.csv --work /mydata/tune_work \\
      --cachesim /mydata/cachesim/_build/bin/cachesim
"""
import argparse, concurrent.futures, csv, json, os, re, subprocess, threading, time

import optuna

from parse_cachesim import LINE_RE, WSS_RE
from trace_names import needs_conversion

DEFAULTS = {"lr": 0.2, "ctx_speed": 0.001, "threshold": 0.4}
FORGIVE_RATE_RE = re.compile(r"(?:forgives|forgive_small)=\d+\(([\d.]+)%\)")
COLUMNS = ["study_id", "dataset", "cache_pct", "policy", "baseline", "n_train", "n_trials",
           "n_complete", "default_value", "best_value", "gain_pp_of_rel", "best_params",
           "default_forgive_rate", "best_forgive_rate", "train_traces", "status", "seconds"]


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def params_str(p, sep=","):
    return (f"lr={p['lr']:.6g}{sep}ctx-speed={p['ctx_speed']:.6g}"
            f"{sep}threshold={p['threshold']:.6g}")


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
        cmd = [self.cachesim, trace, "oracleGeneral", algo, str(size),
               "--num-thread", "1", "--output", out]
        if eparams:
            cmd += ["--eviction-params", eparams]
        try:
            pr = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout,
                                errors="replace")
        finally:
            for f in (out,):
                try:
                    os.remove(f)
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
    """Cached file name for a training trace. oracleGeneral traces stay .zst
    (cachesim reads them directly); Wikimedia TSVs are converted once and kept
    as uncompressed oracleGeneral."""
    if needs_conversion(t["url"]):
        return f"{dataset}__{t['name']}.oracleGeneral"
    return f"{dataset}__{t['name']}.oracleGeneral.zst"


def download(url, dest, scripts_dir):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    tmp = dest + ".part"
    try:
        r = subprocess.run(["wget", "-q", "-O", tmp, url])
        if r.returncode != 0 or not os.path.getsize(tmp):
            raise RuntimeError(f"download failed: {url}")
        if needs_conversion(url):
            c = subprocess.run(["bash", os.path.join(scripts_dir, "convert_wikimedia.sh"), tmp, dest],
                               capture_output=True, text=True)
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


def done_ids(out):
    if not os.path.exists(out):
        return set()
    with open(out, encoding="utf-8", newline="") as f:
        return {r["study_id"] for r in csv.DictReader(f)}


def append_row(out, row):
    new = not os.path.exists(out)
    with open(out, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        w.writerow(row)


def run_study(st, runner, cache_dir, base_cache, args, trials_log):
    t0 = time.time()
    frac, algo, base = st["frac"], st["policy"], st["baseline"]
    row = {"study_id": st["id"], "dataset": st["dataset"], "cache_pct": st["pct"],
           "policy": algo, "baseline": base, "n_trials": 0, "n_complete": 0,
           "default_value": "", "best_value": "", "gain_pp_of_rel": "", "best_params": "",
           "default_forgive_rate": "", "best_forgive_rate": "", "status": "", "seconds": ""}

    # ---- training traces: download + baseline (+ working set) once each
    train = []
    for t in st["train"]:
        path = os.path.join(cache_dir, local_name(st["dataset"], t))
        try:
            download(t["url"], path, args.scripts)
            key = f"{st['dataset']}|{t['name']}|{frac}|{base}"
            if key not in base_cache:
                # fractional size (must contain a '.', or cachesim reads it as bytes:
                # str(1e-05) has none) -> cachesim logs the working set
                bm, _, wss = runner.run(path, base, f"{frac:.10f}".rstrip("0"))
                if not wss:
                    raise RuntimeError("no working-set line")
                base_cache[key] = {"miss": bm, "wss_obj": wss[0], "wss_byte": wss[1]}
            b = base_cache[key]
        except Exception as e:
            log(f"  [{st['id']}] drop {t['name']}: {e}")
            continue
        cap_objs = int(frac * b["wss_obj"])
        if cap_objs < args.min_cache_objs:
            log(f"  [{st['id']}] drop {t['name']}: cache holds ~{cap_objs} objects < {args.min_cache_objs}")
            continue
        train.append({"name": t["name"], "path": path, "base": b["miss"],
                      "size": max(1, int(b["wss_byte"] * frac)),
                      "cap": max(1, int(args.emb_mult * frac * b["wss_obj"]))})
    row["n_train"] = len(train)
    row["train_traces"] = ";".join(t["name"] for t in train)
    if len(train) < 2:
        row["status"] = "SKIPPED_TOO_FEW_TRACES"
        row["seconds"] = f"{time.time() - t0:.0f}"
        return row
    log(f"[{st['id']}] {len(train)} training traces, {st['trials']} trials, "
        f"{args.par_trials}x{args.par_traces} parallel")

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.par_trials * args.par_traces)

    def evaluate(p):
        ep = params_str(p)
        futs = [pool.submit(runner.run, t["path"], algo, t["size"],
                            f"{ep},max-emb-entries={t['cap']}") for t in train]
        per, rates = [], []
        for t, f in zip(train, futs):
            miss, fr, _ = f.result()
            per.append((t["base"] - miss) / t["base"] * 100 if t["base"] > 0 else 0.0)
            if fr is not None:
                rates.append(fr)
        return sum(per) / len(per), per, (sum(rates) / len(rates) if rates else None)

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
    study.optimize(objective, n_trials=st["trials"], n_jobs=args.par_trials,
                   catch=(RuntimeError, subprocess.TimeoutExpired))
    pool.shutdown(wait=True)

    complete = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
    row["n_trials"] = len(study.trials)
    row["n_complete"] = len(complete)
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
    row["best_value"] = f"{best.value:.6f}"
    row["best_params"] = params_str(best.params, sep=";")
    row["best_forgive_rate"] = ("" if best.user_attrs.get("forgive_rate") is None
                                else f"{best.user_attrs['forgive_rate']:.4f}")
    if dflt is not None:
        row["default_value"] = f"{dflt.value:.6f}"
        row["gain_pp_of_rel"] = f"{best.value - dflt.value:.6f}"
        row["default_forgive_rate"] = ("" if dflt.user_attrs.get("forgive_rate") is None
                                       else f"{dflt.user_attrs['forgive_rate']:.4f}")
    row["status"] = "OK" if dflt is not None else "OK_NO_DEFAULT_TRIAL"
    row["seconds"] = f"{time.time() - t0:.0f}"
    log(f"[{st['id']}] default {row['default_value'] or '?'}  best {row['best_value']}  "
        f"({row['best_params']})  {row['seconds']}s")
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--work", default=os.environ.get("WORK", "/mydata/tune_work"))
    ap.add_argument("--cachesim", required=True)
    ap.add_argument("--scripts", default=os.path.dirname(os.path.abspath(__file__)),
                    help="setup-scripts dir (for convert_wikimedia.sh)")
    ap.add_argument("--emb-mult", type=float, default=float(os.environ.get("EMB_MULT", 5)))
    ap.add_argument("--min-cache-objs", type=int, default=int(os.environ.get("MIN_CACHE_OBJS", 1000)))
    ap.add_argument("--par-trials", type=int, default=int(os.environ.get("TUNE_PAR_TRIALS", 4)),
                    help="trials evaluated at once")
    ap.add_argument("--par-traces", type=int, default=int(os.environ.get("TUNE_PAR_TRACES", 4)),
                    help="training traces run at once within a trial")
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("CELL_TIMEOUT", 7200)))
    args = ap.parse_args()

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
    finished = done_ids(args.out)
    runner = Runner(args.cachesim, args.work, args.timeout)
    log(f"{len(studies)} studies in shard, {len(finished & {s['id'] for s in studies})} already done")

    for i, st in enumerate(studies):
        if st["id"] in finished:
            log(f"SKIP {st['id']} (done)")
            continue
        try:
            row = run_study(st, runner, cache_dir, base_cache, args, trials_log)
        except Exception as e:                      # never let one study kill the shard
            log(f"[{st['id']}] ERROR {e}")
            row = {"study_id": st["id"], "dataset": st["dataset"], "cache_pct": st["pct"],
                   "policy": st["policy"], "baseline": st["baseline"], "status": f"ERROR {e}"[:200]}
        append_row(args.out, row)
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


if __name__ == "__main__":
    main()
