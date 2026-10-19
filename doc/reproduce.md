# Reproducing the libCacheSim results

Everything below runs from `libcachesim/scripts/` on your own machine; the
simulations run on CloudLab nodes that `orchestrate.py` drives over SSH.

We used 8 CloudLab **c220g2** nodes (Wisconsin): 40 hardware threads, 160 GB RAM,
about 170 GB free on `/mydata` each.

## 0. Prerequisites

```bash
pip install -r ../../requirements.txt
```

`nodes.yaml` next to the scripts (or pass `--config PATH`):

```yaml
username: YOUR_CLOUDLAB_USER
key_path: ~/.ssh/id_rsa            # optional
repo: https://github.com/Djninja926/tardis.git
nodes:
  - host: c220g2-011130.wisc.cloudlab.us
  - host: c220g2-011122.wisc.cloudlab.us
  # ...
```

## 1. Set up the nodes

```bash
python orchestrate.py --setup
```

On every node: clone this repo to `/mydata/tardis/scripts-repo`, build the
libCacheSim fork (`embeddings` branch) into `/mydata/cachesim`, check that both
forgive policies are compiled in, build `traceConv`, install Optuna.

## 2. Default-parameter sweep

The trace list is `../traces.tsv` (6,352 traces). `python gen_traces.py --out ../traces.tsv`
rebuilds it from the CMU mirror and the Wikimedia index.

```bash
python orchestrate.py --launch --run default --list ../traces.tsv --weights-from ../result/default.csv
python orchestrate.py --status --run default
python orchestrate.py --collect --run default
```

Each node gets a share of the list; datasets stay together and big ones are split.
`--weights-from` balances nodes by requests instead of trace count (one Twitter
trace costs as much as a thousand small ones); leave it out the first time, when
there are no results yet.

Traces that did not finish (a node restarted, a download failed) are listed and
run again under a new run name, then all runs are folded into one file (a later
file wins for the same cell):

```bash
python analyze.py results_default/merged_all_nodes.csv --list ../traces.tsv --remaining remaining.tsv
python orchestrate.py --launch --run default2 --list remaining.tsv
python orchestrate.py --collect --run default2
python analyze.py results_default/merged_all_nodes.csv results_default2/merged_all_nodes.csv --merged-out ../result/default.csv
```

What we actually ran: one pass at 10% and 0.1%, a rerun of what did not finish
plus the Wikimedia traces, and a separate 1% pass (`--env SIZES=0.01`), folded
together as above. About four days on 8 nodes.

## 3. Hyperparameter tuning

```bash
python tune.py plan ../result/default.csv --list ../traces.tsv
python orchestrate.py --launch --run tune --plan tune_plan
python orchestrate.py --collect --run tune
python tune.py params results_tune/merged_all_nodes.csv
```

`tune.py plan` prints which datasets get tuned, with how many training and test
traces, and writes `tune_plan/`. One study per (dataset, cache size, policy),
100 trials each. Training traces are one per family of near-identical traces,
so a held-out trace is never a copy of a training trace. `tune.py params` prints
every study (default vs best score on its training traces, forgive rates, and a
"mostly off" flag when the gain came from switching forgiveness off) and writes
`tuned_params.json`. About 18 hours on 8 nodes.

## 4. Tuned-parameter run

```bash
python orchestrate.py --launch --run tuned --list tune_plan/apply_traces.tsv ^
    --weights-from ../result/default.csv ^
    --ship tuned_params.json --env PARAMS_FILE=/mydata/tardis/ship/tuned_params.json
python orchestrate.py --collect --run tuned
python analyze.py ../result/default.csv --tuned results_tuned/merged_all_nodes.csv --only tune_plan/test_traces.tsv
```

(`^` continues a line in Windows cmd; use `\` on Linux or macOS.)
`apply_traces.tsv` already leaves out the full-size Twitter clusters (several take
over 8 hours per call or run out of memory). Each forgive policy looks up
`dataset|size|policy` in the parameter file and keeps its defaults if there is no
entry. About two days on 8 nodes. The last command is the held-out comparison.

## 5. Borrowed parameters for datasets that could not be tuned

Meta KV, Meta CDN and Tencent Photo (every trace over 150M requests, or too few
traces), rerun with the Wikimedia-upload LRUForgive parameters:

```bash
findstr /l /c:"cacheDatasets/metaKV/" /c:"cacheDatasets/metaCDN/" /c:"cacheDatasets/tencentPhoto/" ..\traces.tsv > transfer_list.tsv
python orchestrate.py --launch --run transfer --list transfer_list.tsv ^
    --env "PARAMS=lr=0.116698,ctx-speed=1.20038e-07,threshold=0.482896" ^
    --env SIZES=0.01,0.1,0.001 --env CELL_TIMEOUT=43200
```

(`grep -E 'metaKV/|metaCDN/|tencentPhoto/' ../traces.tsv > transfer_list.tsv` on Linux or macOS.)

## Worker settings (`--env NAME=VALUE`)

| Setting | Default | Meaning |
|---|---|---|
| `SIZES` | `0.1,0.01,0.001` | cache sizes as fractions of the working set (labels 10, 1, 0.1) |
| `EMB_MULT` | 5 | sketch table size, in multiples of the objects the cache holds |
| `MIN_CACHE_OBJS` | 1000 | skip forgive policies below this cache size (objects) |
| `PARAMS` | none | forgive parameters for every trace; `max-emb-entries=-1` removes the cap |
| `PARAMS_FILE` | none | per-dataset tuned parameters (from `tune.py params`) |
| `CELL_TIMEOUT` | 14400 | seconds per simulator call |
| `NUM_THREAD` / `FORGIVE_THREADS` | 4 / 2 | simulator threads for baselines / forgive policies |
| `TUNE_PAR_TRIALS` / `TUNE_PAR_TRACES` | 4 / 4 | tuning parallelism (trials x traces per node) |
