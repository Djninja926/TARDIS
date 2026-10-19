#!/bin/bash
# subsample_all_traces.sh <branch-label>
# LRUForgive subsampling sweep across all traces + thread counts.
# Mode 0 (skip-whole, the good variant). Rates 1.0/0.5/0.25/0.1.
# 5 reps per cell, reports all reps (take median in analysis).
# Captures BOTH miss ratio and throughput.
# Run AFTER checkout + build of the target branch. Does not switch git.
set -uo pipefail
cd /mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench

LABEL="${1:?pass branch label, e.g. perthread or single-manager}"
TR=/mydata/tardis/traces

# guards
[ "$(cat /sys/devices/system/cpu/intel_pstate/no_turbo)" = "1" ] || { echo "ABORT: turbo on"; exit 1; }

# Explicitly set ALL TARDIS env flags to their DEFAULT values, so this run's
# config is self-documenting and immune to whatever is in the shell. These are
# the defaults every headline number was measured under. SAMPLE_* are overridden
# per-run below (that's the point of this sweep).
export TARDIS_RECENCY_SHARDS=1     # 1 = recency-reorder OFF (inline baseline)
export TARDIS_RECENCY_MODE=0       # 0 = interleave (inert when SHARDS=1)
export TARDIS_APPLIER_CORE=-1      # -1 = no applier pinning
export TARDIS_S3FF_KEEP_IN_SMALL=0 # 0 = promote-to-main (the chosen S3FF design)
export TARDIS_S3FF_MARK_ACCESSED=0 # 0 = promote-to-main
unset TARDIS_SAMPLE_RATE TARDIS_SAMPLE_MODE  # set per-run below

OUT=/mydata/tardis/results/subsample_all_traces_${LABEL}_$(date +%Y%m%d_%H%M%S).csv
echo "branch,trace,mode,rate,threads,rep,miss,tp" > "$OUT"
echo "writing $OUT (label=$LABEL)"

# trace -> "base_size base_hp" at operating point (smallest size only, to keep runtime sane)
declare -A BASE=( [cache-t-00]="512 25" [msr_proj_0]="256 24" [cluster53]="128 23" [msr_prxy_1]="256 24" )

REPS=5
THREADS="1 4 8 16"        # skip t=2 (broken baseline); 1/4/8/16 = full scaling curve
RATES="1.0 0.5 0.25 0.1"
MODE=0                     # skip-whole (the good variant)

log2() { python3 -c "import math;print(int(round(math.log2($1))))"; }

for tr in cache-t-00 msr_proj_0 cluster53 msr_prxy_1; do
  read -r bsz bhp <<< "${BASE[$tr]}"
  echo "=== $tr (base $bsz/$bhp) ==="
  for t in $THREADS; do
    sz=$((bsz * t)); hp=$((bhp + $(log2 $t)))
    for rate in $RATES; do
      for rep in $(seq 1 $REPS); do
        line=$(TARDIS_SAMPLE_MODE=$MODE TARDIS_SAMPLE_RATE=$rate \
               timeout 1800 numactl --membind=0 _build/lruforgive \
               "$TR/$tr.oracleGeneral" "$sz" "$hp" "$t" 0 2>/dev/null | tail -1)
        mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
        tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
        echo "$LABEL,$tr,$MODE,$rate,$t,$rep,${mr:-NA},${tp:-NA}" >> "$OUT"
        echo "  $tr t=$t rate=$rate rep=$rep -> mr=${mr:-NA} tp=${tp:-NA}"
      done
    done
  done
done
echo "DONE -> $OUT"
