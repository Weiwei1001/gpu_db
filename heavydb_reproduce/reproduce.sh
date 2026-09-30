#!/bin/bash
# 一键复现：构建 HeavyDB -> 生成数据 -> Category A（延迟+功率采样）-> B -> C
#
#   ./reproduce.sh --gpu 4 --smoke            # 极小数据全链路验证（约 10 分钟，不含编译）
#   ./reproduce.sh --gpu 4 --full             # repo 规格：4 benchmark × 4 SF（数小时，磁盘约 70 GB）
#   ./reproduce.sh --gpu 4 --full --skip-build --stages "3 5"
#
# 选项：
#   --gpu N             单卡运行在 GPU N（必填）。全程只用这一块卡。
#   --smoke | --full    数据规模（默认 smoke）
#   --skip-build        跳过阶段 1（已有可用的 heavydb 构建时）
#   --skip-data         跳过阶段 2
#   --stages "1 2 3 4 5" 只跑指定阶段
#   --heavydb-home DIR  heavydb 源码/构建目录（默认 <本目录>/heavydb；可指向已有构建）
#   --deps-prefix DIR   源码依赖安装前缀（默认 /usr/local/mapd-deps）
#   --jobs N            编译线程数
#   --catc-scope S      Category C 范围：ab（默认，每个网格点重跑 A+B，对齐 repo，全量约 4–5 天）
#                       | a（只重跑 A，约 1 天）| lite（6 条代表 query，约 45 分钟）
#   --catc-limit N      Category C 每个 benchmark×SF 只跑前 N 条 query（测试用，例如 1）
#   --data-dir DIR      HeavyDB 库文件与导入临时文件放这里（默认在 heavydb 构建目录下；full 约 70 GB，
#                       根盘不够时指到大盘，例如 --data-dir /data/$USER/hb_repro）
# 环境变量：HB_SFS_TPCH="1 10" HB_SFS_H2O="1 4" HB_SFS_CB="1 10" 可缩减 full 模式的 SF；
#           HB_FORCE_RESTART=1 允许停掉别的 heavydb 实例；HB_IGNORE_BUSY=1 忽略 GPU 占用检查。
set -euo pipefail
export HB_ROOT="$(cd "$(dirname "$0")" && pwd)"
export HB_MODE=smoke DEPS_PREFIX=/usr/local/mapd-deps HEAVYDB_HOME="$HB_ROOT/heavydb"
GPU="" SKIP_BUILD=0 SKIP_DATA=0 STAGES="1 2 3 4 5" CATC_LIMIT=""
while [ $# -gt 0 ]; do case "$1" in
  --gpu) GPU="$2"; shift 2;; --smoke) HB_MODE=smoke; shift;; --full) HB_MODE=full; shift;;
  --skip-build) SKIP_BUILD=1; shift;; --skip-data) SKIP_DATA=1; shift;;
  --stages) STAGES="$2"; shift 2;; --heavydb-home) HEAVYDB_HOME="$(readlink -f "$2")"; shift 2;;
  --deps-prefix) DEPS_PREFIX="$2"; shift 2;; --jobs) export BUILD_JOBS="$2"; shift 2;;
  --catc-scope) export HB_CATC_SCOPE="$2"; shift 2;;
  --catc-limit) CATC_LIMIT="$2"; shift 2;;
  --data-dir) mkdir -p "$2"; export HB_DATA_DIR="$(readlink -f "$2")"; shift 2;;
  -h|--help) sed -n '2,25p' "$0"; exit 0;; *) echo "未知参数 $1"; exit 2;; esac; done
[ -n "$GPU" ] || { echo "必须指定 --gpu N"; exit 2; }
export HB_MODE HEAVYDB_HOME DEPS_PREFIX GPU
export HB_RESULTS_BASE="$HB_ROOT/results/$HB_MODE"     # smoke / full 各自一个结果目录
mkdir -p "$HB_RESULTS_BASE" "$HB_ROOT/logs"
log(){ echo "[$(date +%T)] $*"; }
has(){ [[ " $STAGES " == *" $1 "* ]]; }
log "模式=$HB_MODE  GPU=$GPU  heavydb=$HEAVYDB_HOME  数据=${HB_DATA_DIR:-$HEAVYDB_HOME/build/data}  阶段=$STAGES"
IMPORT_PATHS="[\"$HB_ROOT\"${HB_DATA_DIR:+,\"$HB_DATA_DIR\"}]"

# ---------- Python 环境 ----------
export PY="$HB_ROOT/venv/bin/python"
if [ ! -f "$HB_ROOT/venv/.ok" ]; then
  log "创建 venv（duckdb / pyarrow / matplotlib）"
  python3 -m venv "$HB_ROOT/venv" && "$PY" -m pip install -q --upgrade pip \
    && "$PY" -m pip install -q duckdb pyarrow matplotlib numpy && touch "$HB_ROOT/venv/.ok"
