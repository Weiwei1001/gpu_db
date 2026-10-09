#!/bin/bash
# Repair existing full-mode results in place (stage 6), on a machine where ./run_ab.sh has already finished:
#   - TPC-H q14 and ClickBench q28: their query files were empty before this fix, so every row for them is a
#     0 ms "measurement" of nothing; they are re-screened and re-measured (Cat A + B, all SFs)
#   - catC_grid.csv (Cat C lite): the duty column was 3x too large; corrected in place (energies were not affected)
# Build and data import are skipped. Old CSVs are backed up to results/full/<gpu>/backup_<time>/. About 10 min.
#
#   ./fix_results.sh --gpu 0                                  # same GPU as the run_ab.sh run
#   ./fix_results.sh --gpu 0 --data-dir /bigdisk/hb_repro     # same --data-dir as the run_ab.sh run
#   ./fix_results.sh --gpu 0 --queries "tpch:q14"             # other queries (suite:query, space-separated)
set -euo pipefail
HB_ROOT="$(cd "$(dirname "$0")" && pwd)"
log(){ echo "[$(date +%T)] [fix] $*"; }
PYBIN="${PY:-$HB_ROOT/venv/bin/python}"; [ -x "$PYBIN" ] || PYBIN=python3
GPU="" QUERIES="tpch:q14 clickbench:q28" PASS=()
while [ $# -gt 0 ]; do case "$1" in
  --gpu) GPU="$2"; PASS+=("$1" "$2"); shift 2;;
  --queries) QUERIES="$2"; shift 2;;
  --stages|--catc-scope|--catc-limit) echo "!! $1 is not accepted by fix_results.sh"; exit 2;;
  -h|--help) sed -n '2,11p' "$0"; exit 0;;
  *) PASS+=("$1"); shift;; esac; done
[ -n "$GPU" ] || { echo "!! --gpu N is required (the GPU the A/B results were measured on)"; exit 2; }
for f in $(tr ' ' '\n' <<< "$QUERIES" | awk -F: '{print "queries/"$1"/"$2".sql"}'); do
  [ -s "$HB_ROOT/$f" ] || { log "!! $f is missing or empty; update the checkout (git pull) first"; exit 1; }; done

GPU_SLUG=$(nvidia-smi -i "$GPU" --query-gpu=name --format=csv,noheader | sed 's/^NVIDIA //I; s/[^A-Za-z0-9]\+/-/g; s/-$//' | tr 'A-Z' 'a-z')
RDIR="$HB_ROOT/results/full/$GPU_SLUG"
[ -f "$RDIR/catA_powersample.csv" ] || { log "!! no results in $RDIR; run ./run_ab.sh --gpu $GPU first"; exit 1; }

mkdir -p "$HB_ROOT/logs"
log "re-measuring $QUERIES in $RDIR; console log -> $HB_ROOT/logs/fix_results.log"
set +e
HB_RERUN="$QUERIES" "$HB_ROOT/run_full.sh" --skip-build --skip-data --stages 6 "${PASS[@]}" 2>&1 | tee "$HB_ROOT/logs/fix_results.log"
rc=${PIPESTATUS[0]}
set -e
[ "$rc" = 0 ] || { log "!! failed with status $rc; see logs/fix_results.log"; exit "$rc"; }
echo
"$PYBIN" "$HB_ROOT/lib/check_ab.py" "$RDIR" || exit 1
