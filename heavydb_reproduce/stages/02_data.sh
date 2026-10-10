#!/bin/bash
# Stage 2: generate and import all datasets (the server must already be running)
set -euo pipefail
: "${HB_ROOT:?}" "${PY:?}"
source "$HB_ROOT/lib/datasets.sh"
log(){ echo "[$(date +%T)] [data] $*"; }
for d in "${TPCH_SETS[@]}" "${H2O_SETS[@]}" "${CB_SETS[@]}"; do
  IFS=: read -r suite db sf param <<< "$d"
  n=$(db_rows "$suite" "$db")
  if [ "$n" -gt 0 ]; then log "$db already exists ($n rows), skipping"; continue; fi
  [ "$n" = 0 ] && log "$db exists but its main table is empty or missing (an earlier import failed?); re-importing"
  log "preparing $suite/$sf -> $db"
  "$PY" "$HB_ROOT/lib/gen_data.py" "$suite" "$db" "$param" "$sf"
done
df -h "$HB_ROOT" | tail -1
log "data ready"
