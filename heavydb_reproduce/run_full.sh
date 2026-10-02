#!/bin/bash
# One-shot full run: auto-pick an idle GPU → check sudo/disk → auto-discover existing data (generate/download if missing) → build (if needed) → Cat A/B/C
#
#   ./run_full.sh                                  # all defaults: A + B + Cat C lite (6 representative queries), about 5.5 h on a single H100
#   ./run_full.sh --catc-scope ab                  # Cat C per repo spec (rerun A+B at every grid point, about 4–5 days)
#   ./run_full.sh --data-roots /path/gpu_db:/path/other --data-dir /bigdisk/hb_repro
#
#   One command from an existing gpu_db checkout (reuses its tests/ data):
#     git pull && heavydb_reproduce/run_full.sh
#   One command from a fresh machine:
#     git clone https://github.com/Weiwei1001/gpu_db.git && gpu_db/heavydb_reproduce/run_full.sh
#   ./run_full.sh --dry-run                        # only print the chosen GPU, disk, and data found; no build, no run
#
# Options:
#   --gpu N            GPU to use; if omitted, auto-pick: with one GPU use it, with several take the one with no processes and the most memory
#   --data-roots A:B   search roots for existing data (tests/ dirs, csv-*/, *.duckdb, hits.parquet left by earlier gpu_db runs)
#                      checked by default: --data-dir, ~/gpu_db, a gpu_db next to this package
#   --data-dir DIR     directory for HeavyDB databases and temp files (full run about 70 GB; default data/ under this package; warns if space is short)
#   --catc-scope S     Cat C scope: lite (default) | a | ab; see reproduce.sh
#   --dry-run          only do GPU/disk/data discovery, then exit
#   any other arguments are passed through to reproduce.sh (--skip-build, --heavydb-home, --catc-limit, --stages ...)
set -euo pipefail
HB_ROOT="$(cd "$(dirname "$0")" && pwd)"
log(){ echo "[$(date +%T)] [full] $*"; }
GPU="" DATA_DIR="$HB_ROOT/data" ROOTS="" CATC_SCOPE="lite" DRY=0 PASS=()
while [ $# -gt 0 ]; do case "$1" in
  --gpu) GPU="$2"; shift 2;; --data-roots) ROOTS="$2"; shift 2;; --data-dir) DATA_DIR="$2"; shift 2;;
  --catc-scope) CATC_SCOPE="$2"; shift 2;; --dry-run) DRY=1; shift;;
  -h|--help) sed -n '2,21p' "$0"; exit 0;; *) PASS+=("$1"); shift;; esac; done

# ---------- GPU ----------
command -v nvidia-smi >/dev/null || { log "!! nvidia-smi not found"; exit 1; }
NGPU=$(nvidia-smi --query-gpu=index --format=csv,noheader | wc -l)
if [ -z "$GPU" ]; then
  if [ "$NGPU" -eq 1 ]; then GPU=0
  else
    BUSY=$(nvidia-smi --query-compute-apps=gpu_uuid --format=csv,noheader | sort -u)
    best_mem=-1
    while IFS=, read -r i u mem; do          # among idle GPUs take the one with the most memory (avoids picking a small display GPU)
      i=${i// /}; u=${u// /}; mem=${mem// /}
      grep -q "$u" <<< "$BUSY" && continue
      [ "$mem" -gt "$best_mem" ] && { GPU=$i; best_mem=$mem; }
    done < <(nvidia-smi --query-gpu=index,uuid,memory.total --format=csv,noheader,nounits)
    [ -n "$GPU" ] || { log "!! all $NGPU GPUs have processes running; pick one with --gpu N (or wait until one is idle)"; exit 1; }
  fi
fi
log "GPU $GPU of $NGPU: $(nvidia-smi -i "$GPU" --query-gpu=name,memory.total,power.max_limit --format=csv,noheader)"
sudo -n nvidia-smi -L >/dev/null 2>&1 || log "!! no passwordless sudo: Cat C (power/clock changes) will be skipped; A/B still run"

# ---------- Disk ----------
mkdir -p "$DATA_DIR"
FREE_GB=$(df -BG --output=avail "$DATA_DIR" | tail -1 | tr -dc 0-9)
[ "$FREE_GB" -ge 80 ] || log "!! $DATA_DIR has only ${FREE_GB} GB free; the full run needs about 70 GB (data) + temp files. If short, point --data-dir at a big disk"

# ---------- Existing data ----------
REPO_ROOT="$(dirname "$HB_ROOT")"   # this package lives inside the gpu_db checkout: its tests/ data is right here
export HB_DATA_ROOTS="${ROOTS:+$ROOTS:}$DATA_DIR:$REPO_ROOT:$HOME/gpu_db:$REPO_ROOT/gpu_db"
log "searching for existing data: $(tr ':' ' ' <<< "$HB_DATA_ROOTS")"
found=0
for r in $(tr ':' ' ' <<< "$HB_DATA_ROOTS"); do
  for pat in tests/tpch/csv-* tpch/csv-* tests/h2o/csv-* h2o/csv-* tests/clickbench/csv-* clickbench/csv-* \
             tests/*_duckdb/*.duckdb sirius_db/*.duckdb tests/clickbench/hits.parquet clickbench/hits.parquet; do
    for p in "$r"/$pat; do [ -e "$p" ] && { echo "    $p"; found=$((found+1)); }; done
  done
done
[ "$found" -gt 0 ] && log "found $found items; matching datasets are imported directly, the rest are generated/downloaded" || log "no existing data found; everything will be generated/downloaded (ClickBench needs access to datasets.clickhouse.com)"

# ---------- Run ----------
log "Cat C scope: $CATC_SCOPE (lite about 25 min; ab about 4–5 days, select with --catc-scope ab)"
[ "$DRY" = 1 ] && { log "--dry-run: stopping here, nothing built or run"; exit 0; }
exec "$HB_ROOT/reproduce.sh" --gpu "$GPU" --full --data-dir "$DATA_DIR" --catc-scope "$CATC_SCOPE" "${PASS[@]}"
