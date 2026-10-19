# CacheLib results

Results from running LRUForgive and S3FIFOForgive inside Meta's CacheLib (our
fork of the SOSP'23 S3-FIFO artifact) with the artifact's multithreaded
benchmark, `mybench`. The lab notebook behind every file is
[`doc/MultithreadedLRUForgive.md`](../../doc/MultithreadedLRUForgive.md) ("doc" below); the section numbers point into it.

All runs: CloudLab c220g2 (2 x 10-core Xeon, 160 GB), turbo boost off,
`numactl --membind=0`. Cache sizes are the total size of one shared cache, in MB.

## Files

| File | What it is | Doc | Regenerate with `scripts/run_matrix.sh` |
|---|---|---|---|
| `final_sweep_perthread.csv` | **The headline.** Per-thread design (branch `jane-perthread`): LRU and LRUForgive at 1-16 threads, S3-FIFO and S3FIFOForgive at 1 thread; 4 traces x 3 sizes; replicated mode; 3 runs per cell | 38 | `LABEL=perthread bash run_matrix.sh final` |
| `final_sweep_single-manager.csv` | Same for the single shared manager with the log-structured applier (branch `tardis-implementation`); forgive policies only (baselines are branch-independent) | 38 | `LABEL=single-manager BASELINES=no bash run_matrix.sh final` |
| `all_traces_t1.csv` | Every CMU-mirror trace (5,652) at 1 thread, default parameters, all four policies at 10% and 0.1% of each trace's footprint in bytes, 3 runs | 40.7 | `scripts/all_traces_worker.sh` on each node |
| `sizesweep_t1.csv`, `sizesweep_t1.png` | 24 traces over an 8 MB - 8 GB ladder, 1 thread; where the LRUForgive gap peaks (the "operating points") | 16, 18, 20 | `bash run_matrix.sh sizesweep` |
| `shared_mode.csv` | Shared mode (threads split one trace, cache size fixed), single manager with adaptive backoff, after the harness fixes | 31, 33 | `LABEL=logbackoff bash run_matrix.sh shared` |
| `s3fifoforgive_sizes.csv` | S3-FIFO vs S3FIFOForgive, 1 thread, 4 traces x 3 sizes, single manager | 34, 38 | `bash run_matrix.sh s3ff` |
| `s3fifoforgive_t1.csv` | S3-FIFO vs S3FIFOForgive on cache-t-00 at 512 MB, 1 thread, both designs | 36.2, 37 | `TRACES=cache-t-00:512:25 REPS=3 bash run_matrix.sh s3ff` |
| `subsample_single-manager_t1.csv` | LRUForgive applying only a fraction of accesses, cache-t-00 512 MB, 1 thread; mode 0 = skip the whole access, 1 = skip only the math, 2 = every Nth access | 35.1-35.25 | `bash run_matrix.sh subsample` |
| `subsample_perthread.csv` | Same, per-thread design, 1-16 threads (mode 0) | 36.1 | `LABEL=perthread THREADS="1 4 8 16" REPS=2 VARIANTS="TARDIS_SAMPLE_MODE=0,TARDIS_SAMPLE_RATE=1.0 TARDIS_SAMPLE_MODE=0,TARDIS_SAMPLE_RATE=0.5 TARDIS_SAMPLE_MODE=0,TARDIS_SAMPLE_RATE=0.25" bash run_matrix.sh subsample` |
| `bimodality.csv` | 10 runs of LRUForgive at 1 thread on msr_proj_0, cluster53, cache-t-00 (run-to-run spread) | 24 | `bash run_matrix.sh bimodality` |
| `prxy1_cliff.csv` | msr_prxy_1 at 200-800 MB, across its miss-ratio cliff | 24.4 | `bash run_matrix.sh cliff` |
| `one_hit_wonders.txt` | One-hit-wonder share of objects and of requests, 21 traces | 19.10, 19.11, 20.2 | `python scripts/trace_stats.py TRACE...` |

