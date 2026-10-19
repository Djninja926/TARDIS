#!/bin/bash
# node_trace_worker.sh <trace-list-file> <output-csv>
#
# Streaming per-node worker. For each trace URL in the list:
#   download .zst -> decompress -> delete .zst -> measure footprint (traceAnalyzer)
#   -> compute 10% and 0.1% footprint cache sizes -> run t=1 sweep -> record
#   -> delete .oracleGeneral -> next.
# Only one trace on disk at a time (bounded disk). Restartable: skips traces
# already present in the output CSV.
#
# FIRST PASS = single-thread (t=1) only, per Jane. LRU + LRUForgive + S3FIFO +
# S3FIFOForgive at t=1, at two cache sizes:
#   large = 10%  of trace footprint (sum of unique object sizes)
#   small = 0.1% of trace footprint  (skipped if 0.1% * n_objects < 1000)
# Thread-scaling is a separate later phase (S3FIFO family crashes MT on real traces).

set -uo pipefail

LIST="${1:?trace list file (one URL per line)}"
OUT="${2:?output csv path}"

TARDIS=/mydata/tardis
MYBENCH="$TARDIS/sosp23-s3fifo/cachelib-sosp23/mybench"
BUILD="$MYBENCH/_build"
ANALYZER="$TARDIS/sosp23-s3fifo/libCacheSim/_build/bin/traceAnalyzer"
WORK=/mydata/trace_work
mkdir -p "$WORK"

REPS=3
NODE=$(hostname)

# NUMA pinning: use numactl --membind=0 only if numactl exists AND node 0 is a
# valid membind target on this hardware. Otherwise run without pinning. This
# t=1 pass is miss-ratio-focused, so pinning does not affect correctness, only
# throughput consistency (which matters for the later throughput phase, not here).
if command -v numactl >/dev/null 2>&1 && numactl --membind=0 true >/dev/null 2>&1; then
  NUMACTL="numactl --membind=0"
else
  NUMACTL=""
  echo "NOTE: numactl unavailable or membind=0 invalid on $NODE; running without NUMA pinning"
fi

if [ "$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null)" != "1" ]; then
  echo "WARN: turbo not disabled on $NODE"
fi

if [ ! -f "$OUT" ]; then
  echo "node,trace,policy,cache_pct,size_mb,hp,threads,rep,miss,tp,requests,footprint_mb,n_objects" > "$OUT"
fi

hp_for() {
  # round(log2(mb))+16, CAPPED at 27. Uncapped this hits 30-32 for GB-scale
  # caches (billions of hash buckets) which allocates a huge table and OOMs.
  python3 -c "import math;mb=max(1.0,$1);print(min(27,max(16,int(round(math.log2(mb)))+16)))"
}

# Available RAM in MB; skip any cell whose cache exceeds ~80% of it (would OOM).
RAM_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
RAM_CAP_MB=$(python3 -c "print(int($RAM_MB*0.80))")
echo "NOTE: node RAM=${RAM_MB}MB, cache size cap=${RAM_CAP_MB}MB (larger cells skipped)"

run_cell() {  # policy trace_name trace_path cache_pct size_mb n_obj footprint_mb
  local pol=$1 name=$2 path=$3 pct=$4 sz=$5 nobj=$6 fp=$7
  local hp; hp=$(hp_for "$sz")
  local szi; szi=$(python3 -c "print(int(round($sz)))")
  [ "$szi" -lt 1 ] && szi=1
  # skip cells whose cache exceeds available RAM (they OOM / hang)
  if [ "$szi" -gt "$RAM_CAP_MB" ]; then
    for rep in $(seq 1 $REPS); do
      echo "$NODE,$name,$pol,$pct,$szi,$hp,1,$rep,NA_TOOBIG,NA,NA,$fp,$nobj" >> "$OUT"
    done
    return
  fi
  for rep in $(seq 1 $REPS); do
    local line mr tp rq
    line=$(timeout 2400 $NUMACTL "$BUILD/$pol" "$path" "$szi" "$hp" 1 0 2>/dev/null | tail -1)
    mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
    tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
    rq=$(echo "$line" | grep -oE '[0-9]+ requests' | awk '{print $1}')
    echo "$NODE,$name,$pol,$pct,$szi,$hp,1,$rep,${mr:-NA},${tp:-NA},${rq:-NA},$fp,$nobj" >> "$OUT"
  done
}

while IFS= read -r url; do
  url="${url%$'\r'}"   # strip trailing CR if the shard file has Windows CRLF endings
  [ -z "$url" ] && continue
  case "$url" in \#*) continue;; esac

  # Filenames vary: name.oracleGeneral.zst, name.oracleGeneral.bin.zst
  # (cloudphysics), name.oracleGeneral.sample10.zst (twitter samples). Derive a
  # clean trace name = everything before ".oracleGeneral", and keep the full
  # remote basename for the local .zst so wget/zstd handle any variant.
  remote_base=$(basename "$url")                      # e.g. w01.oracleGeneral.bin.zst
  name="${remote_base%%.oracleGeneral*}"              # e.g. w01
  if grep -q ",$name," "$OUT" 2>/dev/null; then
    echo "[$NODE] SKIP $name (done)"; continue
  fi
  echo "[$NODE] === $name ==="

  zst="$WORK/$remote_base"                             # keep full remote name for the .zst
  trace="$WORK/$name.oracleGeneral"                    # decompress to a clean name

  wget -q -O "$zst" "$url" || { echo "[$NODE] DL FAIL $name"; rm -f "$zst"; continue; }
  zstd -d -q --rm "$zst" -o "$trace" || { echo "[$NODE] UNZIP FAIL $name"; rm -f "$zst" "$trace"; continue; }

  stats=$("$ANALYZER" "$trace" oracleGeneral 2>/dev/null | head -5)
  nobj=$(echo "$stats" | grep -oE 'number of objects: [0-9]+' | grep -oE '[0-9]+' | head -1)
  objgib=$(echo "$stats" | grep -oE 'number of obj GiB: [0-9.]+' | grep -oE '[0-9.]+' | head -1)
  if [ -z "${nobj:-}" ] || [ -z "${objgib:-}" ]; then
    echo "[$NODE] FOOTPRINT PARSE FAIL $name"; rm -f "$trace"; continue
  fi
  footprint_mb=$(python3 -c "print($objgib*1024)")
  large_mb=$(python3 -c "print($footprint_mb*0.10)")
  small_mb=$(python3 -c "print($footprint_mb*0.001)")
  small_objs=$(python3 -c "print($nobj*0.001)")
  echo "[$NODE]   footprint=${footprint_mb}MB nobj=$nobj large=${large_mb}MB small=${small_mb}MB"

  for pol in lru lruforgive s3fifo s3fifoforgive; do
    run_cell "$pol" "$name" "$trace" 10 "$large_mb" "$nobj" "$footprint_mb"
  done
  if python3 -c "import sys; sys.exit(0 if $small_objs >= 1000 else 1)"; then
    for pol in lru lruforgive s3fifo s3fifoforgive; do
      run_cell "$pol" "$name" "$trace" 0.1 "$small_mb" "$nobj" "$footprint_mb"
    done
  else
    echo "[$NODE]   SKIP small size ($small_objs objs < 1000)"
  fi

  rm -f "$trace"
  echo "[$NODE] DONE $name"
done < "$LIST"

echo "[$NODE] ALL DONE -> $OUT"
