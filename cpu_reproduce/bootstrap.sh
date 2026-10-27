#!/bin/bash
# Everything in one command, on a fresh or existing machine:
#   curl -fsSL https://raw.githubusercontent.com/Weiwei1001/gpu_db/main/cpu_reproduce/bootstrap.sh | bash
# Installs the tools (git, python3-venv, curl, unzip), clones ~/gpu_db or updates it, and starts cpu_reproduce/run_all.sh
# in the background (prepare the canonical data, run every DuckDB-CPU experiment). sudo is asked for once, up front.
# Options via environment, passed through to run_all.sh: GPU_DB_DIR (default ~/gpu_db), PARTS, TPCH_SF, H2O_SF, CB_SF.
set -euo pipefail
DIR="${GPU_DB_DIR:-$HOME/gpu_db}"
REPO_URL="https://github.com/Weiwei1001/gpu_db.git"
log(){ echo "[$(date +%H:%M:%S)] [bootstrap] $*"; }

sudo -v                                       # ask for the password now, not hours into the run
missing=""
for p in git python3-venv curl unzip; do dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"; done
if [ -n "$missing" ]; then
  log "installing:$missing"
  sudo apt-get update -qq && sudo apt-get install -y -qq $missing >/dev/null
fi
sudo chmod o+r /sys/class/powercap/intel-rapl:*/energy_uj /sys/class/powercap/intel-rapl:*:*/energy_uj 2>/dev/null || true

if [ -d "$DIR/.git" ]; then
  log "updating $DIR"; git -C "$DIR" pull --ff-only
else
  log "cloning into $DIR"; git clone -q "$REPO_URL" "$DIR"
fi

LOG="$DIR/cpu_run_$(date +%Y%m%d_%H%M).log"
nohup "$DIR/cpu_reproduce/run_all.sh" > "$LOG" 2>&1 < /dev/null &
log "started (pid $!). Follow with:  tail -f $LOG"
log "results will be in $DIR/results/cpu/$(hostname -s)_<date>/ (summary.txt at the end)"
