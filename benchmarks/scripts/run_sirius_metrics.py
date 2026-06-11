#!/usr/bin/env python3
"""
Sirius (DuckDB GPU extension) steady-state metrics measurement.

Measures GPU and CPU power consumption for each query under sustained load.
Methodology:
  1. Reads per-query timing from a prior timing CSV (run_sirius_benchmark.py output)
  2. Calculates n_reps so total execution >= TARGET_TIME_S (default 5s)
  3. Each query runs in its own DuckDB process with gpu_buffer_init
  4. nvidia-smi + RAPL sampled at 50ms intervals during sustained execution
  5. Steady-state detected via GPU utilization threshold

Usage:
    python run_sirius_metrics.py [tpch] [h2o] [clickbench]
    python run_sirius_metrics.py --target-time 20 --results-dir ./results tpch

Output:
    - sirius_*_metrics_summary.csv: per-query metrics
    - sirius_*_metrics_samples.csv: raw power samples at 50ms
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from hw_detect import (
    detect_gpu, get_benchmark_config, sirius_db_path, sirius_query_dir,
    buffer_init_sql, ensure_sirius_db, MAXIMUS_DIR,
)

# ── Defaults ─────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SIRIUS_DIR = Path(os.environ.get("SIRIUS_DIR", str(MAXIMUS_DIR / "sirius")))
DEFAULT_RESULTS_DIR = MAXIMUS_DIR / "results"

import sysconfig as _sysconfig
_site = Path(_sysconfig.get_path("purelib"))
LD_EXTRA_SIRIUS = [
    str(p) for p in [
        _site / "nvidia" / "libnvcomp" / "lib64",
        _site / "libkvikio" / "lib64",
        _site / "libcudf" / "lib64",
        _site / "librmm" / "lib64",
        _site / "rapids_logger" / "lib64",
    ] if p.exists()
]
_ld = os.environ.get("LD_LIBRARY_PATH", "")
os.environ["LD_LIBRARY_PATH"] = ":".join(LD_EXTRA_SIRIUS) + (":" + _ld if _ld else "")

# Detect GPU and build dynamic config
_gpu_info = detect_gpu()
GPU_ID = str(_gpu_info["index"])
BUFFER_INIT = buffer_init_sql(_gpu_info["vram_mb"])
QUERY_TIMEOUT_S = 120
TARGET_TIME_S = 5
MIN_REPS = 3
MAX_REPS = 100       # cap reps to limit memory leak impact

# Sirius-supported benchmarks (standard + microbench)
_SIRIUS_BENCHMARKS = {
    "tpch", "h2o", "clickbench",
    "microbench_tpch", "microbench_h2o", "microbench_clickbench",
}

RE_RUN_TIME = re.compile(r"Run Time \(s\):\s*real\s+([\d.]+)", re.IGNORECASE)

# ── RAPL CPU power ───────────────────────────────────────────────────────────
RAPL_PKG_PATHS = []
RAPL_DRAM_PATHS = []
for _d in sorted(Path("/sys/class/powercap").glob("intel-rapl:*")):
    if _d.is_dir() and (_d / "energy_uj").exists():
        _nf = _d / "name"
        if _nf.exists() and _nf.read_text().strip().startswith("package"):
            RAPL_PKG_PATHS.append(_d / "energy_uj")
        for _sub in sorted(_d.glob("intel-rapl:*")):
            if _sub.is_dir() and (_sub / "name").exists():
                if (_sub / "name").read_text().strip() == "dram":
                    RAPL_DRAM_PATHS.append(_sub / "energy_uj")


def read_rapl_uj():
    pkg = sum(int(p.read_text().strip()) for p in RAPL_PKG_PATHS) if RAPL_PKG_PATHS else 0
    dram = sum(int(p.read_text().strip()) for p in RAPL_DRAM_PATHS) if RAPL_DRAM_PATHS else 0
    return pkg, dram


def load_queries(query_dir: Path):
    """Load SQL files: extract gpu_processing() calls."""
    queries = []
    for sql_file in sorted(query_dir.glob("*.sql")):
        qname = sql_file.stem
        lines = sql_file.read_text().strip().splitlines()
        gpu_lines = [l.strip() for l in lines if l.strip().startswith("call gpu_processing(")]
        if gpu_lines:
            queries.append((qname, gpu_lines))
    return queries


def load_timing_csv(csv_path: Path) -> dict[tuple[str, str, str], float]:
    """Load per-query timing from a sirius_benchmark.csv file.

    Returns {(benchmark, sf, query): wall_time_s} for OK queries.
    """
    timing = {}
    if not csv_path.exists():
        return timing
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("status") != "OK":
                continue
            try:
                t = float(row["wall_time_s"])
            except (KeyError, ValueError):
                continue
            key = (row["benchmark"], str(row["sf"]), row["query"])
            timing[key] = t
    return timing


def find_timing_csv(results_dir: Path) -> Path | None:
    """Find the most recent sirius_benchmark.csv in results_dir."""
    candidates = sorted(results_dir.glob("sirius_benchmark*.csv"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def build_metrics_sql(qname, gpu_lines, n_reps, buffer_init=BUFFER_INIT):
    """Build SQL: timer on, gpu_buffer_init, then N repetitions of the query."""
    parts = [".timer on", buffer_init]
    parts.append(f".print ===MARKER {qname}===")
    for _ in range(n_reps):
        parts.extend(gpu_lines)
    parts.append(".print ===END===")
    return "\n".join(parts) + "\n"


def parse_query_times(stdout: str) -> list[float]:
    """Parse Run Time entries from stdout, return list of times in seconds."""
    return [float(m.group(1)) for m in RE_RUN_TIME.finditer(stdout)]


def sample_gpu_metrics(stop_event, samples, interval=0.05):
    """Sample GPU + CPU metrics at 50ms intervals."""
    start = time.time()
    prev_pkg, prev_dram = read_rapl_uj()
    prev_time = start
    while not stop_event.is_set():
        try:
            r = subprocess.run(
                ["nvidia-smi", "-i", GPU_ID,
                 "--query-gpu=power.draw,utilization.gpu,memory.used,pcie.link.gen.current",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5
            )
            now = time.time()
            cur_pkg, cur_dram = read_rapl_uj()
            dt = now - prev_time
            cpu_pkg_w = (cur_pkg - prev_pkg) / 1e6 / dt if dt > 0 else 0
            cpu_dram_w = (cur_dram - prev_dram) / 1e6 / dt if dt > 0 else 0
            prev_pkg, prev_dram, prev_time = cur_pkg, cur_dram, now

            if r.returncode == 0:
                line = r.stdout.strip()
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    samples.append({
                        "time_offset_ms": int((now - start) * 1000),
                        "power_w": float(parts[0]),
                        "gpu_util_pct": float(parts[1]),
                        "mem_used_mb": float(parts[2]),
                        "cpu_pkg_power_w": round(cpu_pkg_w, 1),
                        "cpu_dram_power_w": round(cpu_dram_w, 1),
                    })
        except Exception:
            pass
        stop_event.wait(interval)


# Cache of buffer-init-only wall time per DuckDB database. DuckDB's .timer has
# only millisecond resolution, so sub-millisecond queries (all of ClickBench,
# the fastest microbench queries) report "Run Time: 0.000" -> min_s=0 ->
# energy=0. To recover a real per-query time we measure the wall clock of a
# pass and subtract this buffer-init overhead, then divide by the rep count
# (the same wall-clock approach Maximus uses). buffer_init depends only on the
# database (VRAM pool), not the query, so we measure it once per db.
_BUFFER_INIT_WALL: dict[str, float] = {}


def buffer_init_wall(duckdb_bin, db_path, buffer_init, timeout) -> float:
    """Wall-clock time of process-startup + gpu_buffer_init (no query reps), cached per db."""
    key = str(db_path)
    if key not in _BUFFER_INIT_WALL:
        # n_reps=0 -> build_metrics_sql emits buffer_init + markers, no query.
        _, elapsed, rc = run_sirius_query(
            duckdb_bin, db_path, "buffer_init", [], 0, buffer_init, timeout)
        _BUFFER_INIT_WALL[key] = elapsed if rc == 0 and elapsed > 0 else 0.0
    return _BUFFER_INIT_WALL[key]


def run_sirius_query(duckdb_bin, db_path, qname, gpu_lines, n_reps, buffer_init, timeout):
    """Run a single query N times in one DuckDB process, return (stdout, elapsed, rc)."""
    sql = build_metrics_sql(qname, gpu_lines, n_reps, buffer_init)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
        f.write(sql)
        tmp = f.name
    try:
        t0 = time.time()
        r = subprocess.run(
            [str(duckdb_bin), str(db_path)],
            stdin=open(tmp, "r"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout,
        )
        elapsed = time.time() - t0
        return r.stdout or "", elapsed, r.returncode
    except subprocess.TimeoutExpired:
        return "", time.time() - t0, -1
    except Exception as e:
        return str(e), time.time() - t0, -2
    finally:
        os.unlink(tmp)


def main():
    parser = argparse.ArgumentParser(description="Sirius GPU steady-state metrics")
    parser.add_argument("benchmarks", nargs="*", default=["tpch", "h2o", "clickbench"])
    parser.add_argument("--sf", type=str, default=None,
                        help="Specific scale factor (default: all configured SFs)")
    parser.add_argument("--sirius-dir", type=str, default=str(DEFAULT_SIRIUS_DIR))
    parser.add_argument("--results-dir", type=str, default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--target-time", type=float, default=TARGET_TIME_S)
    parser.add_argument("--buffer-init", type=str, default=BUFFER_INIT)
    parser.add_argument("--timing-csv", type=str, default=None,
                        help="Path to sirius_benchmark.csv from a prior timing run. "
                             "If not given, auto-searches results-dir.")
    parser.add_argument("--test", action="store_true",
                        help="Test mode: use reduced query lists for quick validation")
    parser.add_argument("--minimum", action="store_true",
                        help="Minimum experiment: SF_min + SF_max, no microbench")
    parser.add_argument("--toy", action="store_true",
                        help="Toy experiment: tpch only, 1 query, largest SF (smallest A+B+C smoke)")
    args = parser.parse_args()

    sirius_dir = Path(args.sirius_dir)
    duckdb_bin = sirius_dir / "build" / "release" / "duckdb"
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    if not duckdb_bin.exists():
        print(f"ERROR: Sirius DuckDB binary not found: {duckdb_bin}")
        import sys; sys.exit(1)

    target_time_s = args.target_time
    buffer_init = args.buffer_init

    # Load prior timing results
    if args.timing_csv:
        timing_csv_path = Path(args.timing_csv)
    else:
        timing_csv_path = find_timing_csv(results_dir)
    prior_timing = load_timing_csv(timing_csv_path) if timing_csv_path else {}
    if prior_timing:
        print(f"  Loaded {len(prior_timing)} timing entries from {timing_csv_path}")
    else:
        print(f"  WARNING: No prior timing CSV found — will run calibration as fallback")

    # Build dynamic benchmark config from hw_detect
    bench_config = get_benchmark_config(_gpu_info["vram_mb"],
                                        test_mode=args.test,
                                        minimum_mode=args.minimum,
                                        toy_mode=args.toy)
    BENCHMARKS = {k: v for k, v in bench_config.items() if k in _SIRIUS_BENCHMARKS}

    print("=" * 70)
    print("  SIRIUS GPU STEADY-STATE METRICS")
    print(f"  GPU: {_gpu_info['name']} (index {_gpu_info['index']}, "
          f"{_gpu_info['vram_mb']} MiB)")
    print(f"  Target: {target_time_s}s sustained execution per query")
    print(f"  Started: {datetime.now()}")
    print("=" * 70)

    for bench_name in args.benchmarks:
        if bench_name not in BENCHMARKS:
            print(f"Unknown benchmark: {bench_name}, skipping")
            continue
        cfg = BENCHMARKS[bench_name]
        queries = load_queries(sirius_query_dir(bench_name))
        # In any reduced mode (test/minimum/toy), filter to configured queries.
        if args.test or args.minimum or args.toy:
            allowed = set(cfg["queries"])
            queries = [(qn, gl) for qn, gl in queries if qn in allowed]

        sfs = cfg["scale_factors"]
        if args.sf is not None:
            sfs = [s for s in sfs if str(s) == str(args.sf)]
            if not sfs:
                print(f"SF={args.sf} not configured for {bench_name}")
                continue
        for sf in sfs:
            db_path = sirius_db_path(bench_name, sf)
            if not db_path.exists():
                if not ensure_sirius_db(bench_name, sf):
                    print(f"[SKIP] {bench_name} SF={sf}: {db_path} not found")
                    continue

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            tag = f"{bench_name}_sf{sf}"

            print(f"\n{'=' * 70}")
            print(f"  METRICS: {bench_name.upper()} SF={sf} ({len(queries)} queries)")
            print(f"  DB: {db_path}")
            print(f"{'=' * 70}")

            # Calculate n_reps from prior timing CSV (or fallback calibration)
            print(f"\n--- Calculate n_reps (target={target_time_s}s) ---")
            calibration = {}
            for qname, gpu_lines in queries:
                key = (bench_name, str(sf), qname)
                prior_t = prior_timing.get(key)
                if prior_t is not None and prior_t > 0:
                    n_reps = min(MAX_REPS, max(MIN_REPS, math.ceil(target_time_s / prior_t)))
                    calibration[qname] = {
                        "query_time_s": prior_t,
                        "n_reps": n_reps,
                        "status": "OK",
                    }
                    est_s = n_reps * prior_t
                    print(f"  {qname}: {prior_t:.4f}s x {n_reps} reps = {est_s:.1f}s (from timing CSV)")
                else:
                    # Fallback: run a single calibration rep
                    print(f"  {qname}: no prior timing, calibrating...", end=" ", flush=True)
                    stdout, elapsed, rc = run_sirius_query(
                        duckdb_bin, db_path, qname, gpu_lines, 3,
                        buffer_init, QUERY_TIMEOUT_S)
                    times = parse_query_times(stdout)
                    has_fallback = "fallback" in stdout.lower()

                    if rc == -1:
                        calibration[qname] = {"query_time_s": -1, "n_reps": 0, "status": "TIMEOUT"}
                        print("TIMEOUT")
                    elif not times or has_fallback:
                        calibration[qname] = {"query_time_s": -1, "n_reps": 0,
                                              "status": "FALLBACK" if has_fallback else "FAIL"}
                        print(f"{'FALLBACK' if has_fallback else 'FAIL'}")
                    else:
                        query_times = times[1:]  # exclude buffer_init
                        if len(query_times) >= 2:
                            per_query = min(query_times[1:]) if query_times[1:] else query_times[0]
                        else:
                            per_query = query_times[0] if query_times else sum(times)
                        n_reps = min(MAX_REPS, max(MIN_REPS, math.ceil(target_time_s / per_query))) if per_query > 0 else MIN_REPS
                        calibration[qname] = {
                            "query_time_s": per_query,
                            "n_reps": n_reps,
                            "status": "OK",
                        }
                        est_s = n_reps * per_query
                        print(f"{per_query:.4f}s x {n_reps} reps = {est_s:.1f}s (calibrated)")

            # Metrics run with nvidia-smi + RAPL sampling
            print(f"\n--- Metrics run with power sampling ---")
            all_samples = []
            summaries = []

            for qname, gpu_lines in queries:
                cal = calibration[qname]
                if cal["n_reps"] == 0:
                    print(f"  {qname}: SKIP ({cal['status']})")
                    summaries.append({
                        "run_id": f"{tag}_{qname}", "benchmark": bench_name,
                        "sf": sf, "query": qname, "n_reps": 0,
                        "min_s": "", "avg_s": "", "elapsed_s": "",
                        "num_samples": 0, "num_steady_samples": 0,
                        "avg_power_w": "", "max_power_w": "", "max_mem_mb": "",
                        "avg_gpu_util": "", "max_gpu_util": "", "gpu_energy_j": "",
                        "avg_cpu_pkg_w": "", "avg_cpu_dram_w": "", "cpu_energy_j": "",
                        "status": cal["status"],
                    })
                    continue

                n_reps = cal["n_reps"]
                print(f"  {qname} ({n_reps} reps/pass, target={target_time_s}s)...",
                      end=" ", flush=True)

                # Start sampling
                samples = []
                stop_event = threading.Event()
                sampler = threading.Thread(target=sample_gpu_metrics,
                                           args=(stop_event, samples, 0.05))
                sampler.start()

                # Multi-pass: run DuckDB processes in a loop until target
                # wall time is reached. Each pass does buffer_init + n_reps
                # queries. This ensures we sample long enough for stable
                # nvidia-smi readings even when per-query time is very short.
                all_times = []
                total_elapsed = 0.0
                total_query_wall = 0.0   # wall clock of query reps only (excl. buffer_init)
                n_passes = 0
                status = "OK"
                per_pass_timeout = max(QUERY_TIMEOUT_S, target_time_s * 3)

                # One-time buffer-init wall measurement (cached per db) so we can
                # derive a wall-clock per-query time for sub-ms queries.
                t_init_wall = buffer_init_wall(
                    duckdb_bin, db_path, buffer_init, per_pass_timeout)

                while total_elapsed < target_time_s:
                    stdout, pass_elapsed, rc = run_sirius_query(
                        duckdb_bin, db_path, qname, gpu_lines, n_reps,
                        buffer_init, per_pass_timeout)
                    pass_times = parse_query_times(stdout)
                    has_fallback = "fallback" in stdout.lower()

                    if rc == -1:
                        status = "TIMEOUT"
                        break
                    elif has_fallback:
                        status = "FALLBACK"
                        break
                    elif not pass_times:
                        status = "FAIL"
                        break

                    # Keep cached query times (skip buffer_init + first query)
                    if len(pass_times) > 2:
                        all_times.extend(pass_times[2:])
                    elif pass_times:
                        all_times.extend(pass_times)

                    total_elapsed += pass_elapsed
                    # Query-only wall = pass wall minus the per-process buffer_init overhead.
                    total_query_wall += max(0.0, pass_elapsed - t_init_wall)
                    n_passes += 1

                stop_event.set()
                sampler.join(timeout=5)

                times = all_times
                elapsed = total_elapsed
                total_n_reps = n_reps * n_passes

                # Tag samples
                run_id = f"{tag}_{qname}"
                for s in samples:
                    s["run_id"] = run_id
                    s["sf"] = sf
                    s["query"] = qname
                all_samples.extend(samples)

                # Memory leak detection (post-peak slope).
                # The pool init / first table-load is bursty and asynchronous —
                # on Sirius it can land anywhere from the first to the last
                # quarter of the sampling window depending on duckdb startup
                # latency. So instead of guessing where init ends, we compare
                # the running peak (last 25% of samples once peak is reached)
                # to the absolute end-of-run value. A real per-rep leak would
                # show end > peak; a one-shot init would show end == peak.
                if len(samples) >= 16:
                    mems = [s["mem_used_mb"] for s in samples]
                    peak_idx = max(range(len(mems)), key=lambda i: mems[i])
                    # Need at least a few samples after peak to judge slope.
                    tail_n = len(mems) - peak_idx - 1
                    if tail_n >= max(4, len(mems) // 10):
                        # Average over post-peak window (skip the peak sample
                        # itself to avoid single-sample spikes).
                        post_peak = mems[peak_idx + 1:]
                        mem_peak = mems[peak_idx]
                        mem_end = sum(post_peak[-max(4, len(post_peak) // 4):]) / \
                                  max(4, len(post_peak) // 4)
                        mem_growth_mb = mem_end - mem_peak
                        if mem_growth_mb > 200 and mem_growth_mb > mem_peak * 0.02:
                            print(f"\n  ⚠ MEMORY LEAK DETECTED for {qname} (post-peak): "
                                  f"+{mem_growth_mb:.0f}MB ({mem_peak:.0f}→{mem_end:.0f}MB)")

                # Compute steady-state metrics
                if samples:
                    all_util = [s["gpu_util_pct"] for s in samples]
                    avg_util_all = sum(all_util) / len(all_util)
                    start_idx = 0
                    for i, s in enumerate(samples):
                        if s["gpu_util_pct"] >= avg_util_all:
                            start_idx = i
                            break
                    end_idx = len(samples) - 1
                    for i in range(len(samples) - 1, -1, -1):
                        if samples[i]["gpu_util_pct"] >= avg_util_all:
                            end_idx = i
                            break
                    steady = samples[start_idx:end_idx + 1] if end_idx >= start_idx else samples
                else:
                    steady = []

                if steady:
                    avg_power = sum(s["power_w"] for s in steady) / len(steady)
                    max_power = max(s["power_w"] for s in steady)
                    max_mem = max(s["mem_used_mb"] for s in steady)
                    avg_util = sum(s["gpu_util_pct"] for s in steady) / len(steady)
                    max_util = max(s["gpu_util_pct"] for s in steady)
                    avg_cpu_pkg_w = sum(s.get("cpu_pkg_power_w", 0) for s in steady) / len(steady)
                    avg_cpu_dram_w = sum(s.get("cpu_dram_power_w", 0) for s in steady) / len(steady)
                else:
                    avg_power = max_power = max_mem = avg_util = max_util = 0
                    avg_cpu_pkg_w = avg_cpu_dram_w = 0

                min_s = min(times) if times else 0
                avg_s = sum(times) / len(times) if times else 0
                # Wall-clock per-query time (sub-ms resolution), used when the
                # DuckDB .timer (1ms resolution) rounds the query to 0.000 — this
                # is what made all of ClickBench and the fastest microbench
                # queries report 0 time / 0 energy on both A100 and H100.
                total_reps = n_reps * n_passes
                wall_per_query_s = (total_query_wall / total_reps) if total_reps > 0 else 0.0
                if min_s <= 0 and wall_per_query_s > 0:
                    min_s = wall_per_query_s
                    avg_s = wall_per_query_s
                    timing_source = "wallclock"
                else:
                    timing_source = "duckdb_timer"
                gpu_energy_j = avg_power * min_s if min_s > 0 else 0
                cpu_energy_j = avg_cpu_pkg_w * min_s if min_s > 0 else 0

                summaries.append({
                    "run_id": run_id, "benchmark": bench_name,
                    "sf": sf, "query": qname, "n_reps": total_n_reps,
                    "min_s": f"{min_s:.6f}", "avg_s": f"{avg_s:.6f}",
                    "timing_source": timing_source,
                    "elapsed_s": f"{elapsed:.2f}",
                    "num_samples": len(samples),
                    "num_steady_samples": len(steady),
                    "avg_power_w": f"{avg_power:.1f}",
                    "max_power_w": f"{max_power:.1f}",
                    "max_mem_mb": f"{max_mem:.0f}",
                    "avg_gpu_util": f"{avg_util:.1f}",
                    "max_gpu_util": f"{max_util:.0f}",
                    "gpu_energy_j": f"{gpu_energy_j:.2f}",
                    "avg_cpu_pkg_w": f"{avg_cpu_pkg_w:.1f}",
                    "avg_cpu_dram_w": f"{avg_cpu_dram_w:.1f}",
                    "cpu_energy_j": f"{cpu_energy_j:.2f}",
                    "status": status,
                })

                print(f"{min_s:.3f}s, {elapsed:.1f}s ({n_passes} passes), "
                      f"GPU:{avg_power:.0f}W CPU:{avg_cpu_pkg_w:.0f}W, "
                      f"{max_util:.0f}%util, {max_mem:.0f}MB, GPU_E:{gpu_energy_j:.1f}J "
                      f"CPU_E:{cpu_energy_j:.1f}J [{status}]")

            # Save results
            samples_file = results_dir / f"sirius_{tag}_metrics_samples_{ts}.csv"
            with open(samples_file, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=[
                    "run_id", "sf", "query", "time_offset_ms",
                    "power_w", "gpu_util_pct", "mem_used_mb",
                    "cpu_pkg_power_w", "cpu_dram_power_w",
                ])
                w.writeheader()
                w.writerows(all_samples)

            summary_file = results_dir / f"sirius_{tag}_metrics_summary_{ts}.csv"
            with open(summary_file, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=[
                    "run_id", "benchmark", "sf", "query", "n_reps",
                    "min_s", "avg_s", "timing_source", "elapsed_s",
                    "num_samples", "num_steady_samples",
                    "avg_power_w", "max_power_w", "max_mem_mb",
                    "avg_gpu_util", "max_gpu_util", "gpu_energy_j",
                    "avg_cpu_pkg_w", "avg_cpu_dram_w", "cpu_energy_j", "status",
                ], extrasaction="ignore")
                w.writeheader()
                w.writerows(summaries)

            print(f"\n  Samples: {samples_file} ({len(all_samples)} samples)")
            print(f"  Summary: {summary_file} ({len(summaries)} queries)")

            ok_count = sum(1 for s in summaries if s["status"] == "OK")
            print(f"  --- {ok_count}/{len(queries)} OK")

    print(f"\n{'=' * 70}")
    print(f"  ALL DONE — {datetime.now()}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
