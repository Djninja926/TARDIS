#!/usr/bin/env python3
"""
orchestrate.py - drive multi-node sweeps across CloudLab nodes.

Modeled on the Genet parallel-SSH pattern (paramiko + ThreadPoolExecutor).

  --setup      build on every node (node_setup_libcachesim.sh; pulls scripts first)
  --launch     ship shard_i to node i and start the worker in a detached tmux
  --status     rows so far, RUNNING/DONE, last log line
  --collect    pull each node's result CSV (+ diagnostics log) and merge

NAMED RUNS (--run NAME) let several jobs share the nodes at once, e.g. the
rerun of unfinished traces, the 1% sweep and the tuning studies. Each run gets
its own shard file, output CSV, diagnostics log, worker log, scratch directory
and tmux session, so runs never overwrite each other:

    /mydata/tardis/shards/NAME.txt            shard shipped by --launch
    /mydata/tardis/results/NAME.csv           results (what --collect pulls)
    /mydata/tardis/results/NAME_diag.log      forgive diagnostics
    /mydata/tardis/results/NAME.log           worker stdout/stderr
    /mydata/trace_work_NAME/                  scratch (one trace at a time)
    tmux session: run_NAME

Without --run the original single-run paths are used (shard.txt, node_out.csv,
forgive_diag.log, worker.log, tmux "tracesweep"), so nothing already running
is affected.

  --env K=V    passed to the worker (repeatable), e.g. --env SIZES=0.01 --env PCTS=1
  --worker F   worker script in setup-scripts/ (default node_cachesim_worker.sh)
  --ship F     copy a local file to /mydata/tardis/ship/<basename> on every node
               before launching (repeatable), e.g. a tuned-params JSON

Config: nodes.yaml
  username: ApiaO
  key_path: ~/.ssh/id_rsa            # optional; else uses agent/default
  repo: https://github.com/Djninja926/tardis.git
  nodes:
    - host: node0.cluster.cloudlab.us
    ...

Examples:
  python orchestrate.py --config nodes8.yaml --setup
  python orchestrate.py --config nodes8.yaml --shards rerun_shards --launch --run rerun
  python orchestrate.py --config nodes8.yaml --shards p1_shards --launch --run p1 ^
      --env SIZES=0.01 --env PCTS=1
  python orchestrate.py --config nodes8.yaml --status --run p1
  python orchestrate.py --config nodes8.yaml --collect --run p1 --outdir p1_results
"""
import argparse, os, re, shlex, sys, concurrent.futures
import yaml
import paramiko

REMOTE_TARDIS = "/mydata/tardis"
SCRIPTS_DIR   = f"{REMOTE_TARDIS}/scripts-repo/setup-scripts"
SETUP         = f"{SCRIPTS_DIR}/node_setup_libcachesim.sh"
DEFAULT_WORKER = "node_cachesim_worker.sh"
SHIP_DIR      = f"{REMOTE_TARDIS}/ship"


def run_paths(run):
    """Remote paths for a named run, or the legacy single-run layout."""
    if not run:
        return {
            "shard": f"{REMOTE_TARDIS}/shard.txt",
            "out":   f"{REMOTE_TARDIS}/results/node_out.csv",
            "diag":  f"{REMOTE_TARDIS}/results/forgive_diag.log",
            "log":   f"{REMOTE_TARDIS}/results/worker.log",
            "work":  None,                       # worker default /mydata/trace_work
            "tmux":  "tracesweep",
        }
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run):
        sys.exit(f"--run must be letters, digits, _ or - (got {run!r})")
    return {
        "shard": f"{REMOTE_TARDIS}/shards/{run}.txt",
        "out":   f"{REMOTE_TARDIS}/results/{run}.csv",
        "diag":  f"{REMOTE_TARDIS}/results/{run}_diag.log",
        "log":   f"{REMOTE_TARDIS}/results/{run}.log",
        "work":  f"/mydata/trace_work_{run}",
        "tmux":  f"run_{run}",
    }


def connect(host, user, key_path):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {"username": user, "timeout": 30}
    if key_path:
        kw["key_filename"] = os.path.expanduser(key_path)
    c.connect(host, **kw)
    return c


