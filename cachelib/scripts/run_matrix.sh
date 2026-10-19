#!/usr/bin/env bash
# run_matrix.sh EXPERIMENT [OUT.csv] - every single-node CacheLib experiment.
#
# Runs mybench binaries over traces x cache sizes x policies x thread counts x
# repetitions, one line per run, restartable (a cell with a numeric result is
# skipped). Each EXPERIMENT is a preset of the settings below; any setting can
# be overridden from the environment, e.g.  THREADS="1 16" REPS=1 bash run_matrix.sh final
#
# Experiments (doc = MultithreadedLRUForgive.md; result files in ../result/):
#   final       LRU/LRUForgive at 1-16 threads, S3-FIFO(Forgive) at 1 thread, 3 sizes, replicated mode   (doc 38)
#               run once per fork branch: LABEL=perthread, then LABEL=single-manager (with BASELINES=no)
#   s3ff        S3-FIFO vs S3FIFOForgive, 1 thread, 3 sizes                                               (doc 34, 37)
#   shared      LRU vs LRUForgive, shared mode (fixed cache size), tardis-implementation branch           (doc 31, 33)
#   sizesweep   LRU, S3-FIFO, LRUForgive over an 8 MB - 8 GB ladder, 1 thread (LRUForgive up to 1 GB)    (doc 18, 20)
#   subsample   LRUForgive applying only a fraction of accesses (TARDIS_SAMPLE_MODE/RATE), cache-t-00    (doc 35, 36)
#   bimodality  10 repeats of LRUForgive at 1 thread on three operating points                            (doc 24)
#   cliff       msr_prxy_1 at sizes across its miss-ratio cliff                                           (doc 24.4)
#   zipf        all four policies on the synthetic zipf trace, 1-16 threads (S3-FIFO runs here)           (doc 39)
#   custom      nothing preset; set TRACES and POLICIES yourself
#
# Settings (environment):
#   TRACES      "name:MB:hp[,MB:hp...] ..."  trace (traces/NAME.oracleGeneral), base cache size, base hashpower
#   POLICIES    mybench binaries: lru s3fifo lruforgive s3fifoforgive
#   THREADS     thread counts for lru/lruforgive          S3_THREADS  for s3fifo/s3fifoforgive (default 1:
#               the artifact's S3-FIFO crashes multithreaded on real traces, doc 23)
#   MODE        replicated (every thread replays the whole trace; cache = MB x threads)
#               or shared (threads split the trace; cache stays MB). Hashpower = hp + log2(threads) in both.
#   REPS        repetitions per cell; REPS_<policy> overrides one policy (e.g. REPS_lru=1)
#   VARIANTS    space-separated TARDIS env settings for the forgive policies, each "K=V[,K=V]" (default: "default")
#   BASELINES   no = skip lru and s3fifo (they do not depend on the fork branch)
#   FORGIVE_MAX_MB  skip forgive cells above this cache size
#   LABEL       recorded in every row (default: the fork's branch)
#   TIMEOUT     seconds per run (7200)     ALLOW_TURBO=1 runs with turbo boost on (throughput not comparable)
set -u
EXP="${1:?usage: run_matrix.sh EXPERIMENT [OUT.csv]  (see the top of this file)}"
ROOT=/mydata/tardis
BENCH="${BENCH:-$ROOT/sosp23-s3fifo/cachelib-sosp23/mybench}"
TRACE_DIR="${TRACE_DIR:-$ROOT/traces}"

OPS="cache-t-00:512:25 msr_proj_0:256:24 cluster53:128:23 msr_prxy_1:256:24"   # operating points (doc 20.5)
with_sizes() { local s out=""; for s in $OPS; do out+="$s$1 "; done; echo "$out"; }
LADDER=8:19,16:20,32:21,64:22,128:23,256:24,512:25,1024:26,2048:27,4096:28,8192:29
SIZESWEEP_TRACES="cache-t-00 cluster10 cluster26 cluster45 cluster50 cluster53 2016_LUN0 2016_LUN1
  2016_LUN2 2016_LUN3 msr_hm_0 msr_prn_0 msr_prn_1 msr_proj_0 msr_proj_1 msr_proj_2 msr_proj_4 msr_prxy_0
  msr_prxy_1 msr_src1_0 msr_src1_1 msr_usr_1 msr_usr_2 msr_web_2"
