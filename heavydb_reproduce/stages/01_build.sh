#!/bin/bash
# Stage 1: build HeavyDB from source (upstream master b348f14).
# The official mapd-deps-prebuilt.sh no longer works (the dependencies.mapd.com domain does not exist),
# so dependencies come from apt + 4 from-source builds. Every step is idempotent; a rerun skips finished parts.
set -euo pipefail
: "${HB_ROOT:?}" "${HEAVYDB_HOME:?}" "${DEPS_PREFIX:?}"
JOBS="${BUILD_JOBS:-$(nproc)}"
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
[ -x "$CUDA_HOME/bin/nvcc" ] || { echo "!! $CUDA_HOME/bin/nvcc not found, please set CUDA_HOME"; exit 1; }
export PATH="$CUDA_HOME/bin:$PATH" CUDACXX="$CUDA_HOME/bin/nvcc"   # cmake's enable_language(CUDA) finds nvcc through this
DL="$HB_ROOT/deps-src"; mkdir -p "$DL"
log(){ echo "[$(date +%T)] [build] $*"; }

# ---------- 1. apt ----------
# Everything apt says goes to logs/apt.log. On failure we print the lines that name the
# package whose configure step failed: "dpkg returned an error code (1)" alone never says which.
APT_LOG="$HB_ROOT/logs/apt.log"; : > "$APT_LOG"
export DEBIAN_FRONTEND=noninteractive
log "apt dependencies (log: $APT_LOG)"
# A package left half-configured by an earlier install on this machine makes every later
# apt-get install fail with a "followup error". Finish or repair that first.
sudo dpkg --configure -a >> "$APT_LOG" 2>&1 || log "!! dpkg --configure -a reported errors (see $APT_LOG); trying apt-get -f install"
sudo apt-get -f install -y >> "$APT_LOG" 2>&1 || true
sudo apt-get update -qq >> "$APT_LOG" 2>&1 || log "!! apt-get update reported errors (see $APT_LOG); continuing"
if ! sudo apt-get install -y --no-install-recommends \
  gcc-11 g++-11 cmake ninja-build bison flex maven openjdk-21-jdk-headless \
  llvm-14-dev clang-14 libclang-14-dev libboost-all-dev \
  libgdal-dev gdal-data libproj-dev proj-data libgeos-dev libgeos++-dev \
  libtbb-dev libblosc-dev libarchive-dev libcurl4-openssl-dev libssl-dev \
  libbz2-dev liblz4-dev libzstd-dev libsnappy-dev liblzma-dev \
  libdouble-conversion-dev libevent-dev libunwind-dev libxerces-c-dev \
  libpng-dev libjpeg-dev libtiff-dev libgif-dev libwebp-dev \
  libncurses-dev libsqlite3-dev librdkafka-dev liburiparser-dev libpcre2-dev \
  python3-venv python3-pip curl git ca-certificates >> "$APT_LOG" 2>&1; then
  log "!! apt-get install failed. Lines naming the failing package(s):"
  grep -nE "dpkg: error|dpkg: dependency problems|Errors were encountered|^E: |not installable|has no installation candidate" -A2 "$APT_LOG" | head -40
  log "!! Full log: $APT_LOG  (Ubuntu $(. /etc/os-release && echo "$VERSION_ID"); the package list targets 22.04/24.04)"
  exit 1
fi
sudo mkdir -p "$DEPS_PREFIX"

fetch(){ [ -f "$DL/$2" ] || curl -sSL --retry 3 -o "$DL/$2" "$1"; }

# ---------- 2. Thrift 0.20.0 ----------
if [ ! -f "$DEPS_PREFIX/lib/libthrift.so" ]; then
  log "building Thrift 0.20.0"
  fetch https://archive.apache.org/dist/thrift/0.20.0/thrift-0.20.0.tar.gz thrift-0.20.0.tar.gz
  cd "$DL" && rm -rf thrift-0.20.0 && tar xf thrift-0.20.0.tar.gz && cd thrift-0.20.0
  CC=gcc-11 CXX=g++-11 CFLAGS="-fPIC" CXXFLAGS="-fPIC" ./configure --prefix="$DEPS_PREFIX" \
    --enable-libs=off --with-cpp --without-go --without-python --without-java \
    --without-nodejs --without-rust --without-php --without-ruby --without-erlang \
    --without-haskell --without-c_glib --without-swift --without-netstd --without-lua > "$HB_ROOT/logs/thrift.log" 2>&1
  make -j"$JOBS" >> "$HB_ROOT/logs/thrift.log" 2>&1 && sudo make install >> "$HB_ROOT/logs/thrift.log" 2>&1
