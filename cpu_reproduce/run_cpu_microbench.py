#!/usr/bin/env python3
"""
DuckDB CPU Benchmark Runner — Timing + Intel RAPL Energy Measurement

Measures:
  - Query wall-clock timing (N passes, last pass recorded)
  - CPU package + DRAM energy via Intel RAPL
  - Net energy above idle baseline
  - Per-query and aggregate statistics

Supports three benchmark suites: TPC-H, H2O, ClickBench.

Usage:
  python run_cpu_benchmark.py                              # all benchmarks
  python run_cpu_benchmark.py tpch                         # TPC-H only
  python run_cpu_benchmark.py tpch --sf 1 10               # specific scale factors
  python run_cpu_benchmark.py --bench-dir /data/bench      # custom DB location
  python run_cpu_benchmark.py --threads 32 --passes 5      # custom settings
  python run_cpu_benchmark.py --no-power                   # timing only
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
QUERIES_DIR = SCRIPT_DIR / "queries"
DEFAULT_BENCH_DIR = Path.home() / "bench"
DEFAULT_RESULTS_DIR = SCRIPT_DIR / "results"

# ── Benchmark definitions ─────────────────────────────────────────────────
# db_subdir:    subdirectory under --bench-dir
# queries_dir:  directory containing .sql query files
# db_pattern:   filename pattern with {sf} placeholder
# sf_regex:     regex to extract scale factor from existing DB filenames

BENCHMARKS = {
    "tpch": {
        "db_subdir": "tpch",
        "queries_dir": QUERIES_DIR / "tpch",
        "db_pattern": "tpch_sf{sf}.duckdb",
        "sf_regex": r"tpch_sf(\d+)\.duckdb",
    },
    "h2o": {
        "db_subdir": "H2O",
        "queries_dir": QUERIES_DIR / "h2o",
        "db_pattern": "h2o_{sf}.duckdb",
        "sf_regex": r"h2o_(\d+)\.duckdb",
    },
    "clickbench": {
        "db_subdir": "clickbench",
        "queries_dir": QUERIES_DIR / "clickbench",
        "db_pattern": "clickbench_{sf}.duckdb",
        "sf_regex": r"clickbench_(\d+)\.duckdb",
    },
    "case_bench": {
        "db_subdir": "tpch",
        "queries_dir": QUERIES_DIR / "case_bench",
        "db_pattern": "tpch_sf{sf}.duckdb",
        "sf_regex": r"tpch_sf(\d+)\.duckdb",
    },
    "microbench_tpch": {
        "db_subdir": "tpch",
        "queries_dir": QUERIES_DIR / "microbench_tpch",
        "db_pattern": "tpch_sf{sf}.duckdb",
        "sf_regex": r"tpch_sf(\d+)\.duckdb",
    },
    "microbench_h2o": {
        "db_subdir": "H2O",
        "queries_dir": QUERIES_DIR / "microbench_h2o",
        "db_pattern": "h2o_{sf}gb.duckdb",
        "sf_regex": r"h2o_(\d+)gb\.duckdb",
    },
    "microbench_clickbench": {
        "db_subdir": "clickbench",
        "queries_dir": QUERIES_DIR / "microbench_clickbench",
        "db_pattern": "clickbench_{sf}.duckdb",
        "sf_regex": r"clickbench_(\d+)\.duckdb",
    },
    "h2o_gb": {
        "db_subdir": "H2O",
        "queries_dir": QUERIES_DIR / "h2o",
        "db_pattern": "h2o_{sf}gb.duckdb",
        "sf_regex": r"h2o_(\d+)gb\.duckdb",
    },
}

# ── RAPL (Running Average Power Limit) ────────────────────────────────────

RAPL_BASE = Path("/sys/class/powercap")


@dataclass
class RAPLDomains:
    """Paths to RAPL energy_uj files for CPU package(s) and DRAM."""
    pkg: list[Path]   # intel-rapl:N/energy_uj  (package-level)
    dram: list[Path]  # intel-rapl:N:M/energy_uj where name == "dram"


@dataclass
class RAPLReading:
    """A single point-in-time reading from all RAPL domains."""
    pkg_uj: int
    dram_uj: int
    time_s: float


@dataclass
class RAPLSample:
    """One sample of instantaneous power computed from two consecutive readings."""
    time_offset_ms: float
    pkg_power_w: float
    dram_power_w: float
    pkg_energy_uj: int
    dram_energy_uj: int


def detect_rapl_domains() -> RAPLDomains:
    """Auto-detect RAPL package and DRAM energy_uj paths."""
    pkg_paths = []
    dram_paths = []

    if not RAPL_BASE.exists():
        return RAPLDomains(pkg=[], dram=[])

    # Package-level domains: intel-rapl:N (single number, no colon in suffix)
    for d in sorted(RAPL_BASE.glob("intel-rapl:*")):
        suffix = d.name.split("intel-rapl:")[-1]
        if ":" in suffix:
            continue  # sub-domain, skip
        ej = d / "energy_uj"
        if ej.exists():
            pkg_paths.append(ej)

        # Check sub-domains for DRAM: intel-rapl:N:M where name == "dram"
        for sub in sorted(d.glob("intel-rapl:*")):
            name_file = sub / "name"
            ej_sub = sub / "energy_uj"
            if name_file.exists() and ej_sub.exists():
                name = name_file.read_text().strip().lower()
                if name == "dram":
                    dram_paths.append(ej_sub)

    return RAPLDomains(pkg=pkg_paths, dram=dram_paths)


def read_rapl(domains: RAPLDomains) -> RAPLReading:
    """Read and sum energy_uj across all RAPL domains."""
    pkg_total = 0
    for p in domains.pkg:
        try:
            pkg_total += int(p.read_text().strip())
        except (OSError, ValueError):
            pass

    dram_total = 0
    for p in domains.dram:
        try:
            dram_total += int(p.read_text().strip())
        except (OSError, ValueError):
            pass

    return RAPLReading(pkg_uj=pkg_total, dram_uj=dram_total, time_s=time.perf_counter())


def sample_rapl_thread(
    samples: list[RAPLSample],
    stop_event: threading.Event,
    start_time: float,
    domains: RAPLDomains,
    interval_ms: float = 50,
) -> None:
    """Background thread: sample RAPL energy at ~interval_ms intervals.

    Sleeps one full interval before the first sample to ensure a proper
    time delta for computing instantaneous power.
    """
    interval_s = interval_ms / 1000.0
    prev = read_rapl(domains)
    stop_event.wait(timeout=interval_s)

    while not stop_event.is_set():
        cur = read_rapl(domains)
        dt = cur.time_s - prev.time_s
        if dt > 0:
            pkg_w = max(0, cur.pkg_uj - prev.pkg_uj) / 1e6 / dt
            dram_w = max(0, cur.dram_uj - prev.dram_uj) / 1e6 / dt
        else:
            pkg_w = dram_w = 0.0

        samples.append(RAPLSample(
            time_offset_ms=round((cur.time_s - start_time) * 1000, 2),
            pkg_power_w=round(pkg_w, 2),
            dram_power_w=round(dram_w, 2),
            pkg_energy_uj=cur.pkg_uj,
            dram_energy_uj=cur.dram_uj,
        ))
        prev = cur
        elapsed = time.perf_counter() - cur.time_s
        stop_event.wait(timeout=max(0.001, interval_s - elapsed))


def measure_idle(domains: RAPLDomains, duration_s: float = 5.0) -> tuple[float, float]:
    """Measure idle power (pkg + dram) over a quiet period.

    Returns (idle_pkg_w, idle_dram_w).
    """
    time.sleep(2)  # settle
    r0 = read_rapl(domains)
    time.sleep(duration_s)
    r1 = read_rapl(domains)
    dt = r1.time_s - r0.time_s
    pkg_w = max(0, r1.pkg_uj - r0.pkg_uj) / 1e6 / dt
    dram_w = max(0, r1.dram_uj - r0.dram_uj) / 1e6 / dt
    return round(pkg_w, 2), round(dram_w, 2)


# ── DuckDB execution ──────────────────────────────────────────────────────

def find_duckdb(explicit: str | None = None) -> Path:
    """Locate the DuckDB CLI binary."""
    if explicit:
        p = Path(explicit)
        if p.exists() and p.is_file():
            return p
        raise FileNotFoundError(f"DuckDB not found at: {explicit}")
    result = shutil.which("duckdb")
    if result:
        return Path(result)
    raise FileNotFoundError("DuckDB not found on PATH. Install it or pass --duckdb PATH.")


def run_duckdb(
    duckdb_bin: Path,
    db_path: Path,
    sql: str,
    threads: int,
    timeout_s: int = 600,
) -> tuple[float, int, str]:
    """Run SQL via stdin with SET threads. Returns (wall_time_s, retcode, status)."""
    stdin = f"SET threads = {threads};\n{sql}\n"
    t0 = time.perf_counter()
    try:
        r = subprocess.run(
            [str(duckdb_bin), str(db_path)],
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
        )
        t1 = time.perf_counter()
        output = r.stdout or ""
        has_error = r.returncode != 0 or "Error" in output
        status = "ERROR" if has_error else "OK"
        return t1 - t0, r.returncode, status
    except subprocess.TimeoutExpired:
        return timeout_s, -1, "TIMEOUT"
    except Exception as e:
        return time.perf_counter() - t0, -1, "EXCEPTION"


# ── DB discovery ──────────────────────────────────────────────────────────

def discover_dbs(bench_dir: Path, bench_cfg: dict, sf_filter: list[int] | None) -> list[tuple[int, Path]]:
    """Find available DB files for a benchmark. Returns [(sf, path), ...]."""
    db_dir = bench_dir / bench_cfg["db_subdir"]
    if not db_dir.exists():
        return []

    regex = re.compile(bench_cfg["sf_regex"])
    results = []
    for f in sorted(db_dir.iterdir()):
        m = regex.match(f.name)
        if m:
            sf = int(m.group(1))
            if sf_filter is None or sf in sf_filter:
                results.append((sf, f))
    return sorted(results, key=lambda x: x[0])


# ── Phase 1: Timing ──────────────────────────────────────────────────────

def run_timing_phase(
    bench_name: str,
    dbs: list[tuple[int, Path]],
    queries_dir: Path,
    duckdb_bin: Path,
    threads: int,
    n_passes: int,
    timeout_s: int,
) -> list[dict]:
    """Run all queries n_passes times per SF, record last pass timing."""
    print(f"\n{'=' * 70}")
    print(f"  TIMING: {bench_name.upper()} ({threads} threads, {n_passes} passes)")
    print(f"{'=' * 70}")

    timing_results = []
    sql_files = sorted(queries_dir.glob("*.sql")) if queries_dir.exists() else []
    if not sql_files:
        print(f"  [SKIP] No queries in {queries_dir}")
        return timing_results

    for sf, db_path in dbs:
        print(f"\n  SF={sf}: {db_path.name} ({db_path.stat().st_size / 1e9:.1f} GB)")

        for pass_num in range(1, n_passes + 1):
            is_last = pass_num == n_passes
            label = f"Pass {pass_num}/{n_passes}" + (" [RECORD]" if is_last else "")
            print(f"    {label}")

            for sql_path in sql_files:
                qname = sql_path.stem
                sql = sql_path.read_text()
                wall_time, retcode, status = run_duckdb(
                    duckdb_bin, db_path, sql, threads, timeout_s,
                )
                if is_last:
                    timing_results.append({
                        "benchmark": bench_name,
                        "sf": sf,
                        "query": qname,
                        "time_s": round(wall_time, 4),
                        "status": status,
                        "retcode": retcode,
                    })
                print(f"      {qname}: {wall_time:.3f}s [{status}]")

    return timing_results


# ── Phase 2: Power measurement ───────────────────────────────────────────

def run_power_phase(
    bench_name: str,
    dbs: list[tuple[int, Path]],
    queries_dir: Path,
    duckdb_bin: Path,
    threads: int,
    timeout_s: int,
    domains: RAPLDomains,
    idle_pkg_w: float,
    idle_dram_w: float,
    warmup_passes: int,
    min_power_duration: float,
    rapl_interval_ms: float,
    timing_data: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Warmup page cache, then measure energy with RAPL sampling.

    Returns (all_samples_rows, energy_summary_rows).
    """
    print(f"\n{'=' * 70}")
    print(f"  POWER: {bench_name.upper()} (RAPL, pkg={len(domains.pkg)}, dram={len(domains.dram)})")
    print(f"  Idle baseline: pkg={idle_pkg_w:.1f}W, dram={idle_dram_w:.1f}W")
    print(f"{'=' * 70}")

    sql_files = sorted(queries_dir.glob("*.sql")) if queries_dir.exists() else []
    if not sql_files:
        print(f"  [SKIP] No queries in {queries_dir}")
        return [], []

    # Build timing lookup from phase 1
    timing_lookup: dict[tuple[int, str], float] = {}
    for r in timing_data:
        if r["benchmark"] == bench_name:
            timing_lookup[(r["sf"], r["query"])] = r["time_s"]

    all_samples_rows: list[dict] = []
    energy_summary_rows: list[dict] = []

    for sf, db_path in dbs:
        print(f"\n  SF={sf}: {db_path.name}")

        # Warmup passes to fill OS page cache (exclude disk I/O from energy)
        if warmup_passes > 0:
            print(f"    Warming up ({warmup_passes} passes)...")
            for wp in range(warmup_passes):
                for sql_path in sql_files:
                    sql = sql_path.read_text()
                    run_duckdb(duckdb_bin, db_path, sql, threads, timeout_s)

        # Energy measurement pass
        print(f"    Measuring energy (min {min_power_duration}s per query)...")

        for sql_path in sql_files:
            qname = sql_path.stem
            sql = sql_path.read_text()

            # Determine repetitions for fast queries
            est_time = timing_lookup.get((sf, qname), 0.1)
            if est_time < min_power_duration:
                n_reps = max(1, int(min_power_duration / est_time))
            else:
                n_reps = 1
            repeated_sql = (sql + "\n") * n_reps

            # Start RAPL sampling
            samples: list[RAPLSample] = []
            stop_event = threading.Event()
            start_rapl = read_rapl(domains)
            start_time = time.perf_counter()

            sampler = threading.Thread(
                target=sample_rapl_thread,
                args=(samples, stop_event, start_time, domains),
                kwargs={"interval_ms": rapl_interval_ms},
            )
            sampler.start()

            # Run query
            try:
                stdin = f"SET threads = {threads};\n{repeated_sql}"
                subprocess.run(
                    [str(duckdb_bin), str(db_path)],
                    input=stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=timeout_s,
                )
            except subprocess.TimeoutExpired:
                print(f"      {qname}: TIMEOUT")
            except Exception as e:
                print(f"      {qname}: ERROR {e}")
            finally:
                stop_event.set()
                sampler.join(timeout=5)

            end_rapl = read_rapl(domains)
            wall_s = end_rapl.time_s - start_rapl.time_s

            # Total energy
            pkg_total_j = max(0, end_rapl.pkg_uj - start_rapl.pkg_uj) / 1e6
            dram_total_j = max(0, end_rapl.dram_uj - start_rapl.dram_uj) / 1e6

            # Idle energy over this duration
            pkg_idle_j = idle_pkg_w * wall_s
            dram_idle_j = idle_dram_w * wall_s

            # Net energy (above idle)
            pkg_net_j = max(0, pkg_total_j - pkg_idle_j)
            dram_net_j = max(0, dram_total_j - dram_idle_j)

            # Per-sample rows
            run_id = f"sf{sf}_{qname}"
            for s in samples:
                all_samples_rows.append({
                    "benchmark": bench_name,
                    "run_id": run_id,
                    "sf": sf,
                    "query": qname,
                    "time_offset_ms": s.time_offset_ms,
                    "pkg_power_w": s.pkg_power_w,
                    "dram_power_w": s.dram_power_w,
                    "pkg_energy_uj": s.pkg_energy_uj,
                    "dram_energy_uj": s.dram_energy_uj,
                })

            # Summary row
            pkg_max_w = max((s.pkg_power_w for s in samples), default=0)
            dram_max_w = max((s.dram_power_w for s in samples), default=0)
            energy_summary_rows.append({
                "benchmark": bench_name,
                "sf": sf,
                "query": qname,
                "single_time_s": round(est_time, 4),
                "n_reps": n_reps,
                "wall_s": round(wall_s, 4),
                "pkg_total_j": round(pkg_total_j, 2),
                "pkg_idle_j": round(pkg_idle_j, 2),
                "pkg_net_j": round(pkg_net_j, 2),
                "pkg_net_per_q_j": round(pkg_net_j / n_reps, 4),
                "pkg_avg_w": round(pkg_total_j / wall_s, 1) if wall_s > 0 else 0,
                "pkg_net_avg_w": round(pkg_net_j / wall_s, 1) if wall_s > 0 else 0,
                "pkg_max_w": round(pkg_max_w, 1),
                "dram_total_j": round(dram_total_j, 2),
                "dram_idle_j": round(dram_idle_j, 2),
                "dram_net_j": round(dram_net_j, 2),
                "dram_net_per_q_j": round(dram_net_j / n_reps, 4),
                "dram_avg_w": round(dram_total_j / wall_s, 1) if wall_s > 0 else 0,
                "dram_net_avg_w": round(dram_net_j / wall_s, 1) if wall_s > 0 else 0,
                "dram_max_w": round(dram_max_w, 1),
                "n_samples": len(samples),
            })

            reps_note = f" ({n_reps}x)" if n_reps > 1 else ""
            print(f"      {qname}: {wall_s:.2f}s{reps_note}, "
                  f"pkg_net={pkg_net_j / n_reps:.2f}J/q, "
                  f"dram_net={dram_net_j / n_reps:.2f}J/q, "
                  f"{len(samples)} samples")

    return all_samples_rows, energy_summary_rows


