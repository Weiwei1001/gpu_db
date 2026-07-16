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
#   bash run_all_benchmarks.sh --parallel 4        # Fan the comprehensive run out over 4 GPUs
#     Splits the campaign into independent per-benchmark work units (Cat A/B
#     chains + one Cat C sweep per benchmark) and runs them on N GPUs at once.
#     Each worker is pinned with CUDA_VISIBLE_DEVICES + MAXIMUS_GPU_ID (compute,
#     nvidia-smi sampling, and sweep clock control all follow the pin) and gets
#     an even slice of CPU cores via taskset when available. Cat A/B workers
#     write to results/parallel_<ts>/gpu<i>/ and are merged back afterwards;
#     sweep workers share results/energy_sweep (they partition by benchmark).
#     Caveats: host-side RAPL columns (cpu_*) are contaminated by concurrent
#     workers — GPU energy is per-device and unaffected; the Cat C sweep needs
#     passwordless sudo for nvidia-smi on every pinned GPU. Not combinable
#     with --fix-A100/--fix-H100.
#
#   bash run_all_benchmarks.sh --fix-A100          # One-click full repair of the A100 results
#   bash run_all_benchmarks.sh --fix-H100          # One-click full repair of the H100 results
#     The two --fix-* flags run a complete, self-healing repair in one shot:
#       1. validate H2O CSV row counts and regenerate any short/missing dataset
#          (fixes the A100 csv-4gb that made SF4 cheaper than SF2);
#       2. re-run Category A timing (Maximus + Sirius) — refreshes stale/failed
#          rows such as the A100 TPC-H SF20 10/22 from a pre-managed-memory run;
#       3. re-run Category A metrics (patched Sirius timing fixes energy=0 on
#          sub-ms ClickBench/microbench);
#       4. purge degenerate (smoke / zero-timing) and cross-SF-anomalous sweep
#          summaries; 5. resume the canonical 5x5 (PL x SM-clock) sweep grid for
#          the missing points; 6. re-aggregate energy_summary.csv; 7. coverage.
#     Run each on its matching machine (the grid is pinned per GPU). Category B
#     is left untouched (it was already complete); for a from-scratch run use the
#     no-flag invocation above.
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
PARALLEL_N=1
_EXPECT_PARALLEL_N=0
# Metrics sampling window (seconds). Toy mode caps it at 120s (2 min) so the
# whole A+B+C smoke completes in ~1h; full/test/minimum keep the short 5s window.
METRICS_TT=5
for arg in "$@"; do
    if [ "$_EXPECT_PARALLEL_N" -eq 1 ]; then
        PARALLEL_N="$arg"; _EXPECT_PARALLEL_N=0; continue
    fi
    case "$arg" in
        --parallel) _EXPECT_PARALLEL_N=1 ;;
        --parallel=*) PARALLEL_N="${arg#--parallel=}" ;;
        --test) TEST_FLAG="--test" ;;
        --minimum|--min) MIN_FLAG="--minimum" ;;
        --toy) TOY_FLAG="--toy"; METRICS_TT=120 ;;
        --skip-category-c|--no-energy-sweep) SKIP_CATEGORY_C=1 ;;
        --fix-A100|--fix-a100) FIX_GPU="A100" ;;
        --fix-H100|--fix-h100) FIX_GPU="H100" ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done
if ! [[ "$PARALLEL_N" =~ ^[0-9]+$ ]] || [ "$PARALLEL_N" -lt 1 ]; then
    echo "ERROR: --parallel expects a positive integer (got '$PARALLEL_N')"; exit 1
fi
if [ "$PARALLEL_N" -gt 1 ] && [ -n "$FIX_GPU" ]; then
    echo "ERROR: --parallel cannot be combined with --fix-A100/--fix-H100"; exit 1
fi
if [ "$PARALLEL_N" -gt 1 ]; then
    _NGPU=$(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null | wc -l | tr -d ' ')
    if [ -z "$_NGPU" ] || [ "$_NGPU" -lt "$PARALLEL_N" ]; then
        echo "ERROR: --parallel $PARALLEL_N requested but only ${_NGPU:-0} GPU(s) visible"; exit 1
    fi
