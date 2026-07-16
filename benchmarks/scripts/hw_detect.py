#!/usr/bin/env python3
"""Unified hardware detection and control module for benchmark scripts.

Provides GPU and CPU detection, frequency/power control, and benchmark
configuration. All benchmark scripts should import from this module instead
of duplicating hardware-specific logic.

Usage as a library:
    from hw_detect import detect_gpu, get_benchmark_config, set_gpu_power_limit

Usage standalone (prints detected hardware):
    python hw_detect.py
"""
from __future__ import annotations

import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
MAXIMUS_DIR = SCRIPT_DIR.parent.parent  # benchmarks/scripts -> project root


# ══════════════════════════════════════════════════════════════════════════════
#  GPU Detection
# ══════════════════════════════════════════════════════════════════════════════

def _run_nvidia_smi(args: list[str]) -> str:
    """Run nvidia-smi with the given arguments and return stdout."""
    result = subprocess.run(
        ["nvidia-smi"] + args,
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"nvidia-smi {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _parse_supported_sm_clocks(gpu_id: int) -> list[int]:
    """Query all supported graphics (SM) clocks for the given GPU.

    Returns a sorted list of clock frequencies in MHz (ascending).
    """
    output = _run_nvidia_smi([
        "-i", str(gpu_id),
        "--query-supported-clocks=gr",
        "--format=csv,noheader,nounits",
    ])
    clocks = sorted({int(line.strip()) for line in output.splitlines() if line.strip()})
    return clocks


def detect_gpu() -> dict[str, Any]:
    """Auto-detect the GPU with the most VRAM.

    If the MAXIMUS_GPU_ID environment variable is set, that GPU index is
    selected instead (used by run_all_benchmarks.sh --parallel to pin each
    worker's compute, nvidia-smi sampling, and clock control to one GPU).

    Returns a dict with keys:
        index: int -- GPU index
        name: str -- GPU product name
        vram_mb: int -- total VRAM in MiB
        power_min_w: int -- minimum power limit in watts
        power_max_w: int -- maximum power limit in watts
        power_default_w: int -- default power limit in watts
        sm_clocks: list[int] -- all supported SM clocks in MHz (ascending)
    """
    # Query all GPUs for VRAM.
    output = _run_nvidia_smi([
        "--query-gpu=index,name,memory.total,power.min_limit,"
        "power.max_limit,power.default_limit",
        "--format=csv,noheader,nounits",
    ])

    pinned = os.environ.get("MAXIMUS_GPU_ID")
    best = None
    for line in output.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        idx = int(parts[0])
        name = parts[1]
        vram = int(float(parts[2]))
        pmin = int(float(parts[3]))
        pmax = int(float(parts[4]))
        pdefault = int(float(parts[5]))
        if pinned is not None and idx != int(pinned):
            continue
        if best is None or vram > best["vram_mb"]:
            best = {
                "index": idx,
                "name": name,
                "vram_mb": vram,
                "power_min_w": pmin,
                "power_max_w": pmax,
                "power_default_w": pdefault,
            }

    if best is None:
        if pinned is not None:
            raise RuntimeError(f"MAXIMUS_GPU_ID={pinned} not found by nvidia-smi.")
        raise RuntimeError("No NVIDIA GPUs detected by nvidia-smi.")

    best["sm_clocks"] = _parse_supported_sm_clocks(best["index"])
    return best


# ══════════════════════════════════════════════════════════════════════════════
#  GPU Power / Clock Level Generation
# ══════════════════════════════════════════════════════════════════════════════

def gpu_power_levels(gpu_info: dict[str, Any], n: int = 8) -> list[int]:
    """Generate *n* evenly spaced power limits from min to max (inclusive).

    Values are rounded to the nearest integer watt.
    """
    pmin = gpu_info["power_min_w"]
    pmax = gpu_info["power_max_w"]
    if n <= 1:
        return [pmax]
    step = (pmax - pmin) / (n - 1)
    return [round(pmin + i * step) for i in range(n)]


def gpu_sm_clock_levels(gpu_info: dict[str, Any], n: int = 8) -> list[int]:
    """Generate *n* evenly spaced SM clock levels from min to max supported.

    Each returned value is snapped to the nearest actually-supported clock.
    """
    clocks = gpu_info["sm_clocks"]
    if not clocks:
        raise ValueError("No supported SM clocks found.")
    cmin, cmax = clocks[0], clocks[-1]
    if n <= 1:
        return [cmax]

    step = (cmax - cmin) / (n - 1)
    targets = [cmin + i * step for i in range(n)]

    # Snap each target to the nearest supported clock.
    result: list[int] = []
    for t in targets:
        nearest = min(clocks, key=lambda c: abs(c - t))
        if not result or nearest != result[-1]:
            result.append(nearest)
        else:
            # Avoid duplicates: pick next higher if available.
            candidates = [c for c in clocks if c > result[-1]]
            if candidates:
                result.append(candidates[0])
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  CPU Frequency Detection and Levels
# ══════════════════════════════════════════════════════════════════════════════

def _detect_cpu_governor() -> str:
    """Detect the active CPU frequency scaling driver.

    Returns one of: "intel_pstate", "amd-pstate", "acpi-cpufreq", "unknown".
    """
    pstate_path = Path("/sys/devices/system/cpu/intel_pstate")
    if pstate_path.is_dir():
        return "intel_pstate"

    driver_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_driver")
    if driver_path.exists():
        driver = driver_path.read_text().strip()
        if "amd" in driver.lower():
            return "amd-pstate"
        if driver == "acpi-cpufreq":
            return "acpi-cpufreq"
        return driver

    return "unknown"


def detect_cpu() -> dict[str, Any]:
    """Detect CPU frequency range and governor.

    Returns a dict with keys:
        governor: str -- scaling driver name
        min_freq_khz: int -- minimum frequency in kHz
        max_freq_khz: int -- maximum frequency in kHz
        num_cores: int -- number of online CPU cores
    """
    governor = _detect_cpu_governor()
    min_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq")
    max_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")

    min_khz = int(min_path.read_text().strip()) if min_path.exists() else 0
    max_khz = int(max_path.read_text().strip()) if max_path.exists() else 0

    # Count online CPUs.
    num_cores = 0
    cpu_base = Path("/sys/devices/system/cpu/")
    if cpu_base.exists():
        for entry in cpu_base.iterdir():
            if re.match(r"cpu\d+$", entry.name) and (entry / "cpufreq").is_dir():
                num_cores += 1

    return {
        "governor": governor,
        "min_freq_khz": min_khz,
        "max_freq_khz": max_khz,
        "num_cores": num_cores,
    }


def cpu_freq_levels(cpu_info: dict[str, Any], n: int = 8) -> list[int]:
    """Generate *n* evenly spaced CPU frequencies (kHz) from min to max."""
    fmin = cpu_info["min_freq_khz"]
    fmax = cpu_info["max_freq_khz"]
    if n <= 1:
        return [fmax]
    step = (fmax - fmin) / (n - 1)
    return [round(fmin + i * step) for i in range(n)]


# ══════════════════════════════════════════════════════════════════════════════
#  GPU Control Functions
# ══════════════════════════════════════════════════════════════════════════════

def _sudo_nvidia_smi(args: list[str]) -> subprocess.CompletedProcess:
    """Run nvidia-smi under sudo."""
    return subprocess.run(
        ["sudo", "nvidia-smi"] + args,
        capture_output=True, text=True, timeout=10,
    )


def set_gpu_power_limit(gpu_id: int, watts: int) -> bool:
    """Set GPU power limit in watts. Returns True on success."""
    r = _sudo_nvidia_smi(["-i", str(gpu_id), "-pl", str(watts)])
    if r.returncode != 0:
        # Check if already at the target power limit
        try:
            q = subprocess.run(
                ["nvidia-smi", "-i", str(gpu_id),
                 "--query-gpu=power.limit", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            if q.returncode == 0 and abs(float(q.stdout.strip()) - watts) <= 1.0:
                return True  # already at target
        except Exception:
            pass
        print(f"[hw_detect] WARNING: set power limit {watts}W failed: "
              f"{r.stderr.strip()}")
        return False
    return True


def set_gpu_sm_clock(gpu_id: int, mhz: int) -> bool:
    """Lock GPU SM clock to the given frequency. Returns True on success."""
    r = _sudo_nvidia_smi([
        "-i", str(gpu_id),
        f"--lock-gpu-clocks={mhz},{mhz}",
    ])
    if r.returncode != 0:
        print(f"[hw_detect] WARNING: lock SM clock {mhz}MHz failed: "
              f"{r.stderr.strip()}")
        return False
    return True


def reset_gpu_clocks(gpu_id: int) -> bool:
    """Reset GPU clocks to default (unlock). Returns True on success."""
    r = _sudo_nvidia_smi(["-i", str(gpu_id), "-rgc"])
    if r.returncode != 0:
        print(f"[hw_detect] WARNING: reset GPU clocks failed: "
              f"{r.stderr.strip()}")
        return False
    return True


def restore_gpu_defaults(gpu_id: int, default_pl_w: int | None = None) -> bool:
    """Restore GPU to default power limit and unlock clocks.

    If default_pl_w is None, detect_gpu() is called to find the default.
    """
    if default_pl_w is None:
        try:
            gpu = detect_gpu()
            default_pl_w = gpu["power_default_w"]
        except RuntimeError:
            default_pl_w = None

    ok = reset_gpu_clocks(gpu_id)
    if default_pl_w is not None:
        ok = set_gpu_power_limit(gpu_id, default_pl_w) and ok
    return ok


# ══════════════════════════════════════════════════════════════════════════════
#  CPU Control Functions
# ══════════════════════════════════════════════════════════════════════════════

def _sudo_write(path: str, value: str) -> bool:
    """Write a value to a sysfs file via sudo."""
    r = subprocess.run(
        ["sudo", "bash", "-c", f"echo {value} > {path}"],
        capture_output=True, text=True, timeout=5,
    )
    return r.returncode == 0


def set_cpu_freq(freq_khz: int) -> bool:
    """Set the maximum CPU frequency on all cores.

    For intel_pstate: sets max_perf_pct and disables turbo.
    For amd-pstate / acpi-cpufreq: sets scaling_max_freq on all cores.

    Returns True on success.
    """
    governor = _detect_cpu_governor()

    if governor == "intel_pstate":
        max_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
        max_khz = int(max_path.read_text().strip()) if max_path.exists() else 1
        pct = max(1, min(100, round(100 * freq_khz / max_khz)))
        ok = _sudo_write(
            "/sys/devices/system/cpu/intel_pstate/max_perf_pct", str(pct)
        )
        # Disable turbo when capping frequency below 100%.
        if pct < 100:
            ok = _sudo_write(
                "/sys/devices/system/cpu/intel_pstate/no_turbo", "1"
            ) and ok
        else:
            ok = _sudo_write(
                "/sys/devices/system/cpu/intel_pstate/no_turbo", "0"
            ) and ok
        return ok

    # amd-pstate, acpi-cpufreq, or other: write scaling_max_freq on each core.
    cpu_base = Path("/sys/devices/system/cpu/")
    ok = True
    for entry in sorted(cpu_base.iterdir()):
        if not re.match(r"cpu\d+$", entry.name):
            continue
        freq_path = entry / "cpufreq" / "scaling_max_freq"
        if freq_path.exists():
            ok = _sudo_write(str(freq_path), str(freq_khz)) and ok
    return ok


def reset_cpu_freq() -> bool:
    """Reset CPU frequency to maximum / default settings.

    For intel_pstate: sets max_perf_pct=100 and no_turbo=0.
    For amd-pstate / acpi-cpufreq: sets scaling_max_freq to cpuinfo_max_freq.

    Returns True on success.
    """
    governor = _detect_cpu_governor()

    if governor == "intel_pstate":
        ok = _sudo_write(
            "/sys/devices/system/cpu/intel_pstate/max_perf_pct", "100"
        )
        ok = _sudo_write(
            "/sys/devices/system/cpu/intel_pstate/no_turbo", "0"
        ) and ok
        return ok

    # amd-pstate / acpi-cpufreq: reset each core to cpuinfo_max_freq.
    max_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    if not max_path.exists():
        return False
    max_khz = max_path.read_text().strip()

    cpu_base = Path("/sys/devices/system/cpu/")
    ok = True
    for entry in sorted(cpu_base.iterdir()):
        if not re.match(r"cpu\d+$", entry.name):
            continue
        freq_path = entry / "cpufreq" / "scaling_max_freq"
        if freq_path.exists():
            ok = _sudo_write(str(freq_path), max_khz) and ok
    return ok


# ══════════════════════════════════════════════════════════════════════════════
#  Buffer Init SQL
# ══════════════════════════════════════════════════════════════════════════════

def buffer_init_sql(vram_mb: int) -> str:
    """Return the Sirius gpu_buffer_init SQL call sized for the given VRAM.

    Workload-driven sizing (based on measured worst case in this repo):
      - largest cached base table  : ~20 GB  (ClickBench SF=20 hits)
      - largest live processing set: ~28 GB  (TPC-H SF=20 q3 / SF=10 q3
                                             4-way joins on lineitem+orders;
                                             cuDF managed-memory peak ~57 GB
                                             includes the cached base, so live
                                             intermediates are ~28-30 GB)

    Big GPUs (>= 64 GB VRAM): pinned at 24 GB cache + 32 GB processing
    (= 56 GB total), leaving ~24 GB headroom on an 80 GB A100/H100 for
    CUDA context, RMM internal book-keeping, and short-lived spill.

    Smaller GPUs (5080 16 GB, 5090 32 GB): scale down keeping ≤ 85% of VRAM
    allocated; cache tables that don't fit spill to pinned host memory
    automatically (gpu_buffer_manager.cpp:186-194).

    Why not the old 70%/35% policy: on 80 GB A100 it asked for 56 + 28 = 84 GB,
    which exceeds physical VRAM, forcing RMM to clip. The pool-init jump was
    also being misread as a per-rep memory leak by the metrics scripts.
    """
    vram_gb = vram_mb / 1024.0
    # Reserve ~15% for CUDA context + RMM internal + transient overflow.
    budget_gb = max(2, int(vram_gb * 0.85))
    # Cache is bounded by the largest base table (~20 GB); processing gets the
    # rest, capped at 32 GB so we don't over-allocate on huge GPUs.
    cache_gb = min(24, max(1, int(budget_gb * 0.4)))
    processing_gb = min(32, max(1, budget_gb - cache_gb))
    return f'call gpu_buffer_init("{cache_gb} GB", "{processing_gb} GB");'


# ══════════════════════════════════════════════════════════════════════════════
#  Benchmark Query Configs
# ══════════════════════════════════════════════════════════════════════════════

# TPC-H: 22 queries (q1-q22).
_TPCH_QUERIES = [f"q{i}" for i in range(1, 23)]

# H2O: 9 queries (q8 unimplemented).
_H2O_QUERIES = [f"q{i}" for i in [1, 2, 3, 4, 5, 6, 7, 9, 10]]

# ClickBench <100GB: 39 queries, excluding q18, q27, q28, q42.
_CLICKBENCH_EXCLUDE_SMALL = {18, 27, 28, 42}
_CLICKBENCH_QUERIES_SMALL = [
    f"q{i}" for i in range(0, 43) if i not in _CLICKBENCH_EXCLUDE_SMALL
]

# ClickBench >=100GB: all 43 queries (q0-q42).
_CLICKBENCH_QUERIES_LARGE = [f"q{i}" for i in range(0, 43)]

# Microbench TPC-H: 53 queries.
# Removed w5b_045, w5b_047: cuDF column size limit overflow on join results.
_MICROBENCH_TPCH_QUERIES = [
    "w1_002", "w1_004", "w1_005", "w1_006", "w1_007", "w1_008", "w1_011",
    "w2_003", "w2_012", "w2_013", "w2_014", "w2_015", "w2_016", "w2_017",
    "w3_001", "w3_009", "w3_010", "w3_023", "w3_024", "w3_025", "w3_028",
    "w4_033", "w4_052", "w4_053", "w4_054", "w4_055", "w4_057", "w4_059",
    "w5a_029", "w5a_034", "w5a_035", "w5a_036", "w5a_037", "w5a_038",
    "w5a_048", "w5a_049", "w5a_050", "w5a_056",
    "w5b_030", "w5b_039", "w5b_040", "w5b_041", "w5b_042", "w5b_043",
    "w5b_044", "w5b_051",
    "w6_020", "w6_021", "w6_026", "w6_031", "w6_032", "w6_046", "w6_060",
]

# Microbench H2O: 34 queries.
# Removed w4_031: large_string type unsupported by cuDF at SF=10gb.
_MICROBENCH_H2O_QUERIES = [
    "w1_001", "w1_002", "w1_003", "w1_004", "w1_005", "w1_006", "w1_007",
    "w2_008", "w2_009", "w2_010", "w2_011", "w2_012", "w2_013", "w2_014",
    "w3_016", "w3_017", "w3_018", "w3_019", "w3_020", "w3_021", "w3_023",
    "w4_027", "w4_028", "w4_029", "w4_030", "w4_032", "w4_033",
    "w6_015", "w6_022", "w6_024", "w6_025", "w6_026", "w6_034", "w6_035",
]

# Microbench ClickBench: 29 queries.
_MICROBENCH_CLICKBENCH_QUERIES = [
    "w1_001", "w1_002", "w1_003", "w1_004", "w1_005", "w1_006",
    "w2_007", "w2_008", "w2_009", "w2_010", "w2_018", "w2_019",
    "w3_011", "w3_012", "w3_013", "w3_014", "w3_015", "w3_027",
    "w4_021", "w4_022", "w4_023", "w4_024", "w4_025", "w4_058",
    "w6_016", "w6_017", "w6_018", "w6_019", "w6_020", "w6_022",
]

# case_bench: curated queries where GPU energy advantage is expected to be
# weak (tiny tables, point lookups, small-cardinality group-bys, narrow
# top-N). Uses TPC-H tables — no dedicated data generation.
_CASE_BENCH_QUERIES = ["q1", "q2", "q3", "q4", "q5"]

# Test-mode queries (3 per benchmark for quick validation).
_TEST_QUERIES = {
    "tpch": ["q1", "q3", "q6"],
    "h2o": ["q1", "q3", "q5"],
    "clickbench": ["q1", "q6", "q20"],
    "case_bench": ["q1", "q2", "q3"],
    "microbench_tpch": ["w1_002", "w3_001", "w5a_029"],
    "microbench_h2o": ["w1_001", "w3_016", "w6_022"],
    "microbench_clickbench": ["w1_001", "w3_011", "w6_016"],
}


def _build_benchmarks(large_gpu: bool, test_mode: bool) -> dict[str, dict]:
    """Build the BENCHMARKS configuration dict.

    Args:
        large_gpu: True if GPU has >= 100 GB VRAM.
        test_mode: If True, use reduced query lists for quick validation.
    """
    clickbench_queries = (_CLICKBENCH_QUERIES_LARGE if large_gpu
                          else _CLICKBENCH_QUERIES_SMALL)

    # ClickBench SF = target CSV data size in GB.
    # SF=1 (≈1GB / 1.4%), SF=5 (≈5GB / 7.1%), SF=10 (≈10GB / 14.3%), SF=20 (≈20GB / 28.6%).
    _CLICKBENCH_SFS = [1, 5, 10, 20]

    benchmarks: dict[str, dict] = {
        "tpch": {
            "scale_factors": [1, 5, 10, 20],
            "queries": (_TEST_QUERIES["tpch"] if test_mode
                        else list(_TPCH_QUERIES)),
        },
        "h2o": {
            "scale_factors": ["1gb", "2gb", "4gb", "8gb"],
            "queries": (_TEST_QUERIES["h2o"] if test_mode
                        else list(_H2O_QUERIES)),
        },
        "clickbench": {
            "scale_factors": _CLICKBENCH_SFS,
            "queries": (_TEST_QUERIES["clickbench"] if test_mode
                        else list(clickbench_queries)),
        },
        "case_bench": {
            # Reuses TPC-H data; SF semantics = same as tpch.
            "scale_factors": [1, 5, 10, 20],
            "queries": (_TEST_QUERIES["case_bench"] if test_mode
                        else list(_CASE_BENCH_QUERIES)),
        },
        "microbench_tpch": {
            "scale_factors": [1, 5, 10, 20],
            "queries": (_TEST_QUERIES["microbench_tpch"] if test_mode
                        else list(_MICROBENCH_TPCH_QUERIES)),
        },
        "microbench_h2o": {
            "scale_factors": ["1gb", "2gb", "4gb", "8gb"],
            "queries": (_TEST_QUERIES["microbench_h2o"] if test_mode
                        else list(_MICROBENCH_H2O_QUERIES)),
        },
        "microbench_clickbench": {
            "scale_factors": _CLICKBENCH_SFS,
            "queries": (_TEST_QUERIES["microbench_clickbench"] if test_mode
                        else list(_MICROBENCH_CLICKBENCH_QUERIES)),
        },
    }
    return benchmarks


def get_benchmark_config(
    vram_mb: int,
    test_mode: bool = False,
    minimum_mode: bool = False,
    toy_mode: bool = False,
) -> dict[str, dict]:
    """Return benchmark configuration appropriate for the given GPU VRAM.

    Args:
        vram_mb: GPU VRAM in MiB.
        test_mode: If True, use 3 queries per benchmark for quick validation.
        minimum_mode: If True, restrict to SF_min + SF_max only (2 SFs),
            reuse TEST_QUERIES, and drop every microbench_* benchmark.
            Designed so the whole A+B+C pipeline fits in ~8 hours.
        toy_mode: Smallest possible end-to-end smoke of the A+B+C pipeline.
            Keeps ONLY tpch, a single query, and the single largest SF.
            Intended to exercise every category (A1-C1) in ~1 hour. Implies
            test-query reduction; takes precedence over minimum_mode.
    """
    large_gpu = vram_mb >= (100 * 1024)  # >= 100 GB
    # toy/minimum modes both imply test-query reduction.
    cfg = _build_benchmarks(large_gpu, test_mode or minimum_mode or toy_mode)
    if toy_mode:
        # Only tpch, one query, the single largest SF. This is deliberately
        # the most minimal config that still touches every category.
        tpch = cfg["tpch"]
        tpch["queries"] = list(tpch["queries"])[:1]          # one query (q1)
        tpch["scale_factors"] = [tpch["scale_factors"][-1]]  # largest SF only
        return {"tpch": tpch}
    if minimum_mode:
        # Drop microbench_* entirely.
        for key in list(cfg.keys()):
            if key.startswith("microbench_"):
                del cfg[key]
        # Keep only the smallest and largest SF for each remaining benchmark.
        for key, spec in cfg.items():
            sfs = spec["scale_factors"]
            if len(sfs) >= 2:
                spec["scale_factors"] = [sfs[0], sfs[-1]]
    return cfg


# ══════════════════════════════════════════════════════════════════════════════
#  Data Path Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _base_benchmark_name(benchmark: str) -> str:
    """Map microbench / case_bench names to their base benchmark for data
    paths.

    e.g. "microbench_tpch" -> "tpch", "h2o" -> "h2o",
         "case_bench" -> "tpch" (case_bench shares TPC-H tables).
    """
    if benchmark.startswith("microbench_"):
        return benchmark[len("microbench_"):]
    if benchmark == "case_bench":
        return "tpch"
    return benchmark


def maximus_data_dir(benchmark: str, sf: int | str) -> Path:
    """Return the Maximus CSV data directory for a benchmark and scale factor.

    Example: maximus_data_dir("tpch", 1) -> {MAXIMUS_DIR}/tests/tpch/csv-1
    """
    base = _base_benchmark_name(benchmark)
    return MAXIMUS_DIR / "tests" / base / f"csv-{sf}"


def sirius_db_path(benchmark: str, sf: int | str) -> Path:
    """Return the Sirius DuckDB database file path.

    Example: sirius_db_path("tpch", 1) -> {MAXIMUS_DIR}/tests/tpch_duckdb/tpch_1.duckdb

    Note: clickbench uses "click_duckdb" as its directory prefix (matching
    existing conventions in run_sirius_benchmark.py).
    """
    base = _base_benchmark_name(benchmark)
    dir_name = "click_duckdb" if base == "clickbench" else f"{base}_duckdb"
    db_name = f"{base}_{sf}.duckdb" if base != "tpch" else f"tpch_sf{sf}.duckdb"
    return MAXIMUS_DIR / "tests" / dir_name / db_name


def sirius_query_dir(benchmark: str) -> Path:
    """Return the directory containing Sirius SQL query files.

    Example: sirius_query_dir("tpch")           -> {MAXIMUS_DIR}/tests/tpch_sql/queries/1/
             sirius_query_dir("microbench_tpch") -> {MAXIMUS_DIR}/tests/microbench_tpch_sql/queries/1/
             sirius_query_dir("case_bench")     -> {MAXIMUS_DIR}/tests/case_bench_sql/queries/1/
    """
    if benchmark.startswith("microbench_") or benchmark == "case_bench":
        dir_name = f"{benchmark}_sql"
    else:
        base = _base_benchmark_name(benchmark)
        dir_name = "click_sql" if base == "clickbench" else f"{base}_sql"
    return MAXIMUS_DIR / "tests" / dir_name / "queries" / "1"


# ══════════════════════════════════════════════════════════════════════════════
#  Auto Data Generation
# ══════════════════════════════════════════════════════════════════════════════

_DATA_DIR = MAXIMUS_DIR / "benchmarks" / "data"


def ensure_sirius_db(benchmark: str, sf) -> bool:
    """Generate Sirius DuckDB database if missing. Returns True if DB exists."""
    import subprocess, sys
    base = _base_benchmark_name(benchmark)
    db_path = sirius_db_path(benchmark, sf)
    if db_path.exists():
        return True
    print(f"[GEN] Generating Sirius DuckDB: {benchmark} SF={sf}")
    try:
        if base == "tpch":
            db_dir = MAXIMUS_DIR / "tests" / "tpch_duckdb"
            db_dir.mkdir(parents=True, exist_ok=True)
            cmd = [sys.executable, str(_DATA_DIR / "generate_tpch.py"),
                   "-o", str(db_dir), "--scale-factors", str(sf),
                   "--no-run-query", "--skip-install"]
            subprocess.run(cmd, check=True)
        elif base == "h2o":
            db_dir = MAXIMUS_DIR / "tests" / "h2o_duckdb"
            db_dir.mkdir(parents=True, exist_ok=True)
            cmd = [sys.executable, str(_DATA_DIR / "generate_h2o.py"),
                   "-o", str(db_dir), "--format", "duckdb", str(sf)]
            subprocess.run(cmd, check=True)
        elif base == "clickbench":
            db_dir = MAXIMUS_DIR / "tests" / "click_duckdb"
            db_dir.mkdir(parents=True, exist_ok=True)
            parquet = MAXIMUS_DIR / "tests" / "clickbench" / "clickbench.parquet"
            cmd = [sys.executable, str(_DATA_DIR / "generate_clickbench.py"),
                   "-o", str(db_dir), "--format", "duckdb", "--scales", str(sf)]
            if parquet.exists():
                cmd.extend(["--parquet-path", str(parquet)])
            subprocess.run(cmd, check=True)
    except Exception as e:
        print(f"[WARN] Generation failed for {benchmark} SF={sf}: {e}")
        return False
    return db_path.exists()


def ensure_maximus_csv(benchmark: str, sf) -> bool:
    """Generate Maximus CSV data if missing. Returns True if data exists."""
    import subprocess, sys
    base = _base_benchmark_name(benchmark)
    csv_path = maximus_data_dir(benchmark, sf)
    if csv_path.exists():
        return True
    print(f"[GEN] Generating Maximus CSV: {benchmark} SF={sf}")
    try:
        if base == "tpch":
            # First ensure DuckDB exists, then export CSV
            db_dir = MAXIMUS_DIR / "tests" / "tpch_duckdb"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_file = db_dir / f"tpch_sf{sf}.duckdb"
            if not db_file.exists():
                cmd = [sys.executable, str(_DATA_DIR / "generate_tpch.py"),
                       "-o", str(db_dir), "--scale-factors", str(sf),
                       "--no-run-query", "--skip-install"]
                subprocess.run(cmd, check=True)
            # Export CSV from DuckDB
            import duckdb
            csv_dir = MAXIMUS_DIR / "tests" / "tpch" / f"csv-{sf}"
            csv_dir.mkdir(parents=True, exist_ok=True)
            con = duckdb.connect(str(db_file), read_only=True)
            tables = [r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()]
            for t in tables:
                con.execute(f"COPY {t} TO '{csv_dir / (t + '.csv')}' (HEADER, DELIMITER ',')")
            con.close()
        elif base == "h2o":
            h2o_dir = MAXIMUS_DIR / "tests" / "h2o"
            h2o_dir.mkdir(parents=True, exist_ok=True)
            cmd = [sys.executable, str(_DATA_DIR / "generate_h2o.py"),
                   "-o", str(h2o_dir), "--format", "csv", str(sf)]
            subprocess.run(cmd, check=True)
        elif base == "clickbench":
            cb_dir = MAXIMUS_DIR / "tests" / "clickbench"
            cb_dir.mkdir(parents=True, exist_ok=True)
            parquet = cb_dir / "clickbench.parquet"
            cmd = [sys.executable, str(_DATA_DIR / "generate_clickbench.py"),
                   "-o", str(cb_dir), "--format", "csv", "--scales", str(sf)]
            if parquet.exists():
                cmd.extend(["--parquet-path", str(parquet)])
            subprocess.run(cmd, check=True)
    except Exception as e:
        print(f"[WARN] Generation failed for {benchmark} SF={sf}: {e}")
        return False
    return csv_path.exists()


# ══════════════════════════════════════════════════════════════════════════════
#  Standalone Execution
# ══════════════════════════════════════════════════════════════════════════════

def _print_hardware_info() -> None:
    """Print detected hardware information to stdout."""
    print("=" * 72)
    print("  Hardware Detection Report")
    print("=" * 72)

    # GPU
    try:
        gpu = detect_gpu()
        print(f"\n--- GPU (highest VRAM) ---")
        print(f"  Index:          {gpu['index']}")
        print(f"  Name:           {gpu['name']}")
        print(f"  VRAM:           {gpu['vram_mb']} MiB "
              f"({gpu['vram_mb'] / 1024:.1f} GiB)")
        print(f"  Power limits:   {gpu['power_min_w']}W - {gpu['power_max_w']}W "
              f"(default {gpu['power_default_w']}W)")
        print(f"  SM clocks:      {gpu['sm_clocks'][0]} - "
              f"{gpu['sm_clocks'][-1]} MHz "
              f"({len(gpu['sm_clocks'])} levels)")

        pl = gpu_power_levels(gpu)
        print(f"  Power levels (8): {pl}")

        sm = gpu_sm_clock_levels(gpu)
        print(f"  SM clock levels (8): {sm}")

        print(f"  Buffer init SQL:  {buffer_init_sql(gpu['vram_mb'])}")

        # Benchmark config.
        cfg = get_benchmark_config(gpu["vram_mb"])
        print(f"\n--- Benchmark Config (VRAM={gpu['vram_mb']} MiB) ---")
        for bench, info in cfg.items():
            print(f"  {bench}: SF={info['scale_factors']}, "
                  f"{len(info['queries'])} queries")

    except RuntimeError as exc:
        print(f"\n  GPU detection failed: {exc}")

    # CPU
    print()
    try:
        cpu = detect_cpu()
        print(f"--- CPU ---")
        print(f"  Governor:       {cpu['governor']}")
        print(f"  Freq range:     {cpu['min_freq_khz']} - "
              f"{cpu['max_freq_khz']} kHz "
              f"({cpu['min_freq_khz'] // 1000} - "
              f"{cpu['max_freq_khz'] // 1000} MHz)")
        print(f"  Online cores:   {cpu['num_cores']}")

        fl = cpu_freq_levels(cpu)
        print(f"  Freq levels (8): "
              f"{[f // 1000 for f in fl]} MHz")

    except Exception as exc:
        print(f"  CPU detection failed: {exc}")

    # Data paths (examples).
    print(f"\n--- Data Paths (examples) ---")
    print(f"  Maximus TPCH SF=1:  {maximus_data_dir('tpch', 1)}")
    print(f"  Maximus H2O SF=1gb: {maximus_data_dir('h2o', '1gb')}")
    print(f"  Sirius TPCH SF=1:   {sirius_db_path('tpch', 1)}")
    print(f"  Sirius queries:     {sirius_query_dir('tpch')}")
    print(f"  Microbench TPCH:    {maximus_data_dir('microbench_tpch', 1)}")

    print()


if __name__ == "__main__":
    _print_hardware_info()
