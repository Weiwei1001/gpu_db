#!/bin/bash
# 一键全量：自动选一张空闲 GPU → 检查 sudo/磁盘 → 自动发现现成数据（没有就生成/下载）→ 构建（如需）→ Cat A/B/C
#
#   ./run_full.sh                                  # 全部默认：A + B + Cat C lite（6 条代表 query），单卡 H100 约 5.5 h
#   ./run_full.sh --catc-scope ab                  # Cat C 按 repo 规格（每个网格点重跑 A+B，约 4–5 天）
#   ./run_full.sh --data-roots /path/gpu_db:/path/other --data-dir /bigdisk/hb_repro
#
#   One command from an existing gpu_db checkout (reuses its tests/ data):
#     git pull && heavydb_reproduce/run_full.sh
#   One command from a fresh machine:
#     git clone https://github.com/Weiwei1001/gpu_db.git && gpu_db/heavydb_reproduce/run_full.sh
#   ./run_full.sh --dry-run                        # 只打印选中的 GPU、磁盘、找到的数据，不构建不运行
#
# 选项：
#   --gpu N            指定卡；不给则自动选：只有一张卡就用它，多张卡取第一张没有任何进程的
#   --data-roots A:B   现成数据的搜索根（别人跑 gpu_db 留下的 tests/ 目录、csv-*/、*.duckdb、hits.parquet）
#                      默认自动查：--data-dir、~/gpu_db、本包同级的 gpu_db
#   --data-dir DIR     HeavyDB 库与临时文件目录（全量约 70 GB；默认本包下 data/，空间不够会提示）
#   --catc-scope S     Cat C 范围：lite（默认）| a | ab；见 reproduce.sh
#   --dry-run          只做 GPU/磁盘/数据发现，然后退出
#   其余参数原样传给 reproduce.sh（--skip-build、--heavydb-home、--catc-limit、--stages ...）
set -euo pipefail
HB_ROOT="$(cd "$(dirname "$0")" && pwd)"
log(){ echo "[$(date +%T)] [full] $*"; }
GPU="" DATA_DIR="$HB_ROOT/data" ROOTS="" CATC_SCOPE="lite" DRY=0 PASS=()
while [ $# -gt 0 ]; do case "$1" in
  --gpu) GPU="$2"; shift 2;; --data-roots) ROOTS="$2"; shift 2;; --data-dir) DATA_DIR="$2"; shift 2;;
  --catc-scope) CATC_SCOPE="$2"; shift 2;; --dry-run) DRY=1; shift;;
  -h|--help) sed -n '2,21p' "$0"; exit 0;; *) PASS+=("$1"); shift;; esac; done

# ---------- GPU ----------
command -v nvidia-smi >/dev/null || { log "!! 没有 nvidia-smi"; exit 1; }
NGPU=$(nvidia-smi --query-gpu=index --format=csv,noheader | wc -l)
if [ -z "$GPU" ]; then
  if [ "$NGPU" -eq 1 ]; then GPU=0
  else
    BUSY=$(nvidia-smi --query-compute-apps=gpu_uuid --format=csv,noheader | sort -u)
    for i in $(nvidia-smi --query-gpu=index --format=csv,noheader); do
      u=$(nvidia-smi -i "$i" --query-gpu=uuid --format=csv,noheader)
      grep -q "$u" <<< "$BUSY" || { GPU=$i; break; }
    done
    [ -n "$GPU" ] || { log "!! $NGPU 张卡都有进程在跑，用 --gpu N 指定（或等空闲）"; exit 1; }
  fi
fi
log "GPU $GPU / 共 $NGPU 张：$(nvidia-smi -i "$GPU" --query-gpu=name,memory.total,power.max_limit --format=csv,noheader)"
sudo -n nvidia-smi -L >/dev/null 2>&1 || log "!! 没有 passwordless sudo：Cat C（改功率/时钟）会被跳过，A/B 照跑"

# ---------- 磁盘 ----------
mkdir -p "$DATA_DIR"
FREE_GB=$(df -BG --output=avail "$DATA_DIR" | tail -1 | tr -dc 0-9)
[ "$FREE_GB" -ge 80 ] || log "!! $DATA_DIR 只剩 ${FREE_GB} GB，全量需要约 70 GB（数据）+ 临时文件；不够请 --data-dir 指到大盘"

# ---------- 现成数据 ----------
REPO_ROOT="$(dirname "$HB_ROOT")"   # this package lives inside the gpu_db checkout: its tests/ data is right here
export HB_DATA_ROOTS="${ROOTS:+$ROOTS:}$DATA_DIR:$REPO_ROOT:$HOME/gpu_db:$REPO_ROOT/gpu_db"
log "搜索现成数据：$(tr ':' ' ' <<< "$HB_DATA_ROOTS")"
found=0
for r in $(tr ':' ' ' <<< "$HB_DATA_ROOTS"); do
  for pat in tests/tpch/csv-* tpch/csv-* tests/h2o/csv-* h2o/csv-* tests/clickbench/csv-* clickbench/csv-* \
             tests/*_duckdb/*.duckdb sirius_db/*.duckdb tests/clickbench/hits.parquet clickbench/hits.parquet; do
    for p in "$r"/$pat; do [ -e "$p" ] && { echo "    $p"; found=$((found+1)); }; done
  done
done
[ "$found" -gt 0 ] && log "找到 $found 项，能对上的数据集直接导入；其余生成/下载" || log "没找到现成数据，全部生成/下载（ClickBench 需要访问 datasets.clickhouse.com）"

# ---------- 跑 ----------
log "Cat C 范围：$CATC_SCOPE（lite 约 25 min；ab 约 4–5 天，用 --catc-scope ab 选择）"
[ "$DRY" = 1 ] && { log "--dry-run：到此为止，没有构建或运行"; exit 0; }
exec "$HB_ROOT/reproduce.sh" --gpu "$GPU" --full --data-dir "$DATA_DIR" --catc-scope "$CATC_SCOPE" "${PASS[@]}"
