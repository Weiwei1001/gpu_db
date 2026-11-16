#!/bin/bash
# Stage 7: 120-query microbenchmark, Category A semantics (data resident on the GPU: warm-up, 3 timed runs, NVML energy
# counter with short queries amplified to a >=2 s window), on the same TPC-H / H2O / ClickBench databases as stages 3-4.
# Queries: queries/microbench_{tpch,h2o,clickbench} (55 + 35 + 30, generated from ../microbench by lib/gen_microbench.py).
# Differences from stage 3, all needed because microbench results can be huge (a full sort returns every row):
#   * no screening pass -- a query HeavyDB cannot run is recorded with its error text and the run continues;
#   * the result row count is taken on the server (COUNT(*) of the query), never by fetching the result;
#   * an ORDER BY without LIMIT keeps its sort inside the COUNT(*) wrap (LIMIT <row count>); the Calcite plan of
#     every measured ORDER BY query is checked for the sort (column sort_in_plan).
# Output: $HB_RESULTS_DIR/catA_microbench.csv (+ traces_microbench/). A suite x SF already present without errors is
# skipped, so an interrupted run resumes; HB_MB_FORCE=1 measures everything again (rows are appended).
# Environment: HB_MB_SUITES="tpch h2o clickbench" restricts the suites; HB_SFS_* restrict the scale factors (lib/datasets.sh).
set -euo pipefail
: "${HB_ROOT:?}" "${PY:?}"
source "$HB_ROOT/lib/datasets.sh"
R="$HB_RESULTS_DIR"; L="$HB_ROOT/logs"; OUT="$R/catA_microbench.csv"; mkdir -p "$R" "$L"
log(){ echo "[$(date +%T)] [micro${HB_LOG_TAG}] $*"; }
[ "${HB_MODE:-smoke}" = smoke ] && EXTRA="--window 0.5" || EXTRA="--window 2.0"
SUITES=" ${HB_MB_SUITES:-tpch h2o clickbench} "
"$PY" "$HB_ROOT/lib/gen_microbench.py" --check       # the committed HeavyDB SQL must match ../microbench
done_ok(){  # $1=suite $2=sf: 0 when this suite x SF already has rows and none of them failed
  [ "${HB_MB_FORCE:-0}" = 1 ] || [ ! -s "$OUT" ] && return 1
  awk -F, -v s="$1" -v f="$2" 'NR==1{for(i=1;i<=NF;i++)h[$i]=i; next}
       $h["suite"]==s && $h["sf"]==f {n++; if ($h["error"]!="") e++} END{exit !(n>0 && e==0)}' "$OUT"
}
run_one(){  # $1=bench $2=db $3=sf
  local suite="microbench_$1" only=""
  [[ "$SUITES" == *" $1 "* ]] || return 0
  if done_ok "$suite" "$3"; then log "$suite / $3 already measured, skipping (HB_MB_FORCE=1 to redo)"; return 0; fi
  if [ "${HB_MODE:-smoke}" = smoke ]; then local v="SMOKE_ONLY_$suite"; only="--only ${!v}"; fi
  log "$suite / $3"
  "$PY" -u "$HB_ROOT/lib/run_powersample.py" --suite "$suite" --db "$2" --sf "$3" --reps 3 $EXTRA $only \
      --server-count --screen none --out "$OUT" --traces "$R/traces_microbench" \
      > "$L/micro_$1_$3${HB_LOG_TAG}.log" 2>&1 || log "!! runner exited with status $? (see $L/micro_$1_$3${HB_LOG_TAG}.log)"
  grep -E '^  w' "$L/micro_$1_$3${HB_LOG_TAG}.log" | tail -1 || true
}
for d in "${TPCH_SETS[@]}"; do IFS=: read -r s db sf _ <<< "$d"; run_one tpch "$db" "$sf"; done
for d in "${H2O_SETS[@]}";  do IFS=: read -r s db sf _ <<< "$d"; run_one h2o "$db" "$sf"; done
for d in "${CB_SETS[@]}";   do IFS=: read -r s db sf _ <<< "$d"; run_one clickbench "$db" "$sf"; done
[ -s "$OUT" ] || { log "!! no rows written to $OUT"; exit 1; }
"$PY" - "$OUT" <<'PY'
import csv, sys, collections
rows = list(csv.DictReader(open(sys.argv[1])))
n, err = collections.Counter(), collections.defaultdict(list)
for r in rows:
    n[(r["suite"], r["sf"])] += 1
    if r["error"]:
        err[(r["suite"], r["sf"])].append(f'{r["query"]}: {r["error"][:80]}')
print(f"== {sys.argv[1]}: {len(rows)} rows")
for k in sorted(n):
    print(f"  {k[0]:22s} {k[1]:5s} {n[k]:3d} rows, {len(err[k])} failed")
    for e in err[k]:
        print(f"      {e}")
unk = sorted({r["query"] for r in rows if r.get("sort_in_plan") == "unknown"})
if unk:
    print("  !! could not read the Calcite plan of:", ", ".join(unk), "(check by hand that they sort)")
PY
log "done -> $OUT"
