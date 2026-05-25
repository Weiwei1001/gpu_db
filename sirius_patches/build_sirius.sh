#!/bin/bash
# Build Sirius (DuckDB GPU extension) from submodule sources.
# Applies compatibility patches for:
#   - spdlog/fmt namespace conflict with DuckDB's bundled fmt
#   - CUDA 12.6 compat (cudaMemcpySrcAccessOrder is 12.8+)
#   - libconfig++ cmake finder
#   - fmt v10 enum formatting
#
# Usage:
#   bash sirius_patches/build_sirius.sh          # Build release
#   bash sirius_patches/build_sirius.sh --clean   # Clean + rebuild
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SIRIUS_DIR="$REPO_DIR/sirius"
DUCKDB_BIN="$SIRIUS_DIR/build/release/duckdb"

if [ "${1:-}" = "--clean" ]; then
    echo "[build_sirius] Cleaning previous build..."
    rm -rf "$SIRIUS_DIR/build/release"
fi

# Skip if already built
if [ -x "$DUCKDB_BIN" ]; then
    echo "[build_sirius] Sirius DuckDB binary already exists: $DUCKDB_BIN"
    echo "[build_sirius] Use --clean to force rebuild."
    exit 0
fi

echo "[build_sirius] Building Sirius DuckDB..."

# ── 0. Init submodules ──
cd "$REPO_DIR"
git submodule update --init --recursive

# ── 1. Install system deps ──
#
# Sirius' cmake configure fails silently ("provides a separate development
# package or SDK, be sure it has been installed") when any of these is missing.
# We install the full set up front (idempotent) so the build is reliable on
# a fresh machine without depending on setup.sh having run exactly these
# packages already. If `sudo` is unavailable we run as-is (root in container).
echo "[build_sirius] Installing system dependencies..."
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
fi
$SUDO apt-get update
$SUDO apt-get install -y --no-install-recommends \
    build-essential g++ ninja-build pkg-config ca-certificates wget curl git \
    libnuma-dev libconfig++-dev libssl-dev zlib1g-dev \
    libboost-all-dev libsnappy-dev libbrotli-dev libthrift-dev libre2-dev \
    rapidjson-dev python3 python3-pip

# Ensure CUDA 12.6 nvcc + dev packages (cuDF 26.x CCCL headers need >= 12.6).
NVCC_VER=$(nvcc --version 2>/dev/null | grep "release" | sed 's/.*release //' | sed 's/,.*//')
NVCC_MINOR=$(echo "$NVCC_VER" | cut -d. -f2)
if [ "${NVCC_MINOR:-0}" -lt 6 ]; then
    echo "[build_sirius] Installing CUDA 12.6 nvcc + dev packages..."
    $SUDO apt-get install -y --no-install-recommends \
        cuda-nvcc-12-6 cuda-nvml-dev-12-6 libcurand-dev-12-6 cuda-cudart-dev-12-6
fi

# Verify the critical headers/libs landed; fail fast rather than at cmake time.
for check in \
    "/usr/include/libconfig.h++:libconfig++-dev" \
    "/usr/include/numa.h:libnuma-dev"; do
    path="${check%%:*}"; pkg="${check##*:}"
    if [ ! -f "$path" ]; then
        echo "[build_sirius] ERROR: $path missing after installing $pkg." >&2
        echo "                 Try: $SUDO apt-get install -y $pkg" >&2
        exit 1
    fi
done

# Abseil >= 20230125 (for absl::any_invocable)
if ! pkg-config --atleast-version=20230125 absl_any_invocable 2>/dev/null; then
    if [ ! -f /usr/local/lib/cmake/absl/abslConfig.cmake ] || \
       ! grep -q "20240722" /usr/local/lib/cmake/absl/abslConfigVersion.cmake 2>/dev/null; then
        echo "[build_sirius] Building abseil-cpp 20240722..."
        ABSL_TMP=$(mktemp -d)
        git clone --depth 1 --branch 20240722.0 https://github.com/abseil/abseil-cpp.git "$ABSL_TMP/absl"
        cmake -B "$ABSL_TMP/absl/build" -GNinja "$ABSL_TMP/absl" \
            -DCMAKE_BUILD_TYPE=Release \
            -DCMAKE_INSTALL_PREFIX=/usr/local \
            -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
            -DABSL_BUILD_TESTING=OFF \
            -DCMAKE_CXX_STANDARD=17 >/dev/null
        ninja -C "$ABSL_TMP/absl/build" >/dev/null
        # Installing to /usr/local needs root on a sudo-user machine (only root
        # containers can write there without sudo). Use $SUDO like the apt step.
        $SUDO ninja -C "$ABSL_TMP/absl/build" install >/dev/null
        rm -rf "$ABSL_TMP"
        echo "[build_sirius] abseil installed."
    fi
fi

