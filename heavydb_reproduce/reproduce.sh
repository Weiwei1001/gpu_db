#!/bin/bash
# One-shot reproduction: build HeavyDB -> generate data -> Category A (latency + power sampling) -> B -> C
#
#   ./reproduce.sh --gpu 4 --smoke            # end-to-end check on tiny data (about 10 min, excluding compilation)
#   ./reproduce.sh --gpu 4 --full             # repo spec: 4 benchmarks × 4 SF (several hours, about 70 GB of disk)
#   ./reproduce.sh --gpu 4 --full --skip-build --stages "3 5"
#
# Options:
#   --gpu N             run on GPU N, single GPU (required). Only this one GPU is used throughout.
#   --smoke | --full    data scale (default smoke)
#   --skip-build        skip stage 1 (when a usable heavydb build already exists)
#   --skip-data         skip stage 2
#   --stages "1 2 3 4 5" run only the listed stages
#   --heavydb-home DIR  heavydb source/build directory (default <this dir>/heavydb; may point to an existing build)
#   --deps-prefix DIR   install prefix for the from-source dependencies (default /usr/local/mapd-deps)
#   --jobs N            number of compile threads
#   --catc-scope S      Category C scope: ab (default, rerun A+B at every grid point, matches the repo, full run about 4–5 days)
#                       | a (rerun A only, about 1 day) | lite (6 representative queries, about 45 min)
#   --catc-limit N      Category C: run only the first N queries per benchmark×SF (for testing, e.g. 1)
#   --data-dir DIR      put HeavyDB database files and import temp files here (default: under the heavydb build dir; full is about 70 GB,
#                       point it at a big disk if the root disk is too small, e.g. --data-dir /data/$USER/hb_repro)
# Environment variables: HB_SFS_TPCH="1 10" HB_SFS_H2O="1 4" HB_SFS_CB="1 10" shrink the SF list in full mode;
#           HB_FORCE_RESTART=1 allows stopping other heavydb instances; HB_IGNORE_BUSY=1 skips the GPU-busy check.
set -euo pipefail
export HB_ROOT="$(cd "$(dirname "$0")" && pwd)"
export HB_MODE=smoke DEPS_PREFIX=/usr/local/mapd-deps HEAVYDB_HOME="$HB_ROOT/heavydb"
GPU="" SKIP_BUILD=0 SKIP_DATA=0 STAGES="1 2 3 4 5" CATC_LIMIT=""
while [ $# -gt 0 ]; do case "$1" in
  --gpu) GPU="$2"; shift 2;; --smoke) HB_MODE=smoke; shift;; --full) HB_MODE=full; shift;;
  --skip-build) SKIP_BUILD=1; shift;; --skip-data) SKIP_DATA=1; shift;;
  --stages) STAGES="$2"; shift 2;; --heavydb-home) HEAVYDB_HOME="$(readlink -f "$2")"; shift 2;;
  --deps-prefix) DEPS_PREFIX="$2"; shift 2;; --jobs) export BUILD_JOBS="$2"; shift 2;;
  --catc-scope) export HB_CATC_SCOPE="$2"; shift 2;;
  --catc-limit) CATC_LIMIT="$2"; shift 2;;
  --data-dir) mkdir -p "$2"; export HB_DATA_DIR="$(readlink -f "$2")"; shift 2;;
  -h|--help) sed -n '2,25p' "$0"; exit 0;; *) echo "unknown argument $1"; exit 2;; esac; done
[ -n "$GPU" ] || { echo "--gpu N is required"; exit 2; }
export HB_MODE HEAVYDB_HOME DEPS_PREFIX GPU
GPU_SLUG=$(nvidia-smi -i "$GPU" --query-gpu=name --format=csv,noheader | sed 's/^NVIDIA //I; s/[^A-Za-z0-9]\+/-/g; s/-$//' | tr 'A-Z' 'a-z')
export GPU_SLUG HB_RESULTS_BASE="$HB_ROOT/results/$HB_MODE/$GPU_SLUG"   # one directory per mode/GPU model: results/full/h100-80gb-hbm3
mkdir -p "$HB_RESULTS_BASE" "$HB_ROOT/logs"
log(){ echo "[$(date +%T)] $*"; }
has(){ [[ " $STAGES " == *" $1 "* ]]; }
log "mode=$HB_MODE  GPU=$GPU ($GPU_SLUG)  heavydb=$HEAVYDB_HOME  data=${HB_DATA_DIR:-$HEAVYDB_HOME/build/data}  stages=$STAGES"
IMPORT_PATHS="[\"$HB_ROOT\"${HB_DATA_DIR:+,\"$HB_DATA_DIR\"}]"

