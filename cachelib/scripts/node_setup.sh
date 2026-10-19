#!/usr/bin/env bash
# node_setup.sh - build everything the CacheLib experiments need on a fresh
# CloudLab node (we used c220g2, Wisconsin).
#
#   sudo chown -R $USER:$(id -gn) /mydata      # once, on a fresh node
#   bash node_setup.sh                          # BRANCH=tardis-implementation for the single-manager design
#
# Layout it builds (CacheLib must sit INSIDE sosp23-s3fifo/ so mybench finds
# libCacheSim by relative path):
#   /mydata/tardis/
#     sosp23-s3fifo/                 the SOSP'23 S3-FIFO artifact
#       libCacheSim/                 simulator + trace tools (traceConv, traceAnalyzer, data_gen.py)
#       cachelib-sosp23/             our CacheLib fork, branch $BRANCH
#         mybench/_build/            lru, s3fifo, lruforgive, s3fifoforgive
#     scripts-repo/                  this repo (the scripts below run from here)
#     traces/                        traces (get_traces.sh)
#
# Branches of the fork (github.com/Djninja926/cachelib-sosp23):
#   jane-perthread         per-thread design, the headline (default)
#   tardis-implementation  single shared manager with the log-structured applier
set -u

ROOT=/mydata/tardis
BRANCH="${BRANCH:-jane-perthread}"
ARTIFACT=https://github.com/Thesys-lab/sosp23-s3fifo.git
FORK=https://github.com/Djninja926/cachelib-sosp23.git
REPO="${REPO:-https://github.com/Djninja926/tardis.git}"

echo "### 0. checks"
[ -w /mydata ] || { echo "ERROR: /mydata not writable. Run: sudo chown -R \$USER:\$(id -gn) /mydata"; exit 1; }
mkdir -p "$ROOT/traces" "$ROOT/results"
# numactl keeps memory on one socket (steadier throughput); runs work without it
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y numactl zstd >/dev/null 2>&1 \
  && echo "numactl, zstd installed" || echo "WARN: apt-get failed (runs continue without numactl)"

echo "### 1. this repo"
[ -d "$ROOT/scripts-repo" ] || git clone "$REPO" "$ROOT/scripts-repo"

echo "### 2. the SOSP'23 artifact (ships libCacheSim, not CacheLib)"
[ -d "$ROOT/sosp23-s3fifo" ] || git clone "$ARTIFACT" "$ROOT/sosp23-s3fifo"

echo "### 3. libCacheSim (traceConv for Wikimedia traces, traceAnalyzer for footprints)"
cd "$ROOT/sosp23-s3fifo/libCacheSim/scripts"
bash install_dependency.sh && bash install_libcachesim.sh
[ -x "$ROOT/sosp23-s3fifo/libCacheSim/_build/bin/traceAnalyzer" ] || { echo "ERROR: libCacheSim build failed"; exit 1; }

echo "### 4. the fork, branch $BRANCH, inside the artifact"
cd "$ROOT/sosp23-s3fifo"
[ -d cachelib-sosp23 ] || git clone "$FORK" cachelib-sosp23
cd cachelib-sosp23
git fetch --all -q && git checkout "$BRANCH" && git pull -q origin "$BRANCH"
echo "fork: $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"
# The fork's cachelib/external/zstd is a broken submodule (a gitlink with no
# URL): build.sh's submodule update dies on it, and the package build expects a
# real facebook/zstd clone. Drop the gitlink and clone zstd in its place.
if [ ! -d cachelib/external/zstd/.git ]; then
  git rm -r -q --cached cachelib/external/zstd 2>/dev/null || true
  git config -f .gitmodules --remove-section submodule.cachelib/external/zstd 2>/dev/null || true
  git config --remove-section submodule.cachelib/external/zstd 2>/dev/null || true
  rm -rf cachelib/external/zstd
  git clone -q https://github.com/facebook/zstd cachelib/external/zstd
fi

echo "### 5. turbo boost off (steady throughput; needs root)"
cd mybench
bash turboboost.sh disable || echo "WARN: could not disable turbo"

echo "### 6. build CacheLib + mybench (30-60 min). build.sh wipes _build/ every time."
bash build.sh 2>&1 | tee /tmp/build.log | tail -2
n=$(grep -c FAILED /tmp/build.log)
echo "FAILED lines: $n (must be 0; otherwise the binaries below are stale)"
for b in lru s3fifo lruforgive s3fifoforgive; do
  [ -x "_build/$b" ] && echo "  OK       $b" || echo "  MISSING  $b"
done
echo "next: bash $ROOT/scripts-repo/cachelib/scripts/get_traces.sh cache-t-00 msr_proj_0 cluster53 msr_prxy_1"
