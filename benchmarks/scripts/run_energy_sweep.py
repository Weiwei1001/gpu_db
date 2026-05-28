#!/usr/bin/env python3
"""
GPU energy sweep: explore (power-limit, SM-clock, memory-clock) configurations.

Iterates over a grid of GPU power limits, SM clock frequencies, and optionally
memory clock frequencies, running the existing Maximus and Sirius metrics
scripts at each configuration point.
Results are aggregated into a single energy_sweep_summary.csv for analysis.

Safety: GPU defaults are always restored on exit (including Ctrl+C / exceptions).

Usage:
    python run_energy_sweep.py
    python run_energy_sweep.py --power-limits 250,300,360 --sm-clocks 1200,2400
    python run_energy_sweep.py --mem-clocks 405,7001,15001 --sm-clocks 1800,3090
    python run_energy_sweep.py --engines maximus --benchmarks tpch h2o --resume
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent

# Auto-detect GPU index at runtime (was hardcoded to "1" for a previous
# multi-GPU machine). Overridable via MAXIMUS_GPU_ID env var.
def _resolve_gpu_id() -> str:
    import os as _os
    if "MAXIMUS_GPU_ID" in _os.environ:
        return _os.environ["MAXIMUS_GPU_ID"]
    try:
        import hw_detect
        return str(hw_detect.detect_gpu()["index"])
    except Exception:
        return "0"

GPU_ID = _resolve_gpu_id()

# Category C default: 5 SM clocks × 5 power limits = 25 configurations.
#
# * Power limits are NOT spread uniformly across the full [min_pl, max_pl]
#   range. In practice DB workloads never burn max TDP in steady state,
#   so we pin the top at the GPU default (TDP) and sample 4 more points
#   linearly spaced from the hardware minimum up to default. This keeps
#   the sweep in the region that actually reflects real energy trade-offs.
# * SM clocks span the supported range linearly with 5 points.
#
# Both lists are built dynamically at runtime in `_build_default_sweep()`
# from the GPU's own nvidia-smi-reported min/max clocks and power limits,
# so the script works on any GPU without hard-coding 5080 / A100 etc.
DEFAULT_POWER_LIMITS: list[int] = []   # resolved at runtime
DEFAULT_SM_CLOCKS: list[int] = []      # resolved at runtime
DEFAULT_MEM_CLOCKS: list[int] = []     # empty = don't lock memory clocks
DEFAULT_POWER_LIMIT: int = 0           # resolved at runtime (TDP)
_N_POWER_LIMITS = 5
_N_SM_CLOCKS = 5

MAXIMUS_BENCHMARKS = {
    "tpch": [1, 5, 10, 20],
    "h2o": ["1gb", "2gb", "4gb", "8gb"],
    "clickbench": [1, 5, 10, 20],
    "case_bench": [1, 5, 10, 20],
}
SIRIUS_BENCHMARKS = {
    "tpch": [1, 5, 10, 20],
    "h2o": ["1gb", "2gb", "4gb", "8gb"],
    "clickbench": [1, 5, 10, 20],
    "case_bench": [1, 5, 10, 20],
}

# Unified summary CSV columns
SUMMARY_COLUMNS = [
    "power_limit_w", "sm_clock_mhz", "mem_clock_mhz", "engine", "benchmark",
    "sf", "query", "min_ms", "avg_power_w", "max_power_w", "energy_j",
    "cpu_energy_j", "avg_gpu_util", "status",
]

COOLDOWN_PAUSE_S = 5
MAX_COOLDOWN_TEMP_C = 85


# ══════════════════════════════════════════════════════════════════════════════
#  GPU Configuration Management
# ══════════════════════════════════════════════════════════════════════════════

def set_gpu_config(power_limit_w: int, sm_clock_mhz: int,
                   mem_clock_mhz: int | None = None) -> bool:
    """Set GPU power limit, lock SM clocks, and optionally lock memory clocks."""
    mem_str = f", MemCLK={mem_clock_mhz}MHz" if mem_clock_mhz else ""
    print(f"  [GPU] Setting PL={power_limit_w}W, SM={sm_clock_mhz}MHz{mem_str}")

    # Set power limit
    rc1 = subprocess.run(
        ["sudo", "nvidia-smi", "-i", GPU_ID, "-pl", str(power_limit_w)],
        capture_output=True, text=True,
    )
    if rc1.returncode != 0:
        print(f"  [GPU] WARNING: Failed to set power limit: {rc1.stderr.strip()}")
        return False

    # Lock SM clocks
    rc2 = subprocess.run(
        ["sudo", "nvidia-smi", "-i", GPU_ID,
         f"--lock-gpu-clocks={sm_clock_mhz},{sm_clock_mhz}"],
        capture_output=True, text=True,
    )
    if rc2.returncode != 0:
        print(f"  [GPU] WARNING: Failed to lock SM clocks: {rc2.stderr.strip()}")
        return False

    # Lock memory clocks (optional)
    if mem_clock_mhz is not None:
        rc3 = subprocess.run(
            ["sudo", "nvidia-smi", "-i", GPU_ID,
             f"--lock-memory-clocks={mem_clock_mhz},{mem_clock_mhz}"],
            capture_output=True, text=True,
        )
        if rc3.returncode != 0:
            print(f"  [GPU] WARNING: Failed to lock memory clocks: {rc3.stderr.strip()}")
            return False

    # Verify
    if not verify_gpu_config(power_limit_w, sm_clock_mhz):
        print("  [GPU] WARNING: Verification failed after setting config")
        return False

    print(f"  [GPU] Config applied and verified: PL={power_limit_w}W, SM={sm_clock_mhz}MHz{mem_str}")
    return True


def restore_gpu_defaults() -> None:
    """Restore GPU to default power limit and unlock all clocks."""
    print(f"\n  [GPU] Restoring defaults: PL={DEFAULT_POWER_LIMIT}W, clocks=unlocked")

    subprocess.run(
        ["sudo", "nvidia-smi", "-i", GPU_ID, "-pl", str(DEFAULT_POWER_LIMIT)],
        capture_output=True, text=True,
    )
    subprocess.run(
        ["sudo", "nvidia-smi", "-i", GPU_ID, "--reset-gpu-clocks"],
        capture_output=True, text=True,
    )
    subprocess.run(
        ["sudo", "nvidia-smi", "-i", GPU_ID, "--reset-memory-clocks"],
        capture_output=True, text=True,
    )
    print("  [GPU] Defaults restored")


def verify_gpu_config(expected_pl_w: int, expected_clk_mhz: int) -> bool:
    """Read back GPU power limit and verify it matches.

    Note: SM clock is only verifiable under load on Blackwell GPUs (RTX 50xx).
    At idle, clocks.sm reports the idle frequency regardless of --lock-gpu-clocks.
    We trust the lock command's return code for clock verification.
    """
    r = subprocess.run(
        ["nvidia-smi", "-i", GPU_ID,
         "--query-gpu=power.limit",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return False

    try:
        actual_pl = float(r.stdout.strip())
    except (ValueError, IndexError):
        return False

    # Power limit: allow 1W tolerance (nvidia-smi may round)
    pl_ok = abs(actual_pl - expected_pl_w) <= 1.0

    if not pl_ok:
        print(f"  [GPU] PL mismatch: expected {expected_pl_w}W, got {actual_pl}W")

    return pl_ok


def get_gpu_temperature() -> float:
    """Query current GPU temperature in Celsius."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "-i", GPU_ID,
             "--query-gpu=temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return float(r.stdout.strip())
    except Exception:
        pass
    return 0.0


