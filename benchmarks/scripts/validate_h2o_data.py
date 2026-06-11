#!/usr/bin/env python3
"""Validate (and optionally regenerate) H2O groupby CSV datasets by ROW COUNT.

Why this exists
---------------
run_all_benchmarks.sh Step 0a decides whether to (re)generate an H2O scale
factor by checking only whether ``tests/h2o/csv-{sf}/`` EXISTS — not whether it
holds the right number of rows. A dataset left short by an interrupted or older
generation (observed: an A100 ``csv-4gb`` with far fewer than its canonical
140M rows) therefore passes the existence check and is never rebuilt.

Downstream this shows up as a larger scale factor using LESS time/energy than a
smaller one at the *same* GPU power/clock config — physically impossible, and a
sign the data (not the engine) is wrong. ``--resume`` happily re-measures the
short data as "fine", so the bad numbers persist across re-runs.

This script counts the actual rows in each ``csv-{sf}/groupby.csv`` and compares
them to the canonical ``TARGETS`` in ``benchmarks/data/generate_h2o.py``.
Mismatched (short / missing) datasets are reported and, with ``--regenerate``,
rebuilt so a subsequent sweep measures the correct data.

Exit code is non-zero if any scale factor is short/missing (useful in scripts).
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
GENERATOR = SCRIPT_DIR.parent / "data" / "generate_h2o.py"


def load_targets() -> dict:
    """Import the canonical {sf: n_rows} map from the data generator."""
    spec = importlib.util.spec_from_file_location("generate_h2o", GENERATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return dict(mod.TARGETS)


def count_rows(csv_path: Path) -> int:
    """Row count (excluding the header) via wc -l. Returns -1 on failure."""
    try:
        out = subprocess.run(["wc", "-l", str(csv_path)],
                             capture_output=True, text=True, check=True)
        return int(out.stdout.split()[0]) - 1
    except Exception:
        return -1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_dir",
                    help="H2O data dir containing csv-{sf}/ (e.g. tests/h2o)")
    ap.add_argument("--scales", nargs="+", default=["1gb", "2gb", "4gb", "8gb"],
                    help="Scale factors to check (default: 1gb 2gb 4gb 8gb)")
    ap.add_argument("--tolerance", type=float, default=0.05,
                    help="Allowed fractional shortfall vs the canonical count "
                         "(default 0.05 = up to 5%% short is tolerated)")
    ap.add_argument("--regenerate", action="store_true",
                    help="Rebuild any short/missing scale factor in place")
    ap.add_argument("--dry-run", action="store_true",
                    help="With --regenerate, only print what would be rebuilt")
    args = ap.parse_args()

    targets = load_targets()
    base = Path(args.data_dir)
    suspect: list[str] = []

    print(f"  [validate-h2o] data dir: {base}")
    for sf in args.scales:
        expected = targets.get(sf)
        if expected is None:
            print(f"  [validate-h2o] {sf}: no canonical row target — skipping")
            continue
        csv_path = base / f"csv-{sf}" / "groupby.csv"
        if not csv_path.exists():
            print(f"  [validate-h2o] {sf}: MISSING ({csv_path})")
            suspect.append(sf)
            continue
        actual = count_rows(csv_path)
        floor = int(expected * (1 - args.tolerance))
        ok = actual >= floor
        status = "OK" if ok else "SHORT"
        print(f"  [validate-h2o] {sf}: rows={actual:,} expected~{expected:,} "
              f"(floor {floor:,}) -> {status}")
        if not ok:
            suspect.append(sf)

    if not suspect:
        print("  [validate-h2o] all H2O CSV datasets have the expected size")
        return 0

    print(f"  [validate-h2o] SUSPECT scale factors: {', '.join(suspect)}")

    if not args.regenerate:
        print("  [validate-h2o] re-run with --regenerate to rebuild them, then "
              "purge the matching energy_sweep summaries so the sweep re-runs.")
        return 1

    for sf in suspect:
        csv_path = base / f"csv-{sf}" / "groupby.csv"
        print(f"  [validate-h2o] regenerating {sf} -> {csv_path}")
        if args.dry_run:
            continue
        csv_path.unlink(missing_ok=True)
        rc = subprocess.run(
            [sys.executable, str(GENERATOR), sf, "--format", "csv", "-o", str(base)]
        ).returncode
        if rc != 0:
            print(f"  [validate-h2o] WARNING: regeneration of {sf} exited {rc}")
    print("  [validate-h2o] NOTE: Sirius reads tests/h2o_duckdb/h2o_{sf}.duckdb — "
          "rebuild those too if Sirius h2o was affected (Step 0c / --format duckdb).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
