#!/usr/bin/env bash
# node_setup.sh - bring up a fresh CloudLab node for the multi-node all-traces sweep.
# Clones the tardis scripts repo, builds libCacheSim (for traceAnalyzer), clones and
# builds the jane-perthread fork. Does NOT download traces (the worker streams them).
#
# Run on a fresh node AFTER:  sudo chown -R $USER:$(id -gn) /mydata
# Use identical hardware across all nodes so throughput numbers stay comparable.
set -u

TARDIS=/mydata/tardis
ARTIFACT=https://github.com/Thesys-lab/sosp23-s3fifo.git
FORK=https://github.com/Djninja926/cachelib-sosp23.git
BRANCH=jane-perthread
SCRIPTS_REPO=https://github.com/Djninja926/tardis.git

echo "############ 0. sanity ############"
if [ ! -w /mydata ]; then
  echo "ERROR: /mydata not writable. Run:  sudo chown -R \$USER:\$(id -gn) /mydata"
  exit 1
fi
mkdir -p "$TARDIS"
# numactl: needed for NUMA pinning on multi-socket nodes (e.g. C220G2) so
# throughput numbers stay consistent. The worker uses it only if present.
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y numactl >/dev/null 2>&1 && echo "numactl installed" || echo "WARN: numactl install failed (worker will run without pinning)"

echo "############ 1. scripts repo (workers + helpers) ############"
cd "$TARDIS"
[ -d scripts-repo ] || git clone "$SCRIPTS_REPO" scripts-repo
# worker + orchestration helpers live in scripts-repo/setup-scripts/

echo "############ 2. clone the SOSP'23 artifact (ships libCacheSim) ############"
cd "$TARDIS"
[ -d sosp23-s3fifo ] || git clone "$ARTIFACT" sosp23-s3fifo

echo "############ 3. build libCacheSim (gives traceAnalyzer for footprint sizing) ############"
cd "$TARDIS/sosp23-s3fifo"
pushd libCacheSim/scripts >/dev/null
bash install_dependency.sh
bash install_libcachesim.sh
popd >/dev/null
# sanity: traceAnalyzer must exist
if [ ! -x "$TARDIS/sosp23-s3fifo/libCacheSim/_build/bin/traceAnalyzer" ]; then
  echo "ERROR: traceAnalyzer not built; footprint sizing will fail."; exit 1
fi

echo "############ 4. clone + checkout the fork (jane-perthread) IN PLACE ############"
cd "$TARDIS/sosp23-s3fifo"
[ -d cachelib-sosp23 ] || git clone "$FORK" cachelib-sosp23
cd cachelib-sosp23
git fetch --all
git checkout "$BRANCH"
git pull origin "$BRANCH" || true
echo "code: $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"

# FIX: the fork's cachelib/external/zstd is a broken submodule (a gitlink in the
# index with no .gitmodules URL). This breaks the build two ways: (1) build.sh's
# `git submodule update --init` dies with "No url found for submodule ...zstd",
# and (2) build-package.sh later expects a real facebook/zstd clone with an
# origin/release branch. Fix BOTH: remove the gitlink from the index and drop any
# stale submodule config, then replace the dir with a real facebook/zstd clone so
# `git checkout origin/release` succeeds. Removing the gitlink is what lets
# `git submodule update` skip zstd instead of aborting.
echo "  [zstd fix] removing broken zstd gitlink + re-cloning facebook/zstd"
git rm -r --cached cachelib/external/zstd 2>/dev/null || true
git config -f .gitmodules --remove-section submodule.cachelib/external/zstd 2>/dev/null || true
git config --remove-section submodule.cachelib/external/zstd 2>/dev/null || true
rm -rf cachelib/external/zstd
git clone https://github.com/facebook/zstd cachelib/external/zstd

echo "############ 5. turboboost OFF (consistent throughput) ############"
cd "$TARDIS/sosp23-s3fifo/cachelib-sosp23/mybench"
bash turboboost.sh disable || echo "WARN: could not disable turbo (need root?)"

echo "############ 6. build cachelib + mybench (30-60 min) ############"
bash build.sh 2>&1 | tee /tmp/build.log | tail -2
echo "FAILED lines: $(grep -c FAILED /tmp/build.log)  (should be 0)"
for b in lru s3fifo lruforgive s3fifoforgive; do
  [ -x "_build/$b" ] && echo "  OK  $b" || echo "  MISSING $b <-- build problem"
done

echo "############ done ############"
echo "traceAnalyzer: $TARDIS/sosp23-s3fifo/libCacheSim/_build/bin/traceAnalyzer"
echo "binaries:      $TARDIS/sosp23-s3fifo/cachelib-sosp23/mybench/_build/"
echo "Next: run the worker with this node's trace-shard list:"
echo "  bash $TARDIS/scripts-repo/setup-scripts/node_trace_worker.sh <shard-list> <out.csv>"
