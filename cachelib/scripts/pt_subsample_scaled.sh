#!/bin/bash
cd /mydata/tardis/sosp23-s3fifo/cachelib-sosp23/mybench
TR=/mydata/tardis/traces
OUT=/mydata/tardis/pt_subsample_scaled_$(date +%Y%m%d_%H%M%S).csv
echo "mode,rate,t,rep,miss,tp" > "$OUT"
for cfg in "1 512 25" "4 2048 27" "8 4096 28" "16 8192 29"; do
  read -r t sz hp <<< "$cfg"
  for rate in 1.0 0.5 0.25; do
    for r in 1 2; do
      line=$(TARDIS_SAMPLE_MODE=0 TARDIS_SAMPLE_RATE=$rate numactl --membind=0 _build/lruforgive $TR/cache-t-00.oracleGeneral $sz $hp $t 0 2>/dev/null | tail -1)
      mr=$(echo "$line" | grep -oE 'miss ratio [0-9.]+' | awk '{print $3}')
      tp=$(echo "$line" | grep -oE 'throughput [0-9.]+' | awk '{print $2}')
      echo "0,$rate,$t,$r,${mr:-NA},${tp:-NA}" >> "$OUT"
      echo "  rate=$rate t=$t rep=$r -> mr=$mr tp=$tp"
    done
  done
done
echo "DONE -> $OUT"