# ── 2. Build spdlog from source with bundled fmt ──
#    System spdlog uses fmt 9 which conflicts with DuckDB's duckdb_fmt namespace.
SPDLOG_CONFIG="/usr/local/lib/cmake/spdlog/spdlogConfig.cmake"
if [ ! -f "$SPDLOG_CONFIG" ]; then
    echo "[build_sirius] Building spdlog 1.14.1 with bundled fmt..."
    SPDLOG_TMP=$(mktemp -d)
    git clone --depth 1 --branch v1.14.1 https://github.com/gabime/spdlog.git "$SPDLOG_TMP/spdlog"
    cmake -B "$SPDLOG_TMP/spdlog/build" -GNinja "$SPDLOG_TMP/spdlog" \
        -DCMAKE_BUILD_TYPE=Release \
        -DSPDLOG_FMT_EXTERNAL=OFF \
        -DSPDLOG_BUILD_SHARED=OFF \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON >/dev/null
    ninja -C "$SPDLOG_TMP/spdlog/build" >/dev/null
    # Needs root to install into /usr/local (see abseil note above).
    $SUDO ninja -C "$SPDLOG_TMP/spdlog/build" install >/dev/null
    rm -rf "$SPDLOG_TMP"
    echo "[build_sirius] spdlog installed."
fi

# ── 3. Create nvcomp .so symlinks (pip only ships .so.5) ──
# Locate the site-packages dir that actually has the cuDF wheels. pip without
# --user normally writes to purelib, but on systems where purelib isn't
# writable it falls back to user-site (~/.local/lib/pythonX.Y/site-packages),
# and `sysconfig.get_path('purelib')` would point at an empty system dir.
SITE_PKGS=""
for cand in \
    "$(python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))' 2>/dev/null)" \
    "$(python3 -c 'import site; print(site.getusersitepackages())' 2>/dev/null)" \
    "/usr/local/lib/python3.10/dist-packages" \
    "/usr/lib/python3/dist-packages" \
    "$HOME/.local/lib/python3.10/site-packages"; do
    if [ -n "$cand" ] && [ -d "$cand/libcudf" ]; then
        SITE_PKGS="$cand"
        break
    fi
done
if [ -z "$SITE_PKGS" ]; then
    SITE_PKGS=$(python3 -c "import sysconfig; print(sysconfig.get_path('purelib'))")
fi
echo "[build_sirius] Python site-packages with cuDF: $SITE_PKGS"
NVCOMP_DIR="$SITE_PKGS/nvidia/libnvcomp/lib64"
if [ -f "$NVCOMP_DIR/libnvcomp.so.5" ] && [ ! -f "$NVCOMP_DIR/libnvcomp.so" ]; then
    ln -sf "$NVCOMP_DIR/libnvcomp.so.5" "$NVCOMP_DIR/libnvcomp.so"
    ln -sf "$NVCOMP_DIR/libnvcomp_cpu.so.5" "$NVCOMP_DIR/libnvcomp_cpu.so"
    echo "[build_sirius] Created nvcomp .so symlinks."
fi

# ── 4. Apply patches ──
echo "[build_sirius] Applying compatibility patches..."

# Copy cmake finder module
cp "$SCRIPT_DIR/Findlibconfig++.cmake" "$SIRIUS_DIR/cmake/"

# Apply sirius source patches
cd "$SIRIUS_DIR"
if git diff --quiet; then
    git apply "$SCRIPT_DIR/sirius.patch"
    echo "  Applied sirius.patch"
else
    echo "  Sirius already has local changes, skipping patch"
fi

# Apply cucascade patches
cd "$SIRIUS_DIR/cucascade"
if git diff --quiet; then
    git apply "$SCRIPT_DIR/cucascade.patch"
    echo "  Applied cucascade.patch"
else
    echo "  cuCascade already has local changes, skipping patch"
fi

# ── 5. Detect GPU architecture ──
GPU_ARCH=""
if command -v nvidia-smi &>/dev/null; then
    # Get compute capability from nvidia-smi
    CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d '.')
    if [ -n "$CC" ]; then
        GPU_ARCH="$CC"
    fi
fi
if [ -z "$GPU_ARCH" ]; then
    GPU_ARCH="native"
fi
echo "[build_sirius] GPU architecture: $GPU_ARCH"

# ── 6. Build cmake prefix paths from pip-installed AND conda-installed RAPIDS ──
# On systems where the system Python (e.g. 3.8) cannot install cuDF 26.x,
# setup.sh falls back to conda env "maximus_gpu" with libcudf 24.12. We must
# also add that env's cmake config dir, otherwise sirius cmake fails with
# "Could not find a package configuration file provided by 'cudf'".
CMAKE_PREFIXES=""
SEARCH_DIRS=(
    "$SITE_PKGS/libcudf/lib64/cmake"
    "$SITE_PKGS/librmm/lib64/cmake"
    "$SITE_PKGS/nvidia/libnvcomp/lib64/cmake"
    "$SITE_PKGS/libcudf/lib64/rapids/cmake"
    "$SITE_PKGS/librmm/lib64/rapids/cmake"
    "$SITE_PKGS/libkvikio/lib64/cmake"
    "$SITE_PKGS/rapids_logger/lib64/cmake"
    "$SITE_PKGS/lib64/cmake"
    "$SITE_PKGS/nvidia/cuda_cccl/lib/cmake"
    "$SITE_PKGS/nvidia/libnvcomp/lib64/cmake/nvcomp"
    "/usr/local/lib/cmake"
)