fi
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
    # Benchmark sets, kept in sync with the main pipeline (ALL_BENCH at the
    # bottom of this file). Defined locally because the fix function runs and
    # exits BEFORE those globals are set. The timing/metrics scripts rewrite
    # their whole CSV from only the benchmarks passed, so we must pass the full
    # set here or we would drop rows for the benchmarks we omit.
    local ALL_BENCH="tpch h2o clickbench case_bench microbench_tpch microbench_h2o microbench_clickbench"

    echo ""
    echo "========================================================================"
    echo "  FULL FIX — $gpu  (one-click: data -> Cat A -> Cat C -> summary)"
    echo "  Grid:      PL=[$PL] W  x  SM=[$CLK] MHz  (5x5 = 25 configs)"
    echo "  Sweep dir: $SWEEP_DIR"
    echo "  Started:   $(date)"
    echo "========================================================================"

    # Step 1: validate H2O CSV datasets by ROW COUNT and rebuild any that are
    #         short/missing. Step 0a only checks the dir EXISTS, so a dataset
    #         left short by an interrupted/old generation (observed: A100
    #         csv-4gb) is never rebuilt and makes a larger SF measure CHEAPER
    #         than a smaller one. Rebuilding the data is the real fix; purge
    #         (step 4) then drops the stale summaries so resume re-measures.
    echo ""
    echo "---- Fix step 1/7: validate + regenerate short H2O datasets ----"
    python3 "$SCRIPT_DIR/validate_h2o_data.py" "$MAXIMUS_DIR/tests/h2o" \
        --scales 1gb 2gb 4gb 8gb --regenerate \
        2>&1 | tee "$LOG_DIR/fix_${gpu}_h2o_validate.log" || true

    # Step 2: re-run Category A timing (data on GPU, -s gpu). This refreshes any
    #         stale/failed rows — e.g. the A100 TPC-H SF20 timing that shows
    #         10/22 FAIL from a pre-managed-memory binary but completes 22/22 on
    #         the current build. The CSV is rewritten wholesale, so we pass the
    #         full ALL_BENCH set.
    echo ""
    echo "---- Fix step 2/7: Category A timing (Maximus + Sirius) ----"
    python3 "$SCRIPT_DIR/run_maximus_benchmark.py" $EXTRA_FLAGS --n-reps 3 \
        --results-dir "$RESULTS_DIR" $ALL_BENCH \
        2>&1 | tee "$LOG_DIR/fix_${gpu}_A1_timing.log" || true
    if has_sirius; then
        python3 "$SCRIPT_DIR/run_sirius_benchmark.py" $EXTRA_FLAGS \
            --results-dir "$RESULTS_DIR" $ALL_BENCH \
            2>&1 | tee "$LOG_DIR/fix_${gpu}_A2_timing.log" || true
    fi

    # Step 3: re-run Category A metrics (power/energy). Maximus reuses the fresh
    #         A1 timing; Sirius uses the patched wall-clock fallback so sub-ms
    #         ClickBench/microbench queries no longer record energy=0.
    echo ""
    echo "---- Fix step 3/7: Category A metrics (Maximus + Sirius) ----"
    python3 "$SCRIPT_DIR/run_maximus_metrics.py" $EXTRA_FLAGS --target-time $METRICS_TT \
        --results-dir "$RESULTS_DIR" --timing-csv "$RESULTS_DIR/maximus_benchmark.csv" \
        $ALL_BENCH \
        2>&1 | tee "$LOG_DIR/fix_${gpu}_A3_metrics.log" || true
    if has_sirius; then
        python3 "$SCRIPT_DIR/run_sirius_metrics.py" $EXTRA_FLAGS --target-time $METRICS_TT \
            --results-dir "$RESULTS_DIR" $ALL_BENCH \
            2>&1 | tee "$LOG_DIR/fix_${gpu}_A4_metrics.log" || true
    fi

    # Step 4: purge degenerate (smoke / zero-timing) and cross-SF-anomalous
    #         summaries so --resume regenerates them at full query count.
    #         No-op on a clean A100 dir.
    echo ""
    echo "---- Fix step 4/7: purge degenerate / anomalous summaries ----"
    python3 "$SCRIPT_DIR/purge_degenerate_sweep.py" "$SWEEP_DIR" \
        2>&1 | tee "$LOG_DIR/fix_${gpu}_purge.log"

    # Step 5: resume the sweep over the canonical grid, all 4 benchmarks, both
    #         engines. --resume only runs (bench, sf, config) points still missing.
    echo ""
    echo "---- Fix step 5/7: resume energy sweep (fills missing points) ----"
    python3 "$SCRIPT_DIR/run_energy_sweep.py" \
        --power-limits "$PL" --sm-clocks "$CLK" \
        --engines maximus sirius \
        --benchmarks tpch h2o clickbench case_bench \
        --results-dir "$SWEEP_DIR" \
        --resume \
        2>&1 | tee "$LOG_DIR/fix_${gpu}_sweep.log"
    local rc=${PIPESTATUS[0]}

    # Step 6: re-aggregate the unified energy summary from the refreshed
    #         Category A metrics so energy_summary.csv reflects the re-runs.
    echo ""
    echo "---- Fix step 6/7: re-aggregate energy_summary.csv ----"
    python3 "$SCRIPT_DIR/compute_energy_summary.py" --latest \
        --results-dir "$RESULTS_DIR" --output "$RESULTS_DIR/energy_summary.csv" \
        2>&1 | tee "$LOG_DIR/fix_${gpu}_energy_summary.log" || true

    # Step 7: report coverage of the refreshed sweep so the run can be verified
    #         at a glance (run_energy_sweep.py already re-aggregated the summary).
    echo ""
    echo "---- Fix step 7/7: coverage report ----"
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
    echo "  FULL FIX ($gpu) COMPLETE (sweep rc=$rc)"
    echo "  Sweep:   $SWEEP_DIR/energy_sweep_summary.csv"
    echo "  Summary: $RESULTS_DIR/energy_summary.csv"
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
#  --parallel N : fan the campaign out over N GPUs
#
#  Work units are independent per-benchmark chains (internal dependencies
#  stay inside one unit, e.g. Maximus metrics reuses the timing CSV written
#  moments earlier by the same unit in the same worker dir):
#      A_maximus_<bench> = A1(bench) ; A3(bench)
#      A_sirius_<bench>  = A2(bench) ; A4(bench)
#      B_maximus_<bench> = B1(bench) ; B2(bench)
#      B_sirius_<bench>  = B3(bench)
#      C_sweep_<bench>   = full 5x5 sweep for one benchmark (shared sweep dir;
#                          clocks are set per-GPU via MAXIMUS_GPU_ID)
#  Workers pop units from a flock(1)-guarded queue. Cat A/B units write to
#  $RESULTS_DIR/parallel_<ts>/gpu<i>/ and merge_parallel_results.py folds
#  them back; sweep units write straight into $RESULTS_DIR/energy_sweep.
# ══════════════════════════════════════════════════════════════════════════

