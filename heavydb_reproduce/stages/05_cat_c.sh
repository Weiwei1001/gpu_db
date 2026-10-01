#!/bin/bash
# 阶段 5：Category C —— 功率上限 × SM 时钟网格。对齐 repo 的 run_energy_sweep.py：
# 在每个网格点把 Category A（以及 B）完整重跑一遍，结果按网格点分目录：
#   results/<模式>/catC/pl<W>_sm<MHz>/{catA_powersample.csv, catB_timing.csv, catB_energy.csv}
# 汇总（带 pl_w / sm_mhz 两列）：results/<模式>/catC_A.csv、catC_B_timing.csv、catC_B_energy.csv
#
# 环境变量（reproduce.sh 的对应选项）：
#   HB_CATC_SCOPE   ab（默认，每点重跑 A+B）| a（只重跑 A）| lite（旧版：6 条代表 query，约 45 分钟）
#   HB_QUERY_LIMIT  每个 benchmark×SF 只跑筛查可跑的前 N 条（测试用；不设 = 全部 query）
#   HB_CATC_PLS / HB_CATC_SMS  覆盖网格（空格分隔）；默认按本卡解析：功率 [min..max] 5 档、SM [最低..最高] 5 档
# 断点续跑：跑完的网格点留 .done 标记，重跑时跳过。无论怎么退出都恢复默认功率上限与时钟。
set -euo pipefail
: "${HB_ROOT:?}" "${PY:?}" "${GPU:?}"
source "$HB_ROOT/lib/datasets.sh"
R="$HB_RESULTS_BASE"; L="$HB_ROOT/logs"
SCOPE="${HB_CATC_SCOPE:-ab}"
log(){ echo "[$(date +%T)] [catC] $*"; }
sudo -n nvidia-smi -L >/dev/null 2>&1 || { log "!! 需要 passwordless sudo 才能改功率/时钟，跳过 Cat C"; exit 0; }

