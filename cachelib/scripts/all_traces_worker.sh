#!/usr/bin/env bash
# all_traces_worker.sh LIST OUT.csv - the CacheLib all-traces sweep on one node
# (result: ../result/all_traces_t1.csv).
#
# LIST is a trace list ("<dataset>\t<url>" or bare URLs, as libcachesim/traces.tsv);
# give each node its own slice and start one worker per node in tmux.
# Per trace: download, decompress, footprint (traceAnalyzer), then all four
# policies at 1 thread, REPS times, at 10% and 0.1% of the footprint in bytes,
# then delete the trace. One trace on disk at a time; restartable (a trace
# already in OUT is skipped, by name, so a sampled copy that shares a name with
# a full trace is skipped too).
#   - 0.1% is skipped when it would hold fewer than 1,000 objects
#   - a cell whose cache exceeds 80% of RAM is recorded NA_TOOBIG
#   - hashpower = round(log2(MB)) + 16, between 16 and 27 (above 27 the table alone OOMs)
set -uo pipefail
LIST="${1:?trace list}"
OUT="${2:?output csv}"
ROOT=/mydata/tardis
BUILD="$ROOT/sosp23-s3fifo/cachelib-sosp23/mybench/_build"
ANALYZER="$ROOT/sosp23-s3fifo/libCacheSim/_build/bin/traceAnalyzer"
WORK=/mydata/trace_work
REPS="${REPS:-3}"
NODE=$(hostname)
mkdir -p "$WORK" "$(dirname "$OUT")"

NUMA=""; command -v numactl >/dev/null && numactl --membind=0 true 2>/dev/null && NUMA="numactl --membind=0"
[ "$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null)" = "1" ] || echo "WARN: turbo on ($NODE)"
[ -f "$OUT" ] || echo "node,trace,policy,cache_pct,size_mb,hp,threads,rep,miss,tp,requests,footprint_mb,n_objects" > "$OUT"
RAM_CAP_MB=$(awk '/MemTotal/{print int($2 / 1024 * 0.8)}' /proc/meminfo)

run_cell() {  # policy name path pct size_mb n_obj footprint_mb
  local pol=$1 name=$2 path=$3 pct=$4 nobj=$6 fp=$7 sz hp rep out rc line mr tp rq
  sz=$(python3 -c "print(max(1, int(round($5))))")
  hp=$(python3 -c "import math; print(min(27, max(16, int(round(math.log2(max(1.0, $5)))) + 16)))")
  for rep in $(seq 1 "$REPS"); do
    if [ "$sz" -gt "$RAM_CAP_MB" ]; then
      echo "$NODE,$name,$pol,$pct,$sz,$hp,1,$rep,NA_TOOBIG,NA,NA,$fp,$nobj" >> "$OUT"; continue
    fi
    out=$(timeout 2400 $NUMA "$BUILD/$pol" "$path" "$sz" "$hp" 1 0 2>/dev/null); rc=$?
    line=$(tail -1 <<< "$out")
    mr=$(grep -oE 'miss ratio [0-9.]+' <<< "$line" | awk '{print $3}')
    tp=$(grep -oE 'throughput [0-9.]+' <<< "$line" | awk '{print $2}')
    rq=$(grep -oE '[0-9]+ requests' <<< "$line" | awk '{print $1}')
    [ "$rc" -ne 0 ] && mr=""          # a crashed run ends on a checkpoint, not the result
    echo "$NODE,$name,$pol,$pct,$sz,$hp,1,$rep,${mr:-NA},${tp:-NA},${rq:-NA},$fp,$nobj" >> "$OUT"
  done
}

while IFS= read -r line; do
  line="${line%$'\r'}"; [ -z "$line" ] && continue; case "$line" in \#*) continue ;; esac
  url="${line##*$'\t'}"
  remote=$(basename "$url"); name="${remote%%.oracleGeneral*}"
  grep -q "^[^,]*,$name," "$OUT" && { echo "[$NODE] SKIP $name"; continue; }
  case "$remote" in *.oracleGeneral*) ;; *) echo "[$NODE] SKIP $name (not oracleGeneral)"; continue ;; esac
  echo "[$NODE] === $name"
  zst="$WORK/$remote"; trace="$WORK/$name.oracleGeneral"
  wget -q -O "$zst" "$url" || { echo "[$NODE] DL FAIL $name"; rm -f "$zst"; continue; }
  zstd -d -q --rm "$zst" -o "$trace" || { echo "[$NODE] UNZIP FAIL $name"; rm -f "$zst" "$trace"; continue; }
  stats=$("$ANALYZER" "$trace" oracleGeneral 2>/dev/null | head -5)
  nobj=$(grep -oE 'number of objects: [0-9]+' <<< "$stats" | grep -oE '[0-9]+' | head -1)
  gib=$(grep -oE 'number of obj GiB: [0-9.]+' <<< "$stats" | grep -oE '[0-9.]+' | head -1)
  [ -n "$nobj" ] && [ -n "$gib" ] || { echo "[$NODE] FOOTPRINT FAIL $name"; rm -f "$trace"; continue; }
  fp=$(python3 -c "print($gib * 1024)")
  for pol in lru lruforgive s3fifo s3fifoforgive; do
    run_cell "$pol" "$name" "$trace" 10 "$(python3 -c "print($fp * 0.1)")" "$nobj" "$fp"
  done
  if python3 -c "import sys; sys.exit(0 if $nobj * 0.001 >= 1000 else 1)"; then
    for pol in lru lruforgive s3fifo s3fifoforgive; do
      run_cell "$pol" "$name" "$trace" 0.1 "$(python3 -c "print($fp * 0.001)")" "$nobj" "$fp"
    done
  fi
  rm -f "$trace"
  echo "[$NODE] DONE $name"
done < "$LIST"
echo "[$NODE] ALL DONE -> $OUT"
