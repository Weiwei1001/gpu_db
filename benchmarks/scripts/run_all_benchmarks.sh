#!/bin/bash
# Master script to re-run ALL benchmarks for Maximus and Sirius.
# Covers Category A (GPU-data), B (CPU-data), and C (energy sweep).
# Includes both standard benchmarks and microbenchmarks.
#
# On a fresh machine this script will:
#   1. Generate CSV data (TPC-H, H2O, ClickBench) if missing
#   2. Auto-build Sirius DuckDB if not built
#   3. Generate Sirius DuckDB databases + SQL query files if missing
#   4. Run all three categories of experiments
#
# Categories:
#   A – Data on GPU: timing + power/energy metrics (standard + microbench)
#   B – Data on CPU: timing + power/energy metrics
#   C – Energy sweep: 3 GPU power limits × 5 SM clock frequencies
#
# Usage:
#   bash run_all_benchmarks.sh                     # Full run (A + B + C)
#   bash run_all_benchmarks.sh --test              # Quick smoke test (3 queries per bench)
#   bash run_all_benchmarks.sh --minimum           # 8-hour budget: SF_min+SF_max, no microbench, 3×3 C sweep
#   bash run_all_benchmarks.sh --toy               # ~1-hour smoke of A1-C1: tpch only, 1 query, largest SF,
#                                                  #   metrics target-time=120s, Category C = 2×2 (4 configs)
#   bash run_all_benchmarks.sh --skip-category-c   # Skip energy sweep (A + B only)
#
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MAXIMUS_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Activate Python venv if available (needed for pip-installed cuDF/nvcomp)
if [ -f "/venv/main/bin/activate" ] && [ -z "$VIRTUAL_ENV" ]; then
    source /venv/main/bin/activate
fi

# Source environment (LD_LIBRARY_PATH for libnvcomp, libkvikio, etc.)
_SAVED_SCRIPT_DIR="$SCRIPT_DIR"
if [ -f "$MAXIMUS_DIR/setup_env.sh" ]; then
    source "$MAXIMUS_DIR/setup_env.sh"
fi
SCRIPT_DIR="$_SAVED_SCRIPT_DIR"
RESULTS_DIR="$MAXIMUS_DIR/results"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_DIR="$RESULTS_DIR/logs_${TIMESTAMP}"
mkdir -p "$LOG_DIR"

# ── Parse arguments ────────────────────────────────────────────────────────
TEST_FLAG=""
MIN_FLAG=""
TOY_FLAG=""
SKIP_CATEGORY_C=0
# Metrics sampling window (seconds). Toy mode caps it at 120s (2 min) so the
# whole A+B+C smoke completes in ~1h; full/test/minimum keep the short 5s window.
METRICS_TT=5
for arg in "$@"; do
    case "$arg" in
        --test) TEST_FLAG="--test" ;;
        --minimum|--min) MIN_FLAG="--minimum" ;;
        --toy) TOY_FLAG="--toy"; METRICS_TT=120 ;;
        --skip-category-c|--no-energy-sweep) SKIP_CATEGORY_C=1 ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done
EXTRA_FLAGS="$EXTRA_FLAGS $TEST_FLAG $MIN_FLAG $TOY_FLAG"

MODE="FULL"
[ -n "$EXTRA_FLAGS" ] && MODE="TEST"
[ -n "$TOY_FLAG" ] && MODE="TOY"

DATA_DIR="$MAXIMUS_DIR/benchmarks/data"

echo "========================================================================"
echo "  BENCHMARK SUITE ($MODE MODE)"
echo "  Started: $(date)"
echo "  Results: $RESULTS_DIR"
echo "  Logs:    $LOG_DIR"
echo "========================================================================"

# ══════════════════════════════════════════════════════════════════════════
#  Step 0a: Generate missing Maximus CSV data
# ══════════════════════════════════════════════════════════════════════════
echo ""
echo "======== STEP 0a: Generate missing CSV data ========"

# TPC-H: CSV for Maximus (tests/tpch/csv-{sf})
TPCH_SFS="1 5 10 20"
for sf in $TPCH_SFS; do
    if [ ! -d "$MAXIMUS_DIR/tests/tpch/csv-$sf" ]; then
        echo "  [DATAGEN] TPC-H SF=$sf CSV..."
        (cd "$MAXIMUS_DIR/tests/tpch" && python3 generate_data.py --sf "$sf") 2>&1 | tail -3
    fi
done

# H2O: CSV for Maximus (tests/h2o/csv-{sf})
H2O_SFS="1gb 2gb 4gb 8gb"
H2O_MISSING=""
for sf in $H2O_SFS; do
    if [ ! -d "$MAXIMUS_DIR/tests/h2o/csv-$sf" ]; then
        H2O_MISSING="$H2O_MISSING $sf"
    fi