# ── CSV output ────────────────────────────────────────────────────────────

TIMING_FIELDS = ["benchmark", "sf", "query", "time_s", "status", "retcode"]

ENERGY_SAMPLE_FIELDS = [
    "benchmark", "run_id", "sf", "query", "time_offset_ms",
    "pkg_power_w", "dram_power_w", "pkg_energy_uj", "dram_energy_uj",
]

ENERGY_SUMMARY_FIELDS = [
    "benchmark", "sf", "query", "single_time_s", "n_reps", "wall_s",
    "pkg_total_j", "pkg_idle_j", "pkg_net_j", "pkg_net_per_q_j",
    "pkg_avg_w", "pkg_net_avg_w", "pkg_max_w",
    "dram_total_j", "dram_idle_j", "dram_net_j", "dram_net_per_q_j",
    "dram_avg_w", "dram_net_avg_w", "dram_max_w",
    "n_samples",
]


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


# ── Visualization ─────────────────────────────────────────────────────────

def generate_plots(
    results_dir: Path,
    bench_name: str,
    timing_data: list[dict],
    energy_summary: list[dict],
    energy_samples: list[dict],
    idle_pkg_w: float,
    idle_dram_w: float,
) -> None:
    """Generate timing and energy plots (requires matplotlib + pandas)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("  [SKIP] Visualization requires matplotlib. Install: pip install matplotlib")
        return

    # Group by SF
    sfs = sorted(set(r["sf"] for r in timing_data))

    for sf in sfs:
        sf_timing = [r for r in timing_data if r["sf"] == sf]
        sf_energy = [r for r in energy_summary if r["sf"] == sf]
        sf_samples = [r for r in energy_samples if r["sf"] == sf]

        if not sf_timing:
            continue

        qnames = [r["query"] for r in sf_timing]

        # ── Plot 1: Timing bar chart ──
        fig, ax = plt.subplots(figsize=(max(8, len(qnames) * 0.6), 5))
        times = [r["time_s"] for r in sf_timing]
        colors = ["#4CAF50" if r["status"] == "OK" else "#F44336" for r in sf_timing]
        x = np.arange(len(qnames))
        bars = ax.bar(x, times, color=colors, edgecolor="black", linewidth=0.5)
        for bar, val in zip(bars, times):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                    f"{val:.3f}s", ha="center", va="bottom", fontsize=7)
        ax.set_xticks(x)
        ax.set_xticklabels(qnames, rotation=45, ha="right", fontsize=7)
        ax.set_ylabel("Wall Time (s)")
        ax.set_title(f"{bench_name.upper()} SF{sf} — Query Timing")
        ax.grid(axis="y", alpha=0.3)
        plt.tight_layout()
        plt.savefig(results_dir / f"{bench_name}_sf{sf}_timing.png", dpi=150)
        plt.close()

        if not sf_energy:
            continue

        # ── Plot 2: Energy breakdown (stacked bar: pkg + dram net per query) ──
        fig, axes = plt.subplots(1, 2, figsize=(max(12, len(qnames) * 0.8), 5))

        eq = [r["query"] for r in sf_energy]
        pkg_net = [r["pkg_net_per_q_j"] for r in sf_energy]
        dram_net = [r["dram_net_per_q_j"] for r in sf_energy]
        x = np.arange(len(eq))
        width = 0.6

        ax = axes[0]
        ax.bar(x, pkg_net, width, label="CPU Package (net)", color="#2196F3", edgecolor="black", linewidth=0.5)
        ax.bar(x, dram_net, width, bottom=pkg_net, label="DRAM (net)", color="#FF9800", edgecolor="black", linewidth=0.5)
        for i in range(len(eq)):
            total = pkg_net[i] + dram_net[i]
            ax.text(i, total, f"{total:.2f}", ha="center", va="bottom", fontsize=6)
        ax.set_xticks(x)
        ax.set_xticklabels(eq, rotation=45, ha="right", fontsize=7)
        ax.set_ylabel("Net Energy per Query (J)")
        ax.set_title("Energy Breakdown (above idle)")
        ax.legend(fontsize=8)
        ax.grid(axis="y", alpha=0.3)

        # ── Plot 3: Power time series ──
        ax = axes[1]
        try:
            import pandas as pd
            df = pd.DataFrame(sf_samples)
            for qname in eq:
                sub = df[df["query"] == qname]
                if sub.empty:
                    continue
                t = sub["time_offset_ms"] / 1000
                ax.plot(t, sub["pkg_power_w"], label=qname, alpha=0.7, linewidth=0.8)
            ax.axhline(y=idle_pkg_w, color="gray", linestyle="--", linewidth=1,
                        label=f"idle={idle_pkg_w:.0f}W")
            ax.set_xlabel("Time (s)")
            ax.set_ylabel("CPU Package Power (W)")
            ax.set_title("Power Time Series")
            ax.legend(fontsize=6, ncol=2)
            ax.grid(alpha=0.3)
        except ImportError:
            ax.text(0.5, 0.5, "pandas required\nfor time series",
                    ha="center", va="center", transform=ax.transAxes)

        plt.suptitle(f"{bench_name.upper()} SF{sf} — Energy (idle: pkg={idle_pkg_w:.0f}W, dram={idle_dram_w:.1f}W)")
        plt.tight_layout()
        plt.savefig(results_dir / f"{bench_name}_sf{sf}_energy.png", dpi=150)
        plt.close()

    print(f"  Plots saved to {results_dir}/")


# ── Summary printing ──────────────────────────────────────────────────────

def print_timing_summary(bench_name: str, timing_data: list[dict]) -> None:
    sfs = sorted(set(r["sf"] for r in timing_data))
    for sf in sfs:
        rows = [r for r in timing_data if r["sf"] == sf]
        if not rows:
            continue
        total = sum(r["time_s"] for r in rows)
        ok = sum(1 for r in rows if r["status"] == "OK")
        print(f"    SF{sf}: {len(rows)} queries, {ok} OK, total={total:.2f}s")


def print_energy_summary(bench_name: str, energy_data: list[dict]) -> None:
    sfs = sorted(set(r["sf"] for r in energy_data))
    for sf in sfs:
        rows = [r for r in energy_data if r["sf"] == sf]
        if not rows:
            continue
        header = (f"\n  {'Query':>8} | {'Time':>6} | {'Reps':>4} | "
                  f"{'PkgNet/q':>9} | {'DRAMNet/q':>9} | {'Total/q':>9} | "
                  f"{'PkgNetW':>8} | {'DRAMNetW':>8}")
        print(f"\n  SF{sf} Energy Summary:")
        print(header)
        print(f"  {'-' * 82}")
        sum_pkg = sum_dram = sum_time = 0
        for r in rows:
            total_q = r["pkg_net_per_q_j"] + r["dram_net_per_q_j"]
            print(f"  {r['query']:>8} | {r['single_time_s']:>5.3f}s | {r['n_reps']:>4} | "
                  f"{r['pkg_net_per_q_j']:>8.3f}J | {r['dram_net_per_q_j']:>8.4f}J | "
                  f"{total_q:>8.3f}J | {r['pkg_net_avg_w']:>7.1f}W | "
                  f"{r['dram_net_avg_w']:>7.1f}W")
            sum_pkg += r["pkg_net_per_q_j"]
            sum_dram += r["dram_net_per_q_j"]
            sum_time += r["single_time_s"]
        print(f"  {'-' * 82}")
        print(f"  {'TOTAL':>8} | {sum_time:>5.2f}s |      | "
              f"{sum_pkg:>8.2f}J | {sum_dram:>8.3f}J | "
              f"{sum_pkg + sum_dram:>8.2f}J |")


# ── Main ──────────────────────────────────────────────────────────────────

def auto_detect_threads() -> int:
    """Auto-detect number of CPU threads via nproc."""
    try:
        return int(subprocess.check_output(["nproc"]).decode().strip())
    except Exception:
        return os.cpu_count() or 4


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DuckDB CPU benchmark: timing + RAPL energy measurement.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_cpu_benchmark.py                          # all benchmarks, all SFs
  python run_cpu_benchmark.py tpch                     # TPC-H only
  python run_cpu_benchmark.py tpch h2o --sf 10 20      # specific SFs
  python run_cpu_benchmark.py --bench-dir /data/bench  # custom DB path
  python run_cpu_benchmark.py --no-power               # timing only
  python run_cpu_benchmark.py --threads 32 --passes 5  # custom settings
""",
    )
    parser.add_argument(
        "benchmarks", nargs="*", default=["tpch", "h2o", "clickbench"],
        help="Benchmarks to run (default: all three)",
    )
    parser.add_argument("--bench-dir", type=Path, default=DEFAULT_BENCH_DIR,
                        help=f"Base directory for benchmark DBs (default: {DEFAULT_BENCH_DIR})")
    parser.add_argument("--duckdb", default=None,
                        help="Path to DuckDB CLI binary (default: auto-detect)")
    parser.add_argument("--threads", type=int, default=None,
                        help="Number of DuckDB threads (default: auto-detect via nproc)")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR,
                        help=f"Output directory (default: {DEFAULT_RESULTS_DIR})")
    parser.add_argument("--passes", type=int, default=3,
                        help="Timing passes (last recorded, rest warmup; default: 3)")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Per-query timeout in seconds (default: 600)")
    parser.add_argument("--sf", type=int, nargs="+", default=None,
                        help="Specific scale factors to run (default: all available)")

    # Power measurement options
    power_group = parser.add_argument_group("power measurement")
    power_group.add_argument("--no-power", action="store_true",
                             help="Skip RAPL power measurement")
    power_group.add_argument("--warmup", type=int, default=3,
                             help="Warmup passes to fill page cache before power phase (default: 3)")
    power_group.add_argument("--min-power-duration", type=float, default=3.0,
                             help="Min seconds per query in power phase; fast queries are repeated (default: 3.0)")
    power_group.add_argument("--rapl-interval-ms", type=float, default=50,
                             help="RAPL sampling interval in ms (default: 50)")
    power_group.add_argument("--idle-duration", type=float, default=5.0,
                             help="Idle baseline measurement duration in seconds (default: 5.0)")

    # Visualization
    parser.add_argument("--no-plot", action="store_true",
                        help="Skip visualization (no matplotlib required)")

    args = parser.parse_args()

    # Validate benchmark names
    valid_benches = list(BENCHMARKS.keys())
    for b in args.benchmarks:
        if b not in valid_benches:
            parser.error(f"Unknown benchmark: {b}. Choose from: {valid_benches}")

    # Auto-detect threads
    if args.threads is None:
        args.threads = auto_detect_threads()

    # Locate DuckDB
    duckdb_bin = find_duckdb(args.duckdb)

    # Print configuration
    print(f"DuckDB CPU Benchmark")
    print(f"{'=' * 70}")
    print(f"  DuckDB binary:  {duckdb_bin}")
    print(f"  Threads:        {args.threads}")
    print(f"  Bench dir:      {args.bench_dir}")
    print(f"  Results dir:    {args.results_dir}")
    print(f"  Benchmarks:     {args.benchmarks}")
    print(f"  Passes:         {args.passes}")
    print(f"  Power:          {'disabled' if args.no_power else 'enabled'}")
    if not args.no_power:
        print(f"  Warmup:         {args.warmup} passes")
        print(f"  Min power dur:  {args.min_power_duration}s")
        print(f"  RAPL interval:  {args.rapl_interval_ms}ms")

    # Detect RAPL domains
    rapl_domains = RAPLDomains(pkg=[], dram=[])
    idle_pkg_w = 0.0
    idle_dram_w = 0.0

    if not args.no_power:
        rapl_domains = detect_rapl_domains()
        print(f"  RAPL pkg:       {len(rapl_domains.pkg)} domain(s)")
        print(f"  RAPL dram:      {len(rapl_domains.dram)} domain(s)")

        if not rapl_domains.pkg and not rapl_domains.dram:
            print("\n  [WARNING] No RAPL domains found. Power measurement disabled.")
            print("  Ensure Intel RAPL is available: ls /sys/class/powercap/intel-rapl:*")
            args.no_power = True
        else:
            # Measure idle baseline
            print(f"\n  Measuring idle baseline ({args.idle_duration}s)...")
            idle_pkg_w, idle_dram_w = measure_idle(rapl_domains, args.idle_duration)
            print(f"  Idle CPU package: {idle_pkg_w:.1f} W")
            print(f"  Idle DRAM:        {idle_dram_w:.1f} W")

    print(f"{'=' * 70}")

    # Discover available databases
    bench_dbs: dict[str, list[tuple[int, Path]]] = {}
    for bench_name in args.benchmarks:
        dbs = discover_dbs(args.bench_dir, BENCHMARKS[bench_name], args.sf)
        bench_dbs[bench_name] = dbs
        if dbs:
            sfs_str = ", ".join(f"SF{sf}" for sf, _ in dbs)
            print(f"  {bench_name}: {sfs_str}")
        else:
            db_dir = args.bench_dir / BENCHMARKS[bench_name]["db_subdir"]
            print(f"  {bench_name}: no DBs found in {db_dir}")

    args.results_dir.mkdir(parents=True, exist_ok=True)
    overall_start = time.perf_counter()

    # ── Run benchmarks ──
    all_timing: list[dict] = []
    all_energy_summary: list[dict] = []
    all_energy_samples: list[dict] = []

    for bench_name in args.benchmarks:
        dbs = bench_dbs[bench_name]
        if not dbs:
            continue
        bench_cfg = BENCHMARKS[bench_name]
        bench_start = time.perf_counter()

        # Phase 1: Timing
        timing = run_timing_phase(
            bench_name, dbs, bench_cfg["queries_dir"],
            duckdb_bin, args.threads, args.passes, args.timeout,
        )
        all_timing.extend(timing)

        # Write timing CSV
        timing_csv = args.results_dir / f"{bench_name}_timing.csv"
        write_csv(timing_csv, timing, TIMING_FIELDS)
        print(f"\n  Timing CSV: {timing_csv}")
        print_timing_summary(bench_name, timing)

        # Phase 2: Power
        if not args.no_power and (rapl_domains.pkg or rapl_domains.dram):
            samples, summary = run_power_phase(
                bench_name, dbs, bench_cfg["queries_dir"],
                duckdb_bin, args.threads, args.timeout,
                rapl_domains, idle_pkg_w, idle_dram_w,
                args.warmup, args.min_power_duration, args.rapl_interval_ms,
                timing,
            )
            all_energy_samples.extend(samples)
            all_energy_summary.extend(summary)

            # Write power CSVs
            samples_csv = args.results_dir / f"{bench_name}_energy_samples.csv"
            summary_csv = args.results_dir / f"{bench_name}_energy_summary.csv"
            write_csv(samples_csv, samples, ENERGY_SAMPLE_FIELDS)
            write_csv(summary_csv, summary, ENERGY_SUMMARY_FIELDS)
            print(f"\n  Energy samples CSV: {samples_csv}")
            print(f"  Energy summary CSV: {summary_csv}")
            print_energy_summary(bench_name, summary)

        # Visualization
        if not args.no_plot:
            bench_samples = [r for r in all_energy_samples if r["benchmark"] == bench_name]
            bench_summary = [r for r in all_energy_summary if r["benchmark"] == bench_name]
            generate_plots(
                args.results_dir, bench_name, timing,
                bench_summary, bench_samples,
                idle_pkg_w, idle_dram_w,
            )

        bench_elapsed = time.perf_counter() - bench_start
        print(f"\n  {bench_name.upper()} total: {bench_elapsed:.1f}s ({bench_elapsed / 60:.1f}min)")

    # ── Write idle baseline info ──
    if not args.no_power:
        idle_csv = args.results_dir / "idle_baseline.csv"
        write_csv(idle_csv, [{
            "idle_pkg_w": idle_pkg_w,
            "idle_dram_w": idle_dram_w,
            "measurement_duration_s": args.idle_duration,
            "threads": args.threads,
        }], ["idle_pkg_w", "idle_dram_w", "measurement_duration_s", "threads"])

    overall_elapsed = time.perf_counter() - overall_start
    print(f"\n{'=' * 70}")
    print(f"  ALL BENCHMARKS COMPLETE")
    print(f"  Wall time:  {overall_elapsed:.1f}s ({overall_elapsed / 60:.1f}min)")
    print(f"  Results in: {args.results_dir}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
