#!/bin/bash
# Three-way verification on cache-t-00 at 600MB and 512MB:
#   lru     = _build/lru              (baseline)
#   backoff = /tmp/lruforgive.backoff (new: SIMD norm-caching + tuned backoff)
#   presimd = _build/lruforgive       (old: pre-SIMD Option B + Stage 1 batching)
# Each runs 5x per (size, thread) cell. Writes a CSV for exact means/spreads.
set -uo pipefail
cd /mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench

NT=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null || echo missing)
if [ "$NT" != "1" ]; then echo "ABORT: turbo not disabled (no_turbo=$NT)."; exit 1; fi
echo "turbo OFF (no_turbo=1); governor=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)"

TR=/mydata/tardis/traces
TRACE=cache-t-00
BASE_HP=25
LRU=_build/lru
BACKOFF=/tmp/lruforgive.backoff
PRESIMD=_build/lruforgive
REPS=5
PIN=""; command -v numactl >/dev/null && PIN="numactl --cpunodebind=0 --membind=0"

for b in "$LRU" "$BACKOFF" "$PRESIMD"; do
  if [ ! -x "$b" ]; then echo "ABORT: missing or non-executable binary: $b"; exit 1; fi
done
if cmp -s "$BACKOFF" "$PRESIMD"; then
  echo "ABORT: $PRESIMD is identical to $BACKOFF. Rebuild the pre-SIMD version first."; exit 1
fi

OUT=/mydata/tardis/verify_$(date +%Y%m%d_%H%M%S).csv
echo "size_mb,policy,threads,hp,rep,miss_ratio,mqps,runtime_s" > "$OUT"
echo "writing $OUT"

log2t(){ case $1 in 1)echo 0;;2)echo 1;;4)echo 2;;8)echo 3;;16)echo 4;;esac; }

run_one(){  # bin label size hp threads rep
  local line mr tp rt
  line=$($PIN "$1" "$TR/$TRACE.oracleGeneral" "$3" "$4" "$5" 1 2>/dev/null | tail -1)
  mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
  tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
  rt=$(echo "$line" | grep -oE 'runtime [0-9.]+' | awk '{print $2}')
  echo "$3,$2,$5,$4,$6,${mr:-NA},${tp:-NA},${rt:-NA}" >> "$OUT"
  echo "  ${3}MB $2 t=$5 hp=$4 rep=$6 -> mr=${mr:-NA} tp=${tp:-NA} rt=${rt:-NA}"
}

for SZ in 4000 600 512; do
  echo "=== cache size ${SZ}MB ==="
  for grp in "lru $LRU" "backoff $BACKOFF" "presimd $PRESIMD"; do
    read -r label bin <<< "$grp"
    echo "--- ${label} ---"
    for t in 1 2 4 8 16; do
      hp=$(( BASE_HP + $(log2t "$t") ))
      for r in $(seq 1 "$REPS"); do run_one "$bin" "$label" "$SZ" "$hp" "$t" "$r"; done
    done
  done
done
echo "DONE -> $OUT"