def run(c, cmd, quiet=False, want_pty=False):
    # want_pty=True allocates a pseudo-TTY and sets TERM, needed for commands
    # that run apt-get / debconf or otherwise expect a terminal.
    stdin, stdout, stderr = c.exec_command(cmd, get_pty=want_pty)
    out = stdout.read().decode(); err = stderr.read().decode()
    rc = stdout.channel.recv_exit_status()
    if not quiet:
        if out.strip(): print(out.strip())
        if err.strip(): print(err.strip(), file=sys.stderr)
    return rc, out, err


def short(host):
    return host.split(".")[0]


def do_setup(node, cfg):
    host = node["host"]; user = cfg["username"]; key = cfg.get("key_path")
    repo = cfg["repo"]
    print(f"[{host}] SETUP start")
    c = connect(host, user, key)
    try:
        boot = (
            f"export TERM=xterm; "
            f"sudo chown -R $USER:$(id -gn) /mydata 2>/dev/null; "
            f"mkdir -p {REMOTE_TARDIS} && cd {REMOTE_TARDIS} && "
            # pull if the checkout exists: a stale checkout would not contain
            # a setup script added after the node was first set up
            f"(if [ -d scripts-repo ]; then cd scripts-repo && git pull --ff-only && cd ..; "
            f"else git clone {repo} scripts-repo; fi) && "
            f"bash {SETUP}"
        )
        rc, _, _ = run(c, boot, want_pty=True)
        print(f"[{host}] SETUP {'OK' if rc == 0 else 'FAILED rc=' + str(rc)}")
        return rc == 0
    finally:
        c.close()


def do_launch(node, cfg, shard_path, p, worker, envs, ships):
    host = node["host"]; user = cfg["username"]; key = cfg.get("key_path")
    print(f"[{host}] LAUNCH {os.path.basename(shard_path)} -> {p['tmux']}")
    c = connect(host, user, key)
    try:
        dirs = {os.path.dirname(p["shard"]), os.path.dirname(p["out"]), SHIP_DIR}
        run(c, "mkdir -p " + " ".join(sorted(dirs)), quiet=True)
        sftp = c.open_sftp()
        sftp.put(shard_path, p["shard"])
        for f in ships:
            sftp.put(f, f"{SHIP_DIR}/{os.path.basename(f)}")
        sftp.close()

        env = [f"DIAG={p['diag']}"]
        if p["work"]:
            env.append(f"WORK={p['work']}")
        env += envs                                  # user --env last: it wins
        inner = ("env " + " ".join(shlex.quote(e) for e in env) +
                 f" bash {SCRIPTS_DIR}/{worker} {p['shard']} {p['out']} > {p['log']} 2>&1")
        launch = (f"tmux kill-session -t {p['tmux']} 2>/dev/null; "
                  f"tmux new-session -d -s {p['tmux']} {shlex.quote(inner)}")
        rc, _, _ = run(c, launch)
        print(f"[{host}] LAUNCH {'OK' if rc == 0 else 'FAILED'}")
        return rc == 0
    finally:
        c.close()


def do_status(node, cfg, p):
    host = node["host"]; user = cfg["username"]; key = cfg.get("key_path")
    c = connect(host, user, key)
    try:
        rc, out, _ = run(c,
            f"echo -n 'rows='; wc -l < {p['out']} 2>/dev/null || echo 0; "
            f"echo -n 'tmux='; tmux has-session -t {p['tmux']} 2>/dev/null && echo RUNNING || echo DONE; "
            f"echo -n 'disk_free='; df -h --output=avail /mydata 2>/dev/null | tail -1; "
            f"tail -1 {p['log']} 2>/dev/null",
            quiet=True)
        print(f"[{short(host)}] {out.strip().replace(chr(10), ' | ')}")
    finally:
        c.close()