done
if [ -n "$H2O_MISSING" ]; then
    echo "  [DATAGEN] H2O:$H2O_MISSING ..."
    mkdir -p "$MAXIMUS_DIR/tests/h2o"
    python3 "$DATA_DIR/generate_h2o.py" --format csv \
        -o "$MAXIMUS_DIR/tests/h2o" $H2O_MISSING 2>&1 | tail -5
fi

# ClickBench: CSV for Maximus (tests/clickbench/csv-{sf})
# Requires downloading ~14GB parquet, so skip if no internet or in test mode
CB_SFS="1 5 10 20"
CB_MISSING=""
for sf in $CB_SFS; do
    if [ ! -d "$MAXIMUS_DIR/tests/clickbench/csv-$sf" ]; then
        CB_MISSING="$CB_MISSING $sf"
    fi
done
if [ -n "$CB_MISSING" ]; then
    mkdir -p "$MAXIMUS_DIR/tests/clickbench"
    PARQUET_PATH="$MAXIMUS_DIR/tests/clickbench/clickbench.parquet"
    if [ ! -f "$PARQUET_PATH" ]; then
        echo "  [DATAGEN] Downloading ClickBench parquet (~14GB)..."
        wget -q --show-progress -O "$PARQUET_PATH" \
            "https://datasets.clickhouse.com/hits_compatible/hits.parquet" 2>&1 || true
    fi
    if [ -f "$PARQUET_PATH" ]; then
        echo "  [DATAGEN] ClickBench:$CB_MISSING ..."
        python3 "$DATA_DIR/generate_clickbench.py" --format csv \
            -o "$MAXIMUS_DIR/tests/clickbench" --parquet-path "$PARQUET_PATH" \
            --scales $CB_MISSING 2>&1 | tail -5
    else
        echo "  [WARN] ClickBench: parquet download failed, skipping"
    fi
fi

echo "  [DATAGEN] CSV data done."

cd "$SCRIPT_DIR"

# ══════════════════════════════════════════════════════════════════════════
#  Step 0b: Build binaries (Maximus + Sirius)
# ══════════════════════════════════════════════════════════════════════════
echo ""
echo "======== STEP 0b: Check/build binaries ========"

# Check if maxbench is built
MAXBENCH_BIN="$MAXIMUS_DIR/build/benchmarks/maxbench"
if [ ! -x "$MAXBENCH_BIN" ]; then
    echo "ERROR: maxbench binary not found at $MAXBENCH_BIN"
    echo "       Run: ninja -C build -j\$(nproc)"
    exit 1
fi
echo "  [OK] Maximus maxbench: $MAXBENCH_BIN"

# Check if sirius duckdb binary exists; auto-build if missing
SIRIUS_DUCKDB="$MAXIMUS_DIR/sirius/build/release/duckdb"
if [ ! -x "$SIRIUS_DUCKDB" ] && [ -f "$MAXIMUS_DIR/sirius_patches/build_sirius.sh" ]; then
    echo "  [AUTO] Sirius not built — running build_sirius.sh..."
    bash "$MAXIMUS_DIR/sirius_patches/build_sirius.sh" 2>&1 | tail -20
fi
has_sirius() {
    [ -x "$SIRIUS_DUCKDB" ]
}
if has_sirius; then
    echo "  [OK] Sirius DuckDB: $SIRIUS_DUCKDB"
else
    echo "  [WARN] Sirius DuckDB not available — Sirius steps will be skipped"
fi

# ══════════════════════════════════════════════════════════════════════════
#  Step 0c: Generate Sirius DuckDB databases + SQL query files
# ══════════════════════════════════════════════════════════════════════════
if has_sirius; then
    echo ""
    echo "======== STEP 0c: Generate Sirius data ========"

    # Generate Sirius SQL query files (standard + microbench, idempotent)
    if [ ! -d "$MAXIMUS_DIR/tests/tpch_sql/queries/1" ] || \
       [ ! -d "$MAXIMUS_DIR/tests/h2o_sql/queries/1" ] || \
       [ ! -d "$MAXIMUS_DIR/tests/click_sql/queries/1" ] || \
       [ ! -d "$MAXIMUS_DIR/tests/case_bench_sql/queries/1" ] || \
       [ ! -d "$MAXIMUS_DIR/tests/microbench_tpch_sql/queries/1" ]; then
        echo "  [DATAGEN] Generating Sirius SQL query files..."
        python3 "$SCRIPT_DIR/generate_sirius_sql.py" \
            --output-dir "$MAXIMUS_DIR/tests" 2>&1 | tail -5
    else
        echo "  [OK] Sirius SQL query files already exist"
    fi

    # Generate DuckDB databases from CSV (TPC-H, H2O)
    TPCH_TABLES="lineitem orders customer part partsupp supplier nation region"
    mkdir -p "$MAXIMUS_DIR/tests/tpch_duckdb" "$MAXIMUS_DIR/tests/h2o_duckdb"

    for sf in $TPCH_SFS; do
        DB="$MAXIMUS_DIR/tests/tpch_duckdb/tpch_sf${sf}.duckdb"
        CSV_DIR="$MAXIMUS_DIR/tests/tpch/csv-${sf}"
        if [ -f "$DB" ]; then
            continue
        fi
        if [ -d "$CSV_DIR" ]; then
            echo "  [DATAGEN] Creating tpch_sf${sf}.duckdb..."
            python3 -c "
