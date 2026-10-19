#!/bin/bash
set -uo pipefail
cd /mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench
TR=/mydata/tardis/traces
BIN=_build/lruforgive
TRACE=$TR/cache-t-00.oracleGeneral
SZ=512; HP=25; REPS=3
OUT=/mydata/tardis/subsample_$(date +%Y%m%d_%H%M%S).csv
echo "mode,sample_rate,rep,miss_ratio,mqps" > "$OUT"
echo "writing $OUT"

# LRU baseline + full-rate LRUForgive reference first
echo "== LRU baseline =="
numactl --membind=0 _build/lru $TRACE $SZ $HP 1 0 2>/dev/null | tail -1 | grep -o "miss ratio.*"

for mode in 0 1; do
  for rate in 1.0 0.5 0.25 0.1 0.05 0.01; do
    for r in $(seq 1 $REPS); do
      line=$(TARDIS_SAMPLE_MODE=$mode TARDIS_SAMPLE_RATE=$rate \
             numactl --membind=0 $BIN $TRACE $SZ $HP 1 0 2>/dev/null | tail -1)
      mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
      tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
      echo "$mode,$rate,$r,${mr:-NA},${tp:-NA}" >> "$OUT"
      echo "  mode=$mode rate=$rate rep=$r -> mr=${mr:-NA} tp=${tp:-NA}"
    done
  done
done
echo "DONE -> $OUT"
