#!/usr/bin/env bash
# Repair run for every re-runnable gap in the A100/H100 campaigns
# (2026-07 audit). Auto-detects the GPU and runs the stages that apply.
# All new measurements go to results/fix/ (sweep repairs go to the live
# sweep dir, which is resume-keyed).
#
# Both boxes:
#   B. Sirius Category-B cold re-run, ClickBench + H2O (fresh process per
#      query, load inside the timed window). Fixes: A100 CB cold (never
#      measured validly), A100 H2O 1/2/4GB cold (truncated inputs),
#      H100 CB cold (replaces suite-level latency + estimated energy).
# H100 only:
#   T. Maximus timing (A1) re-run at default clocks. The May timing ran
#      under leftover sweep clock caps; this replaces the reconstructed
#      latencies with direct measurement. Clocks are reset first.
# RTX6000 only (RUN ALONE — single job on the whole box; the July campaign
# ran under 8-GPU contention, which is why its cold data is held out):
#   B. extends the Sirius cold re-run to TPC-H as well.
#   H. Maximus ClickBench SF20 device-resident (97.9GB VRAM fits it; the
#      <90GB CPU-storage rule no longer fires here).
#   S. Maximus streaming campaign for the cold-table rows.
# A100 only:
#   M. Maximus H2O 1/2GB hot metrics re-measure (single-vintage cleanup).
#   S. Cat-C sweep repair: delete the Sirius H2O 1/2GB cells (swept on
#      truncated DuckDBs; the monotonicity purge cannot catch them) and
#      resume the sweep for sirius/h2o only.
#
# NOT re-runnable (documented exclusions, do not chase):
#   - Maximus cold TPC-H SF1 / CB SF1+SF5: below reconstruction resolution.
#   - Maximus cold-vs-warm ratios: harness time-base mismatch.
#   - A100 Maximus TPC-H SF20 hot 10/22: genuine OOM.
#
# Run ALONE on the box (no concurrent jobs): cold load measurements need an
# uncontended host path.
#
# Usage:  bash benchmarks/scripts/fix_categoryB.sh
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root

DUCKDB=sirius/build/release/duckdb
FIX_DIR=results/fix
mkdir -p "$FIX_DIR"

GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader -i "${GPU_ID:-0}" | head -1)
echo "== GPU: $GPU_NAME =="
case "$GPU_NAME" in
  *A100*)      BOX=A100 ;;
  *H100*)      BOX=H100 ;;
  *RTX*6000*)  BOX=RTX6000 ;;
  *)           BOX=OTHER ;;
esac

echo "== reset clocks/power to defaults (sweep leftovers are the enemy) =="
(nvidia-smi -rgc && nvidia-smi -rmc) 2>/dev/null || \
  sudo -n nvidia-smi -rgc 2>/dev/null || \
  echo "  [WARN] could not reset clocks; verify 'nvidia-smi -q -d CLOCK' manually"

echo "== data guards (row counts must match canonical sizes) =="
python3 - <<'EOF'
import subprocess, sys
sys.path.insert(0, "benchmarks/scripts")
from hw_detect import sirius_db_path
DUCKDB = "sirius/build/release/duckdb"
H2O_ROWS = {"1gb": 35_000_000, "2gb": 70_000_000, "4gb": 140_000_000, "8gb": 280_000_000}
fail = False

def count(db, table):
    out = subprocess.run([DUCKDB, str(db), "-csv", "-c", f"SELECT COUNT(*) FROM {table};"],
                         capture_output=True, text=True)
    try:
        return int(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return -1

for sf, want in H2O_ROWS.items():
    n = count(sirius_db_path("h2o", sf), "groupby")
    ok = n == want
    print(f"  h2o {sf}: {n:,} rows ({'OK' if ok else f'WANT {want:,} - REBUILD FIRST'})")
    fail |= not ok
for sf in (1, 5, 10, 20):
    n = count(sirius_db_path("clickbench", sf), "t")
    ok = n > 900_000 * sf   # CSV-GB semantics: ~1.4M rows/GB; guard vs empty/short
    print(f"  clickbench sf{sf}: {n:,} rows ({'OK' if ok else 'TOO SMALL - REBUILD FIRST'})")
    fail |= not ok
sys.exit(1 if fail else 0)
EOF

COLD_BENCHES="clickbench h2o"
if [ "$BOX" = "RTX6000" ]; then
  COLD_BENCHES="tpch clickbench h2o"   # RTX has no uncontended cold data at all
fi
echo "== [B] Sirius Category-B cold re-run: $COLD_BENCHES -> $FIX_DIR =="
python3 benchmarks/scripts/run_sirius_cpu_data.py $COLD_BENCHES --results-dir "$FIX_DIR"

if [ "$BOX" = "H100" ]; then
  echo "== [T] H100: Maximus timing re-run at default clocks -> $FIX_DIR =="
  python3 benchmarks/scripts/run_maximus_benchmark.py tpch h2o clickbench \
      --results-dir "$FIX_DIR"
fi

if [ "$BOX" = "A100" ]; then
  echo "== [M] A100: Maximus H2O 1/2GB hot metrics (vintage cleanup) -> $FIX_DIR =="
  python3 benchmarks/scripts/run_maximus_metrics.py h2o --sf 1gb --results-dir "$FIX_DIR"
  python3 benchmarks/scripts/run_maximus_metrics.py h2o --sf 2gb --results-dir "$FIX_DIR"

  echo "== [S] A100: purge truncated-input sweep cells + resume sirius/h2o sweep =="
  rm -fv results/energy_sweep/*/sirius_h2o_sf1gb_metrics_*.csv \
         results/energy_sweep/*/sirius_h2o_sf2gb_metrics_*.csv
  python3 benchmarks/scripts/run_energy_sweep.py --engines sirius --benchmarks h2o --resume
fi

if [ "$BOX" = "RTX6000" ]; then
  echo "== [H] RTX6000: Maximus ClickBench SF20 device-resident (fills the last hot cell) =="
  # requires the VRAM-conditional storage rule (>=90GB runs -s gpu)
  python3 benchmarks/scripts/run_maximus_metrics.py clickbench --sf 20 --storage gpu \
      --results-dir "$FIX_DIR"

  echo "== [S] RTX6000: Maximus streaming campaign, uncontended (cold-table rows) =="
  python3 benchmarks/scripts/run_maximus_cpu_data.py --timing-only \
      --results-dir "$FIX_DIR" tpch h2o clickbench
  python3 benchmarks/scripts/run_maximus_cpu_data.py \
      --results-dir "$FIX_DIR" --timing-csv "$FIX_DIR/maximus_cpu_data_timing.csv" \
      tpch h2o clickbench
fi

echo "== done — new measurements in $FIX_DIR (sweep repairs in results/energy_sweep) =="
ls -la "$FIX_DIR"
