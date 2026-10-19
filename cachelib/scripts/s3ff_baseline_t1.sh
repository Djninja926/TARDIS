#!/bin/bash
set -uo pipefail
cd /mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench
LABEL="${1:?label}"
TR=/mydata/tardis/traces
[ "$(cat /sys/devices/system/cpu/intel_pstate/no_turbo)" = "1" ] || { echo "turbo on, abort"; exit 1; }
[ -z "${TARDIS_SAMPLE_RATE:-}${TARDIS_SAMPLE_MODE:-}" ] || { echo "subsample env set, abort"; exit 1; }
OUT=/mydata/tardis/s3ff_base_t1_${LABEL}_$(date +%Y%m%d_%H%M%S).csv
echo "branch,policy,t,rep,miss,tp" > "$OUT"
echo "writing $OUT"
for rep in 1 2 3; do
  for pol in s3fifo s3fifoforgive; do
    line=$(timeout 300 numactl --membind=0 "_build/$pol" "$TR/cache-t-00.oracleGeneral" 512 25 1 0 2>/dev/null | tail -1)
    mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
    tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
    echo "$LABEL,$pol,1,$rep,${mr:-NA},${tp:-NA}" >> "$OUT"
    echo "  $pol rep=$rep -> mr=${mr:-NA} tp=${tp:-NA}"
  done
done
echo "DONE -> $OUT"
