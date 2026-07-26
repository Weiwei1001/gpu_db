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
# Default = Stage S + Stage MT only (~2 h, sequential, GPU 0). The metrics
# stage (MM, ~9 h of power sampling) is opt-in via --with-metrics; the
# existing July streaming-energy data is same-protocol and stays valid.
#
# Usage:  bash benchmarks/scripts/rerun_rtx6000_cold.sh [--with-metrics] [--parallel]
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root

FIX_DIR=results/fix
mkdir -p "$FIX_DIR"
PARALLEL=0; METRICS=0
for a in "$@"; do
  [ "$a" = "--parallel" ] && PARALLEL=1
  [ "$a" = "--with-metrics" ] && METRICS=1
done

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
  if [ "$METRICS" = "1" ]; then
    python3 benchmarks/scripts/run_maximus_cpu_data.py \
        --results-dir "$2" --timing-csv "$2/maximus_cpu_data_timing.csv" $1
  fi
}

if [ "$PARALLEL" = "0" ]; then
  echo "== [MT] Maximus -s cpu timing on GPU 0 (~30 min; +metrics ~9 h if --with-metrics) =="
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