import duckdb, os
conn = duckdb.connect('$DB')
for table in '$TPCH_TABLES'.split():
    csv_path = os.path.join('$CSV_DIR', table + '.csv')
    if os.path.exists(csv_path):
        conn.execute(f\"CREATE TABLE {table} AS SELECT * FROM read_csv_auto('{csv_path}')\")
conn.close()
" 2>&1 | tail -3
        fi
    done

    for sf in $H2O_SFS; do
        DB="$MAXIMUS_DIR/tests/h2o_duckdb/h2o_${sf}.duckdb"
        CSV_DIR="$MAXIMUS_DIR/tests/h2o/csv-${sf}"
        if [ -f "$DB" ]; then
            continue
        fi
        if [ -d "$CSV_DIR" ]; then
            echo "  [DATAGEN] Creating h2o_${sf}.duckdb..."
            python3 -c "
import duckdb
conn = duckdb.connect('$DB')
conn.execute(\"CREATE TABLE groupby AS SELECT * FROM read_csv_auto('${CSV_DIR}/groupby.csv')\")
conn.close()
" 2>&1 | tail -3
        fi
    done

    # Generate DuckDB databases from CSV (ClickBench)
    mkdir -p "$MAXIMUS_DIR/tests/click_duckdb"
    for sf in $CB_SFS; do
        DB="$MAXIMUS_DIR/tests/click_duckdb/clickbench_${sf}.duckdb"
        CSV_DIR="$MAXIMUS_DIR/tests/clickbench/csv-${sf}"
        if [ -f "$DB" ]; then
            continue
        fi
        if [ -d "$CSV_DIR" ] && [ -f "$CSV_DIR/t.csv" ]; then
            echo "  [DATAGEN] Creating clickbench_${sf}.duckdb..."
            python3 -c "
import duckdb
conn = duckdb.connect('$DB')
conn.execute(\"CREATE TABLE hits AS SELECT * FROM read_csv_auto('${CSV_DIR}/t.csv')\")
conn.close()
" 2>&1 | tail -3
        fi
    done

    echo "  [DATAGEN] Sirius data done."
fi

# ── GPU memory check (informational) ─────────────────────────────────────
echo ""
echo "======== GPU Memory Check ========"
GPU_VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits -i \
    $(python3 -c "from hw_detect import detect_gpu; print(detect_gpu()['index'])") 2>/dev/null | head -1)
echo "  GPU VRAM: ${GPU_VRAM_MB:-unknown} MiB"
echo "  Buffer sizing and benchmark configs are auto-adjusted by hw_detect.py"

# Benchmarks used per category.
#   A (GPU-data): full set — standard SQL + microbench + case_bench.
#   B (CPU-data): standard SQL + case_bench only; microbenches are already
#                 small, fast queries where a CPU-reload measurement adds no
#                 new information, so we skip them here.
ALL_BENCH="tpch h2o clickbench case_bench microbench_tpch microbench_h2o microbench_clickbench"
CPU_BENCH="tpch h2o clickbench case_bench"

# Helper function
run_step() {
    local step_name="$1"
    shift
    echo ""
    echo "================================================================"
    echo "  STEP: $step_name"
    echo "  Command: $@"
    echo "  Time: $(date)"
    echo "================================================================"
    if "$@" 2>&1 | tee "$LOG_DIR/${step_name}.log"; then
        echo "  DONE: $step_name ($(date))"
    else
        echo "  WARN: $step_name exited non-zero ($(date))"
    fi
}

# ══════════════════════════════════════════════════════════════════════════
#  Category A: Data on GPU (-s gpu) — timing + power/energy metrics
#  Standard SQL benchmarks + microbenchmarks (GPU memory auto-checked)
# ══════════════════════════════════════════════════════════════════════════

echo ""
echo "======== CATEGORY A: Data on GPU (timing + metrics) ========"

# A1: Maximus timing (standard + microbench)
run_step "A1_maximus_timing" \
    python3 run_maximus_benchmark.py $EXTRA_FLAGS --n-reps 3 --results-dir "$RESULTS_DIR" \
    $ALL_BENCH

# A2: Sirius timing (standard SQL only, no microbench)
if has_sirius; then
    run_step "A2_sirius_timing" \
        python3 run_sirius_benchmark.py $EXTRA_FLAGS --results-dir "$RESULTS_DIR" \
        $ALL_BENCH
else
    echo "  [SKIP] A2: Sirius not built"
fi

# A3: Maximus metrics (standard + microbench) — reuse A1 timing to skip calibration
run_step "A3_maximus_metrics" \
    python3 run_maximus_metrics.py $EXTRA_FLAGS --target-time $METRICS_TT --results-dir "$RESULTS_DIR" \
    --timing-csv "$RESULTS_DIR/maximus_benchmark.csv" \
    $ALL_BENCH

# A4: Sirius metrics (standard SQL only)
if has_sirius; then
    run_step "A4_sirius_metrics" \
        python3 run_sirius_metrics.py $EXTRA_FLAGS --target-time $METRICS_TT --results-dir "$RESULTS_DIR" \
        $ALL_BENCH
else
    echo "  [SKIP] A4: Sirius metrics: binary not found"
fi

# ══════════════════════════════════════════════════════════════════════════
#  Category B: Data on CPU (-s cpu) — timing + power/energy metrics
# ══════════════════════════════════════════════════════════════════════════

echo ""
echo "======== CATEGORY B: Data on CPU (timing + metrics) ========"

# B1: Maximus CPU-data timing (standard + case_bench, skip microbench)
run_step "B1_maximus_cpu_timing" \
    python3 run_maximus_cpu_data.py $EXTRA_FLAGS --timing-only --results-dir "$RESULTS_DIR" \
    $CPU_BENCH

# B2: Maximus CPU-data metrics — reuse B1 timing to skip calibration
run_step "B2_maximus_cpu_metrics" \
    python3 run_maximus_cpu_data.py $EXTRA_FLAGS --target-time $METRICS_TT --results-dir "$RESULTS_DIR" \
    --timing-csv "$RESULTS_DIR/maximus_cpu_data_timing.csv" \
    $CPU_BENCH

# B3: Sirius CPU-data timing + metrics (standard + case_bench)
if has_sirius; then
    run_step "B3_sirius_cpu_data" \
        python3 run_sirius_cpu_data.py $EXTRA_FLAGS --n-reps 10 --results-dir "$RESULTS_DIR" \
        $CPU_BENCH
else
    echo "  [SKIP] B3: Sirius CPU-data: binary not found"
fi

# ══════════════════════════════════════════════════════════════════════════
#  Category C: Energy sweep (3 GPU power limits × 5 SM clock frequencies)
#  Only for tpch and h2o benchmarks
# ══════════════════════════════════════════════════════════════════════════

if [ "$SKIP_CATEGORY_C" -eq 1 ]; then
    echo ""
    echo "======== CATEGORY C: Energy Sweep — SKIPPED (--skip-category-c) ========"
else
    echo ""
    echo "======== CATEGORY C: Energy Sweep (3 PL × 5 freq) ========"

    run_step "C1_energy_sweep" \
        python3 run_energy_sweep.py $EXTRA_FLAGS \
        --benchmarks tpch h2o clickbench case_bench \
        --results-dir "$RESULTS_DIR/energy_sweep" \
        --resume
fi

# ══════════════════════════════════════════════════════════════════════════
#  Energy Summary: aggregate Category A metrics into unified energy report
# ══════════════════════════════════════════════════════════════════════════

echo ""
echo "======== ENERGY SUMMARY ========"

run_step "energy_summary" \
    python3 compute_energy_summary.py --latest --results-dir "$RESULTS_DIR" \
    --output "$RESULTS_DIR/energy_summary.csv"

# ══════════════════════════════════════════════════════════════════════════
#  Verification: compare results against baseline
# ══════════════════════════════════════════════════════════════════════════

echo ""
echo "======== VERIFICATION: Compare against baseline ========"

BASELINE_DIR="$RESULTS_DIR/baseline"
if [ -f "$BASELINE_DIR/baseline_latency.csv" ]; then
    run_step "verify_results" \
        python3 verify_results.py --log-dir "$LOG_DIR" --baseline-dir "$BASELINE_DIR"
else
    echo "  [SKIP] No baseline found at $BASELINE_DIR"
    echo "  To create a baseline, copy test_latency.csv and test_energy.csv to $BASELINE_DIR/"
fi

echo ""
echo "========================================================================"
echo "  ALL BENCHMARKS COMPLETE ($MODE MODE)"
echo "  Finished: $(date)"
echo "  Results:  $RESULTS_DIR"
echo "  Logs:     $LOG_DIR"
echo "========================================================================"
