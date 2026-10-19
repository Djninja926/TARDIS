#!/bin/bash
set -uo pipefail
cd /mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench

NT=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null || echo missing)
if [ "$NT" != "1" ]; then echo "ABORT: turbo not disabled (no_turbo=$NT)."; exit 1; fi
echo "turbo OFF; S3FIFOForgive vs S3FIFO, t=1, 4 reps"

TR=/mydata/tardis/traces
REPS=4
OUT=/mydata/tardis/s3ff_sweep_$(date +%Y%m%d_%H%M%S).csv
echo "trace,policy,size_mb,hp,rep,miss_ratio,mqps,requests" > "$OUT"
echo "writing $OUT"

# trace op_size op_hp  (plus 600 and 1200 at size-matched hp)
CONFIGS=( "msr_proj_0 256 24" "cache-t-00 512 25" "cluster53 128 23" "msr_prxy_1 256 24" )

run_one(){  # bin label trace size hp rep
  local line mr tp rq
  line=$(numactl --membind=0 "_build/$1" "$TR/$3.oracleGeneral" "$4" "$5" 1 0 2>/dev/null | tail -1)
  mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
  tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
  rq=$(echo "$line" | grep -oE '[0-9]+ requests' | awk '{print $1}')
  echo "$3,$2,$4,$5,$6,${mr:-NA},${tp:-NA},${rq:-NA}" >> "$OUT"
  echo "  $3 $2 sz=$4 hp=$5 rep=$6 -> mr=${mr:-NA} tp=${tp:-NA}"
}

for cfg in "${CONFIGS[@]}"; do
  read -r tr op_sz op_hp <<< "$cfg"
  for pair in "1200 26" "600 25" "$op_sz $op_hp"; do
    read -r sz hp <<< "$pair"
    echo "=== $tr sz=${sz}MB hp=$hp ==="
    for r in $(seq 1 $REPS); do run_one s3fifo        s3fifo        "$tr" "$sz" "$hp" "$r"; done
    for r in $(seq 1 $REPS); do run_one s3fifoforgive s3fifoforgive "$tr" "$sz" "$hp" "$r"; done
  done
done
echo "DONE -> $OUT"