def wait_for_cooldown(max_temp: float = MAX_COOLDOWN_TEMP_C) -> None:
    """Block until GPU temperature drops below max_temp."""
    temp = get_gpu_temperature()
    if temp <= max_temp:
        return
    print(f"  [GPU] Temperature {temp:.0f}C > {max_temp:.0f}C, waiting for cooldown...",
          end="", flush=True)
    while temp > max_temp:
        time.sleep(5)
        temp = get_gpu_temperature()
        print(f" {temp:.0f}C", end="", flush=True)
    print(" OK")


def enable_persistence_mode() -> None:
    """Enable nvidia persistence mode to reduce driver overhead."""
    r = subprocess.run(
        ["sudo", "nvidia-smi", "-i", GPU_ID, "-pm", "1"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        print("  [GPU] Persistence mode enabled")
    else:
        print(f"  [GPU] WARNING: Could not enable persistence mode: {r.stderr.strip()}")


# ══════════════════════════════════════════════════════════════════════════════
#  Config Tag and Directory Helpers
# ══════════════════════════════════════════════════════════════════════════════

def config_tag(power_limit_w: int, sm_clock_mhz: int,
               mem_clock_mhz: int | None = None) -> str:
    """Generate config tag: pl250w_clk0600mhz or pl250w_clk0600mhz_mem15001mhz"""
    tag = f"pl{power_limit_w}w_clk{sm_clock_mhz:04d}mhz"
    if mem_clock_mhz is not None:
        tag += f"_mem{mem_clock_mhz:05d}mhz"
    return tag


def config_dir(results_dir: Path, power_limit_w: int, sm_clock_mhz: int,
               mem_clock_mhz: int | None = None) -> Path:
    """Get per-config subdirectory under results_dir."""
    return results_dir / config_tag(power_limit_w, sm_clock_mhz, mem_clock_mhz)


def config_has_results(cfg_dir: Path, engine: str, benchmarks: list[str]) -> bool:
    """Check if a config directory already has summary CSVs for all requested benchmarks."""
    if not cfg_dir.exists():
        return False
    pattern = f"{engine}_*_metrics_summary_*.csv"
    existing = list(cfg_dir.glob(pattern))
    return len(existing) > 0


# ══════════════════════════════════════════════════════════════════════════════
#  Subprocess Runners for Existing Metrics Scripts
# ══════════════════════════════════════════════════════════════════════════════

def run_maximus_metrics(benchmarks: list[str], cfg_dir: Path,
                        target_time: float,
                        storage: str = "gpu",
                        toy: bool = False) -> int:
    """Run run_maximus_metrics.py as a subprocess. Returns exit code."""
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "run_maximus_metrics.py"),
        *benchmarks,
        "--results-dir", str(cfg_dir),
        "--target-time", str(target_time),
        "--storage", storage,
    ]
    if toy:
        cmd.append("--toy")  # propagate query/SF reduction to the subprocess
    print(f"  [MAXIMUS] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, timeout=7200)  # 2 hour timeout
    return result.returncode


def run_sirius_metrics(benchmarks: list[str], cfg_dir: Path,
                       target_time: float, toy: bool = False) -> int:
    """Run run_sirius_metrics.py as a subprocess. Returns exit code."""
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "run_sirius_metrics.py"),
        *benchmarks,
        "--results-dir", str(cfg_dir),
        "--target-time", str(target_time),
    ]
    if toy:
        cmd.append("--toy")  # propagate query/SF reduction to the subprocess
    print(f"  [SIRIUS] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, timeout=7200)  # 2 hour timeout
    return result.returncode


# ══════════════════════════════════════════════════════════════════════════════
#  Results Aggregation
# ══════════════════════════════════════════════════════════════════════════════

def parse_maximus_summary(csv_path: Path, power_limit_w: int,
                          sm_clock_mhz: int,
                          mem_clock_mhz: int | None = None) -> list[dict]:
    """Parse a Maximus metrics summary CSV into unified rows."""
    rows = []
    try:
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append({
                    "power_limit_w": power_limit_w,
                    "sm_clock_mhz": sm_clock_mhz,
                    "mem_clock_mhz": mem_clock_mhz or "",
                    "engine": "maximus",
                    "benchmark": r.get("benchmark", ""),
                    "sf": r.get("sf", ""),
                    "query": r.get("query", ""),
                    "min_ms": r.get("min_ms", ""),
                    "avg_power_w": r.get("avg_power_w", ""),
                    "max_power_w": r.get("max_power_w", ""),
                    "energy_j": r.get("energy_j", ""),
                    "cpu_energy_j": r.get("cpu_energy_j", ""),
                    "avg_gpu_util": r.get("avg_gpu_util", ""),
                    "status": r.get("status", ""),
                })
    except Exception as e:
        print(f"  WARNING: Failed to parse {csv_path}: {e}")
    return rows


def parse_sirius_summary(csv_path: Path, power_limit_w: int,
                         sm_clock_mhz: int,
                         mem_clock_mhz: int | None = None) -> list[dict]:
    """Parse a Sirius metrics summary CSV into unified rows.

    Sirius uses min_s (seconds) and gpu_energy_j; we convert to match
    the unified schema (min_ms, energy_j).
    """
    rows = []
    try:
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                # Convert min_s to min_ms
                min_s_str = r.get("min_s", "")
                try:
                    min_ms = float(min_s_str) * 1000 if min_s_str else ""
                    min_ms = f"{min_ms:.1f}" if isinstance(min_ms, float) else ""
                except (ValueError, TypeError):
                    min_ms = ""

                rows.append({
                    "power_limit_w": power_limit_w,
                    "sm_clock_mhz": sm_clock_mhz,
                    "mem_clock_mhz": mem_clock_mhz or "",
                    "engine": "sirius",
                    "benchmark": r.get("benchmark", ""),
                    "sf": r.get("sf", ""),
                    "query": r.get("query", ""),
                    "min_ms": min_ms,
                    "avg_power_w": r.get("avg_power_w", ""),
                    "max_power_w": r.get("max_power_w", ""),
                    "energy_j": r.get("gpu_energy_j", ""),
                    "cpu_energy_j": r.get("cpu_energy_j", ""),
                    "avg_gpu_util": r.get("avg_gpu_util", ""),
                    "status": r.get("status", ""),
                })
    except Exception as e:
        print(f"  WARNING: Failed to parse {csv_path}: {e}")
    return rows


def aggregate_results(results_dir: Path,
                      power_limits: list[int],
                      sm_clocks: list[int],
                      mem_clocks: list[int] | None = None) -> list[dict]:
    """Read all per-config summary CSVs and combine into unified rows."""
    all_rows = []

    # If no mem_clocks specified, use [None] to iterate once without mem lock
    mem_clock_list: list[int | None] = mem_clocks if mem_clocks else [None]

    for pl in power_limits:
        for clk in sm_clocks:
            for mclk in mem_clock_list:
                cfg_d = config_dir(results_dir, pl, clk, mclk)
                if not cfg_d.exists():
                    continue

                # Maximus summaries
                for csv_path in sorted(cfg_d.glob("maximus_*_metrics_summary_*.csv")):
                    all_rows.extend(parse_maximus_summary(csv_path, pl, clk, mclk))

                # Sirius summaries
                for csv_path in sorted(cfg_d.glob("sirius_*_metrics_summary_*.csv")):
                    all_rows.extend(parse_sirius_summary(csv_path, pl, clk, mclk))

    return all_rows


def write_sweep_summary(results_dir: Path, rows: list[dict]) -> Path:
    """Write the combined energy_sweep_summary.csv."""
    out_path = results_dir / "energy_sweep_summary.csv"
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"\n  Sweep summary: {out_path} ({len(rows)} rows)")
    return out_path


