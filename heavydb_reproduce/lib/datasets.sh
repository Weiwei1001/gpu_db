# Dataset list (sourced by every stage). Format "suite:db:sf:param"
# smoke mode: tiny data, only to verify the whole pipeline; full mode: repo spec, 4 benchmarks × 4 SF
if [ "${HB_MODE:-smoke}" = "smoke" ]; then
  TPCH_SETS=("tpch:tpch_smoke:smoke:0.01")            # dbgen sf=0.01 (about 60k lineitem rows)
  H2O_SETS=("h2o:h2o_smoke:smoke:200000")             # 200k rows
  CB_SETS=("clickbench:cb_smoke:smoke:0.02")          # remote sample 0.02% (about 20k rows)
else
  TPCH_SETS=(); for sf in ${HB_SFS_TPCH:-1 5 10 20}; do TPCH_SETS+=("tpch:tpch_sf$sf:sf$sf:$sf"); done
  H2O_SETS=(); for g in ${HB_SFS_H2O:-1 2 4 8}; do H2O_SETS+=("h2o:h2o_${g}gb:${g}gb:$((g*35000000))"); done
  CB_SETS=();  for sf in ${HB_SFS_CB:-1 5 10 20}; do CB_SETS+=("clickbench:cb_sf$sf:sf$sf:$(python3 -c "print(100.0*$sf/70)")"); done
fi
# in smoke mode each suite runs only these queries (one each of scan / aggregate / join / large result set / approximate distinct)
SMOKE_ONLY_tpch="q1,q6"; SMOKE_ONLY_case_bench="q1,q5"; SMOKE_ONLY_h2o="q1,q3"
SMOKE_ONLY_clickbench="q0,q31"; SMOKE_ONLY_clickbench_approx="q4,q13"

# Shared by all stages: this run's output dir / log suffix / query subset. Cat C points them at its own subdirectory at every grid point.
HB_RESULTS_BASE="${HB_RESULTS_BASE:-$HB_ROOT/results/${HB_MODE:-smoke}}"   # per mode: results/smoke, results/full
HB_RESULTS_DIR="${HB_RESULTS_DIR:-$HB_RESULTS_BASE}"
HB_LOG_TAG="${HB_LOG_TAG:-}"
# only_args <suite>: prints the "--only ..." argument for the runner (may be empty)
#   smoke mode -> SMOKE_ONLY_<suite>; with HB_QUERY_LIMIT=N -> the first N queries of that suite that passed screening
only_args(){
  if [ "${HB_MODE:-smoke}" = smoke ]; then local v="SMOKE_ONLY_$1"; echo "--only ${!v}"; return; fi
  [ -n "${HB_QUERY_LIMIT:-}" ] || return 0
  local scr="$HB_RESULTS_BASE/query_screen_$1.csv"
  [ -f "$scr" ] || { echo "!! missing $scr (run the Cat A screening once first)" >&2; return 1; }
  echo "--only $(awk -F, 'NR==1{for(i=1;i<=NF;i++)h[$i]=i; next} $h["ok"]=="True"{print $h["query"]}' "$scr" \
        | sort -V | head -n "$HB_QUERY_LIMIT" | paste -sd,)"
}
