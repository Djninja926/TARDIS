#!/bin/bash
set -uo pipefail
cd /mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench

NT=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null || echo missing)
if [ "$NT" != "1" ]; then echo "ABORT: turbo not disabled (no_turbo=$NT)."; exit 1; fi
BR=$(cd .. && git rev-parse --abbrev-ref HEAD)
echo "turbo OFF; branch=$BR (must be tardis-implementation for shared mode)"

TR=/mydata/tardis/traces
LF=_build/lruforgive ; LRU=_build/lru
REPS=2
OUT=/mydata/tardis/shared_matrix_$(date +%Y%m%d_%H%M%S).csv
echo "trace,policy,mode,size_mb,threads,hp,rep,miss_ratio,mqps,requests" > "$OUT"
echo "writing $OUT"

log2t(){ case $1 in 1)echo 0;;2)echo 1;;4)echo 2;;8)echo 3;;16)echo 4;;esac; }

# trace op_size op_hpbase ; shared mode: cache CONSTANT, hp scales with threads
CONFIGS=( "msr_proj_0 256 24" "cache-t-00 512 25" "cluster53 128 23" "msr_prxy_1 256 24" )

run_one(){  # bin label trace size hp_base threads rep
  local hp line mr tp rq
  hp=$(( $5 + $(log2t "$6") ))
  line=$(numactl --membind=0 "$1" "$TR/$3.oracleGeneral" "$4" "$hp" "$6" 1 2>/dev/null | tail -1)
  mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
  tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
  rq=$(echo "$line" | grep -oE '[0-9]+ requests' | awk '{print $1}')
  echo "$3,$2,shared,$4,$6,$hp,$7,${mr:-NA},${tp:-NA},${rq:-NA}" >> "$OUT"
  echo "  $3 $2 sz=$4 t=$6 hp=$hp rep=$7 -> mr=${mr:-NA} tp=${tp:-NA} req=${rq:-NA}"
}

for cfg in "${CONFIGS[@]}"; do
  read -r tr op_sz op_hp <<< "$cfg"
  for size in 1200 600 "$op_sz"; do
    # hp_base per size: 1200->26, 600->25, operating-point->op_hp
    if [ "$size" = "1200" ]; then hpb=26; elif [ "$size" = "600" ]; then hpb=25; else hpb=$op_hp; fi
    echo "=== $tr sz=${size}MB hp_base=$hpb (shared, constant) ==="
    for t in 1 2 4 8 16; do
      for r in $(seq 1 $REPS); do run_one "$LRU" lru        "$tr" "$size" "$hpb" "$t" "$r"; done
      for r in $(seq 1 $REPS); do run_one "$LF"  logbackoff "$tr" "$size" "$hpb" "$t" "$r"; done
    done
  done
done
echo "DONE -> $OUT"
