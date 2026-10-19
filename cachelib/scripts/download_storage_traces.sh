#!/usr/bin/env bash
# Download more storage-family traces to hunt for LRUForgive-over-LRU gaps.
# Rationale: msr_proj_0 (block storage) was the strongest gap in the sweep.
# Storage traces have the temporal correlation the two-condition model wants.
# This targets the whole MSR family plus a sample of the other block-storage sets.
#
# Run on the CloudLab node. Traces land in /mydata/tardis/traces/ decompressed,
# where cache_size_sweep.sh will pick them up automatically (it is resumable).

set -u
BASE="https://ftp.pdl.cmu.edu/pub/datasets/twemcacheWorkload/cacheDatasets"
DEST="/mydata/tardis/traces"
mkdir -p "$DEST"
cd "$DEST"

# --- helper: download one .zst, decompress, delete the .zst ---
fetch() {
  local url="$1"
  local fname="$(basename "$url")"
  local base="${fname%.zst}"
  if [[ -f "$base" ]]; then echo "SKIP (have) $base"; return 0; fi
  echo "GET $url"
  if wget -q --show-progress -O "$fname" "$url"; then
    if zstd -d -q --rm "$fname"; then
      echo "OK   $base"
    else
      echo "FAIL decompress $fname"; rm -f "$fname"
    fi
  else
    echo "MISS (404 or error) $fname"; rm -f "$fname"
  fi
}

# --- 1. All MSR traces (self-listing, so we grab whatever is actually there) ---
echo "=== listing MSR directory ==="
MSR_LIST=$(curl -s "$BASE/msr/" \
  | grep -oE 'msr_[a-z0-9]+_[0-9]+\.oracleGeneral\.zst' \
  | sort -u)
echo "$MSR_LIST"
echo "=== downloading MSR ==="
# Optional disk guard: comment the head filter to grab all. Default: proj family
# first (the winner's siblings) then one of every other server type.
PRIORITY="msr_proj_0 msr_proj_1 msr_proj_2 msr_proj_3 msr_proj_4 \
          msr_usr_0 msr_src1_0 msr_src2_0 msr_rsrch_0 msr_web_0 \
          msr_stg_0 msr_mds_0 msr_prn_0 msr_ts_0 msr_wdev_0 msr_prxy_0 msr_hm_0"
for name in $PRIORITY; do
  echo "$MSR_LIST" | grep -q "^${name}\.oracleGeneral\.zst$" \
    && fetch "$BASE/msr/${name}.oracleGeneral.zst"
done

# --- 2. Sample the other block-storage families (self-listing each) ---
# These are the families the two-condition model predicts could carry a gap.
# Grab a few from each; expand later if any family shows a benefit.
for fam in metaStorage systor alibabaBlock tencentBlock cloudphysics; do
  echo "=== listing $fam ==="
  LIST=$(curl -s "$BASE/$fam/" \
    | grep -oE '[A-Za-z0-9_.-]+\.oracleGeneral(\.sample[0-9]+)?\.zst' \
    | sort -u)
  echo "$LIST" | head -20
  # take the first 3 of each family to start
  for f in $(echo "$LIST" | head -3); do
    fetch "$BASE/$fam/$f"
  done
done

echo "=== done. current traces: ==="
ls -lhS "$DEST"/*.oracleGeneral* 2>/dev/null | head -40
df -h /mydata | tail -1
