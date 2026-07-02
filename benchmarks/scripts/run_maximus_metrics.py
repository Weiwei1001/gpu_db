#!/usr/bin/env python3
"""
Maximus GPU steady-state metrics measurement.

Measures GPU power consumption and energy for each query under sustained load.
The methodology:
  1. Data loaded to GPU (-s gpu) for realistic query execution
  2. Calibration: 3 reps per query to measure base latency
  3. n_reps calculated so that n_reps * query_latency >= TARGET_TIME_S (default 10s)
  4. nvidia-smi sampled at 50ms intervals during sustained execution
  5. Steady-state detected via GPU utilization threshold:
     - Compute avg_util across all samples
     - t_start = first sample where gpu_util >= avg_util
     - t_end   = last sample where gpu_util >= avg_util
     - P_steady = mean(power[t_start : t_end])

This isolates the compute phase regardless of idle/loading duration.

Usage:
    python run_maximus_metrics.py [--sf 10] [--target-time 10] [--results-dir ./results]

Output:
    - *_metrics_summary.csv: per-query metrics (power, energy, timing, GPU util)
    - *_metrics_samples.csv: raw nvidia-smi samples at 50ms intervals
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from hw_detect import detect_gpu, get_benchmark_config, maximus_data_dir, ensure_maximus_csv, MAXIMUS_DIR

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
MAXBENCH = MAXIMUS_DIR / "build" / "benchmarks" / "maxbench"

import sysconfig as _sysconfig
_site = Path(_sysconfig.get_path("purelib"))
LD_EXTRA = [
    str(p) for p in [
        _site / "nvidia" / "libnvcomp" / "lib64",
        _site / "libkvikio" / "lib64",
        _site / "libcudf" / "lib64",
        _site / "librmm" / "lib64",
    ] if p.exists()
]

# ── Benchmark configurations (loaded dynamically from hw_detect) ──────────
gpu_info = detect_gpu()
BENCHMARKS = get_benchmark_config(gpu_info["vram_mb"])

TARGET_TIME_S = 5    # target sustained execution time per query
MIN_REPS = 3         # minimum repetitions even for slow queries
MAX_REPS = 100       # cap reps to limit memory leak impact
CALIBRATION_REPS = 3
TIMEOUT = 300


def load_timing_from_csv(csv_path, benchmark, sf):
    """Load per-query min_ms from A1 timing CSV, keyed by query name."""
    result = {}
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row["benchmark"] == benchmark
                    and str(row["sf"]) == str(sf)
                    and row["status"] == "OK"
                    and row["min_ms"]):
                result[row["query"]] = float(row["min_ms"])
    return result


def get_env():
    env = os.environ.copy()
    ld = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = ":".join(LD_EXTRA) + (":" + ld if ld else "")
    return env


def run_maxbench(benchmark, query, n_reps, data_path, storage="gpu", timeout=TIMEOUT):
    """Run a single query with maxbench."""
    cmd = [
        str(MAXBENCH),
        "--benchmark", benchmark,
        "-q", query,
        "-d", "gpu",
        "-s", storage,
        "-r", str(n_reps),
        "--n_reps_storage", "1",
        "--path", str(data_path),
        "--engines", "maximus",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout, env=get_env())
        return result.stdout + result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "TIMEOUT", -1
    except Exception as e:
        return f"ERROR: {e}", -2


def parse_timing(output, query):
    """Extract per-rep timing from maxbench summary line."""
    # Match: gpu,maximus,<query>,<times...>
    pattern = rf"gpu,maximus,{re.escape(query)},([\d,]+)"
    match = re.search(pattern, output)
    if match:
        times_str = match.group(1).rstrip(",")
        return [float(t) for t in times_str.split(",") if t.strip()]
    return []


GPU_ID = str(gpu_info["index"])

# RAPL paths for CPU power measurement
RAPL_PKG_PATHS = []
RAPL_DRAM_PATHS = []
for d in sorted(Path("/sys/class/powercap").glob("intel-rapl:*")):
    if d.is_dir() and (d / "energy_uj").exists():
        name_file = d / "name"
        if name_file.exists() and name_file.read_text().strip().startswith("package"):
            RAPL_PKG_PATHS.append(d / "energy_uj")
        for sub in sorted(d.glob("intel-rapl:*")):
            if sub.is_dir() and (sub / "name").exists():
                if (sub / "name").read_text().strip() == "dram":
                    RAPL_DRAM_PATHS.append(sub / "energy_uj")


def read_rapl_uj():
    """Read current RAPL energy counters (microjoules)."""
    pkg = sum(int(p.read_text().strip()) for p in RAPL_PKG_PATHS) if RAPL_PKG_PATHS else 0
    dram = sum(int(p.read_text().strip()) for p in RAPL_DRAM_PATHS) if RAPL_DRAM_PATHS else 0
    return pkg, dram


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


def run_metrics_for_benchmark(benchmark, sf, data_path, queries, target_time_s,
                              results_dir, storage="gpu", timing_data=None):
    """Run full metrics measurement for one (benchmark, sf) combination."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"{benchmark}_sf{sf}"

    print(f"\n{'=' * 70}")
    print(f"  METRICS: {benchmark.upper()} SF={sf}")
    print(f"  Target: {target_time_s}s sustained execution per query")
    print(f"  Started: {datetime.now()}")
    print(f"{'=' * 70}")

    # ── Phase 1: Calibration ──────────────────────────────────────────────
    calibration = {}
    if timing_data:
        print(f"\n--- Phase 1: Using pre-computed timing from A1/B1 ---")
        for q in queries:
            if q in timing_data:
                calibration[q] = {"min_ms": timing_data[q], "storage": storage}
                print(f"  {q}: {timing_data[q]}ms (from timing CSV)")
            else:
                calibration[q] = {"min_ms": 0, "storage": "fail"}
                print(f"  {q}: not found in timing CSV (skip)")
    else:
        # If -s cpu, each rep includes CPU->GPU transfer (data is NOT cached
        # between reps). If -s gpu OOMs (e.g. SF=20 ClickBench), query is skipped.
        print(f"\n--- Phase 1: Calibration ({CALIBRATION_REPS} reps, -s {storage}) ---")
        for q in queries:
            print(f"  {q}...", end=" ", flush=True)
            output, rc = run_maxbench(benchmark, q, CALIBRATION_REPS, data_path,
                                      storage=storage)
            if rc < 0 or "out_of_memory" in output.lower():
                calibration[q] = {"min_ms": 0, "storage": "oom"}
                print("OOM (skip)")
            else:
                times = parse_timing(output, q)
                if times:
                    calibration[q] = {"min_ms": min(times), "storage": storage}
                    print(f"{min(times)}ms")
                else:
                    calibration[q] = {"min_ms": 0, "storage": "fail"}
                    print("FAIL")

    # ── Phase 2: Calculate n_reps ─────────────────────────────────────────
    print(f"\n--- Phase 2: Calculate n_reps ---")
    for q in queries:
        cal = calibration[q]
        if cal["min_ms"] > 0:
            cal["n_reps"] = min(MAX_REPS, max(MIN_REPS,
                                math.ceil(target_time_s * 1000 / cal["min_ms"])))
        else:
            cal["n_reps"] = MAX_REPS  # default for sub-1ms queries
        est_s = cal["n_reps"] * max(cal["min_ms"], 1) / 1000
        print(f"  {q}: {cal['min_ms']}ms x {cal['n_reps']} reps = {est_s:.1f}s "
              f"({cal['storage']})")

    # ── Phase 3: Metrics run with nvidia-smi sampling ─────────────────────
    print(f"\n--- Phase 3: Metrics run with nvidia-smi sampling ---")
    all_samples = []
    summaries = []

    for q in queries:
        cal = calibration[q]
        if cal["storage"] in ("fail", "oom"):
            print(f"  {q}: SKIP ({cal['storage']})")
            continue

        n_reps = cal["n_reps"]
        print(f"  {q} ({n_reps} reps, -s {storage})...", end=" ", flush=True)

        # Start GPU sampling
        samples = []
        stop_event = threading.Event()
        sampler = threading.Thread(target=sample_gpu_metrics,
                                   args=(stop_event, samples, 0.05))
        sampler.start()

        start_time = time.time()
        output, rc = run_maxbench(benchmark, q, n_reps, data_path,
                                  storage=storage, timeout=600)
        elapsed = time.time() - start_time

        stop_event.set()
        sampler.join(timeout=5)

        # Parse timing
        times = parse_timing(output, q)
        status = "OK" if times else "FAIL"
        if rc == -1:
            status = "TIMEOUT"
        if "out_of_memory" in (output or "").lower():
            status = "OOM"

        # Tag samples
        run_id = f"{tag}_{q}"
        for s in samples:
            s["run_id"] = run_id
            s["sf"] = sf
            s["query"] = q
        all_samples.extend(samples)

        # Memory leak detection (post-peak slope).
        # Pool init / first table-load is bursty and asynchronous — it can
        # land anywhere in the sampling window depending on engine startup
        # latency. Instead of guessing where init ends, compare the running
        # peak to the average of the final samples. A real per-rep leak would
        # show end > peak; a one-shot init followed by steady-state would
        # show end == peak.
        if len(samples) >= 16:
            mems = [s["mem_used_mb"] for s in samples]
            peak_idx = max(range(len(mems)), key=lambda i: mems[i])
            tail_n = len(mems) - peak_idx - 1
            if tail_n >= max(4, len(mems) // 10):
                post_peak = mems[peak_idx + 1:]
                mem_peak = mems[peak_idx]
                window = max(4, len(post_peak) // 4)
                mem_end = sum(post_peak[-window:]) / window
                mem_growth_mb = mem_end - mem_peak
                if mem_growth_mb > 200 and mem_growth_mb > mem_peak * 0.02:
                    print(f"\n  ⚠ MEMORY LEAK DETECTED for {q} (post-peak): "
                          f"+{mem_growth_mb:.0f}MB ({mem_peak:.0f}→{mem_end:.0f}MB)")

        # Compute steady-state metrics via GPU utilization threshold
        if samples:
            all_util = [s["gpu_util_pct"] for s in samples]
            avg_util_all = sum(all_util) / len(all_util)

            # Find compute region: first/last sample where util >= avg
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

        min_ms = min(times) if times else 0
        avg_ms = sum(times) / len(times) if times else 0
        # Per-query time: use elapsed/n_reps for sub-ms queries where int ms rounds to 0
        if min_ms > 0:
            query_time_s = min_ms / 1000.0
        elif n_reps > 0 and elapsed > 0:
            query_time_s = elapsed / n_reps
        else:
            query_time_s = 0
        energy_j = avg_power * query_time_s        # per-query GPU energy
        cpu_energy_j = avg_cpu_pkg_w * query_time_s  # per-query CPU energy

        summaries.append({
            "run_id": run_id, "benchmark": benchmark, "sf": sf, "query": q,
            "storage": storage, "n_reps": n_reps,
            "min_ms": min_ms,
            "query_time_ms": f"{query_time_s * 1000:.4f}",
            "avg_ms": f"{avg_ms:.1f}",
            "elapsed_s": f"{elapsed:.2f}",
            "num_samples": len(samples),
            "num_steady_samples": len(steady),
            "avg_power_w": f"{avg_power:.1f}",
            "max_power_w": f"{max_power:.1f}",
            "max_mem_mb": f"{max_mem:.0f}",
            "avg_gpu_util": f"{avg_util:.1f}",
            "max_gpu_util": f"{max_util:.0f}",
            "energy_j": f"{energy_j:.4f}",
            "avg_cpu_pkg_w": f"{avg_cpu_pkg_w:.1f}",
            "avg_cpu_dram_w": f"{avg_cpu_dram_w:.1f}",
            "cpu_energy_j": f"{cpu_energy_j:.4f}",
            "status": status,
        })

        print(f"{query_time_s*1000:.3f}ms, {elapsed:.1f}s, GPU:{avg_power:.0f}W CPU:{avg_cpu_pkg_w:.0f}W, "
              f"{max_util:.0f}%util, {max_mem:.0f}MB, GPU_E:{energy_j:.4f}J CPU_E:{cpu_energy_j:.4f}J [{status}]")

    # ── Save ──────────────────────────────────────────────────────────────
    samples_file = results_dir / f"maximus_{tag}_metrics_samples_{ts}.csv"
    with open(samples_file, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "run_id", "sf", "query", "time_offset_ms",
            "power_w", "gpu_util_pct", "mem_used_mb",
            "cpu_pkg_power_w", "cpu_dram_power_w",
        ])
        w.writeheader()
        w.writerows(all_samples)

    summary_file = results_dir / f"maximus_{tag}_metrics_summary_{ts}.csv"
    with open(summary_file, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "run_id", "benchmark", "sf", "query", "storage", "n_reps",
            "min_ms", "query_time_ms", "avg_ms", "elapsed_s",
            "num_samples", "num_steady_samples",
            "avg_power_w", "max_power_w", "max_mem_mb",
            "avg_gpu_util", "max_gpu_util", "energy_j",
            "avg_cpu_pkg_w", "avg_cpu_dram_w", "cpu_energy_j", "status",
        ])
        w.writeheader()
        w.writerows(summaries)

    print(f"\n  Samples: {samples_file} ({len(all_samples)} samples)")
    print(f"  Summary: {summary_file} ({len(summaries)} queries)")
    return summaries, all_samples


