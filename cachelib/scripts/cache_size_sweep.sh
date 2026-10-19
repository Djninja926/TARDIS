#!/usr/bin/env bash
BUILD=/mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench/_build
TRACE_DIR=/mydata/tardis/traces
OUT=/mydata/tardis/sweep_results_redonefully.csv

# 4MB dropped: always failed (cache too small to init). Ladder starts at 8MB/hp19.
SIZES=(8 16 32 64 128 256 512 1024 2048 4096 8192)
HPS=(19 20 21 22 23 24 25 26 27 28 29)

# TARDIS is 7-10x slower and the LF-over-LRU gap lives BELOW the knee
# (Section 18.3), so there is no value running it past 1024MB.
TARDIS_MAX_MB=1024

TRACES=(
  # --- already swept (325 banked rows; resume logic will skip these) ---
  "cluster26.oracleGeneral"
  "cluster10.oracleGeneral"
  "cluster50.oracleGeneral"
  "cluster53.oracleGeneral"
  "cluster45.oracleGeneral"
  "cache-t-00.oracleGeneral"
  "msr_proj_0.oracleGeneral"
  # metaKV deleted from disk (no gap, banked). The [skip missing] guard
  # handles them cleanly, so leaving them listed is harmless.
  "202206_kv_traces_all.csv.oracleGeneral"
  "202210_kv_traces_all_sort.csv.oracleGeneral"

  # --- NEW: the other 13 MSR traces (the point of this round) ---
  # Does the msr_proj_0 +6.20pp gap generalize across the MSR workload class?
  "msr_proj_1.oracleGeneral"
  "msr_proj_2.oracleGeneral"
  "msr_proj_4.oracleGeneral"
  "msr_prxy_0.oracleGeneral"
  "msr_prxy_1.oracleGeneral"
  "msr_src1_0.oracleGeneral"
  "msr_src1_1.oracleGeneral"
  "msr_hm_0.oracleGeneral"
  "msr_prn_0.oracleGeneral"
  "msr_prn_1.oracleGeneral"
  "msr_usr_1.oracleGeneral"
  "msr_usr_2.oracleGeneral"
  "msr_web_2.oracleGeneral"
)

BASELINES=(lru s3fifo)

# mode 1 = round-robin shared (matches the MT experiments; identical to mode 0
# at n_thread=1, so the banked rows remain valid).
get_miss() {
  "$BUILD/$1" "$TRACE_DIR/$2" "$3" "$4" 1 1 2>&1 | tail -1 \
    | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}'
}

[ -f "$OUT" ] || echo "trace,algo,cache_mb,hashpower,miss_ratio" > "$OUT"

run_pass() {
  local algo="$1"
  for trace in "${TRACES[@]}"; do
    [ -f "$TRACE_DIR/$trace" ] || { echo "[skip missing] $trace"; continue; }
    for i in "${!SIZES[@]}"; do
      sz=${SIZES[$i]}; hp=${HPS[$i]}

      # cap TARDIS at TARDIS_MAX_MB
      if [ "$algo" = "lruforgive" ] && [ "$sz" -gt "$TARDIS_MAX_MB" ]; then
        echo "[skip >${TARDIS_MAX_MB}MB] $algo $trace ${sz}MB"; continue
      fi

      if grep -q "^$trace,$algo,$sz," "$OUT" 2>/dev/null; then
        echo "[have] $algo $trace ${sz}MB"; continue; fi

      echo -n "[run] $algo $trace ${sz}MB hp=$hp ... "
      mr=$(get_miss "$algo" "$trace" "$sz" "$hp")
      if [ -z "$mr" ]; then
        echo "FAILED (no miss ratio parsed)"
        echo "$trace,$algo,$sz,$hp," >> "$OUT"
      else
        echo "miss=$mr"
        echo "$trace,$algo,$sz,$hp,$mr" >> "$OUT"
      fi
    done
  done
}

echo "##### PASS 1: BASELINES (lru, s3fifo) #####"
for algo in "${BASELINES[@]}"; do echo "=== $algo ==="; run_pass "$algo"; done
echo "##### PASS 2: TARDIS (lruforgive, capped at ${TARDIS_MAX_MB}MB) #####"
run_pass lruforgive
echo "Done -> $OUT"
