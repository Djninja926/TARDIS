#!/bin/bash
# node_tune_worker.sh <study-shard> <output-csv>
#
# Node-side wrapper for tuning studies, so orchestrate.py can launch tuning the
# same way as a sweep:
#   python orchestrate.py --config nodes8.yaml --shards tune_plan --launch \
#       --run tune --worker node_tune_worker.sh
#
# Env knobs (pass with --env): TUNE_PAR_TRIALS (4), TUNE_PAR_TRACES (4),
# EMB_MULT (5), MIN_CACHE_OBJS (1000), CELL_TIMEOUT (7200 s per cachesim run).
# Concurrency is TUNE_PAR_TRIALS x TUNE_PAR_TRACES cachesim processes.
set -uo pipefail

SHARD="${1:?study shard (one JSON study per line)}"
OUT="${2:?output csv}"
TARDIS="${TARDIS:-/mydata/tardis}"
SCRIPTS="${SCRIPTS:-$TARDIS/scripts-repo/setup-scripts}"

if [ -z "${CACHESIM:-}" ]; then
  if [ -f "$TARDIS/cachesim_path.txt" ]; then CACHESIM=$(cat "$TARDIS/cachesim_path.txt")
  else CACHESIM=/mydata/cachesim/_build/bin/cachesim; fi
fi
python3 -c "import optuna" 2>/dev/null || {
  echo "ERROR: optuna is not installed on $(hostname). Rerun orchestrate.py --setup."; exit 1; }

echo "[$(hostname)] tuning: shard=$SHARD out=$OUT cachesim=$CACHESIM"
echo "[$(hostname)] parallel: ${TUNE_PAR_TRIALS:-4} trials x ${TUNE_PAR_TRACES:-4} traces"
cd "$SCRIPTS" || exit 1       # tune_study.py imports parse_cachesim from here
exec python3 -u "$SCRIPTS/tune_study.py" --shard "$SHARD" --out "$OUT" \
  --work "${WORK:-/mydata/tune_work}" --cachesim "$CACHESIM"
