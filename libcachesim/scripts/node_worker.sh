#!/bin/bash
# node_worker.sh - everything that runs on a node.
#
#   bash node_worker.sh LIST OUT.csv          sweep: every trace in LIST, all policies and sizes
#   bash node_worker.sh --tune SHARD OUT.csv  tuning studies (tune.py run)
#   bash node_worker.sh --convert IN.gz OUT   one Wikimedia TSV.gz -> oracleGeneral
#
# orchestrate.py starts the first two in tmux; nobody needs to call this by hand.
#
# SWEEP, per trace: download -> (convert Wikimedia / decompress) -> baselines at
# every size -> forgive policies per size -> append rows -> delete the trace.
# One trace on disk at a time. Restartable: a trace already in OUT (any row that
# is not a transient failure) is skipped.
#
# EMBEDDING CAP. The sketch table holds EMB_MULT (5) x the number of objects the
# cache holds. A cache of fraction f of the working set holds f * wss_obj
# objects, so cap = EMB_MULT * f * wss_obj, passed as an absolute count (the
# policy's "Nx" form multiplies by bytes, not objects). The cap differs per size
# and --eviction-params is global to a call, so: one baseline call at
# fractional sizes (which also logs the working set), then one forgive call per
# size, with that size in absolute bytes.
#
# SMALL-CACHE SKIP: forgive cells are recorded as NA_SMALL when the cache would
# hold fewer than MIN_CACHE_OBJS objects (S3-FIFO paper rule). Baselines still run.
#
# Settings (environment; orchestrate.py --env NAME=VALUE):
#   SIZES            cache sizes as fractions of the working set (0.1,0.01,0.001)
#   EMB_MULT         sketch table, multiple of cache capacity in objects (5)
#   MIN_CACHE_OBJS   forgive cells skipped below this many objects (1000)
#   PARAMS           forgive parameters for every trace, e.g. lr=0.1,ctx-speed=1e-6
#                    (max-emb-entries here overrides the cap; -1 = unlimited)
#   PARAMS_FILE      per-dataset parameters, JSON from tune.py params; the two
#                    forgive policies then run as separate calls side by side
#   CELL_TIMEOUT     seconds per simulator call (14400)
#   NUM_THREAD, FORGIVE_THREADS   simulator threads for baselines / forgive (4 / 2)
#   TUNE_PAR_TRIALS, TUNE_PAR_TRACES  tuning parallelism (4 / 4)
set -uo pipefail

TARDIS="${TARDIS:-/mydata/tardis}"
SCRIPTS="${SCRIPTS:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
COMMON="$SCRIPTS/common.py"

if [ -n "${CACHESIM:-}" ]; then :
elif [ -f "$TARDIS/cachesim_path.txt" ]; then CACHESIM=$(cat "$TARDIS/cachesim_path.txt")
else CACHESIM=/mydata/cachesim/_build/bin/cachesim; fi

# ---------------------------------------------------------------------------
# Wikimedia 2019 trace (gzipped TSV, 24 hours) -> oracleGeneral with
# libCacheSim's traceConv, which also computes each request's next access.
#   text   (cache-t): relative_unix  hashed_host_path_query  response_size  time_firstbyte
#   upload (cache-u): relative_unix  hashed_path_query  image_type  response_size  time_firstbyte
# Layout from the column count (4 = text, 5 = upload), header from a non-numeric
# first field. Check: exactly one 24-byte record per row with a positive size
# (libCacheSim skips zero-size requests), otherwise the output is deleted.
convert_wikimedia() {  # in.gz out.oracleGeneral
  local in=$1 out=$2 tconv tsv first f1 header data_line ncol size_col kind rows kept rc recs
  if [ -n "${TRACECONV:-}" ]; then tconv=$TRACECONV
  elif [ -f "$TARDIS/traceconv_path.txt" ]; then tconv=$(cat "$TARDIS/traceconv_path.txt")
  else tconv="$(dirname "$CACHESIM")/traceConv"; fi
  [ -x "$tconv" ] || { echo "convert: traceConv not found at $tconv" >&2; return 2; }
  tsv="${out}.tsv.tmp"
  gunzip -c "$in" > "$tsv" || { echo "convert: gunzip failed for $in" >&2; rm -f "$tsv" "$out"; return 3; }
  first=$(head -1 "$tsv"); f1=$(printf '%s' "$first" | cut -f1)
  if [[ "$f1" =~ ^[0-9]+$ ]]; then header=false; data_line="$first"
  else header=true; data_line=$(sed -n 2p "$tsv"); fi
  ncol=$(printf '%s' "$data_line" | awk -F'\t' '{print NF}')
  case "$ncol" in
    4) size_col=3; kind=text ;;
    5) size_col=4; kind=upload ;;
    *) echo "convert: unexpected column count $ncol in $in" >&2; rm -f "$tsv" "$out"; return 4 ;;
  esac
  read -r rows kept <<< "$(awk -F'\t' -v c="$size_col" -v h="$header" \
      'NR==1 && h=="true" {next} {n++; if (($c + 0) > 0) k++} END {print n + 0, k + 0}' "$tsv")"
  "$tconv" "$tsv" csv \
    -t "time-col=1,obj-id-col=2,obj-size-col=${size_col},delimiter=\t,has-header=${header},obj-id-is-num=true" \
    --output-format oracleGeneral -o "$out" > "${out}.convlog" 2>&1
  rc=$?
  rm -f "$tsv"
  recs=$(( $(stat -c %s "$out" 2>/dev/null || echo 0) / 24 ))
  if [ "$rc" -ne 0 ] || [ "$recs" -ne "$kept" ]; then
    echo "convert: traceConv rc=$rc, $recs records for $kept positive-size rows of $rows ($kind)" >&2
    tail -3 "${out}.convlog" >&2
    rm -f "$out" "${out}.convlog"; return 5
  fi
  rm -f "${out}.convlog"
  echo "convert: $kind trace, $kept requests ($((rows - kept)) of $rows rows had no size and were skipped)"
}

