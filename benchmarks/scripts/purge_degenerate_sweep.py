#!/usr/bin/env python3
"""Delete degenerate / smoke-mode Category-C energy-sweep summary files so a
subsequent ``run_energy_sweep.py --resume`` regenerates them at full query count.

Why this is needed
------------------
``--resume`` decides what to (re)run by checking whether a
``{engine}_{bench}_sf{sf}_metrics_summary_*.csv`` file already exists in each
per-config directory. If a smoke run (``--test``) left behind a file with only
the 3 curated test queries (clickbench q1/q6/q20, case_bench q1/q2/q3), resume
treats that (bench, sf, config) as DONE and never fills in the full query set.

This script removes those degenerate files (and their matching ``_samples_``
companions) so resume sees them as missing and re-runs them properly.

A summary is degenerate if ANY of:
  * it has fewer than ``MIN_QUERIES`` query rows (full benchmarks have >=5;
    the smallest, case_bench, has exactly 5 — smoke runs have 3), OR
  * every ``min_ms`` / ``min_s`` value is zero/empty (timing never captured), OR
  * (cross-SF check) at a fixed (engine, benchmark, GPU config) its total query
    energy is materially BELOW the next-smaller scale factor. More data can only
    cost >= energy, so a larger SF measuring cheaper than a smaller one means the
    dataset behind it is short/corrupt (e.g. an A100 h2o csv-4gb generated with
    too few rows). --resume would otherwise re-measure that bad data as "fine";
    purging the summary forces a fresh run once the data itself is rebuilt
    (see validate_h2o_data.py / the --fix flow).

On a clean directory (e.g. the A100 sweep, which simply lacks clickbench/
case_bench files) this is a safe no-op.
"""
from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

MIN_QUERIES = 4  # full benchmarks have >=5 queries; --test smoke runs have 3

# Cross-SF: a larger SF whose total energy is below this fraction of the
# next-smaller SF (at the same config) is treated as backed by short data.
CROSS_SF_TOL = 0.85
CROSS_SF_MIN_QUERIES = 4  # need at least this many shared queries to judge


def _is_zero(v) -> bool:
    try:
        return float(v) == 0.0
    except (TypeError, ValueError):
        return True


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sf_rank(sf: str) -> float:
    """Numeric ordering key for a scale factor like '4gb', '20', '1gb'."""
    m = re.match(r"(\d+(?:\.\d+)?)", str(sf))
    return float(m.group(1)) if m else 0.0


def is_degenerate(path: Path) -> tuple[bool, str]:
    """Return (degenerate?, reason) from this file alone (no cross-SF context)."""
    try:
        rows = list(csv.DictReader(open(path, newline="")))
    except Exception as e:  # unreadable -> treat as degenerate
        return True, f"unreadable ({e})"
    if not rows:
        return True, "empty"
    if len(rows) < MIN_QUERIES:
        return True, f"only {len(rows)} query rows (< {MIN_QUERIES}, smoke run)"
    timing_col = next((c for c in ("min_ms", "min_s") if c in rows[0]), None)
    if timing_col and all(_is_zero(r.get(timing_col)) for r in rows):
        return True, f"all {timing_col}=0 (timing not captured)"
    return False, ""


def _config_dir_name(pl, clk) -> str:
    """Reconstruct the per-config directory name, e.g. pl225w_clk1410mhz."""
    return f"pl{int(float(pl))}w_clk{int(float(clk)):04d}mhz"


def cross_sf_suspects(base: Path) -> list[tuple[Path, str]]:
    """Find per-config summary files whose SF is cheaper than a smaller SF.

    Reads the aggregated energy_sweep_summary.csv (the source of truth for the
    sweep) and, per (engine, benchmark, config), checks that total query energy
    is non-decreasing as the scale factor grows. Returns (summary_path, reason)
    pairs for the offending (engine, bench, sf, config) files that exist on disk.
    """
    agg = base / "energy_sweep_summary.csv"
    if not agg.exists():
        return []
    try:
        rows = list(csv.DictReader(open(agg, newline="")))
    except Exception:
        return []
    needed = {"engine", "benchmark", "sf", "query", "energy_j",
              "power_limit_w", "sm_clock_mhz"}
    if not rows or not needed.issubset(rows[0].keys()):
        return []

    # energy[(engine, bench, pl, clk)][sf][query] = energy_j
    energy: dict = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        e = _fnum(r["energy_j"])
        if e is None:
            continue
        key = (r["engine"], r["benchmark"], r["power_limit_w"], r["sm_clock_mhz"])
        energy[key][r["sf"]][r["query"]] = e

    suspects: list[tuple[Path, str]] = []
    for (engine, bench, pl, clk), by_sf in energy.items():
        sfs = sorted(by_sf, key=_sf_rank)
        for i in range(1, len(sfs)):
            small, large = sfs[i - 1], sfs[i]
            qs = by_sf[small].keys() & by_sf[large].keys()
            # only compare queries with positive energy in both
            qs = [q for q in qs if by_sf[small][q] > 0 and by_sf[large][q] > 0]
            if len(qs) < CROSS_SF_MIN_QUERIES:
                continue
            tot_small = sum(by_sf[small][q] for q in qs)
            tot_large = sum(by_sf[large][q] for q in qs)
            if tot_small <= 0:
                continue
            ratio = tot_large / tot_small
            if ratio >= CROSS_SF_TOL:
                continue
            cfg_dir = base / _config_dir_name(pl, clk)
            matches = sorted(cfg_dir.glob(
                f"{engine}_{bench}_sf{large}_metrics_summary_*.csv"))
            reason = (f"sf{large} energy {ratio:.2f}x of sf{small} at "
                      f"PL={pl}W/CLK={clk}MHz over {len(qs)} queries "
                      f"(< {CROSS_SF_TOL}; data likely short/corrupt)")
            for m in matches:
                suspects.append((m, reason))
    return suspects


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sweep_dir", help="energy_sweep directory containing pl*/ config dirs")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be deleted without deleting")
    ap.add_argument("--no-cross-sf", action="store_true",
                    help="skip the cross-SF energy-monotonicity check")
    args = ap.parse_args()

    base = Path(args.sweep_dir)
    if not base.exists():
        print(f"  [purge] sweep dir does not exist yet: {base} (nothing to do)")
        return

    # Reasons keyed by file so a file flagged by both passes is only deleted once.
    flagged: dict[Path, str] = {}

    for summary in sorted(base.glob("pl*/*_metrics_summary_*.csv")):
        bad, reason = is_degenerate(summary)
        if bad:
            flagged[summary] = reason

    if not args.no_cross_sf:
        for path, reason in cross_sf_suspects(base):
            flagged.setdefault(path, reason)

    deleted = 0
    for summary, reason in sorted(flagged.items()):
        samples = Path(str(summary).replace("_metrics_summary_", "_metrics_samples_"))
        print(f"  [purge] {summary.relative_to(base)}  — {reason}")
        if not args.dry_run:
            summary.unlink(missing_ok=True)
            samples.unlink(missing_ok=True)
        deleted += 1

    verb = "Would delete" if args.dry_run else "Deleted"
    print(f"  [purge] {verb} {deleted} degenerate/anomalous summary file(s) under {base}")


if __name__ == "__main__":
    main()
