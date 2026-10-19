#!/usr/bin/env bash
# Download new storage traces, screen each ONE AT A TIME, delete failures
# immediately, then sweep the survivors.
#
# FLOW (per trace, sequential):
#   download .zst -> decompress to .oracleGeneral -> delete .zst
#   -> OHW screen -> if OHW request-share > cutoff: DELETE THE TRACE
#                    else: keep it and add to the sweep list
# Screening before the next download means disk never accumulates rejects.
#
# The OHW cutoff comes from our own Section 20.2 result: every trace above ~25%
# OHW request-share had a LRUForgive gap of at most 1.25pp. Screening costs
# seconds; sweeping costs hours.
#
# BASELINES ONLY (lru, s3fifo). Leaves the lruforgive binary free so the
# recency-buffer experiment can rebuild in parallel without contaminating this.

set -u
BASE="https://ftp.pdl.cmu.edu/pub/datasets/twemcacheWorkload/cacheDatasets"
T=/mydata/tardis/traces
BUILD=/mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench/_build
OUT=/mydata/tardis/sweep_results.csv
OHWLOG=/mydata/tardis/ohw_new.txt
SIZES=(8 16 32 64 128 256 512 1024 2048 4096 8192)
HPS=(19 20 21 22 23 24 25 26 27 28 29)

OHW_CUTOFF=25        # percent OF REQUESTS. above this -> delete, no gap expected
PER_FAMILY=4         # how many traces to try per family
MIN_FREE_GB=45       # stop downloading below this
FAMILIES="systor cloudphysics alibabaBlock tencentBlock"
# metaStorage omitted by default: those files are 30-38GB decompressed each.
# Add it back only with plenty of free space.

cd "$T" || exit 1
: > "$OHWLOG"
KEEP=""

free_gb() { df --output=avail -BG /mydata | tail -1 | tr -dc '0-9'; }

echo "########## discover + screen, one trace at a time ##########"
for fam in $FAMILIES; do
  echo "===== family: $fam ====="
  LIST=$(curl -s "$BASE/$fam/" \
         | grep -oE '[A-Za-z0-9_.-]+\.oracleGeneral(\.sample[0-9]+)?\.zst' \
         | sort -u | head -"$PER_FAMILY")
  if [ -z "$LIST" ]; then echo "  (no traces found or family missing)"; continue; fi

  for f in $LIST; do
    out="${f%.zst}"
    if [ -f "$out" ]; then echo "[have] $out"; KEEP="$KEEP $out"; continue; fi

    avail=$(free_gb)
    if [ "$avail" -lt "$MIN_FREE_GB" ]; then
      echo "STOP: ${avail}G free, below ${MIN_FREE_GB}G threshold"
      break 2
    fi

    echo "--- GET $fam/$f  (${avail}G free) ---"
    if ! wget -q --show-progress -O "$f" "$BASE/$fam/$f"; then
      echo "    MISS (404/network)"; rm -f "$f"; continue
    fi
    if ! zstd -d -q --rm "$f"; then
      echo "    FAIL decompress"; rm -f "$f" "$out"; continue
    fi
    sz=$(du -h "$out" | cut -f1)
    echo "    decompressed: $out ($sz)"

    # --- screen immediately ---
    echo "    screening..."
    screen=$(python3 "$T/ohw_fast.py" "$out" 2>&1)
    echo "$screen" | tee -a "$OHWLOG"
    share=$(echo "$screen" | awk '/OHW \/ requests/ {gsub(/%/,"",$NF); print $NF; exit}')

    if [ -z "$share" ]; then
      echo "    NO OHW READING -> deleting (unreadable format?)"
      rm -f "$out"; continue
    fi
    if [ "$(printf '%.0f' "$share")" -gt "$OHW_CUTOFF" ]; then
      echo "    SCREENED OUT: OHW ${share}% of requests > ${OHW_CUTOFF}% -> DELETING"
      rm -f "$out"
    else
      echo "    PASS: OHW ${share}% -> keeping for sweep"
      KEEP="$KEEP $out"
    fi
  done
done

echo "########## survivors ##########"
echo "$KEEP" | tr ' ' '\n' | grep -v '^$' || echo "(none)"
df -h /mydata | tail -1

echo "########## baseline sweep of survivors (lru, s3fifo) ##########"
[ -f "$OUT" ] || echo "trace,algo,cache_mb,hashpower,miss_ratio" > "$OUT"
for tr in $KEEP; do
  echo "===== sweeping $tr ====="
  for algo in lru s3fifo; do
    for i in "${!SIZES[@]}"; do
      s=${SIZES[$i]}; hp=${HPS[$i]}
      grep -q "^$tr,$algo,$s," "$OUT" && { echo "[have] $algo $tr ${s}MB"; continue; }
      echo -n "[run] $algo $tr ${s}MB hp=$hp ... "
      mr=$(timeout 3600 "$BUILD/$algo" "$T/$tr" "$s" "$hp" 1 1 2>&1 | tail -1 \
           | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
      echo "miss=${mr:-FAIL}"
      echo "$tr,$algo,$s,$hp,$mr" >> "$OUT"
    done
  done
done
echo "########## DONE ##########"
df -h /mydata | tail -1
