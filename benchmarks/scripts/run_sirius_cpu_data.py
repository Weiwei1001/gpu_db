#!/usr/bin/env python3
"""
Sirius data-on-CPU benchmark: measures first-run timing (data transfer from CPU)
vs subsequent runs (data already on GPU).

For each query:
  1. Fresh DuckDB process with gpu_buffer_init()
  2. Run gpu_processing() N times
  3. Record EACH individual timing separately
  4. run1 = data-on-CPU (includes CPU→GPU transfer)
  5. run2+ = data-on-GPU (cached)

Also measures GPU+CPU power during a sustained first-run-only workload
to capture the energy cost of data transfer.

Fairness analysis:
  - gpu_buffer_init() cost is constant overhead per session (GPU memory alloc)
  - The FIRST gpu_processing() call includes data transfer from DuckDB (CPU) to GPU buffer
  - Subsequent calls reuse cached GPU data
  - Fair comparison: report first_run_time (with transfer) vs min(subsequent_runs)
"""
from __future__ import annotations

import argparse
import csv
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
N_REPS = 50  # 1st = CPU data, rest = GPU cached; enough reps for steady-state power

# Sirius-supported benchmarks (standard + microbench)
_SIRIUS_BENCHMARKS = {
    "tpch", "h2o", "clickbench",
    "microbench_tpch", "microbench_h2o", "microbench_clickbench",
}

RE_RUN_TIME = re.compile(r"Run Time \(s\):\s*real\s+([\d.]+)", re.IGNORECASE)

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


def load_queries(query_dir: Path):
    queries = []
    for sql_file in sorted(query_dir.glob("*.sql")):
        qname = sql_file.stem
        lines = sql_file.read_text().strip().splitlines()
        gpu_lines = [l.strip() for l in lines if l.strip().startswith("call gpu_processing(")]
        if gpu_lines:
            queries.append((qname, gpu_lines))
    return queries


def build_cpu_data_sql(qname, gpu_lines, n_reps, buffer_init=BUFFER_INIT):
    """Build SQL: init buffer, then run query N times with individual markers."""
    parts = [".timer on", buffer_init]
    for rep in range(n_reps):
        parts.append(f".print ===REP {qname} {rep}===")
        parts.extend(gpu_lines)
    parts.append(".print ===END===")
    return "\n".join(parts) + "\n"