def do_collect(node, cfg, outdir, p):
    host = node["host"]; user = cfg["username"]; key = cfg.get("key_path")
    c = connect(host, user, key)
    try:
        sftp = c.open_sftp()
        local = os.path.join(outdir, f"node_out_{short(host)}.csv")
        try:
            sftp.get(p["out"], local)
            print(f"[{host}] collected -> {local}")
        except FileNotFoundError:
            print(f"[{host}] no result CSV yet")
        diag = os.path.join(outdir, f"forgive_diag_{short(host)}.log")
        try:
            sftp.get(p["diag"], diag)
            print(f"[{host}] collected -> {diag}")
        except FileNotFoundError:
            pass
        # tuning runs also keep a per-trial log next to the CSV
        try:
            sftp.get(p["out"] + ".trials.jsonl",
                     os.path.join(outdir, f"trials_{short(host)}.jsonl"))
        except FileNotFoundError:
            pass
        sftp.close()
    finally:
        c.close()


def parallel(nodes, fn, *a):
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(nodes)) as ex:
        futs = {ex.submit(fn, nd, *a): nd["host"] for nd in nodes}
        for f in concurrent.futures.as_completed(futs):
            try: f.result()
            except Exception as e: print(f"[{futs[f]}] ERROR: {e}", file=sys.stderr)


def merge_csvs(outdir):
    merged = os.path.join(outdir, "merged_all_nodes.csv")
    files = sorted(f for f in os.listdir(outdir) if f.startswith("node_out_") and f.endswith(".csv"))
    hdr = None
    with open(merged, "w", encoding="utf-8", newline="") as out:
        for fn in files:
            with open(os.path.join(outdir, fn), encoding="utf-8-sig", newline="") as f:
                first = f.readline()
                if hdr is None:
                    hdr = first
                    out.write(first)
                elif first != hdr:
                    # a different CSV format in the same folder would corrupt the merge
                    sys.exit(f"ERROR: {fn} has a different header than the other files "
                             f"in {outdir}. Use a separate --outdir per run.")
                for line in f:
                    out.write(line)
    print(f"merged {len(files)} node CSVs -> {merged}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--shards", help="dir with shard_NN.txt (for --launch)")
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--launch", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--outdir", default="collected")
    ap.add_argument("--run", help="named run (see docstring); default = legacy single run")
    ap.add_argument("--env", action="append", default=[], metavar="K=V",
                    help="environment for the worker (repeatable)")
    ap.add_argument("--worker", default=DEFAULT_WORKER,
                    help="worker script name inside setup-scripts/")
    ap.add_argument("--ship", action="append", default=[], metavar="FILE",
                    help="local file copied to /mydata/tardis/ship/ on every node")
    args = ap.parse_args()

    for e in args.env:
        if "=" not in e or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", e.split("=", 1)[0]):
            sys.exit(f"--env must look like NAME=value (got {e!r})")
    for f in args.ship:
        if not os.path.isfile(f):
            sys.exit(f"--ship: no such file {f}")

    cfg = yaml.safe_load(open(args.config, encoding="utf-8-sig"))
    nodes = cfg["nodes"]
    p = run_paths(args.run)

    if args.setup:
        parallel(nodes, do_setup, cfg)
    if args.launch:
        if not args.shards:
            sys.exit("--launch needs --shards")
        shards = sorted(f for f in os.listdir(args.shards) if f.startswith("shard_"))
        if len(shards) != len(nodes):
            print(f"WARN: {len(shards)} shards but {len(nodes)} nodes", file=sys.stderr)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(nodes)) as ex:
            futs = {}
            for i, nd in enumerate(nodes):
                if i < len(shards):
                    sp = os.path.join(args.shards, shards[i])
                    futs[ex.submit(do_launch, nd, cfg, sp, p, args.worker,
                                   args.env, args.ship)] = nd["host"]
            for f in concurrent.futures.as_completed(futs):
                try: f.result()
                except Exception as e: print(f"[{futs[f]}] ERROR: {e}", file=sys.stderr)
    if args.status:
        parallel(nodes, do_status, cfg, p)
    if args.collect:
        os.makedirs(args.outdir, exist_ok=True)
        parallel(nodes, do_collect, cfg, args.outdir, p)
        merge_csvs(args.outdir)


if __name__ == "__main__":
    main()
