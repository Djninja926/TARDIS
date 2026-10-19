#!/usr/bin/env python3
"""
orchestrate.py - run sweeps and tuning across CloudLab nodes over SSH.

  python orchestrate.py --setup
      On every node: clone this repo, build the libCacheSim fork (node_setup.sh).
  python orchestrate.py --launch --run NAME --list TRACES.tsv [--weights-from RESULTS.csv]
      Split the trace list across the nodes and start node_worker.sh on each,
      in a detached tmux session.
  python orchestrate.py --launch --run tune --plan tune_plan
      Start tuning: node i runs tune_plan/shard_i.txt (from tune.py plan).
  python orchestrate.py --status --run NAME
      Rows written so far, RUNNING/DONE, free disk, last log line, per node.
  python orchestrate.py --collect --run NAME
      Pull every node's results into results_NAME/ and merge them into
      results_NAME/merged_all_nodes.csv.

Splitting a list: datasets stay together and big ones are cut into contiguous
chunks; with --weights-from, nodes are balanced by requests (median requests
per trace of each dataset) instead of trace count, since one Twitter trace
costs as much as a thousand small ones.

Runs are named so several can share the nodes; each has its own files on the
node (/mydata/tardis/shards|results/NAME.*, /mydata/trace_work_NAME, tmux run_NAME).

  --env NAME=VALUE   worker setting (repeatable), see node_worker.sh, e.g.
                     --env SIZES=0.01  --env PARAMS_FILE=/mydata/tardis/ship/tuned_params.json
  --ship FILE        copy a local file to /mydata/tardis/ship/ on every node first

nodes.yaml:
  username: YOUR_CLOUDLAB_USER
  key_path: ~/.ssh/id_rsa            # optional
  repo: https://github.com/Djninja926/tardis.git
  nodes:
    - host: c220g2-011130.wisc.cloudlab.us
    ...
"""
import argparse, csv, os, re, shlex, statistics, sys, tempfile, concurrent.futures
from collections import defaultdict
import yaml
import paramiko

REMOTE_TARDIS = "/mydata/tardis"
SCRIPTS_DIR   = f"{REMOTE_TARDIS}/scripts-repo/libcachesim/scripts"
SETUP         = f"{SCRIPTS_DIR}/node_setup.sh"
WORKER        = f"{SCRIPTS_DIR}/node_worker.sh"
SHIP_DIR      = f"{REMOTE_TARDIS}/ship"


def run_paths(run):
    """Files on the node for a named run."""
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