# Detect conda env (CONDA_PREFIX if active, plus the canonical maximus_gpu env
# from setup.sh's fallback path) and add its cmake config dirs.
CONDA_CANDIDATES=()
if [ -n "${CONDA_PREFIX:-}" ]; then
    CONDA_CANDIDATES+=("$CONDA_PREFIX")
fi
for c in \
    "${WORKSPACE:-$REPO_DIR/..}/miniconda3/envs/maximus_gpu" \
    "$HOME/miniconda3/envs/maximus_gpu" \
    "/opt/conda/envs/maximus_gpu"; do
    [ -d "$c" ] && CONDA_CANDIDATES+=("$c")
done
for env in "${CONDA_CANDIDATES[@]}"; do
    SEARCH_DIRS+=(
        "$env/lib/cmake/cudf"
        "$env/lib/cmake/rmm"
        "$env/lib/cmake/nvcomp"
        "$env/lib/cmake/kvikio"
        "$env/lib/cmake/rapids_logger"
        "$env/lib/cmake"
        "$env"
    )
done

for pkg_dir in "${SEARCH_DIRS[@]}"; do
    if [ -d "$pkg_dir" ]; then
        CMAKE_PREFIXES="${CMAKE_PREFIXES:+$CMAKE_PREFIXES;}$pkg_dir"
    fi
done

# Verify cudf-config.cmake actually exists somewhere we'll search; fail loudly
# with diagnostics rather than letting cmake produce its terser error.
CUDF_CFG=$(find ${CMAKE_PREFIXES//;/ } \
    -maxdepth 4 -type f \( -name 'cudf-config.cmake' -o -name 'cudfConfig.cmake' \) \
    2>/dev/null | head -1)
if [ -z "$CUDF_CFG" ]; then
    echo "[build_sirius] ERROR: cudf-config.cmake not found in any search path." >&2
    echo "                 Searched (CMAKE_PREFIX_PATH):" >&2
    echo "$CMAKE_PREFIXES" | tr ';' '\n' | sed 's/^/                   /' >&2
    echo "" >&2
    echo "Likely cause: cuDF was not installed. setup.sh installs cuDF in Step 4" >&2
    echo "via either pip (cudf-cu12==26.2.1, requires Python >= 3.10) or conda" >&2
    echo "fallback (libcudf=24.12 in env 'maximus_gpu'). Verify one of:" >&2
    echo "  - python3 -c 'import cudf'                                    # pip path" >&2
    echo "  - ls \$WORKSPACE/miniconda3/envs/maximus_gpu/lib/cmake/cudf/  # conda path" >&2
    exit 1
fi
echo "[build_sirius] Found cudf cmake config: $CUDF_CFG"

# ── 7. Configure ──
echo "[build_sirius] Configuring (full log: $REPO_DIR/logs/sirius_cmake.log)..."
mkdir -p "$REPO_DIR/logs"
cd "$SIRIUS_DIR/duckdb"
if ! cmake -B ../build/release -GNinja \
        -DCMAKE_BUILD_TYPE=Release \
        -DEXTENSION_STATIC_BUILD=ON \
        -DDUCKDB_EXTENSION_CONFIGS="../extension_config.cmake" \
        -DCMAKE_CUDA_ARCHITECTURES="$GPU_ARCH" \
        -DCMAKE_PREFIX_PATH="$CMAKE_PREFIXES" \
        -DCMAKE_MODULE_PATH="$SIRIUS_DIR/cmake" \
        > "$REPO_DIR/logs/sirius_cmake.log" 2>&1; then
    echo "[build_sirius] cmake configure FAILED. Tail of log:" >&2
    tail -40 "$REPO_DIR/logs/sirius_cmake.log" >&2
    exit 1
fi

# ── 8. Build ──
echo "[build_sirius] Building (full log: $REPO_DIR/logs/sirius_ninja.log; ~5-15 min)..."
if ! ninja -C ../build/release -j"$(nproc)" \
        > "$REPO_DIR/logs/sirius_ninja.log" 2>&1; then
    echo "[build_sirius] ninja build FAILED. Tail of log:" >&2
    tail -60 "$REPO_DIR/logs/sirius_ninja.log" >&2
    exit 1
fi

# ── 9. Verify ──
if [ -x "$DUCKDB_BIN" ]; then
    VERSION=$("$DUCKDB_BIN" --version 2>/dev/null || echo "unknown")
    echo "[build_sirius] SUCCESS: $DUCKDB_BIN ($VERSION)"
else
    echo "[build_sirius] FAILED: duckdb binary not found (ninja returned 0 but target missing)"
    exit 1
fi