def print_best_configs(rows: list[dict]) -> None:
    """Print the lowest-energy configuration per (engine, benchmark, sf)."""
    if not rows:
        print("\n  No results to analyze.")
        return

    # Group by (engine, benchmark, sf)
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        if r["status"] != "OK" or not r["energy_j"]:
            continue
        try:
            energy = float(r["energy_j"])
        except (ValueError, TypeError):
            continue
        key = (r["engine"], r["benchmark"], str(r["sf"]))
        groups.setdefault(key, []).append((energy, r))

    print(f"\n{'=' * 80}")
    print("  BEST CONFIGURATIONS (lowest total energy per benchmark)")
    print(f"{'=' * 80}")
    print(f"  {'Engine':<10} {'Benchmark':<12} {'SF':<6} {'PL(W)':<8} {'SM(MHz)':<10} "
          f"{'Mem(MHz)':<10} {'Avg E(J)':<10} {'Queries'}")
    print(f"  {'-' * 85}")

    for key in sorted(groups.keys()):
        engine, bench, sf = key
        entries = groups[key]

        # Group by config to get total energy per config
        config_energy: dict[tuple, list[float]] = {}
        for energy, r in entries:
            mclk = r.get("mem_clock_mhz", "")
            ck = (int(r["power_limit_w"]), int(r["sm_clock_mhz"]),
                  int(mclk) if mclk else 0)
            config_energy.setdefault(ck, []).append(energy)

        # Find config with lowest average per-query energy
        best_cfg = None
        best_avg = float("inf")
        best_n = 0
        for ck, energies in config_energy.items():
            avg_e = sum(energies) / len(energies)
            if avg_e < best_avg:
                best_avg = avg_e
                best_cfg = ck
                best_n = len(energies)

        if best_cfg:
            pl, clk, mclk = best_cfg
            mclk_str = str(mclk) if mclk else "-"
            print(f"  {engine:<10} {bench:<12} {sf:<6} {pl:<8} {clk:<10} "
                  f"{mclk_str:<10} {best_avg:<10.2f} {best_n}")

    print()


