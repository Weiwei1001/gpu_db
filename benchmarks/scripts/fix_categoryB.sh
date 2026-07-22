#!/usr/bin/env bash
# Category-B (cold) repair: re-measure Sirius load-inclusive first runs for
# ClickBench (all scales) and H2O (all scales), writing into results/fix/.
#
# Fixes, per the 2026-07 audit:
#   * A100/H100 ClickBench cold: original campaign's first runs never
#     exercised the load path (flat ~0.3 s at idle power);
#   * A100 H2O 1/2/4 GB cold: original campaign ran on truncated DuckDBs;
#   * H100 ClickBench cold energy: previously estimated, now measured.
#
# Guards first: refuses to run against an empty or truncated database, the
# failure mode that silently invalidated the May campaign.
#
# Usage (on each GPU box):  bash benchmarks/scripts/fix_categoryB.sh
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root

DUCKDB=sirius/build/release/duckdb
FIX_DIR=results/fix
mkdir -p "$FIX_DIR"

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

echo "== Category B re-run (fresh DuckDB process per query; load inside the timed window) =="
python3 benchmarks/scripts/run_sirius_cpu_data.py clickbench h2o --results-dir "$FIX_DIR"

echo "== done — outputs in $FIX_DIR =="
ls -la "$FIX_DIR"
