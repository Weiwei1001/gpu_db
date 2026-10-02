#!/bin/bash
# Stage 3: Category A -- data resident on the GPU: after warm-up, 3 latency runs + NVML energy-counter power sampling (short queries amplified to a >=2 s window)
# Environment variables (set when Cat C reuses this stage): HB_RESULTS_DIR output dir, HB_LOG_TAG log suffix, HB_QUERY_LIMIT first N queries per suite
set -euo pipefail
: "${HB_ROOT:?}" "${PY:?}"
source "$HB_ROOT/lib/datasets.sh"
R="$HB_RESULTS_DIR"; L="$HB_ROOT/logs"; OUT="$R/catA_powersample.csv"; mkdir -p "$R"
log(){ echo "[$(date +%T)] [catA${HB_LOG_TAG}] $*"; }
[ "${HB_MODE:-smoke}" = smoke ] && EXTRA="--window 0.5" || EXTRA="--window 2.0"
screen_once(){  # $1=suite $2=db -- screen each suite only once (result always goes to the main results dir), produces the passlist
  [ -f "$HB_RESULTS_BASE/query_screen_$1.csv" ] && return
  log "dialect screening $1 (database $2)"
  HB_SCREEN_DBS="$1=$2" "$PY" -u "$HB_ROOT/lib/screen_queries.py" "$1" > "$L/screen_$1.log" 2>&1
  grep 'runnable' "$L/screen_$1.log" | head -1
}
run_one(){  # $1=suite $2=db $3=sf
  local only; only=$(only_args "$1")
  log "$1 / $3"
  "$PY" -u "$HB_ROOT/lib/run_powersample.py" --suite "$1" --db "$2" --sf "$3" \
      --reps 3 $EXTRA $only --out "$OUT" --traces "$R/traces" > "$L/catA_$1_$3${HB_LOG_TAG}.log" 2>&1
  grep -E '^  q' "$L/catA_$1_$3${HB_LOG_TAG}.log" | tail -1
}
for d in "${TPCH_SETS[@]}"; do IFS=: read -r s db sf _ <<< "$d"
  screen_once tpch "$db"; screen_once case_bench "$db"
  run_one tpch "$db" "$sf"; run_one case_bench "$db" "$sf"; done
for d in "${H2O_SETS[@]}";  do IFS=: read -r s db sf _ <<< "$d"
  screen_once h2o "$db"; run_one h2o "$db" "$sf"; done
for d in "${CB_SETS[@]}";   do IFS=: read -r s db sf _ <<< "$d"
  screen_once clickbench "$db"; screen_once clickbench_approx "$db"
  run_one clickbench "$db" "$sf"; run_one clickbench_approx "$db" "$sf"; done
log "done: $(($(wc -l < "$OUT")-1)) rows -> $OUT"
