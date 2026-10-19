#!/usr/bin/env bash
set -e
TRACE_DIR=/mydata/tardis/traces
BASE=https://ftp.pdl.cmu.edu/pub/datasets/twemcacheWorkload/cacheDatasets
cd "$TRACE_DIR"
TRACES=(
  "twitter/cluster10.oracleGeneral"
  "twitter/cluster50.oracleGeneral"
  "twitter/cluster53.oracleGeneral"
  "twitter/cluster45.oracleGeneral"
  "msr/msr_proj_0.oracleGeneral"          # already have, will skip if present
  "metaKV/202206_kv_traces_all.csv.oracleGeneral"
  "metaKV/202210_kv_traces_all_sort.csv.oracleGeneral"
)
for entry in "${TRACES[@]}"; do
  fname=$(basename "$entry")
  if [ -f "$TRACE_DIR/$fname" ]; then echo "[skip] $fname present"; continue; fi
  echo "=== [download] $entry.zst ==="
  curl -f -S -O "$BASE/$entry.zst"
  echo "[df before]"; df -h /mydata | tail -1
  zstd -d --rm "$fname.zst"
  echo "[df after]"; df -h /mydata | tail -1
  ls -la "$TRACE_DIR/$fname"
done
echo "=== all present ==="; ls -la "$TRACE_DIR"/*.oracleGeneral