# ---------- 旧版：6 条代表 query ----------
if [ "$SCOPE" = lite ]; then
  pick(){ local -n arr=$1; local i=2; [ ${#arr[@]} -le 2 ] && i=$((${#arr[@]}-1)); echo "${arr[$i]}"; }
  IFS=: read -r _ TDB TSF _ <<< "$(pick TPCH_SETS)"
  IFS=: read -r _ HDB HSF _ <<< "$(pick H2O_SETS)"
  IFS=: read -r _ CDB CSF _ <<< "$(pick CB_SETS)"
  if [ "${HB_MODE:-smoke}" = smoke ]; then GRID="--n-pl 2 --n-sm 2 --trials 1 --window 0.5"
  else GRID="--n-pl 5 --n-sm 5 --trials 3 --window 2.0"; fi
  log "lite：网格 $GRID；目标 $TDB/$HDB/$CDB"
  "$PY" -u "$HB_ROOT/lib/run_catc.py" $GRID \
    --targets "tpch:$TDB:$TSF:q1,q6;h2o:$HDB:$HSF:q1,q3;clickbench:$CDB:$CSF:q0,q31" \
    --out "$R/catC_grid.csv" > "$L/catC.log" 2>&1
  grep -E '^\[restore\]|^->' "$L/catC.log"
  log "完成 -> $R/catC_grid.csv"; exit 0
fi

# ---------- 完整版：每个网格点重跑 Cat A（+B）----------
case "$SCOPE" in a|ab) ;; *) log "!! HB_CATC_SCOPE 只能是 ab / a / lite"; exit 2;; esac
q(){ nvidia-smi -i "$GPU" --query-gpu="$1" --format=csv,noheader,nounits | head -1 | cut -d. -f1; }
PLMIN=$(q power.min_limit); PLMAX=$(q power.max_limit)
if [ "${HB_MODE:-smoke}" = smoke ]; then NPL=2; NSM=2; else NPL=5; NSM=5; fi
PLS="${HB_CATC_PLS:-$("$PY" -c "n=$NPL; lo,hi=$PLMIN,$PLMAX; print(' '.join(str(round(lo+i*(hi-lo)/(n-1))) for i in range(n)))")}"
SMS="${HB_CATC_SMS:-$(nvidia-smi -i "$GPU" --query-supported-clocks=gr --format=csv,noheader,nounits | "$PY" -c "
import sys
sup = sorted({int(x) for x in sys.stdin.read().split() if x.isdigit()}); n = $NSM
want = [sup[0] + i * (sup[-1] - sup[0]) / (n - 1) for i in range(n)]
print(' '.join(str(v) for v in sorted({min(sup, key=lambda s: abs(s - w)) for w in want})))")}"
DEF_PL=$(q power.default_limit)

restore(){
  sudo nvidia-smi -i "$GPU" -rgc >/dev/null 2>&1 || true
  sudo nvidia-smi -i "$GPU" -pl "$DEF_PL" >/dev/null 2>&1 || true
  log "[restore] GPU $GPU：功率上限 ${DEF_PL} W，时钟解锁"
}
trap restore EXIT
trap 'exit 130' INT TERM

n_pts=$(( $(wc -w <<< "$PLS") * $(wc -w <<< "$SMS") ))
log "范围=$SCOPE  query=$([ -n "${HB_QUERY_LIMIT:-}" ] && echo "每套前 $HB_QUERY_LIMIT 条" || echo 全部)  功率 [$PLS] W × SM [$SMS] MHz = $n_pts 个网格点"
i=0
for pl in $PLS; do
  for sm in $SMS; do
    i=$((i+1)); D="$R/catC/pl${pl}_sm${sm}"
    if [ -f "$D/.done" ]; then log "($i/$n_pts) PL=$pl SM=$sm 已完成，跳过"; continue; fi
    rm -rf "$D"; mkdir -p "$D"
    sudo nvidia-smi -i "$GPU" -pl "$pl" >/dev/null || { log "!! 设功率上限 $pl W 失败"; exit 2; }
    sudo nvidia-smi -i "$GPU" -lgc "$sm,$sm" >/dev/null || { log "!! 锁 SM $sm MHz 失败"; exit 2; }
    sleep 2
    got=$(q power.limit)
    [ "$got" = "$pl" ] || { log "!! 功率上限读回 $got W，应为 $pl W"; exit 2; }
    log "($i/$n_pts) ===== PL=${pl}W SM=${sm}MHz ====="
    # 每条 query 前后 Guard 都会按这两个值核对功率上限与 SM 时钟
    export HB_EXPECT_PL="$pl" HB_EXPECT_SM="$sm" HB_RESULTS_DIR="$D" HB_LOG_TAG="_pl${pl}_sm${sm}"
    bash "$HB_ROOT/stages/03_cat_a.sh"
    [ "$SCOPE" = ab ] && bash "$HB_ROOT/stages/04_cat_b.sh"
    unset HB_EXPECT_PL HB_EXPECT_SM HB_RESULTS_DIR HB_LOG_TAG
    touch "$D/.done"
  done
done

# ---------- 汇总：加上 pl_w / sm_mhz 两列 ----------
"$PY" - "$R" <<'PY'
import csv, glob, os, re, sys
R = sys.argv[1]
for name, out in (("catA_powersample.csv", "catC_A.csv"), ("catB_timing.csv", "catC_B_timing.csv"),
                  ("catB_energy.csv", "catC_B_energy.csv")):
    rows, keys = [], ["pl_w", "sm_mhz"]
    for p in sorted(glob.glob(f"{R}/catC/pl*_sm*/{name}")):
        pl, sm = re.search(r"pl(\d+)_sm(\d+)", p).groups()
        for r in csv.DictReader(open(p)):
            keys += [k for k in r if k not in keys]
            rows.append({"pl_w": pl, "sm_mhz": sm, **r})
    if rows:
        with open(f"{R}/{out}", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(rows)
        print(f"  {out}: {len(rows)} 行")
PY
log "完成 -> $R/catC_A.csv$([ "$SCOPE" = ab ] && echo ", catC_B_timing.csv, catC_B_energy.csv")"
