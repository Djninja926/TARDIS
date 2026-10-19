# libCacheSim results

Everything the libCacheSim sweep produced, merged into one file per experiment.
Which machine or which run produced a row does not matter: each file holds the
final result for every (trace, policy, cache size).

| File | What it is | Rows |
|---|---|---|
| `default.csv` | Every trace, all four policies, at 10%, 1% and 0.1%, with the policies' **default** parameters | about 76,000 |
| `tuned.csv` | Every trace of the tuned datasets (except the full-size Twitter clusters), with **tuned** parameters | about 72,000 |
| `transfer.csv` | Meta KV, Meta CDN, Tencent Photo, which could not be tuned, rerun with the Wikimedia-upload tuned parameters | 9 traces |
| `tuning/studies.csv` | One row per tuning study (dataset, cache size, policy): default and best score on the training traces, best parameters, forgive rates | 84 |
| `tuning/tuned_params.json` | The parameters used for `tuned.csv`, keyed `dataset\|cache_pct\|policy` | 73 |
| `tuning/trials.jsonl` | Every trial of every study (parameters, score, per-trace results) | |
| `tuning/train_traces.tsv`, `tuning/test_traces.tsv` | Which traces each study trained on, and the held-out traces used to judge it | |
| `tuning/split.tsv` | Role of every trace in the tuning split (`train`, `test`, `excluded`, `untuned`) | |
| `tables/` | Per-workload summary tables (CSV and LaTeX), default and tuned | |

## Result CSV columns (`default.csv`, `tuned.csv`, `transfer.csv`)

```
node,dataset,trace,policy,cache_name,cache_pct,size_bytes,requests,miss,byte_miss,params
```

| Column | Meaning |
|---|---|
| `dataset`, `trace` | dataset (path under the mirror, `/` replaced by `_`; Wikimedia gets a `wikimedia_` prefix) and trace name |
| `policy` | `lru`, `s3fifo`, `lruforgiveembcache`, `s3fifoforgive-embcache` |
| `cache_pct` | cache size as a percent of the trace's working set: `10`, `1`, `0.1` |
| `size_bytes` | the absolute cache size that percent turned into |
| `requests` | requests in the trace (zero-size requests are skipped) |
| `miss`, `byte_miss` | object and byte miss ratio, or an `NA_*` code (below) |
| `params` | `default`, or the forgive parameters used, separated by `;` |

`NA_*` codes:

| Code | Meaning |
|---|---|
| `NA_SMALL` | skipped on purpose: the cache would hold fewer than 1,000 objects |
| `NA_FAIL` | the simulation failed twice: out of memory or over the time limit (only the largest Twitter clusters) |
| `NA_DLFAIL` | the trace could not be downloaded |

## Regenerating the numbers

From `libcachesim/scripts/`:

```bash
# default parameters: per-dataset rollups and the per-workload table
python analyze.py ../result/default.csv --list ../traces.tsv --exclude "^twitter$"

# tuned parameters where available (a later file wins for the same cell)
python analyze.py ../result/default.csv ../result/tuned.csv --exclude "^twitter$" --table ../result/tables/table_tuned

# tuned vs default on held-out traces only (the honest headline)
python analyze.py ../result/default.csv --tuned ../result/tuned.csv --only ../result/tuning/test_traces.tsv

# borrowed parameters on Meta KV / Meta CDN / Tencent Photo
python analyze.py ../result/default.csv --tuned ../result/transfer.csv

# the per-study tuning summary
python tune.py params ../result/tuning/studies.csv --out check.json
```

`--exclude "^twitter$"` drops the full-size Twitter clusters so the Twitter row
uses the sampled copies (`twitter_sample10`), which were run with both default
and tuned parameters.

## Things to know when reading these numbers

- **Tuning gains on training traces overstate the real gain.** Quote held-out
  numbers (`analyze.py --tuned ... --only .../test_traces.tsv`).
- **Twitter tuning did not carry over** to held-out traces (it made them worse);
  report Twitter with default parameters.
- **Meta KV, Meta CDN, Tencent Photo were not tuned** (every trace over 150M
  requests, or too few traces); `transfer.csv` uses borrowed parameters and should
  be labeled that way.
- **The largest Twitter clusters run out of memory** in the forgive policies:
  the per-object access counter is never freed, so memory grows with the number
  of unique objects, not the cache size.
