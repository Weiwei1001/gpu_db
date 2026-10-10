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
# rerun_only <suite>: with HB_RERUN="tpch:q14 clickbench:q28" (stage 6), prints the comma-separated queries of this suite that are
#   listed there AND passed screening (and, in smoke mode, are in SMOKE_ONLY_<suite>); empty output = nothing to rerun for this suite
rerun_only(){
  local scr="$HB_RESULTS_BASE/query_screen_$1.csv" want smoke=""
  want=$(tr ' ' '\n' <<< "$HB_RERUN" | awk -F: -v s="$1" '$1==s{print $2}')
  [ -n "$want" ] && [ -f "$scr" ] || return 0
  [ "${HB_MODE:-smoke}" = smoke ] && { local v="SMOKE_ONLY_$1"; smoke="${!v}"; }
  awk -F, -v want="$(paste -sd, <<< "$want")" -v smoke="$smoke" '
    NR==1{for(i=1;i<=NF;i++)h[$i]=i; n=split(want,w,","); for(i=1;i<=n;i++)W[w[i]]=1
          m=split(smoke,s,","); for(i=1;i<=m;i++)S[s[i]]=1; next}
    $h["ok"]=="True" && ($h["query"] in W) && (smoke=="" || ($h["query"] in S)){print $h["query"]}' "$scr" | paste -sd,
}
# query_args <suite>: the --only argument for stages 3/4; returns 1 when this suite has nothing to run (stage-6 rerun of other suites)
query_args(){
  if [ -n "${HB_RERUN:-}" ]; then local q; q=$(rerun_only "$1"); [ -n "$q" ] || return 1; echo "--only $q"
  else only_args "$1" || return 2; fi     # 2 = real error (e.g. missing screening file)
}
# db_rows <suite> <db>: row count of the dataset's main table (lineitem / groupby / t, the last one imported);
#   -1 = database does not exist, 0 = exists but empty / table missing (an import that died halfway)
db_rows(){
  local t; case "$1" in tpch) t=lineitem;; h2o) t=groupby;; *) t=t;; esac
  "$HB_ROOT/lib/hsql" heavyai "\l" 2>/dev/null | grep -q "^$2 |" || { echo -1; return; }
  local n; n=$("$HB_ROOT/lib/hsql" "$2" "SELECT COUNT(*) FROM $t;" 2>/dev/null | head -1 | tr -dc 0-9)
  echo "${n:-0}"
}
# HB_ONLY_DBS / HB_SKIP_DBS (space-separated db names): restrict which datasets stages 3/4 run on (used by stage 6)
_filter_sets(){
  local -n arr=$1; local keep=() d db
  for d in "${arr[@]}"; do IFS=: read -r _ db _ _ <<< "$d"
    [ -n "${HB_ONLY_DBS:-}" ] && [[ " $HB_ONLY_DBS " != *" $db "* ]] && continue
    [[ " ${HB_SKIP_DBS:-} " == *" $db "* ]] && continue
    keep+=("$d"); done
  arr=("${keep[@]}")
}
if [ -n "${HB_ONLY_DBS:-}${HB_SKIP_DBS:-}" ]; then _filter_sets TPCH_SETS; _filter_sets H2O_SETS; _filter_sets CB_SETS; fi
