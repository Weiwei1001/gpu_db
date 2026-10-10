#!/bin/bash
# Stage 6 (only when asked for, e.g. by ./fix_results.sh): repair existing Category A/B results without rerunning everything.
#   0. back up the result CSVs to <results>/backup_<time>/
#   1. empty datasets: every database whose main table is empty or missing (an import that died halfway; stage 2 used to
#      skip such a database because it existed) is re-imported, and all its Cat A/B rows are dropped and re-measured
#   2. queries listed in HB_RERUN ("suite:query ..."): old rows dropped, re-screened on the same database stage 3 screens on
#      (query_screen_<suite>.csv is updated in place), Cat A + B rerun on every SF; one that now fails screening is left out
#   3. corrects the duty column of a catC_grid.csv written before run_catc.py divided by all trial windows (was 3x too large)
set -euo pipefail
: "${HB_ROOT:?}" "${PY:?}"
export HB_RERUN="${HB_RERUN:-}"
source "$HB_ROOT/lib/datasets.sh"
R="$HB_RESULTS_BASE"; L="$HB_ROOT/logs"; mkdir -p "$L"
log(){ echo "[$(date +%T)] [rerun] $*"; }
[ -f "$R/catA_powersample.csv" ] || { log "!! no Cat A results in $R; run ./run_ab.sh first"; exit 1; }