fi

# ---------- 阶段 1：构建 ----------
if has 1 && [ "$SKIP_BUILD" = 0 ]; then bash "$HB_ROOT/stages/01_build.sh"
else log "跳过构建"; fi
[ -x "$HEAVYDB_HOME/build/bin/heavydb" ] || { log "!! 找不到 $HEAVYDB_HOME/build/bin/heavydb"; exit 1; }

# ---------- GPU 占用检查（共享机器礼仪：不碰别人的卡）----------
UUID=$(nvidia-smi -i "$GPU" --query-gpu=uuid --format=csv,noheader)
OTHERS=$(nvidia-smi --query-compute-apps=pid,gpu_uuid --format=csv,noheader | awk -F', *' -v u="$UUID" '$2==u{print $1}' \
         | while read -r p; do [ "$(ps -o comm= -p "$p" 2>/dev/null)" = heavydb ] || echo "$p"; done)
if [ -n "$OTHERS" ] && [ "${HB_IGNORE_BUSY:-0}" != 1 ]; then
  log "!! GPU $GPU 上有别人的进程 (pid $OTHERS)，拒绝在忙碌的卡上测量。换一块卡或 HB_IGNORE_BUSY=1"; exit 1; fi

# ---------- 服务端：单卡，绑定到 GPU ----------
want_cwd="$HEAVYDB_HOME/build"; reuse=0
for p in $(pgrep -x heavydb); do
  cwd=$(readlink -f /proc/$p/cwd 2>/dev/null || true)
  if [ "$cwd" = "$want_cwd" ] && tr '\0' ' ' < /proc/$p/cmdline | grep -q -- "--start-gpu $GPU --num-gpus 1"; then
    reuse=1; log "复用已在 GPU $GPU 上的单卡 heavydb (pid $p)"
  elif [ "${HB_FORCE_RESTART:-0}" = 1 ]; then log "停掉别的 heavydb (pid $p)"; kill -9 "$p"; sleep 3
  else log "!! 已有另一个 heavydb 在跑 (pid $p, $cwd)。停掉它或设 HB_FORCE_RESTART=1"; exit 1; fi
done
if [ "$reuse" = 0 ]; then
  log "启动 heavydb：GPU $GPU，单卡"
  START_GPU="$GPU" NUM_GPUS=1 nohup "$HB_ROOT/lib/start-heavydb.sh" \
      --allowed-import-paths="$IMPORT_PATHS" > "$HEAVYDB_HOME/build/server.log" 2>&1 &
  for i in $(seq 40); do sleep 3; "$HB_ROOT/lib/hsql" heavyai "SELECT 1;" >/dev/null 2>&1 && break; done
  "$HB_ROOT/lib/hsql" heavyai "SELECT 1;" >/dev/null 2>&1 || { log "!! 服务端 2 分钟内没起来，见 $HEAVYDB_HOME/build/server.log"; exit 1; }
fi
"$PY" -c "import sys; sys.path.insert(0,'$HB_ROOT/lib'); import hbench as hb; assert hb.GPUS==[$GPU], hb.GPUS; print('  采样 GPU =', hb.GPUS, ' 空载 %.0f W' % hb.idle_baseline_W(2))"

# ---------- 阶段 2–5 ----------
if has 2 && [ "$SKIP_DATA" = 0 ]; then bash "$HB_ROOT/stages/02_data.sh"; else log "跳过数据生成"; fi
has 3 && bash "$HB_ROOT/stages/03_cat_a.sh"
has 4 && bash "$HB_ROOT/stages/04_cat_b.sh"
has 5 && HB_QUERY_LIMIT="$CATC_LIMIT" bash "$HB_ROOT/stages/05_cat_c.sh"

# ---------- 收尾 ----------
if sudo -n true 2>/dev/null; then sudo nvidia-smi -i "$GPU" -rgc >/dev/null 2>&1 || true
  sudo nvidia-smi -i "$GPU" -pl "$(nvidia-smi -i "$GPU" --query-gpu=power.max_limit --format=csv,noheader,nounits | cut -d. -f1)" >/dev/null 2>&1 || true; fi
log "===== 结果（$HB_RESULTS_BASE）====="
for f in "$HB_RESULTS_BASE"/*.csv; do [ -f "$f" ] && printf "  %-32s %5d 行\n" "$(basename "$f")" "$(($(wc -l < "$f")-1))"; done
[ -d "$HB_RESULTS_BASE/traces" ] && printf "  %-32s %5d 个\n" "traces/*.json" "$(ls "$HB_RESULTS_BASE"/traces/*.json 2>/dev/null | wc -l)"
log "全部完成"