PARALLEL_ROOT="$RESULTS_DIR/parallel_${TIMESTAMP}"
QUEUE_FILE="$PARALLEL_ROOT/queue.txt"
QUEUE_LOCK="$PARALLEL_ROOT/queue.lock"

parallel_unit_cmds() {
    # Emit the shell command(s) for one unit into stdout. $1=unit $2=outdir
    local unit="$1" out="$2" bench="${1#*_*_}"
    case "$unit" in
        A_maximus_*)
            echo "python3 run_maximus_benchmark.py $EXTRA_FLAGS --n-reps 3 --results-dir '$out' $bench && \
python3 run_maximus_metrics.py $EXTRA_FLAGS --target-time $METRICS_TT --results-dir '$out' --timing-csv '$out/maximus_benchmark.csv' $bench" ;;
        A_sirius_*)
            echo "python3 run_sirius_benchmark.py $EXTRA_FLAGS --results-dir '$out' $bench && \
python3 run_sirius_metrics.py $EXTRA_FLAGS --target-time $METRICS_TT --results-dir '$out' $bench" ;;
        B_maximus_*)
            echo "python3 run_maximus_cpu_data.py $EXTRA_FLAGS --timing-only --results-dir '$out' $bench && \
python3 run_maximus_cpu_data.py $EXTRA_FLAGS --target-time $METRICS_TT --results-dir '$out' --timing-csv '$out/maximus_cpu_data_timing.csv' $bench" ;;
        B_sirius_*)
            echo "python3 run_sirius_cpu_data.py $EXTRA_FLAGS --n-reps 10 --results-dir '$out' $bench" ;;
        C_sweep_*)
            echo "python3 run_energy_sweep.py $EXTRA_FLAGS --benchmarks $bench --results-dir '$RESULTS_DIR/energy_sweep' --resume" ;;
    esac
}

