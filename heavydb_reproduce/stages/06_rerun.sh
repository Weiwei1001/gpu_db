#!/bin/bash
# Stage 6 (only when asked for, e.g. by ./fix_results.sh): re-measure a few queries in existing Category A/B results
# without rerunning everything. For every query listed in HB_RERUN ("suite:query ..."):
#   1. back up the result CSVs to <results>/backup_<time>/ and drop that query's old rows
#   2. re-screen it on the same database stages 3 run the screening on (query_screen_<suite>.csv is updated in place)
#   3. rerun Cat A (stage 3) and Cat B (stage 4) for it on every SF; a query that now fails screening is left out, as in a normal run
# It also corrects the duty column of a catC_grid.csv written before run_catc.py divided by all trial windows (was 3x too large).
set -euo pipefail
: "${HB_ROOT:?}" "${PY:?}" "${HB_RERUN:?HB_RERUN is empty, e.g. HB_RERUN=\"tpch:q14 clickbench:q28\"}"
export HB_RERUN
source "$HB_ROOT/lib/datasets.sh"
R="$HB_RESULTS_BASE"; L="$HB_ROOT/logs"; mkdir -p "$L"
log(){ echo "[$(date +%T)] [rerun] $*"; }
[ -f "$R/catA_powersample.csv" ] || { log "!! no Cat A results in $R; run ./run_ab.sh first"; exit 1; }

BK="$R/backup_$(date +%Y%m%d_%H%M%S)"; mkdir -p "$BK"
cp "$R"/*.csv "$BK"/
log "queries: $HB_RERUN   (old CSVs backed up to $BK)"

# ---------- 1. drop the old rows ----------
"$PY" - "$R" <<'PY'
import csv, os, sys
R = sys.argv[1]
drop = {tuple(x.split(":")) for x in os.environ["HB_RERUN"].split()}
for name in ("catA_powersample.csv", "catB_timing.csv", "catB_energy.csv"):
    p = os.path.join(R, name)
    if not os.path.exists(p):
        continue
    with open(p, newline="") as f:
        rd = csv.DictReader(f); keys = rd.fieldnames; rows = list(rd)
    keep = [r for r in rows if (r["suite"], r["query"]) not in drop]
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(keep)
    print(f"  {name}: dropped {len(rows) - len(keep)} old rows")
PY

# ---------- 2. re-screen ----------
first_db(){ local -n arr=$1; IFS=: read -r _ db _ _ <<< "${arr[0]}"; echo "$db"; }
for suite in $(tr ' ' '\n' <<< "$HB_RERUN" | cut -d: -f1 | sort -u); do
  case "$suite" in tpch|case_bench) db=$(first_db TPCH_SETS);; h2o) db=$(first_db H2O_SETS);;
                   clickbench|clickbench_approx) db=$(first_db CB_SETS);; *) log "!! unknown suite $suite"; exit 2;; esac
  qs=$(tr ' ' '\n' <<< "$HB_RERUN" | awk -F: -v s="$suite" '$1==s{print $2}' | paste -sd,)
  log "re-screening $suite: $qs (database $db)"
  HB_SCREEN_DBS="$suite=$db" HB_SCREEN_QUERIES="$qs" "$PY" -u "$HB_ROOT/lib/screen_queries.py" "$suite" > "$L/screen_${suite}_rerun.log" 2>&1
  grep -E '^  (ok|FAIL)' "$L/screen_${suite}_rerun.log" || true
done

# ---------- 3. Cat A + Cat B for the listed queries only ----------
export HB_RESULTS_DIR="$R" HB_LOG_TAG="_rerun"
bash "$HB_ROOT/stages/03_cat_a.sh"
bash "$HB_ROOT/stages/04_cat_b.sh"

# ---------- Cat C lite: old duty column ----------
if [ -f "$R/catC_grid.csv" ] && [ ! -f "$R/.catc_duty_fixed" ]; then
  "$PY" - "$R/catC_grid.csv" <<'PY' && touch "$R/.catc_duty_fixed"
import csv, sys
p = sys.argv[1]
with open(p, newline="") as f:
    rd = csv.DictReader(f); keys = rd.fieldnames; rows = list(rd)
d = [float(r["duty"]) for r in rows if r["duty"]]
# old run_catc.py: all trials' latencies over ONE trial's mean window -> duty = trials x true duty; full mode used 3 trials
if d and max(d) > 1.2:
    for r in rows:
        if r["duty"]:
            r["duty"] = round(float(r["duty"]) / 3, 3)
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(rows)
    print(f"  catC_grid.csv: duty column divided by 3 (was up to {max(d):.2f})")
PY
fi
log "done"