SUBSAMPLE_VARIANTS=""
for m in 0 1 2; do for r in 1.0 0.5 0.25 0.1 0.05 0.01; do
  SUBSAMPLE_VARIANTS+="TARDIS_SAMPLE_MODE=$m,TARDIS_SAMPLE_RATE=$r "
done; done

case "$EXP" in
  final)      : "${TRACES:=$(with_sizes ,600:25,1200:26)}" "${POLICIES:=lru lruforgive s3fifo s3fifoforgive}"
              : "${MODE:=replicated}" "${REPS:=3}" ;;
  s3ff)       : "${TRACES:=$(with_sizes ,600:25,1200:26)}" "${POLICIES:=s3fifo s3fifoforgive}"
              : "${MODE:=replicated}" "${REPS:=4}" ;;
  shared)     : "${TRACES:=$(with_sizes ,600:25,1200:26)}" "${POLICIES:=lru lruforgive}"
              : "${MODE:=shared}" "${REPS:=2}" ;;
  sizesweep)  t=""; for n in $SIZESWEEP_TRACES; do t+="$n:${LADDER//,/,} "; done
              : "${TRACES:=$t}" "${POLICIES:=lru s3fifo lruforgive}" "${THREADS:=1}"
              : "${MODE:=shared}" "${REPS:=1}" "${FORGIVE_MAX_MB:=1024}" ;;
  subsample)  : "${TRACES:=cache-t-00:512:25}" "${POLICIES:=lru lruforgive}" "${THREADS:=1}"
              : "${MODE:=replicated}" "${REPS:=3}" "${VARIANTS:=$SUBSAMPLE_VARIANTS}" ;;
  bimodality) : "${TRACES:=msr_proj_0:256:24 cluster53:128:23 cache-t-00:512:25}" "${POLICIES:=lruforgive}"
              : "${THREADS:=1}" "${MODE:=shared}" "${REPS:=10}" ;;
  cliff)      : "${TRACES:=msr_prxy_1:200:24,256:24,320:24,400:25,800:26}" "${POLICIES:=lru lruforgive}"
              : "${THREADS:=1}" "${MODE:=shared}" "${REPS:=5}" "${REPS_lru:=1}" ;;
  zipf)       : "${TRACES:=zipf:4000:21}" "${POLICIES:=lru lruforgive s3fifo s3fifoforgive}"
              : "${S3_THREADS:=1 2 4 8 16}" "${MODE:=replicated}" "${REPS:=5}" ;;
  custom)     [ -n "${TRACES:-}" ] && [ -n "${POLICIES:-}" ] || { echo "custom needs TRACES and POLICIES"; exit 1; } ;;
  *)          echo "unknown experiment $EXP"; sed -n 9,19p "$0"; exit 1 ;;
esac
: "${THREADS:=1 2 4 8 16}" "${S3_THREADS:=1}" "${MODE:=replicated}" "${REPS:=3}" "${VARIANTS:=default}"
: "${BASELINES:=yes}" "${FORGIVE_MAX_MB:=0}" "${TIMEOUT:=7200}"
: "${LABEL:=$(git -C "$BENCH/.." rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)}"
OUT="${2:-$ROOT/results/${EXP}_${LABEL}.csv}"

case "$MODE" in replicated) MODE_ARG=0 ;; shared) MODE_ARG=1 ;; *) echo "MODE must be replicated or shared"; exit 1 ;; esac
if [ "$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null)" != "1" ] && [ "${ALLOW_TURBO:-0}" != 1 ]; then
  echo "ABORT: turbo boost is on (bash $BENCH/turboboost.sh disable), or set ALLOW_TURBO=1"; exit 1
fi
NUMA=""; command -v numactl >/dev/null && numactl --membind=0 true 2>/dev/null && NUMA="numactl --membind=0"

# every TARDIS switch at its default, so a stray setting in the shell cannot leak in
export TARDIS_RECENCY_SHARDS=1 TARDIS_RECENCY_MODE=0 TARDIS_APPLIER_CORE=-1
export TARDIS_S3FF_KEEP_IN_SMALL=0 TARDIS_S3FF_MARK_ACCESSED=0
unset TARDIS_SAMPLE_RATE TARDIS_SAMPLE_MODE