case "${1:-}" in
  --convert)
    convert_wikimedia "${2:?input .gz}" "${3:?output .oracleGeneral}"; exit $? ;;
  --tune)
    python3 -c "import optuna" 2>/dev/null || {
      echo "ERROR: optuna is not installed on $(hostname). Rerun orchestrate.py --setup."; exit 1; }
    echo "[$(hostname)] tuning: shard=${2:?study shard} out=${3:?output csv} cachesim=$CACHESIM"
    exec python3 -u "$SCRIPTS/tune.py" run --shard "$2" --out "$3" \
      --work "${WORK:-/mydata/tune_work}" --cachesim "$CACHESIM" ;;
esac

LIST="${1:?trace list file (one URL per line)}"
OUT="${2:?output csv path}"
WORK="${WORK:-/mydata/trace_work}"
DIAG="${DIAG:-$TARDIS/results/forgive_diag.log}"

ALGOS_BASE="${ALGOS_BASE:-lru,s3fifo}"
ALGOS_EMB="${ALGOS_EMB:-lruforgiveembcache,s3fifoforgive-embcache}"
SIZES="${SIZES:-0.1,0.01,0.001}"
# labels: the fraction as a percent (0.1 -> 10, 0.001 -> 0.1)
PCTS=$(python3 -c "print(','.join(f'{float(s) * 100:g}' for s in '$SIZES'.split(',')))")
EMB_MULT="${EMB_MULT:-5}"
MIN_CACHE_OBJS="${MIN_CACHE_OBJS:-1000}"
NUM_THREAD="${NUM_THREAD:-4}"
FORGIVE_THREADS="${FORGIVE_THREADS:-2}"
CELL_TIMEOUT="${CELL_TIMEOUT:-14400}"
PARAMS="${PARAMS:-}"
PARAMS_FILE="${PARAMS_FILE:-}"
EXPAND="${EXPAND:-5}"   # assumed worst-case zstd expansion for the disk check

NODE=$(hostname)
mkdir -p "$WORK" "$(dirname "$OUT")" "$(dirname "$DIAG")"

for f in "$CACHESIM" "$COMMON"; do
  [ -e "$f" ] || { echo "ERROR: missing $f (run node_setup.sh first)"; exit 1; }
done
[ -x "$CACHESIM" ] || { echo "ERROR: $CACHESIM is not executable"; exit 1; }

[ -f "$OUT" ] || python3 "$COMMON" header > "$OUT"

IFS=',' read -ra SZ_ARR <<< "$SIZES"
IFS=',' read -ra PC_ARR <<< "$PCTS"

echo "[$NODE] cachesim=$CACHESIM"
echo "[$NODE] sizes=$SIZES (pcts=$PCTS) timeout=${CELL_TIMEOUT}s min-cache-objs=$MIN_CACHE_OBJS"
echo "[$NODE] baselines='$ALGOS_BASE' num-thread=$NUM_THREAD"
echo "[$NODE] forgive='$ALGOS_EMB' num-thread=$FORGIVE_THREADS emb=${EMB_MULT}x main capacity in objects"
[ -n "$PARAMS" ] && echo "[$NODE] extra params: '$PARAMS'"
if [ -n "$PARAMS_FILE" ]; then
  [ -f "$PARAMS_FILE" ] || { echo "ERROR: PARAMS_FILE $PARAMS_FILE not found"; exit 1; }
  echo "[$NODE] tuned params from $PARAMS_FILE ($(python3 -c "import json;print(len(json.load(open('$PARAMS_FILE'))))") entries)"