else log "Thrift already present, skipping"; fi

# ---------- 3. Arrow 18.1.0 (with CUDA + Parquet) ----------
if [ ! -f "$DEPS_PREFIX/lib/libarrow_cuda.so" ]; then
  log "building Arrow 18.1.0"
  fetch https://github.com/apache/arrow/archive/refs/tags/apache-arrow-18.1.0.tar.gz arrow-18.1.0.tar.gz
  cd "$DL" && rm -rf arrow-apache-arrow-18.1.0 && tar xf arrow-18.1.0.tar.gz
  mkdir -p arrow-apache-arrow-18.1.0/cpp/build && cd arrow-apache-arrow-18.1.0/cpp/build
  CC=gcc-11 CXX=g++-11 cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$DEPS_PREFIX" \
    -DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-11 \
    -DARROW_BUILD_SHARED=ON -DARROW_BUILD_STATIC=OFF -DARROW_DEPENDENCY_USE_SHARED=ON \
    -DARROW_CSV=ON -DARROW_JSON=ON -DARROW_IPC=ON -DARROW_COMPUTE=ON \
    -DARROW_WITH_BROTLI=ON -DARROW_WITH_SNAPPY=ON -DARROW_WITH_ZLIB=ON \
    -DARROW_WITH_ZSTD=ON -DARROW_WITH_LZ4=ON -DARROW_WITH_BZ2=ON \
    -DARROW_USE_GLOG=OFF -DARROW_PARQUET=ON -DARROW_FILESYSTEM=ON -DARROW_S3=OFF \
    -DARROW_CUDA=ON -DARROW_JEMALLOC=ON -DARROW_BUILD_TESTS=OFF \
    -DThrift_SOURCE=SYSTEM -DCMAKE_PREFIX_PATH="$DEPS_PREFIX" -DTHRIFT_ROOT="$DEPS_PREFIX" .. > "$HB_ROOT/logs/arrow.log" 2>&1
  make -j"$JOBS" >> "$HB_ROOT/logs/arrow.log" 2>&1 && sudo make install >> "$HB_ROOT/logs/arrow.log" 2>&1
else log "Arrow already present, skipping"; fi

# ---------- 4. CPR 1.11.2 ----------
if [ ! -f "$DEPS_PREFIX/lib/libcpr.so" ]; then
  log "building CPR 1.11.2"
  fetch https://github.com/libcpr/cpr/archive/refs/tags/1.11.2.tar.gz cpr-1.11.2.tar.gz
  cd "$DL" && rm -rf cpr-1.11.2 && tar xf cpr-1.11.2.tar.gz && mkdir -p cpr-1.11.2/build && cd cpr-1.11.2/build
  CC=gcc-11 CXX=g++-11 cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$DEPS_PREFIX" \
    -DCPR_USE_SYSTEM_CURL=ON -DCPR_ENABLE_SSL=ON -DBUILD_SHARED_LIBS=ON -DCPR_BUILD_TESTS=OFF .. > "$HB_ROOT/logs/cpr.log" 2>&1
  make -j"$JOBS" >> "$HB_ROOT/logs/cpr.log" 2>&1 && sudo make install >> "$HB_ROOT/logs/cpr.log" 2>&1
else log "CPR already present, skipping"; fi

# ---------- 5. H3 4.2.0 ----------
if [ ! -f "$DEPS_PREFIX/lib/libh3.so" ] && [ ! -f "$DEPS_PREFIX/lib/libh3.a" ]; then
  log "building H3 4.2.0"
  fetch https://github.com/uber/h3/archive/refs/tags/v4.2.0.tar.gz h3-4.2.0.tar.gz
  cd "$DL" && rm -rf h3-4.2.0 && tar xf h3-4.2.0.tar.gz && mkdir -p h3-4.2.0/build && cd h3-4.2.0/build
  CC=gcc-11 CXX=g++-11 cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$DEPS_PREFIX" \
    -DBUILD_TESTING=OFF -DENABLE_DOCS=OFF -DBUILD_FILTERS=OFF -DBUILD_GENERATORS=OFF \
    -DBUILD_BENCHMARKS=OFF -DBUILD_FUZZERS=OFF -DCMAKE_POSITION_INDEPENDENT_CODE=ON .. > "$HB_ROOT/logs/h3.log" 2>&1
  make -j"$JOBS" >> "$HB_ROOT/logs/h3.log" 2>&1 && sudo make install >> "$HB_ROOT/logs/h3.log" 2>&1
