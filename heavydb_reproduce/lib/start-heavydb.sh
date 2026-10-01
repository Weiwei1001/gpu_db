#!/bin/bash
# 启动 HeavyDB（单卡：START_GPU 指定卡号，NUM_GPUS 固定 1）
HB_ROOT="${HB_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
HEAVYDB_HOME="${HEAVYDB_HOME:-$HB_ROOT/heavydb}"
DEPS_PREFIX="${DEPS_PREFIX:-/usr/local/mapd-deps}"
export LD_LIBRARY_PATH="$DEPS_PREFIX/lib:/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}"
export PATH=/usr/local/cuda/bin:$PATH
cd "$HEAVYDB_HOME/build"
DATA="${HB_DATA_DIR:+$HB_DATA_DIR/heavydb}"; DATA="${DATA:-data}"     # --data-dir 时库放大盘
[ -d "$DATA/catalogs" ] || { mkdir -p "$DATA" && ./bin/initheavy "$DATA"; }
exec ./bin/heavydb "$DATA" --start-gpu "${START_GPU:-0}" --num-gpus "${NUM_GPUS:-1}" "$@"
