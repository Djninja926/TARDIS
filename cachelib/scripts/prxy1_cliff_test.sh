#!/usr/bin/env bash
# Section 24.4: is msr_prxy_1's bimodality specific to sitting on the cliff?
#
# prxy_1's LRU miss falls 0.6437 -> 0.2846 -> 0.0032 across 128/256/512MB, so
# 256MB is mid-cliff and 512MB+ is flat. If bimodality is a cliff property, it
# should appear at sizes on the steep section and vanish once the curve flattens.
#
# Runs LRU once per size (deterministic, gives the curve shape) and LRUForgive
# 5x per size (to expose modes).
#
# COST WARNING: prxy_1 is 168.6M requests. LRUForgive is ~22-25 min per run at
# t=1. 5 sizes x 5 runs = 25 LRUForgive runs = roughly 9-10 HOURS. Run it with
# nohup and leave it. Trim SIZES if you want a faster first answer; 200 and 1000
# alone (cliff vs flat) would answer the question in ~4 hours.

set -u
BUILD=/mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench/_build
T=/mydata/tardis/traces
TRACE=msr_prxy_1.oracleGeneral
OUT=/mydata/tardis/prxy1_cliff.csv
NUMA="numactl --cpunodebind=0 --membind=0"
N=5

# size hashpower  (hp follows the ladder: 256MB=24, doubling adds 1;
# these are interpolated to the nearest ladder value for each size)
SPECS="200:24 256:24 320:24 400:25 800:26"

[ -f "$OUT" ] || echo "cache_mb,hashpower,algo,run,miss_ratio,mqps" > "$OUT"

for spec in $SPECS; do
  sz=${spec%%:*}; hp=${spec##*:}
  echo "===== ${sz}MB hp=$hp ====="

  # LRU once: establishes where on the curve this size sits
  if ! grep -qE "^$sz,$hp,lru,1,[0-9]" "$OUT"; then
    line=$(timeout 7200 $NUMA "$BUILD/lru" "$T/$TRACE" "$sz" "$hp" 1 1 2>&1 | tail -1)
    mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
    tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
    echo "  lru: miss=${mr:-FAIL}"
    echo "$sz,$hp,lru,1,${mr:-FAIL},$tp" >> "$OUT"
  else
    echo "  [have] lru"
  fi

  # LRUForgive N times: exposes modes if present
  for r in $(seq 1 $N); do
    if grep -qE "^$sz,$hp,lruforgive,$r,[0-9]" "$OUT"; then echo "  [have] lf run $r"; continue; fi
    line=$(timeout 7200 $NUMA "$BUILD/lruforgive" "$T/$TRACE" "$sz" "$hp" 1 1 2>&1 | tail -1)
    mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
    tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
    echo "  lruforgive run $r: miss=${mr:-FAIL} mqps=$tp"
    echo "$sz,$hp,lruforgive,$r,${mr:-FAIL},$tp" >> "$OUT"
  done
done

echo "===== summary: sorted LRUForgive miss ratios per size ====="
for spec in $SPECS; do
  sz=${spec%%:*}
  echo -n "${sz}MB (lru=$(awk -F, -v s="$sz" '$1==s && $3=="lru"{print $5}' "$OUT")): "
  awk -F, -v s="$sz" '$1==s && $3=="lruforgive" && $5 ~ /^[0-9]/ {print $5}' "$OUT" | sort -n | tr '\n' ' '
  echo
done
echo "DONE -> $OUT"
