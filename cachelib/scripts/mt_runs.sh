#!/usr/bin/env bash
# Multithreaded runs at the confirmed operating points.
#
# METHODOLOGY (settled July 18):
#  - SHARED mode (arg6=1). The banked log-structured curve (doc 14b, 6.9->4.84pp)
#    was shared mode, so this is the comparable configuration. run.sh is REPLICATED
#    (cmd.h: "mode: 0=replicated replay (default)"; run.sh passes only 4 args),
#    which is why run.sh scales cache size by nThread: in replicated mode each
#    thread replays the WHOLE trace and needs proportionally more cache.
#  - FIXED cache size at each operating point. Shared mode PARTITIONS the trace, so
#    total work is constant and total cache should be too. Scaling size with threads
#    would move the trace out of its high-pressure regime and shrink the gap for
#    pressure reasons, contaminating the threading measurement.
#  - SCALED hashpower (base + log2(threads)), per run.sh. Independent of mode.
#  - numactl pinning IF AVAILABLE (optional; script runs without it).
#  - timeout guard: s3fifo has hung at t>=4 in shared mode (one instance burned
#    50 CPU-hours). Never let one cell stall the batch.
#  - lruforgive 3x per cell (jitter +/-0.35pp on proj_0, doc 20.4); baselines 1x.

set -u
BUILD=/mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench/_build
T=/mydata/tardis/traces
OUT=/mydata/tardis/mt_results.csv
TIMEOUT=7200

# numactl is optional: use it if present, otherwise run unpinned.
if command -v numactl >/dev/null 2>&1; then
  NUMA="numactl --cpunodebind=0 --membind=0"
  echo "numactl: enabled"
else
  NUMA=""
  echo "numactl: NOT FOUND, running unpinned (install with: sudo apt-get install -y numactl)"
fi

[ -f "$OUT" ] || echo "trace,algo,threads,hashpower,run,miss_ratio,mqps" > "$OUT"

# A cell counts as done ONLY if it has a numeric miss ratio.
# FAIL/TIMEOUT rows do not block a retry.
have_result() { # trace algo threads hp run
  grep -qE "^$1,$2,$3,$4,$5,[0-9]" "$OUT" 2>/dev/null
}

run_one() { # algo trace size hp threads run_idx
  local algo=$1 trace=$2 size=$3 hp=$4 th=$5 r=$6
  local line rc mr tp
  line=$(timeout "$TIMEOUT" $NUMA "$BUILD/$algo" "$T/$trace" "$size" "$hp" "$th" 1 2>&1 | tail -1)
  rc=$?
  if [ $rc -eq 124 ]; then
    echo "TIMEOUT: $algo $trace t=$th hp=$hp"
    echo "$trace,$algo,$th,$hp,$r,TIMEOUT,TIMEOUT" >> "$OUT"
    pkill -f "_build/$algo" 2>/dev/null
    return
  fi
  mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
  tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
  if [ -z "$mr" ]; then
    echo "FAIL: $algo $trace t=$th hp=$hp | last line: $line"
    echo "$trace,$algo,$th,$hp,$r,FAIL," >> "$OUT"
    return
  fi
  echo "wrote: $trace $algo t=$th hp=$hp run=$r miss=$mr mqps=$tp"
  echo "$trace,$algo,$th,$hp,$r,$mr,$tp" >> "$OUT"
}

for spec in \
  "msr_proj_0.oracleGeneral 256 24" \
  "cache-t-00.oracleGeneral 512 25" \
  "cluster53.oracleGeneral 128 23" \
  "msr_prxy_1.oracleGeneral 256 24"; do
  set -- $spec; trace=$1; size=$2; hp_base=$3
  echo "===== $trace (${size}MB, hp_base=$hp_base, shared mode) ====="
  for th in 1 2 4 8 16; do
    case $th in 1) inc=0;; 2) inc=1;; 4) inc=2;; 8) inc=3;; 16) inc=4;; esac
    hp=$((hp_base + inc))
    have_result "$trace" lru    "$th" "$hp" 1 || run_one lru    "$trace" "$size" "$hp" "$th" 1
    have_result "$trace" s3fifo "$th" "$hp" 1 || run_one s3fifo "$trace" "$size" "$hp" "$th" 1
    for r in 1 2 3; do
      have_result "$trace" lruforgive "$th" "$hp" "$r" || run_one lruforgive "$trace" "$size" "$hp" "$th" "$r"
    done
  done
done
echo "DONE -> $OUT"
