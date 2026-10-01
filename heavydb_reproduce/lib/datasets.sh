# 数据集清单（被各阶段 source）。格式 "suite:db:sf:参数"
# smoke 模式：极小数据，只为验证全链路；full 模式：repo 规格 4 benchmark × 4 SF
if [ "${HB_MODE:-smoke}" = "smoke" ]; then
  TPCH_SETS=("tpch:tpch_smoke:smoke:0.01")            # dbgen sf=0.01（约 6 万行 lineitem）
  H2O_SETS=("h2o:h2o_smoke:smoke:200000")             # 20 万行
  CB_SETS=("clickbench:cb_smoke:smoke:0.02")          # 远程采样 0.02%（约 2 万行）
else
  TPCH_SETS=(); for sf in ${HB_SFS_TPCH:-1 5 10 20}; do TPCH_SETS+=("tpch:tpch_sf$sf:sf$sf:$sf"); done
  H2O_SETS=(); for g in ${HB_SFS_H2O:-1 2 4 8}; do H2O_SETS+=("h2o:h2o_${g}gb:${g}gb:$((g*35000000))"); done
  CB_SETS=();  for sf in ${HB_SFS_CB:-1 5 10 20}; do CB_SETS+=("clickbench:cb_sf$sf:sf$sf:$(python3 -c "print(100.0*$sf/70)")"); done
fi
# smoke 模式每套只跑这几条 query（覆盖 扫描/聚合/join/大结果集/近似去重 各一种）
SMOKE_ONLY_tpch="q1,q6"; SMOKE_ONLY_case_bench="q1,q5"; SMOKE_ONLY_h2o="q1,q3"
SMOKE_ONLY_clickbench="q0,q31"; SMOKE_ONLY_clickbench_approx="q4,q13"

# 各阶段共用：本次的输出目录 / 日志后缀 / query 子集。Cat C 在每个网格点把它们指到自己的子目录。
HB_RESULTS_BASE="${HB_RESULTS_BASE:-$HB_ROOT/results/${HB_MODE:-smoke}}"   # 按模式分：results/smoke、results/full
HB_RESULTS_DIR="${HB_RESULTS_DIR:-$HB_RESULTS_BASE}"
HB_LOG_TAG="${HB_LOG_TAG:-}"
# only_args <suite>：输出给 runner 的 "--only ..."（可为空）
#   smoke 模式 -> SMOKE_ONLY_<suite>；设了 HB_QUERY_LIMIT=N -> 该 suite 筛查可跑的前 N 条
only_args(){
  if [ "${HB_MODE:-smoke}" = smoke ]; then local v="SMOKE_ONLY_$1"; echo "--only ${!v}"; return; fi
  [ -n "${HB_QUERY_LIMIT:-}" ] || return 0
  local scr="$HB_RESULTS_BASE/query_screen_$1.csv"
  [ -f "$scr" ] || { echo "!! 缺 $scr（先跑一次 Cat A 筛查）" >&2; return 1; }
  echo "--only $(awk -F, 'NR==1{for(i=1;i<=NF;i++)h[$i]=i; next} $h["ok"]=="True"{print $h["query"]}' "$scr" \
        | sort -V | head -n "$HB_QUERY_LIMIT" | paste -sd,)"
}
