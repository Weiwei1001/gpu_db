#!/bin/bash
# Category A + B only (stages 1-4: build, data, Cat A, Cat B), without Category C.
# Use this first, look at the results, then start the long Cat C grid with ./run_c.sh.
#
#   ./run_ab.sh                                    # auto-pick an idle GPU, auto-find existing data, build, Cat A + B (about 3.5 h on one H100 with data present)
#   ./run_ab.sh --gpu 4 --data-dir /bigdisk/hb_repro
#   ./run_ab.sh --dry-run                          # only print the chosen GPU, disk, and data found
#   HB_SFS_TPCH="1 10" HB_SFS_H2O="1 4" HB_SFS_CB="1 10" ./run_ab.sh    # fewer SFs
#
# All options are those of run_full.sh / reproduce.sh (--gpu, --data-dir, --data-roots, --skip-build, --heavydb-home, ...);
# --stages and --catc-* are not accepted here. When it finishes, the A/B result files are checked (lib/check_ab.py:
# row counts per suite x SF, failed queries, suspicious energy values) and the matching ./run_c.sh command is printed.
set -euo pipefail
HB_ROOT="$(cd "$(dirname "$0")" && pwd)"
log(){ echo "[$(date +%T)] [ab] $*"; }
PYBIN="${PY:-$HB_ROOT/venv/bin/python}"; [ -x "$PYBIN" ] || PYBIN=python3   # check_ab.py needs only the stdlib
GPU="" DATA_DIR="" DRY=0 PASS=()
while [ $# -gt 0 ]; do case "$1" in
  --gpu) GPU="$2"; PASS+=("$1" "$2"); shift 2;;
  --data-dir) DATA_DIR="$2"; PASS+=("$1" "$2"); shift 2;;
  --dry-run) DRY=1; PASS+=("$1"); shift;;
  --stages|--catc-scope|--catc-limit) echo "!! $1 is not accepted by run_ab.sh (use run_full.sh or run_c.sh)"; exit 2;;
  -h|--help) sed -n '2,12p' "$0"; exit 0;;
  *) PASS+=("$1"); shift;; esac; done

mkdir -p "$HB_ROOT/logs"
LOG="$HB_ROOT/logs/run_ab.log"
log "Category A + B only (stages 1 2 3 4); console log -> $LOG"
set +e
"$HB_ROOT/run_full.sh" --stages "1 2 3 4" "${PASS[@]}" 2>&1 | tee "$LOG"
rc=${PIPESTATUS[0]}
set -e
[ "$DRY" = 1 ] && exit "$rc"
# run_full.sh picked the GPU; the results directory is keyed by its model name (same rule as reproduce.sh)
[ -n "$GPU" ] || GPU=$(grep -m1 -oP '\[full\] GPU \K[0-9]+' "$LOG" || true)
if [ "$rc" != 0 ]; then log "!! run_full.sh exited with status $rc; see $LOG and logs/"; exit "$rc"; fi
[ -n "$GPU" ] || { log "!! could not tell which GPU was used; check results/full/ by hand"; exit 1; }
GPU_SLUG=$(nvidia-smi -i "$GPU" --query-gpu=name --format=csv,noheader | sed 's/^NVIDIA //I; s/[^A-Za-z0-9]\+/-/g; s/-$//' | tr 'A-Z' 'a-z')
RDIR="$HB_ROOT/results/full/$GPU_SLUG"

echo
"$PYBIN" "$HB_ROOT/lib/check_ab.py" "$RDIR" || exit 1
echo
log "next step, Category C on the same GPU and data (both must match this run):"
log "    $HB_ROOT/run_c.sh --gpu $GPU${DATA_DIR:+ --data-dir $DATA_DIR}                  # lite: 6 representative queries, about 25 min"
log "    $HB_ROOT/run_c.sh --gpu $GPU${DATA_DIR:+ --data-dir $DATA_DIR} --catc-scope ab --catc-limit 1   # 1 query per benchmark x SF, about 7 h"
log "    $HB_ROOT/run_c.sh --gpu $GPU${DATA_DIR:+ --data-dir $DATA_DIR} --catc-scope ab   # repo spec, about 4-5 days"