BK="$R/backup_$(date +%Y%m%d_%H%M%S)"; mkdir -p "$BK"
cp "$R"/*.csv "$BK"/
log "old CSVs backed up to $BK"

# ---------- 1. find empty datasets; preflight (source data present, enough disk) before touching anything ----------
first_db(){ local -n arr=$1; IFS=: read -r _ db _ _ <<< "${arr[0]}"; echo "$db"; }
EMPTY=() NEED_GB=0
for d in "${TPCH_SETS[@]}" "${H2O_SETS[@]}" "${CB_SETS[@]}"; do
  IFS=: read -r suite db sf param <<< "$d"
  n=$(db_rows "$suite" "$db")
  [ "$n" -gt 0 ] && continue
  read -r _ src bytes < <(HB_DISCOVER_ONLY=1 "$PY" "$HB_ROOT/lib/gen_data.py" "$suite" "$db" "$param" "$sf")
  gb=$(( (bytes + 999999999) / 1000000000 ))
  if [ "$src" = generate ]; then
    [ "$suite" = clickbench ] && note="will download/sample hits.parquet" || note="no source data found, will be GENERATED"
    [ "$suite" = h2o ] && note="$note (H2O generator has no seed: rows will differ from the other engines' data)"
    gb=$(( ${param%%.*} / 15000000 + 1 ))             # rough size of generated data
  else note="source $src (${gb} GB)"; fi
  log "$db is $([ "$n" = -1 ] && echo missing || echo empty) -> will re-import; $note"
  NEED_GB=$(( NEED_GB + 2 * gb + 2 ))                  # parquet temp file + HeavyDB storage
  EMPTY+=("$d")
done
if [ ${#EMPTY[@]} -gt 0 ]; then
  DD="${HB_DATA_DIR:-$HEAVYDB_HOME/build}"
  FREE=$(df -BG --output=avail "$DD" | tail -1 | tr -dc 0-9)
  log "preflight: about $NEED_GB GB needed for the re-import, $FREE GB free on $DD"
  if [ "$FREE" -lt "$NEED_GB" ]; then
    log "!! not enough disk on $DD for the re-import (an import that runs out of disk is what leaves an empty database)."
    log "!! free some space or pass --data-dir on a bigger disk; nothing has been changed"; exit 1
  fi
fi
REDO="" REDO_SF="" FULL_SCREEN=""
for d in "${EMPTY[@]}"; do
  IFS=: read -r suite db sf param <<< "$d"
  log "re-importing $db"
  if ! "$PY" "$HB_ROOT/lib/gen_data.py" "$suite" "$db" "$param" "$sf"; then
    log "!! re-import of $db failed (see the message above); old CSVs are untouched, backup in $BK"; exit 1
  fi
  REDO="$REDO $db"
  case "$suite" in tpch) fam="tpch case_bench";; h2o) fam="h2o";; *) fam="clickbench clickbench_approx";; esac
  for s in $fam; do REDO_SF="$REDO_SF $s:$sf"; done
  # the screening ran on the first dataset of each family; if that one was empty, its screening is worthless too
  case "$suite" in tpch) [ "$db" = "$(first_db TPCH_SETS)" ] && FULL_SCREEN="$FULL_SCREEN tpch:$db case_bench:$db";;
                   h2o) [ "$db" = "$(first_db H2O_SETS)" ] && FULL_SCREEN="$FULL_SCREEN h2o:$db";;
                   *) [ "$db" = "$(first_db CB_SETS)" ] && FULL_SCREEN="$FULL_SCREEN clickbench:$db clickbench_approx:$db";; esac
done
[ -n "$REDO" ] || log "no empty datasets"
[ -n "$REDO$HB_RERUN" ] || log "nothing to re-measure (HB_RERUN is empty)"
export HB_REDO_SF="$REDO_SF"

# ---------- 2. drop the old rows ----------
"$PY" - "$R" <<'PY'
import csv, os, sys
R = sys.argv[1]
drop = {tuple(x.split(":")) for x in os.environ["HB_RERUN"].split()}            # (suite, query) on every SF
drop_sf = {tuple(x.split(":")) for x in os.environ["HB_REDO_SF"].split()}       # (suite, sf): every query
for name in ("catA_powersample.csv", "catB_timing.csv", "catB_energy.csv"):
    p = os.path.join(R, name)
    if not os.path.exists(p):
        continue
    with open(p, newline="") as f:
        rd = csv.DictReader(f); keys = rd.fieldnames; rows = list(rd)
    keep = [r for r in rows if (r["suite"], r["query"]) not in drop and (r["suite"], r["sf"]) not in drop_sf]
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys); w.writeheader(); w.writerows(keep)
    print(f"  {name}: dropped {len(rows) - len(keep)} old rows")
PY

# ---------- 3. re-screen ----------
for sd in $FULL_SCREEN; do
  IFS=: read -r suite db <<< "$sd"
  log "re-screening all of $suite (its screening database $db was re-imported)"
  HB_SCREEN_DBS="$suite=$db" "$PY" -u "$HB_ROOT/lib/screen_queries.py" "$suite" > "$L/screen_${suite}_rerun.log" 2>&1
  grep 'runnable' "$L/screen_${suite}_rerun.log" | head -1
done
for suite in $(tr ' ' '\n' <<< "$HB_RERUN" | cut -d: -f1 | sort -u); do
  [[ " $FULL_SCREEN " == *" $suite:"* ]] && continue
  case "$suite" in tpch|case_bench) db=$(first_db TPCH_SETS);; h2o) db=$(first_db H2O_SETS);;
                   clickbench|clickbench_approx) db=$(first_db CB_SETS);; *) log "!! unknown suite $suite"; exit 2;; esac
  qs=$(tr ' ' '\n' <<< "$HB_RERUN" | awk -F: -v s="$suite" '$1==s{print $2}' | paste -sd,)
  log "re-screening $suite: $qs (database $db)"
  HB_SCREEN_DBS="$suite=$db" HB_SCREEN_QUERIES="$qs" "$PY" -u "$HB_ROOT/lib/screen_queries.py" "$suite" > "$L/screen_${suite}_rerun.log" 2>&1
  grep -E '^  (ok|FAIL)' "$L/screen_${suite}_rerun.log" || true
done

# ---------- 4. Cat A + Cat B ----------
export HB_RESULTS_DIR="$R" HB_LOG_TAG="_rerun"
if [ -n "$HB_RERUN" ]; then             # listed queries, on the datasets that are not re-measured in full below
  HB_SKIP_DBS="$REDO" bash "$HB_ROOT/stages/03_cat_a.sh"
  HB_SKIP_DBS="$REDO" bash "$HB_ROOT/stages/04_cat_b.sh"
fi
if [ -n "$REDO" ]; then                 # re-imported datasets: every query that passed screening
  log "re-measuring all queries on:$REDO"
  HB_RERUN="" HB_ONLY_DBS="$REDO" bash "$HB_ROOT/stages/03_cat_a.sh"
  HB_RERUN="" HB_ONLY_DBS="$REDO" bash "$HB_ROOT/stages/04_cat_b.sh"
fi

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
