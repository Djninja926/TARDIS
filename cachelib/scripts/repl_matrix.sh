#!/bin/bash
set -uo pipefail
cd /mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench

NT=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null || echo missing)
if [ "$NT" != "1" ]; then echo "ABORT: turbo not disabled (no_turbo=$NT)."; exit 1; fi
echo "turbo OFF; branch=$(cd .. && git rev-parse --abbrev-ref HEAD)"

TR=/mydata/tardis/traces
LF=_build/lruforgive ; LRU=_build/lru
REPS=4
OUT=/mydata/tardis/repl_matrix_$(date +%Y%m%d_%H%M%S).csv
echo "trace,policy,mode,size_base_mb,size_mb,threads,hp,rep,miss_ratio,mqps,requests" > "$OUT"
echo "writing $OUT"

log2t(){ case $1 in 1)echo 0;;2)echo 1;;4)echo 2;;8)echo 3;;16)echo 4;;esac; }

# trace  op_size  op_hpbase
CONFIGS=(
  "msr_proj_0 256 24"
  "cache-t-00 512 25"
  "cluster53  128 23"
  "msr_prxy_1 256 24"
)

run_one(){  # bin label trace size_base hp_base threads rep
  local sz hp line mr tp rq l2
  l2=$(log2t "$6")
  sz=$(( $4 * $6 ))          # replicated: cache scales with threads
  hp=$(( $5 + l2 ))          # hp scales with threads
  line=$(numactl --membind=0 "$1" "$TR/$3.oracleGeneral" "$sz" "$hp" "$6" 0 2>/dev/null | tail -1)
  mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
  tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
  rq=$(echo "$line" | grep -oE '[0-9]+ requests' | awk '{print $1}')
  echo "$3,$2,replicated,$4,$sz,$6,$hp,$7,${mr:-NA},${tp:-NA},${rq:-NA}" >> "$OUT"
  echo "  $3 $2 base=$4 sz=$sz t=$6 hp=$hp rep=$7 -> mr=${mr:-NA} tp=${tp:-NA}"
}

for cfg in "${CONFIGS[@]}"; do
  read -r tr op_sz op_hp <<< "$cfg"
  # (size_base, hp_base): 1200->26, 600->25, operating-point->op_hp
  for pair in "1200 26" "600 25" "$op_sz $op_hp"; do
    read -r base hpb <<< "$pair"
    echo "=== $tr base=${base}MB hp_base=$hpb ==="
    for t in 1 2 4 8 16; do
      for r in $(seq 1 $REPS); do run_one "$LRU" lru       "$tr" "$base" "$hpb" "$t" "$r"; done
      for r in $(seq 1 $REPS); do run_one "$LF"  perthread "$tr" "$base" "$hpb" "$t" "$r"; done
    done
  done
done
echo "DONE -> $OUT"
