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
#   bash run_all_benchmarks.sh --fix-A100          # Cat-C only: complete the A100 energy sweep
#   bash run_all_benchmarks.sh --fix-H100          # Cat-C only: complete the H100 energy sweep
#     The two --fix-* flags re-run ONLY Category C to fill the gaps left by the
#     original sweeps (clickbench / case_bench / h2o-8gb on A100; smoke-mode
#     --test runs on H100). They purge degenerate (smoke / zero-timing) summary
#     files, then `run_energy_sweep.py --resume` over that GPU's canonical
#     5x5 (PL x SM-clock) grid so only the missing (bench, sf, config) points run.
#     Run each on its matching machine. A/B categories are left untouched.
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
FIX_GPU=""
# Metrics sampling window (seconds). Toy mode caps it at 120s (2 min) so the
# whole A+B+C smoke completes in ~1h; full/test/minimum keep the short 5s window.
METRICS_TT=5
for arg in "$@"; do
    case "$arg" in
        --test) TEST_FLAG="--test" ;;
        --minimum|--min) MIN_FLAG="--minimum" ;;
        --toy) TOY_FLAG="--toy"; METRICS_TT=120 ;;
        --skip-category-c|--no-energy-sweep) SKIP_CATEGORY_C=1 ;;
        --fix-A100|--fix-a100) FIX_GPU="A100" ;;
        --fix-H100|--fix-h100) FIX_GPU="H100" ;;
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
            # Freshness guard: rebuild if DB size is >10x off the CSVs or
            # the main `lineitem` table is missing/empty.
            if python3 "$SCRIPT_DIR/check_duckdb.py" --db "$DB" \
                 --ref "$CSV_DIR"/*.csv --table lineitem; then
                continue
            fi
            echo "  [DATAGEN] tpch_sf${sf}.duckdb failed freshness check — rebuilding"
            rm -f "$DB"
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
            # Freshness guard: rebuild if DB size is >10x off the CSV or the
            # `groupby` table is missing/empty.
            if python3 "$SCRIPT_DIR/check_duckdb.py" --db "$DB" \
                 --ref "$CSV_DIR/groupby.csv" --table groupby; then
                continue
            fi
            echo "  [DATAGEN] h2o_${sf}.duckdb failed freshness check — rebuilding"
            rm -f "$DB"
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
    #
    # Freshness guard: a stale / incomplete / empty clickbench DB that already
    # exists would otherwise be reused silently (the regression where Sirius
    # clickbench queries returned in ~5ms with the GPU idle -> 0 time/energy).
    # check_duckdb.py deletes-and-rebuilds when the DB size is >10x off from
    # t.csv OR when table `t` is missing/empty. NOTE: the table is named `t`
    # (NOT `hits`) to match the Sirius SQL, which queries `FROM t`.
    mkdir -p "$MAXIMUS_DIR/tests/click_duckdb"
    for sf in $CB_SFS; do
        DB="$MAXIMUS_DIR/tests/click_duckdb/clickbench_${sf}.duckdb"
        CSV_DIR="$MAXIMUS_DIR/tests/clickbench/csv-${sf}"
        CSV="$CSV_DIR/t.csv"
        [ -f "$CSV" ] || continue
        if [ -f "$DB" ]; then
            if python3 "$SCRIPT_DIR/check_duckdb.py" --db "$DB" --ref "$CSV" --table t; then
                continue   # existing DB is valid (size + table t non-empty)
            else
                echo "  [DATAGEN] clickbench_${sf}.duckdb failed freshness check — rebuilding"
                rm -f "$DB"
            fi
        fi
        echo "  [DATAGEN] Creating clickbench_${sf}.duckdb..."
        python3 -c "
import duckdb
conn = duckdb.connect('$DB')
conn.execute(\"CREATE TABLE t AS SELECT * FROM read_csv_auto('${CSV}')\")
conn.close()
" 2>&1 | tail -3
        # Verify the freshly-built DB actually populated table t.
        python3 "$SCRIPT_DIR/check_duckdb.py" --db "$DB" --ref "$CSV" --table t \
            || echo "  [WARN] clickbench_${sf}.duckdb still invalid after rebuild — check t.csv"
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

# ══════════════════════════════════════════════════════════════════════════
#  --fix-A100 / --fix-H100 : Category-C-only repair
#
#  Re-runs ONLY the energy sweep to fill the gaps in a previous run:
#    * A100: the original sweep predates clickbench/case_bench and full SF, so
#            those (plus h2o 8gb) are simply absent — a plain --resume adds them.
#    * H100: parts of the sweep ran in --test (smoke) mode, leaving 3-query
#            summaries that --resume mistakes for "done"; we purge those first.
#  Each GPU's canonical 5x5 grid is pinned so aggregation matches the existing
#  summary and no new off-grid config dirs are created. Data is already ensured
#  by Step 0a/0c above; the metrics subprocesses also auto-generate any missing
#  CSV via ensure_maximus_csv (clickbench needs the ~14GB parquet + internet).
# ══════════════════════════════════════════════════════════════════════════
run_category_c_fix() {
    local gpu="$1"
    local PL CLK
    case "$gpu" in
        A100) PL="150,188,225,262,300"; CLK="210,510,810,1110,1410" ;;
        H100) PL="200,228,255,282,310"; CLK="345,705,1065,1425,1785" ;;
        *) echo "ERROR: unknown --fix target '$gpu'"; return 2 ;;
    esac
    local SWEEP_DIR="$RESULTS_DIR/energy_sweep"

    echo ""
    echo "========================================================================"
    echo "  CATEGORY C FIX — $gpu"
    echo "  Grid:      PL=[$PL] W  x  SM=[$CLK] MHz  (5x5 = 25 configs)"
    echo "  Sweep dir: $SWEEP_DIR"
    echo "  Started:   $(date)"
    echo "========================================================================"

    # Step 1: validate H2O CSV datasets by ROW COUNT and rebuild any that are
    #         short/missing. Step 0a only checks the dir EXISTS, so a dataset
    #         left short by an interrupted/old generation (observed: A100
    #         csv-4gb) is never rebuilt and makes a larger SF measure CHEAPER
    #         than a smaller one. Rebuilding the data is the real fix; purge
    #         (step 2) then drops the stale summaries so resume re-measures.
    echo ""
    echo "---- Fix step 1/4: validate + regenerate short H2O datasets ----"
    python3 "$SCRIPT_DIR/validate_h2o_data.py" "$MAXIMUS_DIR/tests/h2o" \
        --scales 1gb 2gb 4gb 8gb --regenerate \
        2>&1 | tee "$LOG_DIR/fix_${gpu}_h2o_validate.log" || true

    # Step 2: purge degenerate (smoke / zero-timing) and cross-SF-anomalous
    #         summaries so --resume regenerates them at full query count.
    #         No-op on a clean A100 dir.
    echo ""
    echo "---- Fix step 2/4: purge degenerate / anomalous summaries ----"
    python3 "$SCRIPT_DIR/purge_degenerate_sweep.py" "$SWEEP_DIR" \
        2>&1 | tee "$LOG_DIR/fix_${gpu}_purge.log"

    # Step 3: resume the sweep over the canonical grid, all 4 benchmarks, both
    #         engines. --resume only runs (bench, sf, config) points still missing.
    echo ""
    echo "---- Fix step 3/4: resume energy sweep (fills missing points) ----"
    python3 "$SCRIPT_DIR/run_energy_sweep.py" \
        --power-limits "$PL" --sm-clocks "$CLK" \
        --engines maximus sirius \
        --benchmarks tpch h2o clickbench case_bench \
        --results-dir "$SWEEP_DIR" \
        --resume \
        2>&1 | tee "$LOG_DIR/fix_${gpu}_sweep.log"
    local rc=${PIPESTATUS[0]}

    # Step 4: report coverage of the refreshed sweep so the run can be verified
    #         at a glance (run_energy_sweep.py already re-aggregated the summary).
    echo ""
    echo "---- Fix step 4/4: coverage report ----"
    python3 - "$SWEEP_DIR/energy_sweep_summary.csv" <<'PYCOV' 2>&1 | tee "$LOG_DIR/fix_${gpu}_coverage.log" || true
import csv, sys, os
from collections import defaultdict
path = sys.argv[1]
if not os.path.exists(path):
    print(f"  [coverage] summary not found: {path}"); sys.exit(0)
q = defaultdict(set); st = defaultdict(lambda: defaultdict(int))
for r in csv.DictReader(open(path)):
    k = (r["engine"], r["benchmark"], str(r["sf"]))
    q[k].add(r["query"]); st[k][r.get("status", "OK") or "OK"] += 1
print(f"  {'engine':<8}{'benchmark':<12}{'sf':<6}{'queries':<9}status")
for k in sorted(q):
    eng, b, sf = k
    stat = ", ".join(f"{s}={n}" for s, n in sorted(st[k].items()))
    print(f"  {eng:<8}{b:<12}{sf:<6}{len(q[k]):<9}{stat}")
PYCOV

    echo ""
    echo "========================================================================"
    echo "  CATEGORY C FIX ($gpu) COMPLETE (sweep rc=$rc)"
    echo "  Summary: $SWEEP_DIR/energy_sweep_summary.csv"
    echo "  Finished: $(date)"
    echo "========================================================================"
    return "$rc"
}

if [ -n "$FIX_GPU" ]; then
    cd "$SCRIPT_DIR"
    run_category_c_fix "$FIX_GPU"
    exit $?
fi

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
