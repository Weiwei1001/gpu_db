#!/bin/bash
# Stage 5: Category C -- power limit × SM clock grid. Matches the repo's run_energy_sweep.py:
# at every grid point Category A (and B) is rerun in full; results go in one directory per grid point:
#   results/<mode>/catC/pl<W>_sm<MHz>/{catA_powersample.csv, catB_timing.csv, catB_energy.csv}
# Aggregates (with pl_w / sm_mhz columns): results/<mode>/catC_A.csv, catC_B_timing.csv, catC_B_energy.csv
#
# Environment variables (the matching reproduce.sh options):
#   HB_CATC_SCOPE   ab (default, rerun A+B at every point) | a (rerun A only) | lite (legacy: 6 representative queries, about 45 min)
#   HB_QUERY_LIMIT  run only the first N queries per benchmark×SF that passed screening (for testing; unset = all queries)
#   HB_CATC_PLS / HB_CATC_SMS  override the grid (space-separated); default is derived from this GPU: power [min..max] in 5 levels, SM [lowest..highest] in 5 levels
# Resumable: a finished grid point leaves a .done marker and is skipped on rerun. The default power limit and clocks are restored however the script exits.
set -euo pipefail
: "${HB_ROOT:?}" "${PY:?}" "${GPU:?}"
source "$HB_ROOT/lib/datasets.sh"
R="$HB_RESULTS_BASE"; L="$HB_ROOT/logs"
SCOPE="${HB_CATC_SCOPE:-ab}"
log(){ echo "[$(date +%T)] [catC] $*"; }
sudo -n nvidia-smi -L >/dev/null 2>&1 || { log "!! passwordless sudo is required to change power/clocks; skipping Cat C"; exit 0; }

# ---------- Legacy: 6 representative queries ----------
if [ "$SCOPE" = lite ]; then
  pick(){ local -n arr=$1; local i=2; [ ${#arr[@]} -le 2 ] && i=$((${#arr[@]}-1)); echo "${arr[$i]}"; }
  IFS=: read -r _ TDB TSF _ <<< "$(pick TPCH_SETS)"
  IFS=: read -r _ HDB HSF _ <<< "$(pick H2O_SETS)"
  IFS=: read -r _ CDB CSF _ <<< "$(pick CB_SETS)"
  if [ "${HB_MODE:-smoke}" = smoke ]; then GRID="--n-pl 2 --n-sm 2 --trials 1 --window 0.5"
  else GRID="--n-pl 5 --n-sm 5 --trials 3 --window 2.0"; fi
  log "lite: grid $GRID; targets $TDB/$HDB/$CDB"
  "$PY" -u "$HB_ROOT/lib/run_catc.py" $GRID \
    --targets "tpch:$TDB:$TSF:q1,q6;h2o:$HDB:$HSF:q1,q3;clickbench:$CDB:$CSF:q0,q31" \
    --out "$R/catC_grid.csv" > "$L/catC.log" 2>&1
  grep -E '^\[restore\]|^->' "$L/catC.log"
  log "done -> $R/catC_grid.csv"; exit 0
fi

# ---------- Full version: rerun Cat A (+B) at every grid point ----------
case "$SCOPE" in a|ab) ;; *) log "!! HB_CATC_SCOPE must be one of ab / a / lite"; exit 2;; esac
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
  log "[restore] GPU $GPU: power limit ${DEF_PL} W, clocks unlocked"
}
trap restore EXIT
trap 'exit 130' INT TERM

n_pts=$(( $(wc -w <<< "$PLS") * $(wc -w <<< "$SMS") ))
log "scope=$SCOPE  query=$([ -n "${HB_QUERY_LIMIT:-}" ] && echo "first $HB_QUERY_LIMIT per suite" || echo all)  power [$PLS] W × SM [$SMS] MHz = $n_pts grid points"
i=0
for pl in $PLS; do
  for sm in $SMS; do
    i=$((i+1)); D="$R/catC/pl${pl}_sm${sm}"
    if [ -f "$D/.done" ]; then log "($i/$n_pts) PL=$pl SM=$sm already done, skipping"; continue; fi
    rm -rf "$D"; mkdir -p "$D"
    sudo nvidia-smi -i "$GPU" -pl "$pl" >/dev/null || { log "!! failed to set power limit $pl W"; exit 2; }
    sudo nvidia-smi -i "$GPU" -lgc "$sm,$sm" >/dev/null || { log "!! failed to lock SM clock at $sm MHz"; exit 2; }
    sleep 2
    got=$(q power.limit)
    [ "$got" = "$pl" ] || { log "!! power limit reads back $got W, expected $pl W"; exit 2; }
    log "($i/$n_pts) ===== PL=${pl}W SM=${sm}MHz ====="
    # Guard checks the power limit and SM clock against these two values before and after every query
    export HB_EXPECT_PL="$pl" HB_EXPECT_SM="$sm" HB_RESULTS_DIR="$D" HB_LOG_TAG="_pl${pl}_sm${sm}"
    bash "$HB_ROOT/stages/03_cat_a.sh"
    [ "$SCOPE" = ab ] && bash "$HB_ROOT/stages/04_cat_b.sh"
    unset HB_EXPECT_PL HB_EXPECT_SM HB_RESULTS_DIR HB_LOG_TAG
    touch "$D/.done"
  done
done

# ---------- Aggregate: add the pl_w / sm_mhz columns ----------
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
        print(f"  {out}: {len(rows)} rows")
PY
log "done -> $R/catC_A.csv$([ "$SCOPE" = ab ] && echo ", catC_B_timing.csv, catC_B_energy.csv")"
