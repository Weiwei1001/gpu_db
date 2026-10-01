#!/bin/bash
# 阶段 1：从源码构建 HeavyDB（上游 master b348f14）。
# 官方 mapd-deps-prebuilt.sh 已失效（dependencies.mapd.com 域名不存在），
# 依赖改为 apt + 4 个源码构建。每一步幂等，重跑会跳过已完成的部分。
set -euo pipefail
: "${HB_ROOT:?}" "${HEAVYDB_HOME:?}" "${DEPS_PREFIX:?}"
JOBS="${BUILD_JOBS:-$(nproc)}"
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
[ -x "$CUDA_HOME/bin/nvcc" ] || { echo "!! 找不到 $CUDA_HOME/bin/nvcc，请设 CUDA_HOME"; exit 1; }
export PATH="$CUDA_HOME/bin:$PATH" CUDACXX="$CUDA_HOME/bin/nvcc"   # cmake 的 enable_language(CUDA) 靠这个找 nvcc
DL="$HB_ROOT/deps-src"; mkdir -p "$DL"
log(){ echo "[$(date +%T)] [build] $*"; }

# ---------- 1. apt ----------
log "apt 依赖"
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  gcc-11 g++-11 cmake ninja-build bison flex maven openjdk-21-jdk-headless \
  llvm-14-dev clang-14 libclang-14-dev libboost-all-dev \
  libgdal-dev gdal-data libproj-dev proj-data libgeos-dev libgeos++-dev \
  libtbb-dev libblosc-dev libarchive-dev libcurl4-openssl-dev libssl-dev \
  libbz2-dev liblz4-dev libzstd-dev libsnappy-dev liblzma-dev \
  libdouble-conversion-dev libevent-dev libunwind-dev libxerces-c-dev \
  libpng-dev libjpeg-dev libtiff-dev libgif-dev libwebp-dev \
  libncurses-dev libsqlite3-dev librdkafka-dev liburiparser-dev libpcre2-dev \
  python3-venv python3-pip curl git ca-certificates >/dev/null
sudo mkdir -p "$DEPS_PREFIX"

fetch(){ [ -f "$DL/$2" ] || curl -sSL --retry 3 -o "$DL/$2" "$1"; }

# ---------- 2. Thrift 0.20.0 ----------
if [ ! -f "$DEPS_PREFIX/lib/libthrift.so" ]; then
  log "构建 Thrift 0.20.0"
  fetch https://archive.apache.org/dist/thrift/0.20.0/thrift-0.20.0.tar.gz thrift-0.20.0.tar.gz
  cd "$DL" && rm -rf thrift-0.20.0 && tar xf thrift-0.20.0.tar.gz && cd thrift-0.20.0
  CC=gcc-11 CXX=g++-11 CFLAGS="-fPIC" CXXFLAGS="-fPIC" ./configure --prefix="$DEPS_PREFIX" \
    --enable-libs=off --with-cpp --without-go --without-python --without-java \
    --without-nodejs --without-rust --without-php --without-ruby --without-erlang \
    --without-haskell --without-c_glib --without-swift --without-netstd --without-lua > "$HB_ROOT/logs/thrift.log" 2>&1
  make -j"$JOBS" >> "$HB_ROOT/logs/thrift.log" 2>&1 && sudo make install >> "$HB_ROOT/logs/thrift.log" 2>&1
else log "Thrift 已存在，跳过"; fi

# ---------- 3. Arrow 18.1.0（含 CUDA + Parquet）----------
if [ ! -f "$DEPS_PREFIX/lib/libarrow_cuda.so" ]; then
  log "构建 Arrow 18.1.0"
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
else log "Arrow 已存在，跳过"; fi

# ---------- 4. CPR 1.11.2 ----------
if [ ! -f "$DEPS_PREFIX/lib/libcpr.so" ]; then
  log "构建 CPR 1.11.2"
  fetch https://github.com/libcpr/cpr/archive/refs/tags/1.11.2.tar.gz cpr-1.11.2.tar.gz
  cd "$DL" && rm -rf cpr-1.11.2 && tar xf cpr-1.11.2.tar.gz && mkdir -p cpr-1.11.2/build && cd cpr-1.11.2/build
  CC=gcc-11 CXX=g++-11 cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$DEPS_PREFIX" \
    -DCPR_USE_SYSTEM_CURL=ON -DCPR_ENABLE_SSL=ON -DBUILD_SHARED_LIBS=ON -DCPR_BUILD_TESTS=OFF .. > "$HB_ROOT/logs/cpr.log" 2>&1
  make -j"$JOBS" >> "$HB_ROOT/logs/cpr.log" 2>&1 && sudo make install >> "$HB_ROOT/logs/cpr.log" 2>&1
else log "CPR 已存在，跳过"; fi

