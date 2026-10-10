#!/bin/bash
# Category C only (stage 5: power limit x SM clock grid), on a machine where ./run_ab.sh has already finished.
# Build and data import are skipped; the existing heavydb build and databases are reused.
#
#   ./run_c.sh --gpu 4                             # repo spec (default): rerun A + B at every grid point (about 4-5 days)
#   ./run_c.sh --gpu 4 --catc-limit 1              # 1 query per benchmark x SF at every grid point (about 7 h)
#   ./run_c.sh --gpu 4 --catc-scope lite           # Cat C lite (6 representative queries, about 25 min)
#   ./run_c.sh --gpu 4 --data-dir /bigdisk/hb_repro      # same --data-dir as the run_ab.sh run (the databases live there)
#
# Options: --gpu N (required: Cat C must use the GPU the A/B results were measured on; the results directory is keyed by GPU model),
#   --data-dir DIR (must match run_ab.sh), --catc-scope ab|a|lite (default ab), --catc-limit N, --no-check (start even without A/B results),
#   --dry-run, and any other reproduce.sh option (--heavydb-home ...). Needs passwordless sudo for nvidia-smi power/clock changes.
# Before starting, the A/B results of this GPU model are checked with lib/check_ab.py; missing results abort (override with --no-check).
set -euo pipefail
HB_ROOT="$(cd "$(dirname "$0")" && pwd)"
log(){ echo "[$(date +%T)] [c] $*"; }
PYBIN="${PY:-$HB_ROOT/venv/bin/python}"; [ -x "$PYBIN" ] || PYBIN=python3   # check_ab.py needs only the stdlib
GPU="" CHECK=1 DRY=0 PASS=()
while [ $# -gt 0 ]; do case "$1" in
  --gpu) GPU="$2"; PASS+=("$1" "$2"); shift 2;;
  --no-check) CHECK=0; shift;;
  --dry-run) DRY=1; PASS+=("$1"); shift;;
  --stages) echo "!! --stages is not accepted by run_c.sh (it always runs stage 5 only)"; exit 2;;
  -h|--help) sed -n '2,13p' "$0"; exit 0;;
  *) PASS+=("$1"); shift;; esac; done
[ -n "$GPU" ] || { echo "!! --gpu N is required (the GPU the A/B results were measured on; run_ab.sh prints the exact command)"; exit 2; }

GPU_SLUG=$(nvidia-smi -i "$GPU" --query-gpu=name --format=csv,noheader | sed 's/^NVIDIA //I; s/[^A-Za-z0-9]\+/-/g; s/-$//' | tr 'A-Z' 'a-z')
RDIR="$HB_ROOT/results/full/$GPU_SLUG"
if [ "$CHECK" = 1 ]; then
  if ! "$PYBIN" "$HB_ROOT/lib/check_ab.py" "$RDIR"; then
    log "!! Category A/B results for GPU $GPU ($GPU_SLUG) are missing or incomplete; run ./run_ab.sh --gpu $GPU first, or pass --no-check"; exit 1
  fi
  echo
fi
sudo -n nvidia-smi -L >/dev/null 2>&1 || { log "!! passwordless sudo is required for the power/clock grid (sudo nvidia-smi -pl / -lgc)"; [ "$DRY" = 1 ] || exit 1; }
[ -x "$HB_ROOT/heavydb/build/bin/heavydb" ] || [[ " ${PASS[*]} " == *" --heavydb-home "* ]] \
  || log "!! $HB_ROOT/heavydb/build/bin/heavydb not found; pass --heavydb-home if the build is elsewhere"

mkdir -p "$HB_ROOT/logs"
log "Category C only (stage 5, build and data skipped); console log -> $HB_ROOT/logs/run_c.log"
set +e
"$HB_ROOT/run_full.sh" --skip-build --skip-data --stages 5 "${PASS[@]}" 2>&1 | tee "$HB_ROOT/logs/run_c.log"
exit "${PIPESTATUS[0]}"
