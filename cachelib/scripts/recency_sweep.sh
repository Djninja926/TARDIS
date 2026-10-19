#!/usr/bin/env bash
# ITEM 7: does scaling the recency buffer fix the thread-induced gap decay?
#
# USAGE: edit recentWindow in MMLruForgive.h, rebuild, then run:
#     bash recency_sweep.sh <buffer_size_label>
#   e.g.  bash recency_sweep.sh 64
#
# The label is recorded in the CSV; it must match what was compiled in. There is
# no runtime check, so mislabeling silently corrupts the comparison. Verify with:
#     grep -n "recentWindow{" cachelib/allocator/MMLruForgive.h
#
# Only lruforgive is run: LRU has no recency buffer, so the LRU numbers already
# in mt_results.csv remain valid for computing the gap at every cell.
#
# The FULL thread sweep is run at each buffer size on purpose. Comparing only
# t=16 cannot distinguish "bigger buffer is better everywhere" (which would show
# at t=1 too) from "bigger buffer fixes thread dilution" (which would show as a
# flatter decay curve with an unchanged t=1). The curve SHAPE is the result.

set -u
BUF=${1:?usage: recency_sweep.sh <buffer_size_label, e.g. 64>}
BUILD=/mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench/_build
T=/mydata/tardis/traces
OUT=/mydata/tardis/recency_results.csv
NUMA="numactl --cpunodebind=0 --membind=0"

[ -f "$OUT" ] || echo "recent_window,trace,threads,hashpower,run,miss_ratio,mqps" > "$OUT"

# msr_proj_0 DECAYS with threads (6.47 -> 2.00pp): dilution should be fixable here.
# cluster53 IMPROVES with threads (1.92 -> 2.88pp): control, should be unaffected
#   if the improvement has nothing to do with the buffer.
for spec in \
  "msr_proj_0.oracleGeneral 256 24" \
  "cluster53.oracleGeneral 128 23"; do
  set -- $spec; tr=$1; sz=$2; hpb=$3
  echo "===== $tr @${sz}MB, recentWindow=$BUF ====="
  for th in 1 2 4 8 16; do
    case $th in 1) inc=0;; 2) inc=1;; 4) inc=2;; 8) inc=3;; 16) inc=4;; esac
    hp=$((hpb + inc))
    for r in 1 2 3; do
      grep -qE "^$BUF,$tr,$th,$hp,$r,[0-9]" "$OUT" && { echo "[have] t=$th run=$r"; continue; }
      line=$(timeout 3600 $NUMA "$BUILD/lruforgive" "$T/$tr" "$sz" "$hp" "$th" 1 2>&1 | tail -1)
      mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
      tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
      echo "  buf=$BUF $tr t=$th hp=$hp run=$r miss=${mr:-FAIL} mqps=$tp"
      echo "$BUF,$tr,$th,$hp,$r,${mr:-FAIL},$tp" >> "$OUT"
    done
  done
done
echo "DONE (recentWindow=$BUF) -> $OUT"
