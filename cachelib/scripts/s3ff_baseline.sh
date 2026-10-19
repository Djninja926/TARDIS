#!/bin/bash
# Usage: s3ff_baseline.sh <branch-label>
# Run this AFTER you've checked out the branch and built it.
# It does NOT switch git or build, just runs the current _build binaries.
set -uo pipefail
cd /mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench

LABEL="${1:?pass a branch label, e.g. perthread or single-manager}"
TR=/mydata/tardis/traces

# guard: turbo must be off for stable throughput
NT=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null || echo missing)
if [ "$NT" != "1" ]; then echo "ABORT: turbo not disabled (no_turbo=$NT)"; exit 1; fi

# guard: make sure no stale subsample env is set (would corrupt these numbers)
if [ -n "${TARDIS_SAMPLE_RATE:-}" ] || [ -n "${TARDIS_SAMPLE_MODE:-}" ]; then
  echo "ABORT: TARDIS_SAMPLE_* is set in env; unset it first (would subsample the baseline)"; exit 1
fi

OUT=/mydata/tardis/s3ff_baseline_${LABEL}_$(date +%Y%m%d_%H%M%S).csv
echo "branch,policy,t,rep,miss,tp" > "$OUT"
echo "writing $OUT (label=$LABEL)"

# cache-t-00, replicated, scaled per run.sh: size=512*t, hp=25+log2(t)
# t=1:512/25  t=4:2048/27  t=8:4096/28  t=16:8192/29
for cfg in "1 512 25" "4 2048 27" "8 4096 28" "16 8192 29"; do
  read -r t sz hp <<< "$cfg"
  echo "== t=$t (size=$sz hp=$hp) =="
  for rep in 1 2 3; do
    for pol in s3fifo s3fifoforgive; do
      line=$(numactl --membind=0 "_build/$pol" "$TR/cache-t-00.oracleGeneral" "$sz" "$hp" "$t" 0 2>/dev/null | tail -1)
      mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
      tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
      echo "$LABEL,$pol,$t,$rep,${mr:-NA},${tp:-NA}" >> "$OUT"
      echo "  $pol t=$t rep=$rep -> mr=${mr:-NA} tp=${tp:-NA}"
    done
  done
done
echo "DONE -> $OUT"