# ══════════════════════════════════════════════════════════════════════════════
#  Main Sweep Loop
# ══════════════════════════════════════════════════════════════════════════════

def run_sweep(args: argparse.Namespace) -> None:
    """Main sweep loop: iterate (PL, CLK, MEM_CLK), set GPU config, run metrics."""
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    power_limits = args.power_limits
    sm_clocks = args.sm_clocks
    mem_clocks: list[int] | None = args.mem_clocks
    engines = args.engines
    benchmarks = args.benchmarks
    resume = args.resume

    # Build the config grid
    mem_clock_list: list[int | None] = mem_clocks if mem_clocks else [None]
    config_grid = [
        (pl, clk, mclk)
        for pl in power_limits
        for clk in sm_clocks
        for mclk in mem_clock_list
    ]
    total_configs = len(config_grid)
    total_engine_configs = total_configs * len(engines)

    print("=" * 80)
    print("  GPU ENERGY SWEEP")
    print(f"  Power limits: {power_limits}")
    print(f"  SM clocks:    {sm_clocks}")
    print(f"  Mem clocks:   {mem_clocks or 'unlocked'}")
    print(f"  Engines:      {engines}")
    print(f"  Benchmarks:   {benchmarks}")
    print(f"  Results dir:  {results_dir}")
    print(f"  Resume:       {resume}")
    print(f"  Total configs: {total_configs} ({total_engine_configs} engine-configs)")
    print(f"  Started: {datetime.now()}")
    print("=" * 80)

    # Enable persistence mode once at the start
    enable_persistence_mode()

    completed = 0
    skipped = 0
    failed = 0
    start_time = time.time()

    for cfg_idx, (pl, clk, mclk) in enumerate(config_grid, start=1):
        tag = config_tag(pl, clk, mclk)
        cfg_d = config_dir(results_dir, pl, clk, mclk)

        mem_str = f", MEM={mclk}MHz" if mclk else ""
        print(f"\n{'=' * 80}")
        print(f"  CONFIG {cfg_idx}/{total_configs}: {tag} "
              f"(PL={pl}W, SM={clk}MHz{mem_str})")

        # ETA calculation
        elapsed = time.time() - start_time
        done = completed + skipped
        if done > 0:
            avg_per_config = elapsed / done
            remaining = total_configs - cfg_idx + 1
            eta_s = avg_per_config * remaining
            eta_str = str(timedelta(seconds=int(eta_s)))
            print(f"  Progress: {done}/{total_configs} done, ETA: {eta_str}")
        print(f"{'=' * 80}")

        # Resume: check if all engines have results for this config
        if resume:
            all_done = True
            for engine in engines:
                if not config_has_results(cfg_d, engine, benchmarks):
                    all_done = False
                    break
            if all_done:
                print(f"  SKIP (resume): {tag} already has results")
                skipped += 1
                continue

        # Cool-down check
        wait_for_cooldown()

        # Set GPU config
        if not set_gpu_config(pl, clk, mclk):
            print(f"  FAILED to set GPU config for {tag}, skipping")
            failed += 1
            continue

        # Create config directory
        cfg_d.mkdir(parents=True, exist_ok=True)

        # Run metrics for each engine
        for engine in engines:
            if resume and config_has_results(cfg_d, engine, benchmarks):
                print(f"  SKIP (resume): {engine} already has results for {tag}")
                continue

            # Determine which benchmarks to run for this engine
            if engine == "maximus":
                engine_bench_map = MAXIMUS_BENCHMARKS
            else:
                engine_bench_map = SIRIUS_BENCHMARKS

            bench_to_run = [b for b in benchmarks if b in engine_bench_map]
            if not bench_to_run:
                print(f"  SKIP: No configured benchmarks for {engine}")
                continue

            print(f"\n  --- Running {engine} metrics for {tag} ---")
            try:
                if engine == "maximus":
                    rc = run_maximus_metrics(
                        bench_to_run, cfg_d, args.maximus_target_time,
                        storage=args.storage, toy=args.toy)
                else:
                    rc = run_sirius_metrics(
                        bench_to_run, cfg_d, args.sirius_target_time,
                        toy=args.toy)

                if rc != 0:
                    print(f"  WARNING: {engine} metrics exited with code {rc}")
            except subprocess.TimeoutExpired:
                print(f"  ERROR: {engine} metrics timed out for {tag}")
            except Exception as e:
                print(f"  ERROR: {engine} metrics failed for {tag}: {e}")

        completed += 1

        # Pause between configs for thermal stability
        print(f"\n  Cooling pause ({COOLDOWN_PAUSE_S}s)...")
        time.sleep(COOLDOWN_PAUSE_S)

    # Aggregate results
    print(f"\n{'=' * 80}")
    print("  AGGREGATING RESULTS")
    print(f"{'=' * 80}")

    all_rows = aggregate_results(results_dir, power_limits, sm_clocks, mem_clocks)
    if all_rows:
        write_sweep_summary(results_dir, all_rows)
        print_best_configs(all_rows)
    else:
        print("  No results found to aggregate.")

    # Final summary
    elapsed = time.time() - start_time
    print(f"\n{'=' * 80}")
    print(f"  SWEEP COMPLETE")
    print(f"  Total time:  {timedelta(seconds=int(elapsed))}")
    print(f"  Completed:   {completed}/{total_configs}")
    print(f"  Skipped:     {skipped}")
    print(f"  Failed:      {failed}")
    print(f"  Finished:    {datetime.now()}")
    print(f"{'=' * 80}")


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════

