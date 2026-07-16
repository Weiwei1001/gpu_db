#!/usr/bin/env python3
"""Merge per-GPU worker result directories (run_all_benchmarks.sh --parallel)
back into the canonical results directory.

Worker layout:  <results>/parallel_<ts>/gpu<i>/...
Merge rules:
  * timestamped files (*_metrics_summary_*.csv, *_metrics_samples_*.csv,
    *_samples_*.csv) are unique per (bench, sf, time) -> moved as-is;
  * union CSVs that the runners (over)write per invocation
    (maximus_benchmark.csv, sirius_benchmark.csv, maximus_cpu_data_timing.csv,
    sirius_cpu_data_analysis.csv, ...) -> concatenated across workers with a
    single header and written to the canonical dir (replacing it, matching
    the sequential campaign semantics);
  * anything else (logs, unknown files) -> moved into the canonical dir,
    suffixed with the worker name on collision.
Nothing inside energy_sweep/ needs merging: parallel sweep workers share the
canonical sweep dir and partition by benchmark, so their files never collide.
"""
import argparse
import shutil
import sys
from pathlib import Path

TIMESTAMPED = ("_metrics_summary_", "_metrics_samples_", "_samples_")
UNION_CSVS = {
    "maximus_benchmark.csv",
    "sirius_benchmark.csv",
    "maximus_cpu_data_timing.csv",
    "sirius_cpu_data_analysis.csv",
}


def is_timestamped(name: str) -> bool:
    return name.endswith(".csv") and any(t in name for t in TIMESTAMPED)


def concat_csvs(parts: list[Path], dest: Path) -> int:
    rows = 0
    header = None
    out = []
    for p in sorted(parts):
        with open(p) as fh:
            lines = fh.read().splitlines()
        if not lines:
            continue
        if header is None:
            header = lines[0]
            out.append(header)
        elif lines[0] != header:
            print(f"  [WARN] header mismatch in {p}, keeping its rows anyway")
        out.extend(lines[1:])
        rows += len(lines) - 1
    if header is not None:
        dest.write_text("\n".join(out) + "\n")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("parallel_dir", help="the parallel_<ts> directory")
    ap.add_argument("results_dir", help="canonical results directory")
    args = ap.parse_args()

    par = Path(args.parallel_dir)
    res = Path(args.results_dir)
    workers = sorted(d for d in par.glob("gpu*") if d.is_dir())
    if not workers:
        print(f"[merge] no worker dirs under {par}")
        return 1

    union: dict[str, list[Path]] = {}
    moved = 0
    for w in workers:
        for f in sorted(w.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(w)
            if f.name in UNION_CSVS:
                union.setdefault(f.name, []).append(f)
            elif is_timestamped(f.name):
                dest = res / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), dest)
                moved += 1
            else:
                dest = res / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    dest = dest.with_name(f"{dest.stem}.{w.name}{dest.suffix}")
                shutil.move(str(f), dest)
                moved += 1

    for name, parts in sorted(union.items()):
        n = concat_csvs(parts, res / name)
        print(f"[merge] {name}: {n} rows from {len(parts)} worker(s)")
    print(f"[merge] moved {moved} files from {len(workers)} worker(s) into {res}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
