#!/bin/bash
# One command for every DuckDB-CPU experiment of the paper, on the same data as the GPU engines:
#   main  : TPC-H SF 1/5/10/20, H2O 1/2/4/8 GB, ClickBench SF 1/5/10/20  (timing + RAPL energy)
#   micro : the 120 microbench queries (tpch, clickbench, h2o)
#   case  : case_bench on the TPC-H databases
#
#   cpu_reproduce/run_all.sh                       # everything
#   PARTS="main" TPCH_SF="1 10" H2O_SF="" CB_SF="" cpu_reproduce/run_all.sh   # subset ("" skips a benchmark)
#   SKIP_DATA=1 cpu_reproduce/run_all.sh           # databases already prepared
#
# Data: the canonical DuckDB databases under gpu_db/tests/{tpch,h2o,click}_duckdb (see prepare_data.py), i.e. the
# same files and row counts that Sirius, Maximus and HeavyDB use. Missing ones are built with benchmarks/data/*.
# Results: gpu_db/results/cpu/<host>_<YYYYmmdd_HHMM>/{main,micro,case,machine_info.txt,data_sources.csv,summary.txt}
# Needs: x86 CPU with readable RAPL counters, python3 with venv, curl + unzip (only if DuckDB CLI must be fetched).
# Do not run it while a GPU benchmark (or anything else heavy) runs on the same machine.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
DUCKDB_VERSION="${DUCKDB_VERSION:-1.4.1}"          # the version of the dbresearch1 / dbresearch3 runs
PARTS="${PARTS-main micro case}"
TPCH_SF="${TPCH_SF-1 5 10 20}"; H2O_SF="${H2O_SF-1 2 4 8}"; CB_SF="${CB_SF-1 5 10 20}"
RUN="${RUN_DIR:-$REPO/results/cpu/$(hostname -s)_$(date +%Y%m%d_%H%M)}"
log(){ echo "[$(date +%H:%M:%S)] $*"; }
has(){ [[ " $PARTS " == *" $1 "* ]]; }

# ---------- preflight ----------
RAPL=/sys/class/powercap/intel-rapl:0/energy_uj
if ! cat "$RAPL" >/dev/null 2>&1; then         # readable by root only after every boot
  log "RAPL energy counters are root-only; making them readable (sudo)"
  sudo chmod o+r /sys/class/powercap/intel-rapl:*/energy_uj /sys/class/powercap/intel-rapl:*:*/energy_uj 2>/dev/null || true
  cat "$RAPL" >/dev/null 2>&1 || { log "!! RAPL counters still not readable (no Intel RAPL on this CPU, or sudo refused)"; exit 1; }
fi
busy=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | wc -l || true)
[ "${busy:-0}" -eq 0 ] || log "note: $busy GPU process(es) running on this machine; their host CPU load will add to the RAPL readings"
mkdir -p "$RUN"
log "results -> $RUN"

# ---------- Python (data preparation only) and DuckDB CLI ----------
VENV="$HERE/venv"
if [ ! -f "$VENV/.ok" ]; then
  log "creating $VENV (duckdb==$DUCKDB_VERSION)"
  if ! python3 -m venv "$VENV" 2>/dev/null; then          # a fresh Ubuntu has no python3-venv
    log "python3 -m venv failed; installing python3-venv"; rm -rf "$VENV"
    sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv >/dev/null
    python3 -m venv "$VENV"
  fi
  "$VENV/bin/pip" install -q --upgrade pip && "$VENV/bin/pip" install -q "duckdb==$DUCKDB_VERSION" && touch "$VENV/.ok"
fi
DUCKDB="$(command -v duckdb || true)"
if [ -z "$DUCKDB" ] || ! "$DUCKDB" --version | grep -q "v$DUCKDB_VERSION"; then
  DUCKDB="$HERE/bin/duckdb"
  if [ ! -x "$DUCKDB" ]; then
    arch=$(uname -m); case "$arch" in x86_64) arch=amd64;; aarch64) arch=arm64;; esac
    log "fetching DuckDB CLI v$DUCKDB_VERSION ($arch) into $HERE/bin"
    mkdir -p "$HERE/bin"
    curl -sSL -o "$HERE/bin/duckdb.zip" \
      "https://github.com/duckdb/duckdb/releases/download/v$DUCKDB_VERSION/duckdb_cli-linux-$arch.zip"
    unzip -oq "$HERE/bin/duckdb.zip" -d "$HERE/bin" && rm "$HERE/bin/duckdb.zip"
  fi