fi
echo "[$NODE] diagnostics -> $DIAG"

na_rows() {  # dataset trace reason algos pcts...  (11 columns, matches the parser)
  local ds=$1 name=$2 reason=$3 algos=$4; shift 4
  local pol pct
  for pol in ${algos//,/ }; do
    for pct in "$@"; do
      local p="${PARAMS:-default}"; echo "$NODE,$ds,$name,$pol,NA,$pct,NA,NA,$reason,NA,${p//,/;}" >> "$OUT"
    done
  done
}

# Trace naming: the SAME rule as common.py (keep the two in sync).
#   .../cacheDatasets/alibabaBlock/new/1M/x.oracleGeneral.zst -> alibabaBlock_new_1M / x
#   .../published/datasets/caching/2019/text/cache-t-00.gz    -> wikimedia_2019_text / cache-t-00
trace_name() {
  local b; b=$(basename "$1")
  if [[ "$b" == *.oracleGeneral* ]]; then printf '%s' "${b%%.oracleGeneral*}"; return; fi
  local changed=1 e
  while [ "$changed" = 1 ]; do
    changed=0
    for e in .gz .zst .tsv .csv .txt; do
      if [[ "$b" == *"$e" && ${#b} -gt ${#e} ]]; then b="${b%"$e"}"; changed=1; fi
    done
  done
  printf '%s' "$b"
}
dataset_of() {
  local u=$1 p
  if [[ "$u" == */cacheDatasets/* ]]; then
    p="${u#*/cacheDatasets/}"; p="${p%/*}"; printf '%s' "${p//\//_}"
  elif [[ "$u" == */published/datasets/caching/* ]]; then
    p="${u#*/published/datasets/caching/}"; p="${p%/*}"; printf 'wikimedia_%s' "${p//\//_}"
  else
    printf 'unknown'
  fi
}

# params string + the embedding cap, unless the caller already set one
with_cap() {  # params cap
  local prm=$1 cap=$2
  case "$prm" in *max-emb-entries*) printf '%s' "$prm"; return;; esac
  printf '%s' "${prm:+$prm,}max-emb-entries=$cap"
}

# A forgive call failed (most often out of memory: each policy grows its own
# embedding table, and huge traces like full twitter can need tens of GB per
# table). cachesim prints results only after every cell finishes, so a failed
# call wrote no rows and a retry is clean. Retry each policy that has no row
# yet ON ITS OWN, one table at a time; if it still fails, record NA_FAIL so the
# gap is visible and analyze.py --remaining lists the trace.
retry_alone() {  # dataset trace path pct abs_size tag params policies...
  local ds=$1 name=$2 path=$3 pct=$4 size=$5 tag=$6 prm=$7; shift 7
  local pol rc_all=0
  echo "[$NODE]   ${pct}%: forgive call failed ($(grep MemAvailable /proc/meminfo | tr -s ' '));" \
       "retrying one policy at a time"
  for pol in "$@"; do
    if grep -q "^[^,]*,$ds,$name,$pol,[^,]*,$pct," "$OUT" 2>/dev/null; then
      continue                                   # this policy's row did get written
    fi
    if ! run_invocation "$ds" "$name" "$path" "$pol" "$prm" "$size" "$pct" "retry${tag}_$pol" 1; then
      echo "[$NODE]   ${pct}%: $pol failed on its own too -> NA_FAIL"
      na_rows "$ds" "$name" NA_FAIL "$pol" "$pct"
      rc_all=1
    fi
  done
  return $rc_all
}

# one cachesim call -> rows appended to $OUT. Leaves $WORK/.raw.<tag>{,.err}
# in place (the baseline's .err carries the working set); cleaned per trace.
run_invocation() {  # dataset trace path algos params sizes pcts tag threads
  local ds=$1 name=$2 path=$3 algos=$4 prm=$5 sizes=$6 pcts=$7 tag=$8 threads=$9
  local raw="$WORK/.raw.$tag"
  local rc=0
  # cachesim ALSO appends results to result/<trace>.cachesim under the CWD by
  # default; point that at scratch so thousands of files don't land in $HOME.
  local extra=(--num-thread "$threads" --output "$WORK/.cachesim_out.$tag")
  [ -n "$prm" ] && extra+=(--eviction-params "$prm")

  timeout "$CELL_TIMEOUT" "$CACHESIM" "$path" oracleGeneral "$algos" "$sizes" \
    "${extra[@]}" > "$raw" 2>"$raw.err" || rc=$?

  if [ "$rc" -ne 0 ]; then
    echo "[$NODE]   cachesim rc=$rc ($tag); stderr tail:"
    tail -3 "$raw.err" | sed "s/^/[$NODE]     /"
  fi

  # Forgive policies print forgive counts + final_emb_count on free: the only
  # direct measurement of embedding-table size. No cache size on the line, so
  # it is logged rather than joined into the CSV.
  # Built first and appended with ONE write, so two forgive calls running side
  # by side (PARAMS_FILE mode) can never interleave their lines in the log.
  if grep -aq "final_emb_count" "$raw.err" 2>/dev/null; then
    local block
    block="### $ds/$name tag=$tag pct=$pcts sizes=$sizes params=${prm:-default}"$'\n'
    block+="$(grep -a "final_emb_count" "$raw.err")"$'\n'
    printf '%s' "$block" >> "$DIAG"
  fi

  if ! python3 "$COMMON" parse "$raw" "$raw.err" "$NODE" "$ds" "$name" \
        "$sizes" "$pcts" "${prm:-default}" >> "$OUT"; then
    echo "[$NODE]   PARSE FAIL ($tag)"
    return 1
  fi
  [ "$rc" -eq 0 ] || return 1
  return 0
}

while IFS= read -r line; do
  line="${line%$'\r'}"
  [ -z "$line" ] && continue
  case "$line" in \#*) continue;; esac
  url="${line##*$'\t'}"                      # accept "<dataset>\t<url>" or a bare url

  remote_base=$(basename "$url")
  # dataset+name is the key: the same volume id appears in several block
  # subfolders, so the bare name is not unique across the mirror
  name=$(trace_name "$url")
  dataset=$(dataset_of "$url")

  # Done = any row for this trace that is NOT a transient failure. NA_DISK /
  # NA_UNZIP / NA_DLFAIL / NA_NOWSS / NA_CONVERT rows don't count, so rerunning a shard
  # retries exactly the traces that failed. NA_SMALL is deterministic and
  # always sits beside real baseline rows, so it never triggers a retry.
  if grep "^[^,]*,$dataset,$name," "$OUT" 2>/dev/null \
       | grep -qvE ',NA_(DISK|UNZIP|DLFAIL|NOWSS|CONVERT|FAIL),'; then
    echo "[$NODE] SKIP $dataset/$name (done)"; continue
  fi
  echo "[$NODE] === $dataset/$name ==="

  zst="$WORK/$remote_base"
  trace="$WORK/$name.oracleGeneral"

  if ! wget -q -O "$zst" "$url"; then
    echo "[$NODE] DL FAIL"; rm -f "$zst"
    na_rows "$dataset" "$name" NA_DLFAIL "$ALGOS_BASE,$ALGOS_EMB" "${PC_ARR[@]}"; continue
  fi

  # Wikimedia 2019 traces are gzipped TSV, not oracleGeneral: convert once
  # with traceConv (convert_wikimedia checks one record per row), then run
  # on the uncompressed oracleGeneral file like any other trace.
  if [[ "$remote_base" != *.oracleGeneral* ]]; then
    if ! convert_wikimedia "$zst" "$trace"; then
      echo "[$NODE] CONVERT FAIL $dataset/$name"; rm -f "$zst" "$trace"
      na_rows "$dataset" "$name" NA_CONVERT "$ALGOS_BASE,$ALGOS_EMB" "${PC_ARR[@]}"; continue
    fi
    rm -f "$zst"
  else
  # Decompress when it fits (faster: each reader thread skips zstd work).
  # Otherwise, or if decompression fails part-way (typically the disk filling
  # on a trace that expands more than EXPAND x), run cachesim on the .zst
  # itself. libCacheSim reads .zst natively and the results are identical,
  # verified cell-for-cell including the working-set line.
  zsize=$(stat -c %s "$zst" 2>/dev/null || echo 0)
  avail=$(df -B1 --output=avail "$WORK" 2>/dev/null | tail -1 || echo 0)
  if [ "$avail" -gt 0 ] && [ $((zsize * EXPAND)) -gt "$avail" ]; then
    echo "[$NODE]   too big to decompress (~$((zsize*EXPAND/1073741824))GiB vs $((avail/1073741824))GiB free); reading .zst directly"
    trace="$zst"
  elif ! zstd -d -q --rm "$zst" -o "$trace"; then
    rm -f "$trace"                              # partial output; --rm keeps the .zst on failure
    if [ -s "$zst" ]; then
      echo "[$NODE]   decompression failed; reading .zst directly"
      trace="$zst"
    else
      echo "[$NODE] UNZIP FAIL"; rm -f "$zst"
      na_rows "$dataset" "$name" NA_UNZIP "$ALGOS_BASE,$ALGOS_EMB" "${PC_ARR[@]}"; continue
    fi
  fi
  fi

  ok=1

  # ---- 1. baselines at fractional sizes; also logs the working set ----------
  run_invocation "$dataset" "$name" "$trace" "$ALGOS_BASE" "" \
                 "$SIZES" "$PCTS" base "$NUM_THREAD" || ok=0

  WSS=$(python3 "$COMMON" wss "$WORK/.raw.base.err" 2>/dev/null) || WSS=""

  if [ -z "$WSS" ]; then
    # No working set -> no capacity to derive a cap or apply the skip rule.
    # Record it rather than silently running unbounded.
    echo "[$NODE]   NO WORKING SET for $dataset/$name; forgive cells not run"
    na_rows "$dataset" "$name" NA_NOWSS "$ALGOS_EMB" "${PC_ARR[@]}"
    ok=0
  else
    set -- $WSS
    wss_obj=$1; wss_byte=$2; ratio=$3
    echo "[$NODE]   wss: $wss_obj objects, $wss_byte bytes (sample ratio $ratio)"

    # ---- 2. one forgive call per cache size, each with its own cap ----------
    for i in "${!SZ_ARR[@]}"; do
      frac="${SZ_ARR[$i]}"; pct="${PC_ARR[$i]}"
      read -r abs_size cap cap_objs <<< "$(python3 -c "
wb, wo, frac, mult = $wss_byte, $wss_obj, $frac, $EMB_MULT
print(max(1, int(wb*frac)), max(1, int(mult*frac*wo)), int(frac*wo))")"

      if [ "$MIN_CACHE_OBJS" -gt 0 ] && [ "$cap_objs" -lt "$MIN_CACHE_OBJS" ]; then
        echo "[$NODE]   ${pct}%: SKIP forgive (cache holds ~$cap_objs objects < $MIN_CACHE_OBJS)"
        na_rows "$dataset" "$name" NA_SMALL "$ALGOS_EMB" "$pct"
        continue
      fi

      echo "[$NODE]   ${pct}%: cache=${abs_size}B (~$cap_objs objs) emb-cap=$cap"
      if [ -z "$PARAMS_FILE" ]; then
        prm=$(with_cap "$PARAMS" "$cap")
        if ! run_invocation "$dataset" "$name" "$trace" "$ALGOS_EMB" "$prm" \
                            "$abs_size" "$pct" "emb$i" "$FORGIVE_THREADS"; then
          retry_alone "$dataset" "$name" "$trace" "$pct" "$abs_size" "$i" "$prm" ${ALGOS_EMB//,/ } || ok=0
        fi
      else
        # tuned: each policy has its own params for (dataset, size), so one call
        # per policy, run side by side (same 2 concurrent tables as before)
        pids=(); pols=(); prms=(); j=0
        for pol in ${ALGOS_EMB//,/ }; do
          tuned=$(python3 "$COMMON" lookup "$PARAMS_FILE" "$dataset" "$pct" "$pol")
          prm=$(with_cap "${tuned}${PARAMS:+${tuned:+,}$PARAMS}" "$cap")
          echo "[$NODE]     $pol: ${tuned:-default params}"
          run_invocation "$dataset" "$name" "$trace" "$pol" "$prm" \
                         "$abs_size" "$pct" "emb${i}_$j" 1 &
          pids+=($!); pols+=("$pol"); prms+=("$prm"); j=$((j + 1))
        done
        for k in "${!pids[@]}"; do
          if ! wait "${pids[$k]}"; then
            # ran side by side with the other policy; retry it with the node to itself
            retry_alone "$dataset" "$name" "$trace" "$pct" "$abs_size" "${i}_$k" "${prms[$k]}" "${pols[$k]}" || ok=0
          fi
        done
      fi
    done
  fi

  rm -f "$trace" "$zst" "$WORK"/.raw.* "$WORK"/.cachesim_out.* 2>/dev/null
  if [ "$ok" = "1" ]; then echo "[$NODE] DONE $dataset/$name"
  else echo "[$NODE] PARTIAL $dataset/$name (grep NA_ in the CSV)"; fi
done < "$LIST"

echo "[$NODE] ALL DONE -> $OUT"
