#!/bin/bash
# 阶段 2：生成并导入全部数据集（需要服务端已启动）
set -euo pipefail
: "${HB_ROOT:?}" "${PY:?}"
source "$HB_ROOT/lib/datasets.sh"
log(){ echo "[$(date +%T)] [data] $*"; }
for d in "${TPCH_SETS[@]}" "${H2O_SETS[@]}" "${CB_SETS[@]}"; do
  IFS=: read -r suite db sf param <<< "$d"
  if "$HB_ROOT/lib/hsql" heavyai "\l" 2>/dev/null | grep -q "^$db |"; then
    log "$db 已存在，跳过"; continue
  fi
  log "准备 $suite/$sf -> $db"
  "$PY" "$HB_ROOT/lib/gen_data.py" "$suite" "$db" "$param" "$sf"
done
df -h "$HB_ROOT" | tail -1
log "数据就绪"