else log "H3 already present, skipping"; fi

# ---------- 6. HeavyDB source ----------
if [ ! -d "$HEAVYDB_HOME/.git" ]; then
  log "cloning heavydb"
  git clone -q https://github.com/heavyai/heavydb.git "$HEAVYDB_HOME" || { log "!! cloning heavydb failed (network?)"; exit 1; }
fi
cd "$HEAVYDB_HOME"
git checkout -q "${HEAVYDB_COMMIT:-b348f14}"
[ -f LICENSE.md ] || cp LICENSE.txt LICENSE.md          # upstream pitfall #2: CMake wants LICENSE.md

# ---------- 7. Configure + compile ----------
# ENABLE_ONLY_ONE_ARCH generates PTX for a single architecture: cmake probes the first GPU it sees at configure time.
# So at configure time we pin the target GPU ($GPU) with CUDA_VISIBLE_DEVICES and record the detected architecture;
# if the target GPU's architecture differs next time (e.g. A100 first, then H100), reconfigure + recompile.
WANT_CC=$(nvidia-smi -i "${GPU:-0}" --query-gpu=compute_cap --format=csv,noheader | tr -d '. ')
HAVE_CC=$(cat "$HEAVYDB_HOME/build/NvidiaComputeCapability.txt" 2>/dev/null | tr -d ' ' || true)
if [ -x "$HEAVYDB_HOME/build/bin/heavydb" ] && [ -n "$HAVE_CC" ] && [ "$HAVE_CC" != "$WANT_CC" ]; then
  log "existing binary is compute_$HAVE_CC, target GPU $GPU is compute_$WANT_CC: recompiling"
  rm -f "$HEAVYDB_HOME/build/bin/heavydb" "$HEAVYDB_HOME/build/NvidiaComputeCapability.txt"
fi
if [ ! -x "$HEAVYDB_HOME/build/bin/heavydb" ]; then
  log "cmake configure (gcc-11, CUDA, single arch compute_$WANT_CC, probe pinned to GPU ${GPU:-0})"
  mkdir -p build && cd build && rm -f CMakeCache.txt        # a cache from a failed earlier run would poison this configure
  rm -rf NvidiaComputeCapability NvidiaComputeCapability.txt   # an old probe result would be reused as-is
  CUDA_VISIBLE_DEVICES="${GPU:-0}" \
  cmake -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER=/usr/bin/gcc-11 -DCMAKE_CXX_COMPILER=/usr/bin/g++-11 \
    -DCMAKE_CUDA_COMPILER="$CUDA_HOME/bin/nvcc" -DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-11 \
    -DCMAKE_PREFIX_PATH="$DEPS_PREFIX;/usr/lib/llvm-14" \
    -DENABLE_CUDA=on -DENABLE_ONLY_ONE_ARCH=on \
    -DENABLE_AWS_S3=off -DENABLE_PDAL=off -DENABLE_ONEDAL=off -DENABLE_SAML=off \
    -DENABLE_TESTS=off -DENABLE_GEOS=ON -DENABLE_IMPORT_PARQUET=ON .. > "$HB_ROOT/logs/cmake.log" 2>&1
  log "compiling (about 30 min, $JOBS threads)"
  make -j"$JOBS" > "$HB_ROOT/logs/make.log" 2>&1
  GOT_CC=$(cat NvidiaComputeCapability.txt 2>/dev/null | tr -d ' ' || true)
  [ "$GOT_CC" = "$WANT_CC" ] || log "!! note: cmake detected compute_${GOT_CC:-?}, target GPU is compute_$WANT_CC (see logs/cmake.log)"
else log "heavydb binary already present (compute_$HAVE_CC), skipping compile"; cd "$HEAVYDB_HOME/build"; fi

# ---------- 8. Upstream pitfall #4: PROJ/GDAL data files ----------
mkdir -p ThirdParty
[ -e ThirdParty/proj ] || ln -s /usr/share/proj ThirdParty/proj
[ -e ThirdParty/gdal ] || ln -s /usr/share/gdal ThirdParty/gdal

# ---------- 9. Initialize the data directory ----------
if [ ! -d data/catalogs ]; then
  log "initheavy"
  export LD_LIBRARY_PATH="$DEPS_PREFIX/lib:/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}"
  mkdir -p data && ./bin/initheavy data > "$HB_ROOT/logs/initheavy.log" 2>&1
fi
log "build complete: $HEAVYDB_HOME/build/bin/heavydb"
