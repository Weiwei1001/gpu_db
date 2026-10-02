#!/bin/bash
# Stage 2: generate and import all datasets (the server must already be running)
set -euo pipefail
: "${HB_ROOT:?}" "${PY:?}"
source "$HB_ROOT/lib/datasets.sh"
log(){ echo "[$(date +%T)] [data] $*"; }
for d in "${TPCH_SETS[@]}" "${H2O_SETS[@]}" "${CB_SETS[@]}"; do
  IFS=: read -r suite db sf param <<< "$d"
  if "$HB_ROOT/lib/hsql" heavyai "\l" 2>/dev/null | grep -q "^$db |"; then
    log "$db already exists, skipping"; continue
  fi
  log "preparing $suite/$sf -> $db"
  "$PY" "$HB_ROOT/lib/gen_data.py" "$suite" "$db" "$param" "$sf"
done
df -h "$HB_ROOT" | tail -1
log "data ready"