def main():
    parser = argparse.ArgumentParser(
        description="Maximus GPU steady-state metrics measurement")
    parser.add_argument("benchmarks", nargs="*", default=["clickbench"],
                        help="Benchmarks to measure (default: clickbench)")
    parser.add_argument("--sf", type=str, default=None,
                        help="Specific scale factor (default: all configured SFs)")
    parser.add_argument("--target-time", type=float, default=TARGET_TIME_S,
                        help=f"Target sustained time per query in seconds "
                             f"(default: {TARGET_TIME_S})")
    parser.add_argument("--results-dir", type=str, default=None,
                        help="Directory for output CSVs")
    parser.add_argument("--storage", type=str, default="gpu",
                        choices=["gpu", "cpu"],
                        help="Storage device for tables (default: gpu)")
    parser.add_argument("--timing-csv", type=str, default=None,
                        help="Path to A1 timing CSV (skip calibration phase)")
    parser.add_argument("--test", action="store_true",
                        help="Quick test with 3 queries per benchmark")
    parser.add_argument("--minimum", action="store_true",
                        help="Minimum experiment: SF_min + SF_max, no microbench")
    parser.add_argument("--toy", action="store_true",
                        help="Toy experiment: tpch only, 1 query, largest SF (smallest A+B+C smoke)")
    args = parser.parse_args()

    global BENCHMARKS
    if args.test or args.minimum or args.toy:
        BENCHMARKS = get_benchmark_config(gpu_info["vram_mb"],
                                          test_mode=args.test,
                                          minimum_mode=args.minimum,
                                          toy_mode=args.toy)

    results_dir = (Path(args.results_dir) if args.results_dir
                   else MAXIMUS_DIR / "benchmark_results")
    results_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("  MAXIMUS GPU STEADY-STATE METRICS")
    print(f"  Target: {args.target_time}s sustained execution per query")
    print(f"  Started: {datetime.now()}")
    print("=" * 70)

    for bench_name in args.benchmarks:
        if bench_name not in BENCHMARKS:
            print(f"Unknown benchmark: {bench_name}, skipping")
            continue
        cfg = BENCHMARKS[bench_name]

        sfs = cfg["scale_factors"]
        if args.sf is not None:
            # Filter to requested SF
            try:
                sf_val = int(args.sf)
            except ValueError:
                sf_val = args.sf
            sfs = [sf for sf in sfs if str(sf) == str(sf_val)]
            if not sfs:
                print(f"SF={args.sf} not configured for {bench_name}")
                continue

        for sf in sfs:
            data_path = maximus_data_dir(bench_name, sf)
            if not data_path.exists():
                if not ensure_maximus_csv(bench_name, sf):
                    print(f"[SKIP] {bench_name} SF={sf}: {data_path} not found")
                    continue
            td = None
            if args.timing_csv and os.path.exists(args.timing_csv):
                td = load_timing_from_csv(args.timing_csv, bench_name, sf)
                if not td:
                    print(f"  [WARN] No timing data in CSV for {bench_name} SF={sf}, will calibrate")
            # ClickBench SF20 exhausts VRAM + host RAM under -s gpu: the 20 GB
            # preload plus a wide-aggregate query's intermediates (q28-q34, the
            # ~90-way SUM) spill through managed memory until the kernel OOM-kills
            # the process at a wandering query, leaving 0 summaries. It's the
            # documented oversized-data case, so measure it with CPU storage
            # (data stays on host, cuDF pulls what it needs) instead of the full
            # GPU preload. See CLAUDE.md "GPU Memory and Storage Device".
            q_storage = args.storage
            if bench_name == "clickbench" and str(sf) == "20" and q_storage == "gpu":
                q_storage = "cpu"
                print(f"  [storage] {bench_name} SF={sf}: forcing -s cpu "
                      f"(-s gpu OOM-crashes; see run_maximus_metrics.py)")
            run_metrics_for_benchmark(
                bench_name, sf, data_path, cfg["queries"],
                args.target_time, results_dir, storage=q_storage,
                timing_data=td)

    print(f"\n{'=' * 70}")
    print(f"  ALL DONE — {datetime.now()}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
