#!/usr/bin/env python3
"""Validate a generated DuckDB benchmark database; exit 0 if usable, 1 if it
should be deleted and rebuilt.

Motivation: `run_all_benchmarks.sh` reuses any DuckDB file that already exists
(`if [ -f "$DB" ]; then continue`). A stale / incomplete / empty database (the
ClickBench regression: a no-op DB whose queries return in ~5 ms with the GPU
idle) is then silently reused, producing zero-time / zero-energy results. This
check guards against that.

A database is considered STALE (rebuild needed) if ANY of:
  * the file is missing;
  * its size differs from the source CSV(s) by more than --max-ratio (default
    10x) in either direction — an empty/broken DB is orders of magnitude
    smaller than its source CSV;
  * --table is given and that table is missing or has 0 rows (this also catches
    the `t` vs `hits` table-name mismatch — Sirius queries `FROM t`).

Usage:
  check_duckdb.py --db DB.duckdb --ref CSV [CSV ...] [--table t] [--max-ratio 10]
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--ref", nargs="+", default=[],
                    help="source CSV file(s); DB size is compared to their total")
    ap.add_argument("--table", default=None,
                    help="if given, require this table to exist with >0 rows")
    ap.add_argument("--max-ratio", type=float, default=10.0)
    args = ap.parse_args()

    db = args.db
    if not os.path.isfile(db):
        print(f"STALE: {db} missing")
        return 1

    db_sz = os.path.getsize(db)
    ref_sz = sum(os.path.getsize(p) for p in args.ref if os.path.isfile(p))

    if ref_sz > 0:
        # Off by more than max-ratio in either direction -> stale.
        if db_sz * args.max_ratio < ref_sz or db_sz > ref_sz * args.max_ratio:
            print(f"STALE: {os.path.basename(db)} size {db_sz/1e6:.1f}MB vs "
                  f"source {ref_sz/1e6:.1f}MB (>{args.max_ratio:g}x off)")
            return 1

    if args.table:
        try:
            import duckdb
            con = duckdb.connect(db, read_only=True)
            try:
                n = con.execute(f'SELECT count(*) FROM "{args.table}"').fetchone()[0]
            finally:
                con.close()
        except Exception as e:
            print(f"STALE: {os.path.basename(db)} table '{args.table}' "
                  f"unreadable/missing ({type(e).__name__})")
            return 1
        if n <= 0:
            print(f"STALE: {os.path.basename(db)} table '{args.table}' has 0 rows")
            return 1
        print(f"OK: {os.path.basename(db)} ({db_sz/1e6:.1f}MB, "
              f"{args.table}={n:,} rows)")
    else:
        print(f"OK: {os.path.basename(db)} ({db_sz/1e6:.1f}MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