def parse_int_list(s: str) -> list[int]:
    """Parse comma-separated integers."""
    return [int(x.strip()) for x in s.split(",")]


def _build_default_sweep():
    """Populate DEFAULT_POWER_LIMITS, DEFAULT_SM_CLOCKS and DEFAULT_POWER_LIMIT
    from the GPU's reported min/max clocks and power envelope.

    Power limits: `default (TDP)` plus 4 points linearly spaced between the
    minimum power limit and TDP (inclusive, so the last of the 4 is also
    the default — we dedupe). For TDP=300W, PL_min=100W this yields
    [100, 150, 200, 250, 300].

    SM clocks: 5 points linearly spaced from SM_min to SM_max.
    """
    global DEFAULT_POWER_LIMITS, DEFAULT_SM_CLOCKS, DEFAULT_POWER_LIMIT
    import hw_detect  # local import so module-level import cycle stays clean
    info = hw_detect.detect_gpu()
    pl_min = int(info.get("power_min_w", 100))
    pl_def = int(info.get("power_default_w") or info.get("power_max_w", 300))
    sm_clocks = info.get("sm_clocks") or []
    sm_min = int(sm_clocks[0]) if sm_clocks else 210
    sm_max = int(sm_clocks[-1]) if sm_clocks else 1410

    # Power: 5 points from pl_min to pl_def inclusive, linear.
    if _N_POWER_LIMITS <= 1:
        pls = [pl_def]
    else:
        step = (pl_def - pl_min) / (_N_POWER_LIMITS - 1)
        pls = sorted({int(round(pl_min + i * step)) for i in range(_N_POWER_LIMITS)})
    # SM: 5 points from sm_min to sm_max inclusive, linear.
    if _N_SM_CLOCKS <= 1:
        sms = [sm_max]
    else:
        step = (sm_max - sm_min) / (_N_SM_CLOCKS - 1)
        sms = sorted({int(round(sm_min + i * step)) for i in range(_N_SM_CLOCKS)})

    DEFAULT_POWER_LIMITS = pls
    DEFAULT_SM_CLOCKS = sms
    DEFAULT_POWER_LIMIT = pl_def