fi
log "DuckDB CLI: $DUCKDB ($("$DUCKDB" --version))"

# ---------- data: canonical databases in gpu_db/tests ----------
if [ "${SKIP_DATA:-0}" != 1 ]; then
  "$VENV/bin/python" "$HERE/prepare_data.py" --results "$RUN" --tpch $TPCH_SF --h2o $H2O_SF --clickbench $CB_SF
fi
# the runners look for <bench-dir>/{tpch,H2O,clickbench}/<pattern>; point them at the shared files
BENCH="$RUN/bench_links"; mkdir -p "$BENCH/tpch" "$BENCH/H2O" "$BENCH/clickbench"
for n in $TPCH_SF; do ln -sf "$REPO/tests/tpch_duckdb/tpch_sf$n.duckdb" "$BENCH/tpch/tpch_sf$n.duckdb"; done
for n in $H2O_SF;  do ln -sf "$REPO/tests/h2o_duckdb/h2o_${n}gb.duckdb" "$BENCH/H2O/h2o_${n}gb.duckdb"; done
for n in $CB_SF;   do ln -sf "$REPO/tests/click_duckdb/clickbench_$n.duckdb" "$BENCH/clickbench/clickbench_$n.duckdb"; done
for l in "$BENCH"/*/*.duckdb; do [ -e "$l" ] || { log "!! missing database $(readlink "$l")"; exit 1; }; done

# ---------- machine description ----------
{ echo "host: $(hostname)"; date; uptime; echo "logged-in users: $(who | wc -l)"
  echo "repo commit: $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null)"
  echo "duckdb: $("$DUCKDB" --version)"
  lscpu | grep -E "Model name|^CPU\(s\)|Thread|Core|Socket|MHz|L3"
  echo "governor: $(sort -u /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | paste -sd,)"
  echo "epp: $(sort -u /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference 2>/dev/null | paste -sd,)"
  free -g
  echo "top processes at start:"; ps -eo pcpu,pmem,user,comm --sort=-pcpu | head -8
} > "$RUN/machine_info.txt"

# ---------- experiments (one at a time) ----------
PY=python3
COMMON=(--bench-dir "$BENCH" --duckdb "$DUCKDB" --no-plot)
if has main; then
  [ -n "$TPCH_SF" ] && { log "main: tpch";       $PY "$HERE/run_cpu_benchmark.py" tpch       --sf $TPCH_SF --results-dir "$RUN/main" "${COMMON[@]}"; }
  [ -n "$H2O_SF" ]  && { log "main: h2o_gb";     $PY "$HERE/run_cpu_benchmark.py" h2o_gb     --sf $H2O_SF  --results-dir "$RUN/main" "${COMMON[@]}"; }
  [ -n "$CB_SF" ]   && { log "main: clickbench"; $PY "$HERE/run_cpu_benchmark.py" clickbench --sf $CB_SF   --results-dir "$RUN/main" "${COMMON[@]}"; }
fi
if has micro; then
  [ -n "$TPCH_SF" ] && { log "micro: tpch";       $PY "$HERE/run_cpu_microbench.py" microbench_tpch       --sf $TPCH_SF --results-dir "$RUN/micro" "${COMMON[@]}"; }
  [ -n "$CB_SF" ]   && { log "micro: clickbench"; $PY "$HERE/run_cpu_microbench.py" microbench_clickbench --sf $CB_SF   --results-dir "$RUN/micro" "${COMMON[@]}"; }
  [ -n "$H2O_SF" ]  && { log "micro: h2o";        $PY "$HERE/run_cpu_microbench.py" microbench_h2o        --sf $H2O_SF  --results-dir "$RUN/micro" "${COMMON[@]}"; }
fi
if has case && [ -n "$TPCH_SF" ]; then
  log "case_bench"; $PY "$HERE/run_cpu_microbench.py" case_bench --sf $TPCH_SF --results-dir "$RUN/case" "${COMMON[@]}"
fi
{ echo "finished: $(date)"; uptime; ps -eo pcpu,pmem,user,comm --sort=-pcpu | head -8; } >> "$RUN/machine_info.txt"

# ---------- summary: both latency definitions ----------
"$VENV/bin/python" "$HERE/summarize.py" "$RUN" | tee "$RUN/summary.txt"
log "ALL DONE -> $RUN"
