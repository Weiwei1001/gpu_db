#!/usr/bin/env bash
# RTX 6000: clean re-measurement of everything the contended July-18 session
# tainted — Sirius cold (load-inclusive first run) for ALL benchmarks, and the
# Maximus data-on-CPU (-s cpu) campaign. All output goes to results/fix/.
#
# Stage S  Sirius cold, tpch + clickbench + h2o (fresh session per query,
#          load inside the timed window). MUST run with the box otherwise
#          idle: the load path is host-side, and co-located jobs inflate it
#          ~3x (that is exactly what invalidated the July-18 numbers).
# Stage MT Maximus -s cpu timing (3 reps per query, direct latency).
# Stage MM Maximus -s cpu metrics (steady-state power/energy sampling).
#
# Default is fully sequential on GPU 0 (safest; ~10-11 h total).
# --parallel fans the Maximus stages out per-benchmark across GPUs 1-3
# AFTER the Sirius stage finishes (~6-7 h wall). The Maximus latencies are
# host-path sensitive too, so prefer sequential when the numbers matter.
#
# Usage:  bash benchmarks/scripts/rerun_rtx6000_cold.sh [--parallel]
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root

FIX_DIR=results/fix
mkdir -p "$FIX_DIR"
PARALLEL=0
[ "${1:-}" = "--parallel" ] && PARALLEL=1

GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader -i 0 | head -1)
echo "== GPU: $GPU_NAME =="
case "$GPU_NAME" in
  *RTX*6000*) ;;
  *) echo "ABORT: this script is for the RTX 6000 box"; exit 1 ;;
esac

N_PROC=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | grep -c . || true)
if [ "$N_PROC" -gt 0 ]; then
  echo "ABORT: $N_PROC compute process(es) already on the GPUs — cold loads need an idle host."
  nvidia-smi --query-compute-apps=pid,process_name --format=csv
  exit 1
fi

echo "== reset clocks/power to defaults =="
(nvidia-smi -rgc && nvidia-smi -rmc) 2>/dev/null || \
  sudo -n nvidia-smi -rgc 2>/dev/null || \
  echo "  [WARN] could not reset clocks; verify 'nvidia-smi -q -d CLOCK' manually"

echo "== [S] Sirius cold: tpch clickbench h2o -> $FIX_DIR  (~1.5 h, box must stay idle) =="
python3 benchmarks/scripts/run_sirius_cpu_data.py tpch clickbench h2o \
    --results-dir "$FIX_DIR"

run_maximus () {  # $1 = benchmark list, $2 = results dir
  python3 benchmarks/scripts/run_maximus_cpu_data.py --timing-only \
      --results-dir "$2" $1
  python3 benchmarks/scripts/run_maximus_cpu_data.py \
      --results-dir "$2" --timing-csv "$2/maximus_cpu_data_timing.csv" $1
}

if [ "$PARALLEL" = "0" ]; then
  echo "== [MT+MM] Maximus -s cpu, sequential on GPU 0 (~9 h) =="
  run_maximus "tpch h2o clickbench case_bench" "$FIX_DIR"
else
  echo "== [MT+MM] Maximus -s cpu, per-benchmark on GPUs 1-3 (~5 h wall) =="
  echo "   [WARN] concurrent host loads — latencies are mildly contended; rerun"
  echo "          sequentially if a number looks off."
  pids=()
  gpu=1
  for b in "clickbench" "tpch h2o" "case_bench"; do
    d="$FIX_DIR/gpu$gpu"; mkdir -p "$d"
    CUDA_VISIBLE_DEVICES=$gpu GPU_ID=$gpu run_maximus "$b" "$d" \
        > "$FIX_DIR/maximus_gpu$gpu.log" 2>&1 &
    pids+=($!)
    gpu=$((gpu+1))
  done
  fail=0
  for p in "${pids[@]}"; do wait "$p" || fail=1; done
  [ "$fail" = "0" ] || { echo "ABORT: a parallel Maximus job failed — check $FIX_DIR/maximus_gpu*.log"; exit 1; }
fi

echo "== done — new measurements in $FIX_DIR =="
ls -la "$FIX_DIR"