def partition(list_path, n, weights_from):
    """Split a trace list into n shard lists of URLs. Datasets stay together;
    a dataset costing more than ~1.3x a node's share is cut into contiguous
    chunks; chunks are packed longest-first onto the least-loaded node.
    Cost = traces x weight, weight = the dataset's median requests per trace
    (millions, floor 1) from weights_from, else 1."""
    by_ds = defaultdict(list)
    with open(list_path, encoding="utf-8-sig") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 2 and parts[0] and not parts[0].startswith("#"):
                by_ds[parts[0]].append(parts[1])
    for ds in by_ds:
        by_ds[ds].sort()
    total = sum(len(u) for u in by_ds.values())

    weights = {}
    if weights_from:
        req = defaultdict(dict)
        for path in weights_from:
            with open(path, encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    try:
                        req[r["dataset"]][r["trace"]] = int(r["requests"])
                    except (KeyError, TypeError, ValueError):
                        pass
        for ds, d in req.items():
            weights[ds] = max(1.0, statistics.median(d.values()) / 1e6)
    default_w = sorted(weights.values())[len(weights) // 2] if weights else 1.0
    missing = sorted(d for d in by_ds if d not in weights)
    if weights and missing:
        print(f"WARN: no request counts for {', '.join(missing)}; using {default_w:.1f}M per trace")
    wt = lambda ds: weights.get(ds, default_w)

    target = sum(len(u) * wt(ds) for ds, u in by_ds.items()) / n
    chunks = []                                  # (label, urls, weight)
    for ds, urls in sorted(by_ds.items(), key=lambda x: -len(x[1]) * wt(x[0])):
        size, cost = len(urls), len(urls) * wt(ds)
        if cost > target * 1.3 and size > 1:
            k = max(2, min(size, round(cost / target)))
            base, rem, idx = size // k, size % k, 0
            for i in range(k):
                cnt = base + (1 if i < rem else 0)
                chunks.append((f"{ds}[{i + 1}/{k}]", urls[idx:idx + cnt], wt(ds)))
                idx += cnt
        else:
            chunks.append((ds, urls, wt(ds)))
    chunks.sort(key=lambda c: -len(c[1]) * c[2])
    bins, costs, labels = [[] for _ in range(n)], [0.0] * n, [[] for _ in range(n)]
    for label, urls, w in chunks:
        i = costs.index(min(costs))
        bins[i].extend(urls)
        costs[i] += len(urls) * w
        labels[i].append(f"{label}({len(urls)})")
    assert sum(len(b) for b in bins) == total
    print(f"{total} traces over {n} nodes:")
    for i in range(n):
        work = f"  ~{costs[i] / 1e3:.1f}B requests" if weights else ""
        print(f"  node {i}: {len(bins[i]):>5} traces{work}  {' '.join(labels[i])}")
    return bins


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


def do_launch(node, cfg, shard_path, p, tune, envs, ships):
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
                 f" bash {WORKER}{' --tune' if tune else ''} {p['shard']} {p['out']} > {p['log']} 2>&1")
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
        # results, forgive diagnostics, and (tuning only) the per-trial log
        for remote, name in ((p["out"], f"node_out_{short(host)}.csv"),
                             (p["diag"], f"forgive_diag_{short(host)}.log"),
                             (p["out"] + ".trials.jsonl", f"trials_{short(host)}.jsonl")):
            try:
                sftp.stat(remote)
            except FileNotFoundError:
                if name.startswith("node_out_"):
                    print(f"[{host}] no results yet")
                continue
            sftp.get(remote, os.path.join(outdir, name))
            print(f"[{host}] collected -> {os.path.join(outdir, name)}")
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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="nodes.yaml")
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--launch", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--run", default="sweep", help="run name (default: sweep)")
    ap.add_argument("--list", help="--launch: trace list to split across the nodes")
    ap.add_argument("--weights-from", nargs="+", metavar="CSV",
                    help="--launch --list: results CSV(s) to balance nodes by requests")
    ap.add_argument("--plan", help="--launch: tuning plan dir from tune.py plan")
    ap.add_argument("--env", action="append", default=[], metavar="NAME=VALUE")
    ap.add_argument("--ship", action="append", default=[], metavar="FILE")
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
        if bool(args.list) == bool(args.plan):
            sys.exit("--launch needs exactly one of --list (a sweep) or --plan (tuning)")
        if args.plan:
            shards = [os.path.join(args.plan, f) for f in sorted(os.listdir(args.plan))
                      if f.startswith("shard_")]
            if len(shards) != len(nodes):
                print(f"WARN: {len(shards)} shards but {len(nodes)} nodes", file=sys.stderr)
        else:
            tmp = tempfile.mkdtemp(prefix=f"shards_{args.run}_")
            shards = []
            for i, urls in enumerate(partition(args.list, len(nodes), args.weights_from)):
                shards.append(os.path.join(tmp, f"shard_{i:02d}.txt"))
                with open(shards[-1], "w", encoding="utf-8", newline="\n") as f:
                    f.writelines(u + "\n" for u in urls)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(nodes)) as ex:
            futs = {ex.submit(do_launch, nd, cfg, shards[i], p, bool(args.plan), args.env, args.ship): nd["host"]
                    for i, nd in enumerate(nodes) if i < len(shards)}
            for f in concurrent.futures.as_completed(futs):
                try: f.result()
                except Exception as e: print(f"[{futs[f]}] ERROR: {e}", file=sys.stderr)
    if args.status:
        parallel(nodes, do_status, cfg, p)
    if args.collect:
        outdir = f"results_{args.run}"
        os.makedirs(outdir, exist_ok=True)
        parallel(nodes, do_collect, cfg, outdir, p)
        merge_csvs(outdir)


if __name__ == "__main__":
    main()
