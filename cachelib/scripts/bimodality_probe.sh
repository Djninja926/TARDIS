#!/usr/bin/env bash
# ITEM 6 probe: is the t=1 bimodality seen on msr_prxy_1 (22.3) present on other
# traces, and does it split along the improve-vs-decay line?
#
# Hypothesis: traces whose gap IMPROVES with threads (cluster53, prxy_1) are
# bimodal at t=1, and concurrency reliably selects the better mode. Traces whose
# gap DECAYS (proj_0, cache-t-00) are unimodal, so their decay needs a different
# explanation (dilution).
#
# RUN THIS BEFORE REBUILDING FOR THE RECENCY EXPERIMENT: it uses the current
# recentWindow=16 binary, and its results are the baseline for that comparison.

set -u
BUILD=/mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench/_build
T=/mydata/tardis/traces
OUT=/mydata/tardis/bimodality.csv
N=10                       # runs per trace at t=1
NUMA="numactl --cpunodebind=0 --membind=0"

[ -f "$OUT" ] || echo "trace,threads,run,miss_ratio,mqps" > "$OUT"

# trace size hp   (fast traces only; prxy_1 is 22min/run, already have 9 samples)
for spec in \
  "msr_proj_0.oracleGeneral 256 24" \
  "cluster53.oracleGeneral 128 23" \
  "cache-t-00.oracleGeneral 512 25"; do
  set -- $spec; tr=$1; sz=$2; hp=$3
  echo "===== $tr @${sz}MB t=1, $N runs ====="
  for r in $(seq 1 $N); do
    grep -q "^$tr,1,$r," "$OUT" && { echo "[have] run $r"; continue; }
    line=$(timeout 3600 $NUMA "$BUILD/lruforgive" "$T/$tr" "$sz" "$hp" 1 1 2>&1 | tail -1)
    mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
    tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
    echo "  run $r: miss=${mr:-FAIL} mqps=$tp"
    echo "$tr,1,$r,${mr:-FAIL},$tp" >> "$OUT"
  done
done
echo "===== summary (sorted miss ratios per trace) ====="
for tr in msr_proj_0 cluster53 cache-t-00; do
  echo "--- $tr ---"
  grep "^$tr" "$OUT" | cut -d, -f4 | grep -v FAIL | sort -n | tr '\n' ' '; echo
done
echo "DONE -> $OUT"
