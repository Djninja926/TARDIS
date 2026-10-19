#!/bin/bash
set -uo pipefail
cd /mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench

NT=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null || echo missing)
if [ "$NT" != "1" ]; then echo "ABORT: turbo not disabled (no_turbo=$NT)."; exit 1; fi
echo "turbo OFF; governor=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)"

TR=/mydata/tardis/traces
LF=_build/lruforgive ; LRU=_build/lru
PIN=""; command -v numactl >/dev/null && PIN="numactl --cpunodebind=0 --membind=0"
REPS=5
OUT=/mydata/tardis/shared_mt_$(date +%Y%m%d_%H%M%S).csv
echo "trace,policy,threads,hp,rep,miss_ratio,mqps,runtime_s,requests" > "$OUT"
echo "writing $OUT"

# trace  sizeMB  base_hp
CONFIGS=( "msr_proj_0 256 24" "cache-t-00 512 25" "cluster53 128 23" "msr_prxy_1 256 24" )
log2t(){ case $1 in 1)echo 0;;2)echo 1;;4)echo 2;;8)echo 3;;16)echo 4;;esac; }

run_one(){  # bin label trace size hp threads rep
  local line mr tp rt rq
  line=$($PIN "$1" "$TR/$3.oracleGeneral" "$4" "$5" "$6" 1 2>/dev/null | tail -1)  # mode 1 = shared
  mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
  tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
  rt=$(echo "$line" | grep -oE 'runtime [0-9.]+' | awk '{print $2}')
  rq=$(echo "$line" | grep -oE '[0-9]+ requests' | awk '{print $1}')
  echo "$3,$2,$6,$5,$7,${mr:-NA},${tp:-NA},${rt:-NA},${rq:-NA}" >> "$OUT"
  echo "  $3 $2 t=$6 hp=$5 rep=$7 -> mr=${mr:-NA} tp=${tp:-NA} req=${rq:-NA}"
}

for cfg in "${CONFIGS[@]}"; do
  read -r tr sz base_hp <<< "$cfg"
  echo "=== $tr @${sz}MB ==="
  for t in 1 2 4 8 16; do
    hp=$(( base_hp + $(log2t "$t") ))
    for r in $(seq 1 "$REPS"); do run_one "$LRU" lru "$tr" "$sz" "$hp" "$t" "$r"; done
    for r in $(seq 1 "$REPS"); do run_one "$LF"  lruforgive "$tr" "$sz" "$hp" "$t" "$r"; done
  done
done
echo "DONE -> $OUT"
