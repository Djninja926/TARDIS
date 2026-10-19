# TARDIS: Temporal Relationship Sketches for Cache Eviction

This repo holds the cache-eviction evaluation of **TARDIS** (LDOS lab, UT Austin):
the code that runs it and the results it produced. TARDIS is described in the paper
*TARDIS: A Temporal Relationship Sketch for Online Systems Control*.

## Abstract

Cache eviction policies decide what to drop using an object's own history: how
recently (LRU) or how often (LFU, S3-FIFO) it was accessed. They miss
*relationships between objects*: an access to one object can make another object
likely to be accessed soon, even when that object's own recency is poor.

TARDIS keeps a small vector (a *sketch*) per object that summarizes when the object
was active, relative to a slowly drifting shared context. Two objects whose sketches
are similar have been active at similar times. TARDIS uses this as an **eviction
override**: when the base policy picks an eviction victim, TARDIS compares the
victim's sketch with the sketches of the most recently accessed objects, and if
they are similar enough it **forgives** the victim (keeps it) and evicts the next
candidate instead.

We add forgiveness to two base policies:

- **LRUForgive** (LRU + forgiveness)
- **S3FIFOForgive** (S3-FIFO + forgiveness; a forgiven object goes to the head of
  the main FIFO)

and evaluate them in two simulators: the libCacheSim trace simulator (thousands of
production traces) and Meta's CacheLib (single node, multithreaded).

## Repo structure

```
tardis/
  libcachesim/
    scripts/       the trace-sweep pipeline (7 files, below)
    result/        all libCacheSim results (default, tuned, borrowed parameters)
    traces.tsv     the exact trace list the sweep ran on
  cachelib/
    scripts/       CacheLib experiments (6 files, below)
    result/        CacheLib results, with a README per file
  doc/
    reproduce.md   step-by-step instructions to rerun the libCacheSim results
    MultithreadedLRUForgive.md   the lab notebook: every experiment, in date order
  requirements.txt Python packages for the scripts
```

## How to use libCacheSim

