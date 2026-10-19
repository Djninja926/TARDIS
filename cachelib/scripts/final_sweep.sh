#!/bin/bash
# Usage: final_sweep.sh <branch-label> <with-baselines: yes|no>
#   with-baselines=yes  -> also run LRU + S3FIFO (branch-independent, run once)
#   with-baselines=no   -> only LRUForgive + S3FIFOForgive
# Run AFTER checking out + building the branch. Does not switch git.
set -uo pipefail
cd /mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench
LABEL="${1:?branch label}"
BASELINES="${2:?yes or no}"
TR=/mydata/tardis/traces

# guards
[ "$(cat /sys/devices/system/cpu/intel_pstate/no_turbo)" = "1" ] || { echo "ABORT: turbo on"; exit 1; }
for v in TARDIS_RECENCY_SHARDS TARDIS_RECENCY_MODE TARDIS_APPLIER_CORE \
         TARDIS_SAMPLE_RATE TARDIS_SAMPLE_MODE TARDIS_S3FF_KEEP_IN_SMALL TARDIS_S3FF_MARK_ACCESSED; do
  if [ -n "${!v:-}" ]; then echo "ABORT: $v is set ($v=${!v}); unset it"; exit 1; fi
done

OUT=/mydata/tardis/final_sweep_${LABEL}_$(date +%Y%m%d_%H%M%S).csv
echo "branch,trace,policy,size_mb,hp,threads,rep,miss,tp,requests" > "$OUT"
echo "writing $OUT (label=$LABEL, baselines=$BASELINES)"

# trace -> "base_size base_hp" per size; sizes: op-point, 600, 1200
# hp_base per size: round(log2(mb))+16 -> 128:23 256:24 512:25 600:25 1200:26
declare -A TRACES=(
  [cache-t-00]="512:25 600:25 1200:26"
  [msr_proj_0]="256:24 600:25 1200:26"
  [cluster53]="128:23 600:25 1200:26"
  [msr_prxy_1]="256:24 600:25 1200:26"
)
REPS=3
FORGIVE_THREADS="1 2 4 8 16"
BASE_LRU_THREADS="1 2 4 8 16"
S3_THREADS="1"   # S3FIFO/S3FIFOForgive: t=1 only (MT defect)

log2() { python3 -c "import math;print(int(round(math.log2($1))))"; }

run () {  # policy trace base_sz base_hp t
  local pol=$1 tr=$2 bsz=$3 bhp=$4 t=$5
  local sz=$((bsz * t))
  local hp=$((bhp + $(log2 $t)))
  for rep in $(seq 1 $REPS); do
    local line mr tp rq
    line=$(timeout 1800 numactl --membind=0 "_build/$pol" "$TR/$tr.oracleGeneral" "$sz" "$hp" "$t" 0 2>/dev/null | tail -1)
    mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
    tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
    rq=$(echo "$line" | grep -oE '[0-9]+ requests' | awk '{print $1}')
    echo "$LABEL,$tr,$pol,$sz,$hp,$t,$rep,${mr:-NA},${tp:-NA},${rq:-NA}" >> "$OUT"
    echo "  $pol $tr sz=$sz hp=$hp t=$t rep=$rep -> mr=${mr:-NA} tp=${tp:-NA}"
  done
}

for tr in cache-t-00 msr_proj_0 cluster53 msr_prxy_1; do
  for spec in ${TRACES[$tr]}; do
    bsz=${spec%:*}; bhp=${spec#*:}
    echo "=== $tr base_sz=$bsz base_hp=$bhp ==="
    # forgiveness policies (per-branch)
    for t in $FORGIVE_THREADS; do run lruforgive $tr $bsz $bhp $t; done
    for t in $S3_THREADS;       do run s3fifoforgive $tr $bsz $bhp $t; done
    # baselines (once, only if requested)
    if [ "$BASELINES" = "yes" ]; then
      for t in $BASE_LRU_THREADS; do run lru $tr $bsz $bhp $t; done
      for t in $S3_THREADS;       do run s3fifo $tr $bsz $bhp $t; done
    fi
  done
done
echo "DONE -> $OUT"