New runs of `run_matrix.sh` write one format:
`label,trace,policy,mode,size_mb,hp,threads,rep,variant,miss,tp,requests,runtime_s`.
The files above were written by the earlier scripts, so some use older column
names (`branch`, `algo`, `miss_ratio`, `mqps`, ...) and some leave out the trace
or size when an experiment had only one. `scripts/analyze.py` reads all of them.

## The headline, regenerated

```bash
cd cachelib/scripts
python analyze.py final ../result/final_sweep_perthread.csv ../result/final_sweep_single-manager.csv
```

LRUForgive gap over LRU in points at the operating-point size (t=2 left out, see below):

| Trace | Design | 1 thread | 4 | 8 | 16 |
|---|---|---|---|---|---|
| cache-t-00, 512 MB | per-thread | 5.64 | 5.32 | 5.24 | 4.76 |
| | single manager | 5.69 | 3.81 | 2.78 | 1.31 |
| msr_proj_0, 256 MB | per-thread | 6.22 | 4.13 | 3.87 | 2.85 |
| | single manager | 6.62 | 2.65 | 1.74 | 0.25 |

Everything else: `python analyze.py cells ../result/FILE.csv` (repetitions and
the gap to the baseline per cell; with a size ladder, the operating point per
trace) and `python analyze.py alltraces ../result/all_traces_t1.csv --list ../../libcachesim/traces.tsv`
(per workload).

## Things to know when reading these numbers

- **t=2 is broken on every trace.** LRU at 2 threads gives miss ratios above
  its 1-thread value (cache-t-00: 0.95 vs 0.83), so every t=2 gap is an
  artifact. We report 1, 4, 8 and 16 threads (doc 38.4).
- **S3-FIFO runs only at 1 thread on real traces.** The artifact's S3-FIFO
  eviction path is not thread-safe (check-then-remove is not atomic) and
  crashes under heavy eviction from 4 threads up (doc 23, 39). On the zipf
  trace, which evicts rarely, it runs at every thread count.
- **msr_prxy_1 is diagnostic only.** Its LRU baseline gives different values
  across runs (0.2846, 0.2758, 0.3574), which deterministic LRU should not;
  the leading suspect is CacheLib's TTL reaper (doc 31.3). Its LRUForgive runs
  are bimodal on the cliff (doc 24).
- **Replicated vs shared mode.** Replicated (the artifact's default): every
  thread replays the whole trace and the cache is `size x threads`. Shared:
  threads split one trace and the cache stays the same. The final sweep is
  replicated; `size_mb` there is the total, so the base size is `size_mb / threads`.
- **Crashed runs in the older files.** `mybench` prints a miss ratio at every
  checkpoint, and the earlier scripts kept the last line without checking the
  exit code, so a run that crashed part way was recorded as a (too high) miss
  ratio. This may explain the rare S3FIFOForgive outliers on msr_proj_0 (doc
  38.2) and the single-manager S3FIFOForgive run at 0.7725 in
  `s3fifoforgive_t1.csv`. The current scripts record such runs as `FAIL_exitN`.
- **Defaults across all traces** (`all_traces_t1.csv`): LRUForgive +0.93% at
  10% and +1.69% at 0.1% on average; S3FIFOForgive about 0. Twitter averages
  -8.8% at 10% from a few large losses (median 0).

## Not kept, and why

| Earlier file | Why it is not here |
|---|---|
| `replicated_mode_matrix.csv` (doc 32, 33) | Per-thread runs from before the shard-lookup fix (doc 35-36): forgiveness looked up the wrong shard, so its ~1.1-point gaps are the bug. The final sweep replaces it. |
| `recency_window_sweep.csv` (doc 24.1) | Multithreaded runs predate the shared-mode harness fixes (doc 31), and every cluster53 run failed. |
| `subsample_perthread_bugged_baseline.csv` | Same shard-lookup bug. |
| `subsample_all_traces_perthread_partial.csv` | Incomplete and not used in the doc. |
| Logs, per-node outputs, shard lists | Not needed to check any number. |