# ---------- Python environment ----------
export PY="$HB_ROOT/venv/bin/python"
if [ ! -f "$HB_ROOT/venv/.ok" ]; then
  log "creating venv (duckdb / pyarrow / matplotlib)"
  if ! python3 -m venv "$HB_ROOT/venv" 2>/dev/null; then        # a fresh Ubuntu has no python3-venv
    log "python3 -m venv failed; installing python3-venv and retrying"; rm -rf "$HB_ROOT/venv"
    sudo apt-get update -qq && sudo apt-get install -y --no-install-recommends python3-venv python3-pip >/dev/null
    python3 -m venv "$HB_ROOT/venv"
  fi
  "$PY" -m pip install -q --upgrade pip \
    && "$PY" -m pip install -q duckdb pyarrow matplotlib numpy && touch "$HB_ROOT/venv/.ok"
fi

# ---------- Stage 1: build ----------
if has 1 && [ "$SKIP_BUILD" = 0 ]; then bash "$HB_ROOT/stages/01_build.sh"
else log "skipping build"; fi
[ -x "$HEAVYDB_HOME/build/bin/heavydb" ] || { log "!! $HEAVYDB_HOME/build/bin/heavydb not found"; exit 1; }

# ---------- GPU-busy check (shared-machine etiquette: do not touch other people's GPUs) ----------
UUID=$(nvidia-smi -i "$GPU" --query-gpu=uuid --format=csv,noheader)
OTHERS=$(nvidia-smi --query-compute-apps=pid,gpu_uuid --format=csv,noheader | awk -F', *' -v u="$UUID" '$2==u{print $1}' \
         | while read -r p; do [ "$(ps -o comm= -p "$p" 2>/dev/null)" = heavydb ] || echo "$p"; done)
if [ -n "$OTHERS" ] && [ "${HB_IGNORE_BUSY:-0}" != 1 ]; then
  log "!! GPU $GPU has other people's processes (pid $OTHERS); refusing to measure on a busy GPU. Use another GPU or set HB_IGNORE_BUSY=1"; exit 1; fi

# ---------- Server: single GPU, bound to GPU ----------
want_cwd="$HEAVYDB_HOME/build"; reuse=0
for p in $(pgrep -x heavydb); do
  cwd=$(readlink -f /proc/$p/cwd 2>/dev/null || true)
  if [ "$cwd" = "$want_cwd" ] && tr '\0' ' ' < /proc/$p/cmdline | grep -q -- "--start-gpu $GPU --num-gpus 1"; then
    reuse=1; log "reusing the single-GPU heavydb already on GPU $GPU (pid $p)"
  elif [ "${HB_FORCE_RESTART:-0}" = 1 ]; then log "stopping another heavydb (pid $p)"; kill -9 "$p"; sleep 3
  else log "!! another heavydb is already running (pid $p, $cwd). Stop it or set HB_FORCE_RESTART=1"; exit 1; fi
done
if [ "$reuse" = 0 ]; then
  log "starting heavydb: GPU $GPU, single GPU"
  START_GPU="$GPU" NUM_GPUS=1 nohup "$HB_ROOT/lib/start-heavydb.sh" \
      --allowed-import-paths="$IMPORT_PATHS" > "$HEAVYDB_HOME/build/server.log" 2>&1 &
  for i in $(seq 40); do sleep 3; "$HB_ROOT/lib/hsql" heavyai "SELECT 1;" >/dev/null 2>&1 && break; done
  "$HB_ROOT/lib/hsql" heavyai "SELECT 1;" >/dev/null 2>&1 || { log "!! server did not come up within 2 minutes, see $HEAVYDB_HOME/build/server.log"; exit 1; }
fi
"$PY" -c "import sys; sys.path.insert(0,'$HB_ROOT/lib'); import hbench as hb; assert hb.GPUS==[$GPU], hb.GPUS; print('  sampling GPU =', hb.GPUS, ' idle %.0f W' % hb.idle_baseline_W(2))"

# ---------- Stages 2–5 ----------
if has 2 && [ "$SKIP_DATA" = 0 ]; then bash "$HB_ROOT/stages/02_data.sh"; else log "skipping data generation"; fi
has 3 && bash "$HB_ROOT/stages/03_cat_a.sh"
has 4 && bash "$HB_ROOT/stages/04_cat_b.sh"
has 5 && HB_QUERY_LIMIT="$CATC_LIMIT" bash "$HB_ROOT/stages/05_cat_c.sh"

# ---------- Wrap-up ----------
if sudo -n true 2>/dev/null; then sudo nvidia-smi -i "$GPU" -rgc >/dev/null 2>&1 || true
  sudo nvidia-smi -i "$GPU" -pl "$(nvidia-smi -i "$GPU" --query-gpu=power.max_limit --format=csv,noheader,nounits | cut -d. -f1)" >/dev/null 2>&1 || true; fi
log "===== Results ($HB_RESULTS_BASE) ====="
for f in "$HB_RESULTS_BASE"/*.csv; do [ -f "$f" ] && printf "  %-32s %5d rows\n" "$(basename "$f")" "$(($(wc -l < "$f")-1))"; done
[ -d "$HB_RESULTS_BASE/traces" ] && printf "  %-32s %5d files\n" "traces/*.json" "$(ls "$HB_RESULTS_BASE"/traces/*.json 2>/dev/null | wc -l)"
log "all done"