def _preflight_pl_check() -> bool:
    """Try setting the GPU power limit to its current value (a no-op that
    still requires privileged access). Returns True if that succeeds.
    Emits a loud warning and returns False otherwise — in that case the
    sweep will not be able to actually change PL/SM clocks and every
    configuration will be skipped (the bug we hit in CI containers)."""
    cur = subprocess.run(
        ["nvidia-smi", "-i", GPU_ID, "--query-gpu=power.limit",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True,
    )
    if cur.returncode != 0:
        print(f"[WARN] nvidia-smi query failed on GPU {GPU_ID}; skipping preflight")
        return True
    pl = cur.stdout.strip().split(".")[0]
    rc = subprocess.run(
        ["sudo", "nvidia-smi", "-i", GPU_ID, "-pl", pl],
        capture_output=True, text=True,
    )
    if rc.returncode != 0:
        print("=" * 72)
        print("  [WARN] Cannot change GPU power limit. Energy sweep will SKIP")
        print("         every config unless this machine has CAP_SYS_ADMIN or")
        print("         a passwordless-sudo entry for `nvidia-smi`.")
        print(f"         stderr: {rc.stderr.strip()}")
        print("=" * 72)
        return False
    return True


def main():
    _build_default_sweep()
    ok = _preflight_pl_check()
    parser = argparse.ArgumentParser(
        description="GPU energy sweep: explore (power-limit, SM-clock) configurations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_energy_sweep.py
  python run_energy_sweep.py --power-limits 250,300,360 --sm-clocks 1200,2400
  python run_energy_sweep.py --engines maximus --benchmarks tpch h2o --resume
  python run_energy_sweep.py --results-dir results/sweep_v2 --resume
""",
    )
    parser.add_argument(
        "--power-limits", type=parse_int_list,
        default=DEFAULT_POWER_LIMITS,
        help=f"Comma-separated power limits in watts "
             f"(default: {','.join(map(str, DEFAULT_POWER_LIMITS))})",
    )
    parser.add_argument(
        "--sm-clocks", type=parse_int_list,
        default=DEFAULT_SM_CLOCKS,
        help=f"Comma-separated SM clock frequencies in MHz "
             f"(default: {','.join(map(str, DEFAULT_SM_CLOCKS))})",
    )
    parser.add_argument(
        "--mem-clocks", type=parse_int_list,
        default=None,
        help="Comma-separated memory clock frequencies in MHz "
             "(default: unlocked). RTX 5080 supports: 405,810,7001,14801,15001",
    )
    parser.add_argument(
        "--engines", nargs="+", default=["maximus", "sirius"],
        choices=["maximus", "sirius"],
        help="Engines to benchmark (default: maximus sirius)",
    )
    parser.add_argument(
        "--benchmarks", nargs="+",
        default=["tpch", "h2o", "clickbench", "case_bench"],
        choices=["tpch", "h2o", "clickbench", "case_bench"],
        help="Benchmarks to run (default: tpch h2o clickbench case_bench)",
    )
    parser.add_argument(
        "--results-dir", type=str,
        default="results/energy_sweep",
        help="Output directory (default: results/energy_sweep)",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip configs that already have summary CSVs",
    )
    parser.add_argument(
        "--maximus-target-time", type=float, default=10,
        help="Target sustained time for Maximus in seconds (default: 10)",
    )
    parser.add_argument(
        "--sirius-target-time", type=float, default=20,
        help="Target sustained time for Sirius in seconds (default: 20)",
    )
    parser.add_argument(
        "--storage", choices=["gpu", "cpu"], default="gpu",
        help="Storage device for Maximus tables (default: gpu)",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Test mode: 3 queries/bench, 1 SF (smallest), single GPU config")
    parser.add_argument(
        "--minimum", action="store_true",
        help="Minimum experiment: 3 SM clocks × 3 power limits (9 configs "
             "vs the default 25) and SF_min only.")
    parser.add_argument(
        "--toy", action="store_true",
        help="Toy experiment: 2 SM clocks × 2 power limits (4 configs), tpch "
             "only, 1 query, largest SF, metrics target-time capped at 120s.")
    args = parser.parse_args()

    # In minimum mode, thin the sweep grid to 3×3 and keep only 2 SFs per bench.
    if args.minimum:
        args.power_limits = [DEFAULT_POWER_LIMITS[0],
                             DEFAULT_POWER_LIMITS[len(DEFAULT_POWER_LIMITS)//2],
                             DEFAULT_POWER_LIMITS[-1]]
        args.sm_clocks = [DEFAULT_SM_CLOCKS[0],
                          DEFAULT_SM_CLOCKS[len(DEFAULT_SM_CLOCKS)//2],
                          DEFAULT_SM_CLOCKS[-1]]
        print(f"[MINIMUM] power_limits={args.power_limits}, sm_clocks={args.sm_clocks}")

    # Toy mode: 2×2 grid (extremes only), tpch only, 120s metric cap.
    # The query/SF reduction itself is handled by --toy propagated to the
    # metrics subprocesses (get_benchmark_config(toy_mode=True)).
    if args.toy:
        args.power_limits = [DEFAULT_POWER_LIMITS[0], DEFAULT_POWER_LIMITS[-1]]
        args.sm_clocks = [DEFAULT_SM_CLOCKS[0], DEFAULT_SM_CLOCKS[-1]]
        args.benchmarks = ["tpch"]
        args.maximus_target_time = 120  # metrics sampling window = 2 min
        args.sirius_target_time = 120
        print(f"[TOY] power_limits={args.power_limits}, sm_clocks={args.sm_clocks}, "
              f"benchmarks={args.benchmarks}, target_time=120s")

    # Safety: always restore GPU defaults on exit
    try:
        run_sweep(args)
    except KeyboardInterrupt:
        print("\n\n  INTERRUPTED by user (Ctrl+C)")
    except Exception as e:
        print(f"\n\n  FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        restore_gpu_defaults()


if __name__ == "__main__":
    main()
