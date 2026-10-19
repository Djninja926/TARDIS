#!/usr/bin/env bash
# get_traces.sh NAME... - put traces in /mydata/tardis/traces/NAME.oracleGeneral
#
#   bash get_traces.sh cache-t-00 msr_proj_0 cluster53 msr_prxy_1    # the four operating points
#   bash get_traces.sh zipf                                           # synthetic control
#   bash get_traces.sh systor/2016_LUN0                               # any path under the CMU mirror
#
# Names:
#   cache-t-NN   Wikimedia 2019 text, day NN (TSV), converted to oracleGeneral
#   msr_*        CMU mirror msr/
#   clusterN     CMU mirror twitter/ (full size)
#   20*_kv_*     CMU mirror metaKV/
#   2016_LUN*    CMU mirror systor/
#   zipf         Zipf(1.0), 1M objects, 20M requests, from libCacheSim's data_gen.py
#   a/b          CMU mirror path a/b.oracleGeneral.zst
# Already-present traces are skipped.
set -u
ROOT=/mydata/tardis
DEST="$ROOT/traces"
LCS="$ROOT/sosp23-s3fifo/libCacheSim"
CMU=https://ftp.pdl.cmu.edu/pub/datasets/twemcacheWorkload/cacheDatasets
WIKI=https://analytics.wikimedia.org/published/datasets/caching/2019/text
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$DEST"

fetch_zst() {  # url local_name
  local url=$1 out="$DEST/$2.oracleGeneral"
  wget -q -O "$out.zst" "$url" || { echo "  DOWNLOAD FAILED: $url"; rm -f "$out.zst"; return 1; }
  zstd -d -q --rm "$out.zst" -o "$out" || { echo "  UNZIP FAILED: $2"; rm -f "$out" "$out.zst"; return 1; }
}

[ $# -gt 0 ] || { sed -n 2,16p "$0"; exit 1; }
for name in "$@"; do
  base=$(basename "$name")
  if [ -s "$DEST/$base.oracleGeneral" ]; then echo "[have] $base"; continue; fi
  echo "[get]  $name"
  case "$name" in
    cache-t-*)
      wget -q -O "$DEST/$base.gz" "$WIKI/$base.gz" || { echo "  DOWNLOAD FAILED"; continue; }
      # same converter as the libCacheSim sweep (checks one record per request)
      TRACECONV="$LCS/_build/bin/traceConv" bash "$HERE/../../libcachesim/scripts/node_worker.sh" \
        --convert "$DEST/$base.gz" "$DEST/$base.oracleGeneral"
      rm -f "$DEST/$base.gz" ;;
    zipf)
      python3 "$LCS/scripts/data_gen.py" -m 1000000 -n 20000000 --alpha 1.0 \
        --bin-output "$DEST/zipf.oracleGeneral" ;;
    msr_*)      fetch_zst "$CMU/msr/$base.oracleGeneral.zst" "$base" ;;
    cluster*)   fetch_zst "$CMU/twitter/$base.oracleGeneral.zst" "$base" ;;
    20*_kv_*)   fetch_zst "$CMU/metaKV/$base.oracleGeneral.zst" "$base" ;;
    2016_LUN*)  fetch_zst "$CMU/systor/$base.oracleGeneral.zst" "$base" ;;
    */*)        fetch_zst "$CMU/$name.oracleGeneral.zst" "$base" ;;
    *)          echo "  unknown name $name (give a path under the CMU mirror, e.g. systor/2016_LUN0)" ;;
  esac
done
ls -la "$DEST"