mkdir -p "$(dirname "$OUT")"
[ -f "$OUT" ] || echo "label,trace,policy,mode,size_mb,hp,threads,rep,variant,miss,tp,requests,runtime_s" > "$OUT"
echo "$EXP -> $OUT  (label=$LABEL mode=$MODE reps=$REPS numactl=${NUMA:-off})"

log2() { local n=$1 l=0; while [ "$n" -gt 1 ]; do n=$((n / 2)); l=$((l + 1)); done; echo $l; }

run_cell() {  # policy trace size hp threads rep variant
  local pol=$1 tr=$2 sz=$3 hp=$4 t=$5 rep=$6 var=$7 key line rc mr tp rq rt
  key="$LABEL,$tr,$pol,$MODE,$sz,$hp,$t,$rep,${var//,/;},"
  grep -qF -- "$key" "$OUT" && grep -F -- "$key" "$OUT" | grep -qE ",[0-9.]+,[^,]*,[^,]*,[^,]*$" && return
  local envs=(); [ "$var" != default ] && IFS=, read -ra envs <<< "$var"
  env ${envs[@]+"${envs[@]}"} timeout "$TIMEOUT" $NUMA "$BENCH/_build/$pol" "$TRACE_DIR/$tr.oracleGeneral" \
      "$sz" "$hp" "$t" "$MODE_ARG" > "$OUT.run" 2>/dev/null; rc=$?
  line=$(tail -1 "$OUT.run"); rm -f "$OUT.run"
  mr=$(grep -oE 'miss ratio [0-9.]+' <<< "$line" | awk '{print $3}')
  tp=$(grep -oE 'throughput [0-9.]+' <<< "$line" | awk '{print $2}')
  rq=$(grep -oE '[0-9]+ requests' <<< "$line" | awk '{print $1}')
  rt=$(grep -oE 'runtime [0-9.]+' <<< "$line" | awk '{print $2}')
  # mybench prints a miss ratio at every checkpoint, so a run that crashes part
  # way (S3-FIFO multithreaded, doc 39) still ends with a number: trust it only
  # when the run exited cleanly
  if [ "$rc" -ne 0 ] || [ -z "$mr" ]; then
    case $rc in 124) mr=TIMEOUT ;; 0) mr=FAIL ;; *) mr=FAIL_exit$rc ;; esac
    pkill -f "_build/$pol" 2>/dev/null
  fi
  echo "$key$mr,${tp:-NA},${rq:-NA},${rt:-NA}" >> "$OUT"
  echo "  $tr $pol ${sz}MB hp=$hp t=$t rep=$rep $var -> miss=$mr tp=${tp:-NA}"
}

for spec in $TRACES; do
  tr=${spec%%:*}
  [ -f "$TRACE_DIR/$tr.oracleGeneral" ] || { echo "[missing] $TRACE_DIR/$tr.oracleGeneral (get_traces.sh $tr)"; continue; }
  IFS=, read -ra sizes <<< "${spec#*:}"
  for s in "${sizes[@]}"; do
    base=${s%%:*}; hpb=${s#*:}
    for pol in $POLICIES; do
      case $pol in lru|s3fifo) [ "$BASELINES" = no ] && continue; vars=default ;; *) vars=$VARIANTS ;; esac
      case $pol in s3fifo*) ths=$S3_THREADS ;; *) ths=$THREADS ;; esac
      rv="REPS_$pol"; reps=${!rv:-$REPS}
      for t in $ths; do
        hp=$((hpb + $(log2 "$t")))
        sz=$base; [ "$MODE" = replicated ] && sz=$((base * t))
        case $pol in *forgive) [ "$FORGIVE_MAX_MB" -gt 0 ] && [ "$sz" -gt "$FORGIVE_MAX_MB" ] && continue ;; esac
        for var in $vars; do
          for rep in $(seq 1 "$reps"); do run_cell "$pol" "$tr" "$sz" "$hp" "$t" "$rep" "$var"; done
        done
      done
    done
  done
done
echo "DONE -> $OUT"