def parse_rep_times(stdout: str, qname: str, n_reps: int) -> list[float]:
    """Parse individual rep timings from output."""
    rep_times = []
    for rep in range(n_reps):
        marker = f"===REP {qname} {rep}==="
        next_marker = f"===REP {qname} {rep + 1}===" if rep < n_reps - 1 else "===END==="
        start_pos = stdout.find(marker)
        end_pos = stdout.find(next_marker, start_pos + 1) if start_pos >= 0 else -1
        if start_pos >= 0 and end_pos >= 0:
            section = stdout[start_pos:end_pos]
            times = [float(m.group(1)) for m in RE_RUN_TIME.finditer(section)]
            rep_times.append(round(sum(times), 4) if times else -1)
        else:
            rep_times.append(-1)
    return rep_times


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
    parser = argparse.ArgumentParser(description="Sirius data-on-CPU benchmark")
    parser.add_argument("benchmarks", nargs="*", default=["tpch", "h2o", "clickbench"])
    parser.add_argument("--sirius-dir", type=str, default=str(DEFAULT_SIRIUS_DIR))
    parser.add_argument("--results-dir", type=str, default=str(DEFAULT_RESULTS_DIR))
    parser.add_argument("--n-reps", type=int, default=N_REPS)
    parser.add_argument("--buffer-init", type=str, default=BUFFER_INIT)
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
    n_reps = args.n_reps
    buffer_init = args.buffer_init

    if not duckdb_bin.exists():
        print(f"ERROR: Sirius DuckDB binary not found: {duckdb_bin}")
        import sys; sys.exit(1)

    # Build dynamic benchmark config from hw_detect
    bench_config = get_benchmark_config(_gpu_info["vram_mb"],
                                        test_mode=args.test,
                                        minimum_mode=args.minimum,
                                        toy_mode=args.toy)
    BENCHMARKS = {k: v for k, v in bench_config.items() if k in _SIRIUS_BENCHMARKS}

    print("=" * 70)
    print("  SIRIUS DATA-ON-CPU BENCHMARK")
    print(f"  GPU: {_gpu_info['name']} (index {_gpu_info['index']}, "
          f"{_gpu_info['vram_mb']} MiB)")
    print(f"  Method: gpu_buffer_init() + {n_reps} x gpu_processing()")
    print(f"  Rep 1 = data-on-CPU (includes transfer), Rep 2+ = data-on-GPU")
    print(f"  Started: {datetime.now()}")
    print("=" * 70)

    all_rows = []
    all_samples = []
    t0 = time.perf_counter()

    for bench_name in args.benchmarks:
        if bench_name not in BENCHMARKS:
            print(f"Unknown: {bench_name}, skipping")
            continue
        cfg = BENCHMARKS[bench_name]
        queries = load_queries(sirius_query_dir(bench_name))
        # In any reduced mode (test/minimum/toy), filter to configured queries.
        if args.test or args.minimum or args.toy:
            allowed = set(cfg["queries"])
            queries = [(qn, gl) for qn, gl in queries if qn in allowed]

        for sf in cfg["scale_factors"]:
            db_path = sirius_db_path(bench_name, sf)
            if not db_path.exists():
                if not ensure_sirius_db(bench_name, sf):
                    print(f"[SKIP] {bench_name} SF={sf}: {db_path} not found")
                    continue

            print(f"\n{'=' * 70}")
            print(f"  {bench_name.upper()} SF={sf} ({len(queries)} queries, {n_reps} reps)")
            print(f"{'=' * 70}")

            for qname, gpu_lines in queries:
                print(f"  {qname}...", end=" ", flush=True)

                sql = build_cpu_data_sql(qname, gpu_lines, n_reps, buffer_init)
                with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False) as f:
                    f.write(sql)
                    tmp = f.name

                # Start power sampling
                samples = []
                stop_event = threading.Event()
                sampler = threading.Thread(target=sample_gpu_metrics,
                                           args=(stop_event, samples, 0.05))
                sampler.start()

                # Read RAPL before
                pkg0, dram0 = read_rapl_uj()
                t_start = time.time()

                try:
                    timeout = 180
                    r = subprocess.run(
                        [str(duckdb_bin), str(db_path)],
                        stdin=open(tmp, "r"),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, timeout=timeout,
                    )
                    stdout = r.stdout or ""
                    rc = r.returncode
                except subprocess.TimeoutExpired:
                    stdout = ""
                    rc = -1
                except Exception as e:
                    stdout = str(e)
                    rc = -2
                finally:
                    os.unlink(tmp)

                t_end = time.time()
                wall_s = t_end - t_start
                pkg1, dram1 = read_rapl_uj()
                cpu_energy_j = (pkg1 - pkg0) / 1e6

                stop_event.set()
                sampler.join(timeout=5)

                has_fallback = "fallback" in stdout.lower()
                rep_times = parse_rep_times(stdout, qname, n_reps)

                # Compute GPU energy from samples
                if samples:
                    gpu_energy_j = sum(s["power_w"] for s in samples) * 0.05  # ~50ms per sample
                    avg_gpu_power = sum(s["power_w"] for s in samples) / len(samples)
                    max_mem = max(s["mem_used_mb"] for s in samples)
                else:
                    gpu_energy_j = 0
                    avg_gpu_power = 0
                    max_mem = 0

                # Tag samples
                run_id = f"{bench_name}_sf{sf}_cpu_{qname}"
                for s in samples:
                    s["run_id"] = run_id
                    s["sf"] = sf
                    s["query"] = qname
                all_samples.extend(samples)

                first_run = rep_times[0] if rep_times else -1
                subsequent = [t for t in rep_times[1:] if t >= 0]
                min_subsequent = min(subsequent) if subsequent else -1

                if has_fallback:
                    status = "FALLBACK"
                elif first_run < 0:
                    status = "ERROR"
                else:
                    status = "OK"

                transfer_overhead = first_run - min_subsequent if first_run >= 0 and min_subsequent >= 0 else -1

                all_rows.append({
                    "benchmark": bench_name, "sf": sf, "query": qname,
                    "first_run_s": f"{first_run:.4f}" if first_run >= 0 else "",
                    "min_subsequent_s": f"{min_subsequent:.4f}" if min_subsequent >= 0 else "",
                    "transfer_overhead_s": f"{transfer_overhead:.4f}" if transfer_overhead >= 0 else "",
                    "all_rep_times": str(rep_times),
                    "wall_time_s": f"{wall_s:.2f}",
                    "gpu_power_avg_w": f"{avg_gpu_power:.1f}",
                    "gpu_energy_j": f"{gpu_energy_j:.1f}",
                    "cpu_energy_j": f"{cpu_energy_j:.1f}",
                    "max_mem_mb": f"{max_mem:.0f}",
                    "status": status,
                })

                if status == "OK":
                    overhead_pct = (transfer_overhead / min_subsequent * 100) if min_subsequent > 0 else 0
                    print(f"1st={first_run:.3f}s, min_sub={min_subsequent:.4f}s, "
                          f"overhead={transfer_overhead:.4f}s ({overhead_pct:.0f}%), "
                          f"GPU:{avg_gpu_power:.0f}W [{status}]")
                else:
                    print(f"[{status}]")

            # Summary for this SF
            sf_rows = [r for r in all_rows if r["benchmark"] == bench_name and str(r["sf"]) == str(sf)]
            ok_n = sum(1 for r in sf_rows if r["status"] == "OK")
            print(f"  --- {ok_n}/{len(queries)} OK")

    elapsed = time.perf_counter() - t0

    # Save results
    csv_path = results_dir / "sirius_cpu_data_analysis.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "benchmark", "sf", "query",
            "first_run_s", "min_subsequent_s", "transfer_overhead_s",
            "all_rep_times", "wall_time_s",
            "gpu_power_avg_w", "gpu_energy_j", "cpu_energy_j", "max_mem_mb",
            "status",
        ])
        w.writeheader()
        w.writerows(all_rows)

    if all_samples:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        samples_csv = results_dir / f"sirius_cpu_data_samples_{ts}.csv"
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
    print(f"  Results: {csv_path}")
    print(f"{'=' * 70}")

    # Fairness analysis summary
    print(f"\n{'=' * 70}")
    print("  FAIRNESS ANALYSIS: Sirius Data-on-CPU")
    print(f"{'=' * 70}")
    print("  For each query in a FRESH DuckDB process:")
    print("    - gpu_buffer_init() allocates GPU memory (constant cost)")
    print("    - 1st gpu_processing() = transfers data from CPU → GPU + computes")
    print("    - 2nd+ gpu_processing() = data already on GPU, just computes")
    print("")
    print("  Fair measurement approach:")
    print("    - Data-on-CPU cost = first_run_s (includes buffer init + transfer + compute)")
    print("    - Data-on-GPU cost = min_subsequent_s (just compute, data cached)")
    print("    - Transfer overhead = first_run_s - min_subsequent_s")
    print("")

    for bench in args.benchmarks:
        rows = [r for r in all_rows if r["benchmark"] == bench and r["status"] == "OK"]
        if not rows:
            continue
        print(f"  {bench.upper()}:")
        for sf in BENCHMARKS[bench]["scale_factors"]:
            sf_rows = [r for r in rows if str(r["sf"]) == str(sf)]
            if not sf_rows:
                continue
            overheads = []
            for r in sf_rows:
                if r["transfer_overhead_s"]:
                    overheads.append(float(r["transfer_overhead_s"]))
            if overheads:
                avg_oh = sum(overheads) / len(overheads)
                max_oh = max(overheads)
                firsts = [float(r["first_run_s"]) for r in sf_rows if r["first_run_s"]]
                subs = [float(r["min_subsequent_s"]) for r in sf_rows if r["min_subsequent_s"]]
                avg_first = sum(firsts) / len(firsts) if firsts else 0
                avg_sub = sum(subs) / len(subs) if subs else 0
                print(f"    SF={sf}: {len(sf_rows)} queries OK")
                print(f"      Avg first_run: {avg_first:.4f}s")
                print(f"      Avg gpu_cached: {avg_sub:.4f}s")
                print(f"      Avg transfer overhead: {avg_oh:.4f}s ({avg_oh/avg_sub*100:.0f}% of compute)" if avg_sub > 0 else f"      Avg transfer overhead: {avg_oh:.4f}s")
                print(f"      Max transfer overhead: {max_oh:.4f}s")


if __name__ == "__main__":
    main()
