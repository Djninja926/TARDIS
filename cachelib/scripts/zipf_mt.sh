#!/bin/bash
cd /mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench
[ "$(cat /sys/devices/system/cpu/intel_pstate/no_turbo)" = "1" ] || echo "WARN turbo"
TRACE=zipf1.0_1_100.oracleGeneral.bin   # adjust path if not in mybench/
OUT=/mydata/tardis/results/zipf_mt_s3fifo.csv
echo "policy,threads,rep,miss,tp" > "$OUT"
median() { printf '%s\n' "$@" | sort -n | awk '{a[NR]=$1} END{print (NR%2)?a[(NR+1)/2]:(a[NR/2]+a[NR/2+1])/2}'; }
for nThread in 1 2 4 8 16; do
  sz=$((4000 * nThread))
  hp=$(echo "21 + l($nThread)/l(2)" | bc -l | cut -d'.' -f1)
  for pol in s3fifo s3fifoforgive; do
    for rep in 1 2 3 4 5; do
      line=$(numactl --membind=0 _build/$pol $TRACE $sz $hp $nThread 2>/dev/null | tail -1)
      mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
      tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
      echo "$pol,$nThread,$rep,${mr:-NA},${tp:-NA}" >> "$OUT"
      echo "  $pol t=$nThread rep=$rep -> mr=$mr tp=$tp"
    done
  done
done
echo "DONE -> $OUT"
