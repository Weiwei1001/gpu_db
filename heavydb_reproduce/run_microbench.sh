#!/bin/bash
# The 120-query microbenchmark on HeavyDB (stage 7), on its own: 55 TPC-H + 35 H2O + 30 ClickBench queries x 4 SFs,
# data resident on the GPU, same method as Category A (warm-up, 3 timed runs, NVML energy counter, amplified windows).
# Uses the same databases as run_ab.sh: on a machine where run_ab.sh already ran, nothing is rebuilt or re-imported.
#
#   ./run_microbench.sh --gpu 0                                   # all 120 queries x 4 SFs (about 2-3 h with data present)
#   ./run_microbench.sh --gpu 0 --data-dir /bigdisk/hb_repro      # same --data-dir as the run_ab.sh run on this machine
#   ./run_microbench.sh --gpu 0 --smoke                           # 3 queries per suite on tiny data, checks the pipeline (minutes)
#   HB_MB_SUITES="h2o" HB_SFS_H2O="1 8" ./run_microbench.sh --gpu 0   # a subset
#   HB_MB_FORCE=1 ./run_microbench.sh --gpu 0                     # measure again suites x SFs already in the result file
#
# Result: results/full/<gpu-model>/catA_microbench.csv (columns as catA_powersample.csv, plus sort_in_plan), traces in
# traces_microbench/, per-run logs logs/micro_<bench>_<sf>.log. An interrupted run resumes where it stopped.
# Other options are those of run_full.sh / reproduce.sh (--skip-build, --heavydb-home, --deps-prefix, ...).
set -euo pipefail
HB_ROOT="$(cd "$(dirname "$0")" && pwd)"
log(){ echo "[$(date +%T)] [micro] $*"; }
SMOKE=0 PASS=()
while [ $# -gt 0 ]; do case "$1" in
  --smoke) SMOKE=1; shift;;
  --stages|--catc-scope|--catc-limit) echo "!! $1 is not accepted by run_microbench.sh"; exit 2;;
  -h|--help) sed -n '2,15p' "$0"; exit 0;;
  *) PASS+=("$1"); shift;; esac; done
mkdir -p "$HB_ROOT/logs"
LOG="$HB_ROOT/logs/run_microbench.log"
# stages 1 and 2 are idempotent: an existing build and existing databases are reused
if [ "$SMOKE" = 1 ]; then
  [[ " ${PASS[*]} " == *" --gpu "* ]] || { echo "--smoke needs --gpu N"; exit 2; }
  log "smoke run (stages 1 2 7); console log -> $LOG"
  "$HB_ROOT/reproduce.sh" --smoke --stages "1 2 7" "${PASS[@]}" 2>&1 | tee "$LOG"
else
  log "microbenchmark (stages 1 2 7); console log -> $LOG"
  "$HB_ROOT/run_full.sh" --stages "1 2 7" "${PASS[@]}" 2>&1 | tee "$LOG"
fi
exit "${PIPESTATUS[0]}"