The forgive policies live in a fork of libCacheSim:
[Aryan470/libCacheSim, branch `embeddings`](https://github.com/Aryan470/libCacheSim/tree/embeddings)
(`libCacheSim/cache/eviction/LRUForgiveEmbCache.cpp`, `S3FIFOForgiveEmbCache.cpp`).

### Setup

```bash
git clone --branch embeddings https://github.com/Aryan470/libCacheSim.git
cd libCacheSim
bash scripts/install_dependency.sh
bash scripts/install_libcachesim.sh      # builds _build/bin/cachesim
```

`libcachesim/scripts/node_setup.sh` does all of this on a fresh CloudLab node
(and installs the Python package the tuner needs).

### Usage

```bash
./cachesim DATAPATH TRACE_FORMAT EVICTION_ALGO CACHE_SIZE [OPTION...]
```

A cache size containing a `.` is a fraction of the trace's working set, for example
`0.01` is 1%. Examples:

```bash
# baselines at 10%, 1% and 0.1% of the working set
./cachesim twitter_cluster52.oracleGeneral.zst oracleGeneral lru,s3fifo 0.1,0.01,0.001

# forgive policies at 1%, embedding table capped at 50,000 objects
./cachesim twitter_cluster52.oracleGeneral.zst oracleGeneral \
    lruforgiveembcache,s3fifoforgive-embcache 0.01 \
    --eviction-params lr=0.2,ctx-speed=0.001,threshold=0.4,max-emb-entries=50000
```

Forgive-policy parameters (`--eviction-params`):

| Parameter | Default | Meaning |
|---|---|---|
| `lr` | 0.2 | how fast an object's sketch moves toward the current context |
| `ctx-speed` | 0.001 | how fast the shared context drifts |
| `threshold` | 0.4 | similarity needed to forgive an eviction candidate |
| `window` | 16 | number of recently accessed objects compared against |
| `min-access` | 2 | accesses before an object gets a sketch |
| `max-forgives` | 5 | consecutive forgives allowed per eviction |
| `max-emb-entries` | -1 (unlimited) | sketch table size in **objects**; we use 5x the number of objects the cache holds |

`max-emb-entries` also accepts `Nx`, but that multiplies the cache size in
*bytes*, not objects, so we always pass an explicit object count.

## Running the full sweep

The sweep runs one trace at a time on each of several CloudLab nodes (download,
simulate every policy and size, delete, next). Everything is driven from your
own machine with `orchestrate.py` over SSH:

```bash
cd libcachesim/scripts
python orchestrate.py --setup                                        # build on every node
python orchestrate.py --launch --run default --list ../traces.tsv    # start the sweep
python orchestrate.py --status --run default                         # progress
python orchestrate.py --collect --run default                        # -> results_default/
python analyze.py results_default/merged_all_nodes.csv               # per-dataset and per-workload results
```

Tuning and the tuned rerun use the same commands; the full sequence is in
[`doc/reproduce.md`](doc/reproduce.md). Section 6 there is the paper-style
rerun: object sizes ignored (`--env IGNORE_OBJ_SIZE=1`, so cache sizes are a
percent of unique objects) and one tuning study per workload.

| Script | What it does |
|---|---|
| `orchestrate.py` | set up nodes, split a trace list across them and launch, status, collect and merge |
| `node_setup.sh` | per-node build of the libCacheSim fork (run by `orchestrate.py --setup`) |
| `node_worker.sh` | everything that runs on a node: the sweep loop, Wikimedia conversion, starting tuning |
| `tune.py` | `plan` the tuning studies, `run` them (on a node), turn the results into tuned `params` |
| `analyze.py` | results per dataset and per workload, default vs tuned, progress and reruns, merging runs |
| `gen_traces.py` | rebuild the trace list by crawling the CMU mirror and the Wikimedia index |
| `common.py` | shared code: trace naming, reading simulator output, the paper's workload groups, grouping near-identical traces |

### Evaluation setup

| | |
|---|---|
| Traces | 6,352: the CMU cache-trace mirror (CloudPhysics, MSR, FIU, SYSTOR, Alibaba Block, Tencent Block, Tencent Photo, Twitter, Meta KV / CDN / Storage, Wikipedia) and Wikimedia 2019 text + upload (42 days) |
| Cache sizes | 10%, 1%, 0.1% of each trace's working set (bytes; object sizes are kept) |
| Sketch table | 5x the number of objects the cache holds |
| Skip rule | forgive policies are not run when the cache holds fewer than 1,000 objects (S3-FIFO paper rule) |
| Metric | relative miss-ratio reduction per trace, `(baseline - forgive) / baseline`, averaged |
| Tuning | per dataset and cache size: 100-trial Bayesian optimization (Optuna TPE) over `lr`, `ctx-speed`, `threshold`, on up to 8 training traces, judged on held-out traces |

## Results

All results are in [`libcachesim/result/`](libcachesim/result/), with a README
describing every file and how to regenerate each number from them.

Pooled over all traces, LRUForgive vs LRU:

| Cache size | Default parameters | Tuned parameters |
|---|---|---|
| 10% | +0.18% | +2.79% |
| 1% | +0.74% | +3.15% |
| 0.1% | +1.20% | +2.09% |

The default `ctx-speed` (0.001) is too fast for most traces, so forgiveness rarely
fires; tuned values are around 1e-7 to 1e-5. S3FIFOForgive vs S3-FIFO stays close
to zero with either setting.

## How to use CacheLib

The CacheLib implementation is a fork of the SOSP'23 S3-FIFO artifact's CacheLib:
[Djninja926/cachelib-sosp23](https://github.com/Djninja926/cachelib-sosp23). It adds
`MMLruForgive` and `MMS3FIFOForgive` and the multithreaded designs we explored:

| Branch | Design |
|---|---|
| `jane-perthread` | per-thread state, no shared applier: the design that keeps the gain at 16 threads (the headline) |
| `tardis-implementation` | one shared embedding manager fed by a log and a single background applier, with adaptive backoff |

### Setup

The fork must sit inside the artifact so `mybench` finds libCacheSim:

```bash
git clone https://github.com/Thesys-lab/sosp23-s3fifo.git && cd sosp23-s3fifo
pushd libCacheSim/scripts && bash install_dependency.sh && bash install_libcachesim.sh && popd
git clone -b jane-perthread https://github.com/Djninja926/cachelib-sosp23.git cachelib-sosp23
cd cachelib-sosp23/mybench
bash turboboost.sh disable          # steady throughput (needs root)
bash build.sh                       # full build; wipes _build/; must print no "FAILED:" lines
```

`cachelib/scripts/node_setup.sh` does all of this on a fresh CloudLab node,
including a fix for the fork's broken zstd submodule. Any change to a header in
`cachelib/allocator/` needs the full `build.sh` (explicit template
instantiations freeze inlined code; a partial rebuild links stale code).

### Usage

```bash
./_build/<algo> <trace.oracleGeneral> <cache_MB> <hashpower> <threads> [mode]
./_build/lruforgive cache-t-00.oracleGeneral 512 25 8 0
```

`algo` is `lru`, `s3fifo`, `lruforgive` or `s3fifoforgive`. The cache is one
shared cache of `cache_MB` in total. `hashpower` is log2 of the hash table's
buckets; we use log2(MB) + 16, plus log2(threads). `mode` 0 (replicated, the
default) has every thread replay the whole trace, so the cache is scaled with
the thread count; 1 (shared) splits the trace across threads and keeps the
cache fixed. The binary prints a miss ratio at checkpoints; the last line is
the result, and only if the run exited cleanly.

### Running the experiments

```bash
cd cachelib/scripts
bash get_traces.sh cache-t-00 msr_proj_0 cluster53 msr_prxy_1
LABEL=perthread bash run_matrix.sh final        # the headline sweep, about a day
python analyze.py final /mydata/tardis/results/final_perthread.csv ...
```

| Script | What it does |
|---|---|
| `node_setup.sh` | build the artifact, libCacheSim and the fork on a fresh node (`BRANCH=` picks the design) |
| `get_traces.sh` | download or convert traces by name (Wikimedia, CMU mirror, synthetic zipf) |
| `run_matrix.sh` | every single-node experiment, as presets: `final`, `s3ff`, `shared`, `sizesweep`, `subsample`, `bimodality`, `cliff`, `zipf` |
| `all_traces_worker.sh` | the all-traces 1-thread sweep, one node's share of the trace list |
| `analyze.py` | the headline tables, per-cell repetition summaries, the all-traces table |
| `trace_stats.py` | one-hit wonders, skew and hot-set churn of a trace |

[`cachelib/result/README.md`](cachelib/result/README.md) lists every result
file, the doc section it backs, and the command that regenerates it.

Known limits: the artifact's S3-FIFO is not thread-safe under heavy eviction
(non-atomic check-then-remove in `S3FIFOList::getEvictionCandidate`), so S3-FIFO
and S3FIFOForgive run at 1 thread on real traces; and 2-thread runs give a
broken LRU baseline on every trace, so we report 1, 4, 8 and 16 threads.

## Traces

- CMU PDL mirror: <https://ftp.pdl.cmu.edu/pub/datasets/twemcacheWorkload/cacheDatasets/>
  (`oracleGeneral` format, zstd-compressed)
- Wikimedia 2019 caching traces: <https://analytics.wikimedia.org/published/datasets/caching/2019/>
  (TSV, converted to `oracleGeneral` on the node by `libcachesim/scripts/node_worker.sh`)

`oracleGeneral` is a binary format of 24-byte records:

```c
struct {
    uint32_t timestamp;
    uint64_t obj_id;
    uint32_t obj_size;
    int64_t  next_access_vtime;   // -1 if not accessed again
};
```

## Acknowledgements

- Testbed: [CloudLab](https://www.cloudlab.us).
- Simulator: [libCacheSim](https://github.com/1a1a11a/libCacheSim) and the
  [SOSP'23 S3-FIFO artifact](https://github.com/Thesys-lab/sosp23-s3fifo).
- Open-source traces: Twitter, Tencent Block, Tencent Photo, Wikimedia CDN,
  Alibaba Block, MSR, FIU, CloudPhysics, SYSTOR and Meta.