parallel_worker() {
    # $1 = worker id (0..N-1). Pinned to physical GPU $1 and a core slice.
    local wid="$1"
    local wdir="$PARALLEL_ROOT/gpu${wid}"
    mkdir -p "$wdir"
    local ncores=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 0)
    local pin=""
    if command -v taskset >/dev/null 2>&1 && [ "$ncores" -ge "$PARALLEL_N" ]; then
        local per=$(( ncores / PARALLEL_N ))
        local lo=$(( wid * per )); local hi=$(( lo + per - 1 ))
        pin="taskset -c ${lo}-${hi}"
    fi
    while :; do
        local unit
        unit=$(flock "$QUEUE_LOCK" sh -c "head -n1 '$QUEUE_FILE' 2>/dev/null; sed -i.bak '1d' '$QUEUE_FILE' 2>/dev/null")
        [ -z "$unit" ] && break
        echo "[gpu${wid}] START $unit ($(date))"
        local cmd; cmd=$(parallel_unit_cmds "$unit" "$wdir")
        if MAXIMUS_GPU_ID="$wid" CUDA_VISIBLE_DEVICES="$wid" \
             bash -c "cd '$SCRIPT_DIR' && $pin $cmd" \
             > "$LOG_DIR/par_gpu${wid}_${unit}.log" 2>&1; then
            echo "[gpu${wid}] DONE  $unit ($(date))"
        else
            echo "[gpu${wid}] WARN  $unit exited non-zero — see $LOG_DIR/par_gpu${wid}_${unit}.log"
        fi
    done
}

run_parallel_campaign() {
    mkdir -p "$PARALLEL_ROOT"
    : > "$QUEUE_FILE"; : > "$QUEUE_LOCK"
    # Longest units first so the tail of the schedule stays packed.
    if [ "$SKIP_CATEGORY_C" -eq 0 ]; then
        for b in tpch h2o clickbench case_bench; do echo "C_sweep_${b}" >> "$QUEUE_FILE"; done
    fi
    for b in $ALL_BENCH;  do echo "A_maximus_${b}" >> "$QUEUE_FILE"; done
    if has_sirius; then
        for b in $ALL_BENCH; do echo "A_sirius_${b}" >> "$QUEUE_FILE"; done
    fi
    for b in $CPU_BENCH; do echo "B_maximus_${b}" >> "$QUEUE_FILE"; done
    if has_sirius; then
        for b in $CPU_BENCH; do echo "B_sirius_${b}" >> "$QUEUE_FILE"; done
    fi

    echo ""
    echo "======== PARALLEL CAMPAIGN: $(wc -l < "$QUEUE_FILE") units on $PARALLEL_N GPUs ========"
    if [ "${PARALLEL_DRY_RUN:-0}" = "1" ]; then
        echo "[dry-run] queue:"; cat "$QUEUE_FILE"; return 0
    fi

    local pids=()
    for i in $(seq 0 $(( PARALLEL_N - 1 ))); do
        parallel_worker "$i" &
        pids+=($!)
    done
    for p in "${pids[@]}"; do wait "$p"; done

    run_step "parallel_merge" \
        python3 merge_parallel_results.py "$PARALLEL_ROOT" "$RESULTS_DIR"

    # One final aggregation pass over the shared sweep dir: with --resume and
    # every config complete it skips all measurements and just rebuilds
    # energy_sweep_summary.csv from the per-config files.
    if [ "$SKIP_CATEGORY_C" -eq 0 ]; then
        run_step "C1_sweep_aggregate" \
            python3 run_energy_sweep.py $EXTRA_FLAGS \
            --benchmarks tpch h2o clickbench case_bench \
            --results-dir "$RESULTS_DIR/energy_sweep" --resume
    fi
}

# ══════════════════════════════════════════════════════════════════════════
#  Category A: Data on GPU (-s gpu) — timing + power/energy metrics
#  Standard SQL benchmarks + microbenchmarks (GPU memory auto-checked)
# ══════════════════════════════════════════════════════════════════════════

if [ "$PARALLEL_N" -gt 1 ]; then
    run_parallel_campaign
else

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

fi   # end sequential path (--parallel 1)

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