# ---------- 5. H3 4.2.0 ----------
if [ ! -f "$DEPS_PREFIX/lib/libh3.so" ] && [ ! -f "$DEPS_PREFIX/lib/libh3.a" ]; then
  log "构建 H3 4.2.0"
  fetch https://github.com/uber/h3/archive/refs/tags/v4.2.0.tar.gz h3-4.2.0.tar.gz
  cd "$DL" && rm -rf h3-4.2.0 && tar xf h3-4.2.0.tar.gz && mkdir -p h3-4.2.0/build && cd h3-4.2.0/build
  CC=gcc-11 CXX=g++-11 cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$DEPS_PREFIX" \
    -DBUILD_TESTING=OFF -DENABLE_DOCS=OFF -DBUILD_FILTERS=OFF -DBUILD_GENERATORS=OFF \
    -DBUILD_BENCHMARKS=OFF -DBUILD_FUZZERS=OFF -DCMAKE_POSITION_INDEPENDENT_CODE=ON .. > "$HB_ROOT/logs/h3.log" 2>&1
  make -j"$JOBS" >> "$HB_ROOT/logs/h3.log" 2>&1 && sudo make install >> "$HB_ROOT/logs/h3.log" 2>&1
else log "H3 已存在，跳过"; fi

# ---------- 6. HeavyDB 源码 ----------
if [ ! -d "$HEAVYDB_HOME/.git" ]; then
  log "克隆 heavydb"
  git clone -q https://github.com/heavyai/heavydb.git "$HEAVYDB_HOME" || { log "!! 克隆 heavydb 失败（网络？）"; exit 1; }
fi
cd "$HEAVYDB_HOME"
git checkout -q "${HEAVYDB_COMMIT:-b348f14}"
[ -f LICENSE.md ] || cp LICENSE.txt LICENSE.md          # 上游坑 #2：CMake 要 LICENSE.md

# ---------- 7. 配置 + 编译 ----------
# ENABLE_ONLY_ONE_ARCH 只生成一种架构的 PTX：cmake 在配置时探测它看到的第一块卡。
# 所以配置时用 CUDA_VISIBLE_DEVICES 锁定目标卡（$GPU），并把探测到的架构记下来；
# 下次目标卡架构不同（例如先 A100 后 H100）就重新配置+编译。
WANT_CC=$(nvidia-smi -i "${GPU:-0}" --query-gpu=compute_cap --format=csv,noheader | tr -d '. ')
HAVE_CC=$(cat "$HEAVYDB_HOME/build/NvidiaComputeCapability.txt" 2>/dev/null | tr -d ' ' || true)
if [ -x "$HEAVYDB_HOME/build/bin/heavydb" ] && [ -n "$HAVE_CC" ] && [ "$HAVE_CC" != "$WANT_CC" ]; then
  log "已有二进制是 compute_$HAVE_CC，目标 GPU $GPU 是 compute_$WANT_CC：重新编译"
  rm -f "$HEAVYDB_HOME/build/bin/heavydb" "$HEAVYDB_HOME/build/NvidiaComputeCapability.txt"
fi
if [ ! -x "$HEAVYDB_HOME/build/bin/heavydb" ]; then
  log "cmake 配置（gcc-11, CUDA, 单架构 compute_$WANT_CC，探测锁定 GPU ${GPU:-0}）"
  mkdir -p build && cd build && rm -f CMakeCache.txt        # 上次失败的缓存会带坏本次配置
  rm -rf NvidiaComputeCapability NvidiaComputeCapability.txt   # 旧的探测结果会被直接复用
  CUDA_VISIBLE_DEVICES="${GPU:-0}" \
  cmake -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER=/usr/bin/gcc-11 -DCMAKE_CXX_COMPILER=/usr/bin/g++-11 \
    -DCMAKE_CUDA_COMPILER="$CUDA_HOME/bin/nvcc" -DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-11 \
    -DCMAKE_PREFIX_PATH="$DEPS_PREFIX;/usr/lib/llvm-14" \
    -DENABLE_CUDA=on -DENABLE_ONLY_ONE_ARCH=on \
    -DENABLE_AWS_S3=off -DENABLE_PDAL=off -DENABLE_ONEDAL=off -DENABLE_SAML=off \
    -DENABLE_TESTS=off -DENABLE_GEOS=ON -DENABLE_IMPORT_PARQUET=ON .. > "$HB_ROOT/logs/cmake.log" 2>&1
  log "编译（约 30 分钟，$JOBS 线程）"
  make -j"$JOBS" > "$HB_ROOT/logs/make.log" 2>&1
  GOT_CC=$(cat NvidiaComputeCapability.txt 2>/dev/null | tr -d ' ' || true)
  [ "$GOT_CC" = "$WANT_CC" ] || log "!! 注意：cmake 探测到 compute_${GOT_CC:-?}，目标卡是 compute_$WANT_CC（见 logs/cmake.log）"
else log "heavydb 二进制已存在（compute_$HAVE_CC），跳过编译"; cd "$HEAVYDB_HOME/build"; fi

# ---------- 8. 上游坑 #4：PROJ/GDAL 数据文件 ----------
mkdir -p ThirdParty
[ -e ThirdParty/proj ] || ln -s /usr/share/proj ThirdParty/proj
[ -e ThirdParty/gdal ] || ln -s /usr/share/gdal ThirdParty/gdal

# ---------- 9. 初始化数据目录 ----------
if [ ! -d data/catalogs ]; then
  log "initheavy"
  export LD_LIBRARY_PATH="$DEPS_PREFIX/lib:/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}"
  mkdir -p data && ./bin/initheavy data > "$HB_ROOT/logs/initheavy.log" 2>&1
fi
log "构建完成：$HEAVYDB_HOME/build/bin/heavydb"
