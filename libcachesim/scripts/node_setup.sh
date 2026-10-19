#!/usr/bin/env bash
# node_setup.sh - prepare a CloudLab node for the libCacheSim sweep.
#
# orchestrate.py --setup clones this repo to /mydata/tardis/scripts-repo and runs
# this. It builds Aryan's libCacheSim fork (embeddings branch) into
# /mydata/cachesim, checks that both forgive policies are compiled in, builds
# traceConv (Wikimedia conversion) and installs Optuna (tuning).
# Traces are not downloaded here; node_worker.sh streams them one at a time.
set -u

TARDIS=/mydata/tardis
LCS=/mydata/cachesim
LCS_REPO=https://github.com/Aryan470/libCacheSim.git
LCS_BRANCH=embeddings

echo "############ 0. sanity ############"
if [ ! -w /mydata ]; then
  echo "ERROR: /mydata not writable. Run:  sudo chown -R \$USER:\$(id -gn) /mydata"
  exit 1
fi
mkdir -p "$TARDIS/results"

# The worker needs wget + zstd only. numactl is not used: this is a miss-ratio
# sweep, and cachesim parallelizes internally across the algo x size grid.
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y wget zstd tmux python3-pip >/dev/null 2>&1 \
  && echo "wget + zstd + tmux + pip ok" || echo "WARN: apt-get install wget zstd tmux python3-pip failed"

# optuna: used only by tuning (tune.py run). A failure here
# doesn't affect sweeps, so warn rather than stop.
if ! python3 -c "import optuna" 2>/dev/null; then
  python3 -m pip install --user -q optuna 2>/dev/null \
    || python3 -m pip install --user -q --break-system-packages optuna 2>/dev/null
fi
python3 -c "import optuna; print('optuna', optuna.__version__, 'ok')" 2>/dev/null \
  || echo "WARN: optuna not installed (tuning runs will fail; sweeps unaffected)"

echo "############ 1. clone Aryan's libCacheSim fork ($LCS_BRANCH) ############"
if [ -d "$LCS/.git" ]; then
  cd "$LCS"
  git fetch origin "$LCS_BRANCH" && git checkout "$LCS_BRANCH" && git pull --ff-only origin "$LCS_BRANCH"
else
  rm -rf "$LCS"
  git clone --branch "$LCS_BRANCH" "$LCS_REPO" "$LCS" || { echo "ERROR: clone failed"; exit 1; }
fi
cd "$LCS"
echo "code: $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"

echo "############ 2. dependencies (cmake, ninja, glib, tcmalloc, zstd) ############"
# Aryan's fork (newer libCacheSim) installs its OWN CMake into ~/software/cmake
# and only puts it on PATH inside that script's process (plus ~/.bashrc, which a
# non-interactive ssh session never reads). The SOSP'23 artifact's script used
# the system cmake instead, which is why this step used to just work. So put
# that directory on PATH for the build below.
export PATH="$HOME/software/cmake/bin:$PATH"
if [ "${FORCE_DEPS:-0}" != "1" ] && command -v cmake >/dev/null 2>&1 \
     && command -v ninja >/dev/null 2>&1 && pkg-config --exists glib-2.0 2>/dev/null; then
  echo "dependencies already installed (cmake $(cmake --version | head -1 | awk '{print $3}')); skipping (FORCE_DEPS=1 to redo)"
else
  bash scripts/install_dependency.sh 2>&1 | tail -5
fi
hash -r
if ! command -v cmake >/dev/null 2>&1; then
  echo "cmake still not on PATH; installing the distro package as a fallback"
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y cmake ninja-build >/dev/null 2>&1
  hash -r
fi
command -v cmake >/dev/null 2>&1 || { echo "ERROR: no cmake available; cannot build"; exit 1; }
echo "using $(command -v cmake) ($(cmake --version | head -1))"

echo "############ 3. build (cmake -G Ninja + ninja) ############"
bash scripts/install_libcachesim.sh 2>&1 | tee /tmp/lcs_build.log | tail -5

BIN="$LCS/_build/bin/cachesim"
if [ ! -x "$BIN" ]; then
  # the build script targets <repo>/_build, but don't guess if the layout moved
  FOUND=$(find "$LCS" -type f -name cachesim -perm -u+x 2>/dev/null | head -1)
  if [ -n "$FOUND" ]; then
    echo "NOTE: cachesim not at the expected path; found at $FOUND"
    BIN="$FOUND"
  else
    echo "ERROR: cachesim was not built. Last 30 lines of the build log:"
    tail -30 /tmp/lcs_build.log
    exit 1
  fi
fi

echo "############ 4. verify the two embedding policies are registered ############"
# cache_init.h maps these strings; if the binary lacks them the sweep would run
# but silently produce nothing for the policies we actually care about.
MISSING=0
for algo in lruforgiveembcache s3fifoforgive-embcache; do
  if strings "$BIN" | grep -qx "$algo"; then
    echo "  OK       $algo"
  else
    echo "  MISSING  $algo  <-- not registered in this build"
    MISSING=1
  fi
done
[ "$MISSING" = "1" ] && { echo "ERROR: expected policies missing; stopping."; exit 1; }

# traceConv converts the Wikimedia 2019 TSV traces to oracleGeneral
# (convert_wikimedia.sh). install_libcachesim.sh builds every target, so it
# sits next to cachesim; build it explicitly if a partial build missed it.
TCONV="$(dirname "$BIN")/traceConv"
if [ ! -x "$TCONV" ]; then
  (cd "$(dirname "$(dirname "$BIN")")" && ninja traceConv >/dev/null 2>&1)
fi
if [ -x "$TCONV" ]; then
  echo "  OK       traceConv"
  echo "$TCONV" > "$TARDIS/traceconv_path.txt"
else
  echo "  WARN     traceConv missing: Wikimedia traces will fail to convert (NA_CONVERT)"
fi

echo "############ done ############"
echo "cachesim: $BIN"
echo "$BIN" > "$TARDIS/cachesim_path.txt"
echo "Ready. Start runs from your machine with orchestrate.py --launch."
