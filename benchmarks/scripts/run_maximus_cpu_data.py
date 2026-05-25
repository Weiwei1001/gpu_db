#!/usr/bin/env python3
"""
Maximus data-on-CPU benchmark: timing + metrics with -s cpu.

When using -s cpu, data resides in CPU memory and is transferred to GPU
for each query execution. This measures the combined transfer + compute cost.

Includes both timing (min/avg of N reps) and power metrics (nvidia-smi + RAPL).
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

from hw_detect import detect_gpu, get_benchmark_config, maximus_data_dir, MAXIMUS_DIR

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

gpu_info = detect_gpu()
BENCHMARKS = get_benchmark_config(gpu_info["vram_mb"])

GPU_ID = str(gpu_info["index"])
TARGET_TIME_S = 5
MIN_REPS = 3
MAX_REPS = 100       # cap reps to limit memory leak impact
CALIBRATION_REPS = 3


def load_timing_from_csv(csv_path, benchmark, sf):
    """Load per-query min_ms from B1 timing CSV, keyed by query name."""
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

# RAPL
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


def get_env():
    env = os.environ.copy()
    ld = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = ":".join(LD_EXTRA) + (":" + ld if ld else "")
    return env


def run_maxbench(benchmark, queries, n_reps, data_path, storage="cpu", timeout=300):
    cmd = [
        str(MAXBENCH), "--benchmark", benchmark,
        "-q", ",".join(queries), "-d", "gpu", "-s", storage,
        "-r", str(n_reps), "--n_reps_storage", "1",
        "--path", str(data_path), "--engines", "maximus",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=get_env())
        return r.stdout + (r.stderr or ""), r.returncode
    except subprocess.TimeoutExpired:
        return "TIMEOUT", -1
    except Exception as e:
        return str(e), -2


def run_maxbench_single(benchmark, query, n_reps, data_path, storage="cpu", timeout=300):
    cmd = [
        str(MAXBENCH), "--benchmark", benchmark,
        "-q", query, "-d", "gpu", "-s", storage,
        "-r", str(n_reps), "--n_reps_storage", "1",
        "--path", str(data_path), "--engines", "maximus",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=get_env())
        return r.stdout + (r.stderr or ""), r.returncode
    except subprocess.TimeoutExpired:
        return "TIMEOUT", -1
    except Exception as e:
        return str(e), -2


def parse_maxbench_output(output):
    result = {"query_times": {}}
    for line in output.split("\n"):
        if line.startswith("gpu,maximus,"):
            parts = line.strip().split(",")
            if len(parts) >= 4:
                qname = parts[2]
                times = [float(t) for t in parts[3:] if t.strip()]
                result["query_times"][qname] = times
    # Also parse MAXIMUS TIMINGS
    current_query = None
    for line in output.split("\n"):
        qm = re.match(r"\s*QUERY (\w+)\s*", line.strip())
        if qm:
            current_query = qm.group(1)
        tm = re.match(r"- MAXIMUS TIMINGS \[ms\]:\s*(.*)", line.strip())
        if tm and current_query:
            ts = tm.group(1).strip().rstrip(",")
            times = [float(t.strip()) for t in ts.split(",") if t.strip()]
            if current_query not in result["query_times"]:
                result["query_times"][current_query] = times
    return result


def sample_gpu_metrics(stop_event, samples, interval=0.05):
    start = time.time()
    prev_pkg, prev_dram = read_rapl_uj()
    prev_time = start
    while not stop_event.is_set():
        try:
            r = subprocess.run(
                ["nvidia-smi", "-i", GPU_ID,
                 "--query-gpu=power.draw,utilization.gpu,memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
            now = time.time()
            cur_pkg, cur_dram = read_rapl_uj()
            dt = now - prev_time
            cpu_pkg_w = (cur_pkg - prev_pkg) / 1e6 / dt if dt > 0 else 0
            cpu_dram_w = (cur_dram - prev_dram) / 1e6 / dt if dt > 0 else 0
            prev_pkg, prev_dram, prev_time = cur_pkg, cur_dram, now
            if r.returncode == 0:
                parts = [p.strip() for p in r.stdout.strip().split(",")]
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


def main():
    parser = argparse.ArgumentParser(description="Maximus data-on-CPU benchmark")
    parser.add_argument("benchmarks", nargs="*", default=["tpch", "h2o", "clickbench"])
    parser.add_argument("--results-dir", type=str, default=str(MAXIMUS_DIR / "results"))
    parser.add_argument("--target-time", type=float, default=TARGET_TIME_S)
    parser.add_argument("--timing-only", action="store_true", help="Skip metrics, only timing")
    parser.add_argument("--timing-csv", type=str, default=None,
                        help="Path to B1 timing CSV (skip calibration, metrics only)")
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

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("  MAXIMUS DATA-ON-CPU BENCHMARK (-s cpu)")
    print(f"  Started: {datetime.now()}")
    print("=" * 70)

    all_timing_rows = []
    all_metrics_rows = []
    all_samples = []
    t0 = time.perf_counter()

    for bench_name in args.benchmarks:
        if bench_name not in BENCHMARKS:
            print(f"Unknown benchmark: {bench_name}, skipping")
            continue
        cfg = BENCHMARKS[bench_name]

        for sf in cfg["scale_factors"]:
            data_path = maximus_data_dir(bench_name, sf)
            if not data_path.exists():
                print(f"[SKIP] {bench_name} SF={sf}: {data_path} not found")
                continue

            queries = cfg["queries"]
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            tag = f"{bench_name}_sf{sf}"

            print(f"\n{'=' * 70}")
            print(f"  {bench_name.upper()} SF={sf} ({len(queries)} queries, -s cpu)")
            print(f"{'=' * 70}")

            # --- TIMING: run all queries together ---
            # When --timing-csv is provided, skip the timing run and load from CSV
            timing_from_csv = None
            if args.timing_csv and os.path.exists(args.timing_csv):
                timing_from_csv = load_timing_from_csv(args.timing_csv, bench_name, sf)

            if timing_from_csv and not args.timing_only:
                print(f"\n--- Using pre-computed timing from B1 CSV ---")
                parsed = {"query_times": {}}
                for q in queries:
                    if q in timing_from_csv:
                        print(f"  {q}: {timing_from_csv[q]}ms (from timing CSV)")
                    else:
                        print(f"  {q}: not found in timing CSV (skip)")
            else:
                print(f"\n--- Timing ({CALIBRATION_REPS} reps, -s cpu) ---")
                # CPU-storage pays full PCIe reload per rep; big SFs can take
                # minutes per heavy query (e.g. TPC-H q19 @ SF=20). 300s/query
                # batch budget + 600s individual-retry budget so large SFs
                # don't get truncated.
                timeout = max(600, 300 * len(queries))
                output, rc = run_maxbench(bench_name, queries, CALIBRATION_REPS,
                                          data_path, storage="cpu", timeout=timeout)
                parsed = parse_maxbench_output(output)

                # Retry missing individually
                if len(parsed["query_times"]) < len(queries) // 2:
                    print(f"  Batch incomplete ({len(parsed['query_times'])}/{len(queries)}), retrying...")
                    for q in queries:
                        if q not in parsed["query_times"]:
                            o2, _ = run_maxbench_single(bench_name, q, CALIBRATION_REPS,
                                                         data_path, storage="cpu", timeout=600)
                            parsed["query_times"].update(parse_maxbench_output(o2)["query_times"])

                ok = 0
                for q in queries:
                    times = parsed["query_times"].get(q, [])
                    if times:
                        status = "OK"
                        ok += 1
                        print(f"  {q}: min={min(times)}ms avg={sum(times)/len(times):.1f}ms [{status}]")
                    else:
                        status = "FAIL"
                        print(f"  {q}: NO DATA [{status}]")
                    all_timing_rows.append({
                        "benchmark": bench_name, "sf": sf, "query": q,
                        "storage": "cpu", "n_reps": len(times),
                        "min_ms": min(times) if times else "",
                        "avg_ms": round(sum(times)/len(times), 2) if times else "",
                        "max_ms": max(times) if times else "",
                        "status": status,
                    })
                print(f"  --- {ok}/{len(queries)} OK")

            if args.timing_only:
                continue

            # --- METRICS: per-query with power sampling ---
            print(f"\n--- Metrics with power sampling (-s cpu) ---")
            for q in queries:
                # Use timing CSV data if available, otherwise use calibration run
                if timing_from_csv:
                    if q not in timing_from_csv:
                        print(f"  {q}: SKIP (not in timing CSV)")
                        continue
                    min_ms = timing_from_csv[q]
                else:
                    cal_times = parsed["query_times"].get(q, [])
                    if not cal_times:
                        print(f"  {q}: SKIP (no timing data)")
                        continue
                    min_ms = min(cal_times)
                if min_ms > 0:
                    n_reps = min(MAX_REPS, max(MIN_REPS, math.ceil(args.target_time * 1000 / min_ms)))
                else:
                    n_reps = MAX_REPS
                print(f"  {q} ({n_reps} reps, -s cpu)...", end=" ", flush=True)

                samples = []
                stop_event = threading.Event()
                sampler = threading.Thread(target=sample_gpu_metrics,
                                           args=(stop_event, samples, 0.05))
                sampler.start()

                start_time = time.time()
                output, rc = run_maxbench_single(bench_name, q, n_reps,
                                                  data_path, storage="cpu", timeout=1200)
                elapsed = time.time() - start_time

                stop_event.set()
                sampler.join(timeout=5)

                times = parse_maxbench_output(output)["query_times"].get(q, [])
                status = "OK" if times else ("TIMEOUT" if rc == -1 else "FAIL")

                run_id = f"{tag}_cpu_{q}"
                for s in samples:
                    s["run_id"] = run_id
                    s["sf"] = sf
                    s["query"] = q
                all_samples.extend(samples)

                # Memory leak detection
                if len(samples) >= 8:
                    quarter = len(samples) // 4
                    mem_first = sum(s["mem_used_mb"] for s in samples[:quarter]) / quarter
                    mem_last = sum(s["mem_used_mb"] for s in samples[-quarter:]) / quarter
                    mem_growth_mb = mem_last - mem_first
                    if mem_growth_mb > 500:
                        print(f"\n  ⚠ MEMORY LEAK DETECTED for {q}: "
                              f"+{mem_growth_mb:.0f}MB ({mem_first:.0f}→{mem_last:.0f}MB)")

                # Steady-state analysis
                if samples:
                    all_util = [s["gpu_util_pct"] for s in samples]
                    avg_util_all = sum(all_util) / len(all_util)
                    start_idx = next((i for i, s in enumerate(samples) if s["gpu_util_pct"] >= avg_util_all), 0)
                    end_idx = next((i for i in range(len(samples)-1, -1, -1) if samples[i]["gpu_util_pct"] >= avg_util_all), len(samples)-1)
                    steady = samples[start_idx:end_idx+1] if end_idx >= start_idx else samples
                else:
                    steady = []

                if steady:
                    avg_power = sum(s["power_w"] for s in steady) / len(steady)
                    max_power = max(s["power_w"] for s in steady)
                    max_mem = max(s["mem_used_mb"] for s in steady)
                    avg_util = sum(s["gpu_util_pct"] for s in steady) / len(steady)
                    max_util = max(s["gpu_util_pct"] for s in steady)
                    avg_cpu_pkg = sum(s.get("cpu_pkg_power_w", 0) for s in steady) / len(steady)
                    avg_cpu_dram = sum(s.get("cpu_dram_power_w", 0) for s in steady) / len(steady)
                else:
                    avg_power = max_power = max_mem = avg_util = max_util = 0
                    avg_cpu_pkg = avg_cpu_dram = 0

                m_min = min(times) if times else 0
                m_avg = sum(times)/len(times) if times else 0
                gpu_e = avg_power * (m_min / 1000) if m_min > 0 else 0
                cpu_e = avg_cpu_pkg * (m_min / 1000) if m_min > 0 else 0

                all_metrics_rows.append({
                    "run_id": run_id, "benchmark": bench_name, "sf": sf, "query": q,
                    "storage": "cpu", "n_reps": n_reps,
                    "min_ms": m_min, "avg_ms": f"{m_avg:.1f}",
                    "elapsed_s": f"{elapsed:.2f}",
                    "num_samples": len(samples), "num_steady_samples": len(steady),
                    "avg_power_w": f"{avg_power:.1f}", "max_power_w": f"{max_power:.1f}",
                    "max_mem_mb": f"{max_mem:.0f}",
                    "avg_gpu_util": f"{avg_util:.1f}", "max_gpu_util": f"{max_util:.0f}",
                    "gpu_energy_j": f"{gpu_e:.1f}",
                    "avg_cpu_pkg_w": f"{avg_cpu_pkg:.1f}", "avg_cpu_dram_w": f"{avg_cpu_dram:.1f}",
                    "cpu_energy_j": f"{cpu_e:.1f}", "status": status,
                })
                print(f"{m_min}ms, {elapsed:.1f}s, GPU:{avg_power:.0f}W CPU:{avg_cpu_pkg:.0f}W [{status}]")

    elapsed = time.perf_counter() - t0

    # Save timing CSV
    timing_csv = results_dir / "maximus_cpu_data_timing.csv"
    with open(timing_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["benchmark", "sf", "query", "storage",
                                           "n_reps", "min_ms", "avg_ms", "max_ms", "status"])
        w.writeheader()
        w.writerows(all_timing_rows)

    if all_metrics_rows:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        metrics_csv = results_dir / f"maximus_cpu_data_metrics_summary_{ts}.csv"
        with open(metrics_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[
                "run_id", "benchmark", "sf", "query", "storage", "n_reps",
                "min_ms", "avg_ms", "elapsed_s",
                "num_samples", "num_steady_samples",
                "avg_power_w", "max_power_w", "max_mem_mb",
                "avg_gpu_util", "max_gpu_util", "gpu_energy_j",
                "avg_cpu_pkg_w", "avg_cpu_dram_w", "cpu_energy_j", "status",
            ])
            w.writeheader()
            w.writerows(all_metrics_rows)
        print(f"  Metrics: {metrics_csv}")

    if all_samples:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        samples_csv = results_dir / f"maximus_cpu_data_metrics_samples_{ts}.csv"
        with open(samples_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[
                "run_id", "sf", "query", "time_offset_ms",
                "power_w", "gpu_util_pct", "mem_used_mb",
                "cpu_pkg_power_w", "cpu_dram_power_w",
            ])
            w.writeheader()
            w.writerows(all_samples)
        print(f"  Samples: {samples_csv}")

    print(f"\n{'=' * 70}")
    print(f"  DONE ({elapsed:.0f}s = {elapsed/60:.1f}min)")
    print(f"  Timing: {timing_csv}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
