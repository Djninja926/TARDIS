#!/usr/bin/env bash
# Download 5 more MSR traces. Flow: wget .zst -> zstd -d to .oracleGeneral -> delete .zst.
set -u
BASE="https://ftp.pdl.cmu.edu/pub/datasets/twemcacheWorkload/cacheDatasets/msr"
DEST="/mydata/tardis/traces"
cd "$DEST" || exit 1
WANT="msr_proj_3 msr_usr_0 msr_rsrch_0 msr_web_0 msr_mds_0"
for name in $WANT; do
  out="${name}.oracleGeneral"; zst="${out}.zst"
  if [[ -f "$out" ]]; then echo "SKIP (already have) $out"; continue; fi
  echo "=== $name ==="
  if wget -q --show-progress -O "$zst" "$BASE/${out}.zst"; then
    if zstd -d -q --rm "$zst"; then echo "OK   $out ($(du -h "$out" | cut -f1))"
    else echo "FAIL decompress; leaving $zst"; fi
  else echo "MISS (404 or network) $name"; rm -f "$zst"; fi
done
echo "=== traces now on node ==="; ls -1 "$DEST"/*.oracleGeneral | wc -l; df -h /mydata | tail -1
