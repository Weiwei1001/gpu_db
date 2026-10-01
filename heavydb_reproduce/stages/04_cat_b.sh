#!/bin/bash
# 阶段 4：Category B —— 数据在 CPU 内存，每次执行前 \clear_gpu，含 PCIe 传输：计时 + 能耗
# 环境变量同阶段 3：HB_RESULTS_DIR / HB_LOG_TAG / HB_QUERY_LIMIT
set -euo pipefail
: "${HB_ROOT:?}" "${PY:?}"
source "$HB_ROOT/lib/datasets.sh"
R="$HB_RESULTS_DIR"; L="$HB_ROOT/logs"; mkdir -p "$R"
log(){ echo "[$(date +%T)] [catB${HB_LOG_TAG}] $*"; }
if [ "${HB_MODE:-smoke}" = smoke ]; then T_EXTRA="--reps 2 --min-reps 1 --budget 10"; E_EXTRA="--trials 1 --window 0.5"
else T_EXTRA="--reps 3 --warmup 0 --budget 25 --min-reps 2"; E_EXTRA="--trials 3 --window 2.0"; fi
run_one(){  # $1=suite $2=db $3=sf
  local only; only=$(only_args "$1")
  log "$1 / $3 计时"
  "$PY" -u "$HB_ROOT/lib/run_timing.py" --suite "$1" --db "$2" --sf "$3" --mode gpu --cold \
      $T_EXTRA $only --out "$R/catB_timing.csv" > "$L/catB_t_$1_$3${HB_LOG_TAG}.log" 2>&1
  log "$1 / $3 能耗"
  "$PY" -u "$HB_ROOT/lib/run_energy.py" --suite "$1" --db "$2" --sf "$3" --cold \
      $E_EXTRA $only --out "$R/catB_energy.csv" > "$L/catB_e_$1_$3${HB_LOG_TAG}.log" 2>&1
  grep -E '^  q' "$L/catB_e_$1_$3${HB_LOG_TAG}.log" | tail -1
}
for d in "${TPCH_SETS[@]}"; do IFS=: read -r s db sf _ <<< "$d"; run_one tpch "$db" "$sf"; run_one case_bench "$db" "$sf"; done
for d in "${H2O_SETS[@]}";  do IFS=: read -r s db sf _ <<< "$d"; run_one h2o "$db" "$sf"; done
for d in "${CB_SETS[@]}";   do IFS=: read -r s db sf _ <<< "$d"; run_one clickbench "$db" "$sf"; run_one clickbench_approx "$db" "$sf"; done
log "完成 -> $R/catB_timing.csv, $R/catB_energy.csv"
