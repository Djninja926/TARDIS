#!/bin/bash
# convert_wikimedia.sh <in.gz> <out.oracleGeneral>
#
# Convert one Wikimedia 2019 caching trace (gzipped TSV, one file = 24 hours)
# to oracleGeneral with libCacheSim's own traceConv, which also computes each
# request's next-access time.
#
# Layouts (https://wikitech.wikimedia.org/wiki/Data_Platform/Data_Lake/Traffic/Caching):
#   text   (cache-t): relative_unix  hashed_host_path_query  response_size  time_firstbyte
#   upload (cache-u): relative_unix  hashed_path_query  image_type  response_size  time_firstbyte
# The layout is detected from the column count (4 = text, 5 = upload) and a
# header line is detected from a non-numeric first field, so neither depends on
# the file name. The hashed ids are signed 64-bit; traceConv keeps their exact
# 64-bit pattern (verified: every id, size and timestamp matched the TSV, and
# cachesim gave identical results on the converted file and on the raw TSV).
#
# Safety check: the output must hold exactly one 24-byte record per data row,
# otherwise the conversion is treated as failed and nothing is left behind.
set -uo pipefail

IN="${1:?input .gz}"
OUT="${2:?output .oracleGeneral}"
TARDIS="${TARDIS:-/mydata/tardis}"

if [ -z "${TRACECONV:-}" ]; then
  if [ -f "$TARDIS/traceconv_path.txt" ]; then TRACECONV=$(cat "$TARDIS/traceconv_path.txt")
  elif [ -n "${CACHESIM:-}" ]; then TRACECONV="$(dirname "$CACHESIM")/traceConv"
  else TRACECONV=/mydata/cachesim/_build/bin/traceConv; fi
fi
[ -x "$TRACECONV" ] || { echo "convert: traceConv not found at $TRACECONV" >&2; exit 2; }

TSV="${OUT}.tsv.tmp"
trap 'rm -f "$TSV"' EXIT

gunzip -c "$IN" > "$TSV" || { echo "convert: gunzip failed for $IN" >&2; rm -f "$OUT"; exit 3; }

first=$(head -1 "$TSV")
f1=$(printf '%s' "$first" | cut -f1)
if [[ "$f1" =~ ^[0-9]+$ ]]; then header=false; data_line="$first"
else header=true; data_line=$(sed -n 2p "$TSV"); fi
ncol=$(printf '%s' "$data_line" | awk -F'\t' '{print NF}')
case "$ncol" in
  4) size_col=3; kind=text ;;
  5) size_col=4; kind=upload ;;
  *) echo "convert: unexpected column count $ncol in $IN (first data line: ${data_line:0:120})" >&2
     rm -f "$OUT"; exit 4 ;;
esac

rows=$(wc -l < "$TSV"); [ "$header" = true ] && rows=$((rows - 1))

"$TRACECONV" "$TSV" csv \
  -t "time-col=1,obj-id-col=2,obj-size-col=${size_col},delimiter=\t,has-header=${header},obj-id-is-num=true" \
  --output-format oracleGeneral -o "$OUT" > "${OUT}.convlog" 2>&1
rc=$?
recs=$(( $(stat -c %s "$OUT" 2>/dev/null || echo 0) / 24 ))
if [ "$rc" -ne 0 ] || [ "$recs" -ne "$rows" ]; then
  echo "convert: traceConv rc=$rc, $recs records for $rows rows ($kind, header=$header); log:" >&2
  tail -3 "${OUT}.convlog" >&2
  rm -f "$OUT" "${OUT}.convlog"; exit 5
fi
rm -f "${OUT}.convlog"
echo "convert: $kind trace, header=$header, $rows requests -> $OUT"
