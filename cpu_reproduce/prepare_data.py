#!/usr/bin/env python3
"""Make sure the canonical DuckDB databases exist in the repo's shared data location, gpu_db/tests/:

    TPC-H       tests/tpch_duckdb/tpch_sf<N>.duckdb       lineitem = dbgen SF N
    H2O         tests/h2o_duckdb/h2o_<N>gb.duckdb          groupby  = 35M rows per GB
    ClickBench  tests/click_duckdb/clickbench_<N>.duckdb   t        = first total_rows x N/70 rows of hits.parquet

These are the same files and the same row counts the GPU engines use (Sirius reads these DuckDB files, Maximus and
HeavyDB read tests/<bench>/csv-*), produced by the repo's own generators in benchmarks/data/. For each dataset:
  1. an existing database with the canonical row count is used as is;
  2. otherwise it is built: TPC-H with generate_tpch.py (dbgen, deterministic); H2O from tests/h2o/csv-<N>gb/groupby.csv
     when that CSV has the canonical row count (so the CPU sees the same rows as Maximus/HeavyDB), else with
     generate_h2o.py; ClickBench with generate_clickbench.py from a local hits.parquet (downloaded once if missing).
A database with a wrong row count is moved aside (renamed *.noncanonical-<time>), never deleted.
Every dataset's path, row count and source go to <results>/data_sources.csv.

Usage: prepare_data.py --results DIR [--tpch 1 5 10 20] [--h2o 1 2 4 8] [--clickbench 1 5 10 20]
"""
import argparse, csv, os, shutil, subprocess, sys, time
from pathlib import Path
import duckdb

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
GEN = REPO / "benchmarks" / "data"
CB_FULL_CSV_GB = 70.0                         # benchmarks/data/generate_clickbench.py: SF N = first total x N/70 rows
TPCH_LINEITEM = {1: 6001215, 5: 29999795, 10: 59986052, 20: 119994608}
H2O_ROWS_PER_GB = 35_000_000                  # benchmarks/data/generate_h2o.py TARGETS
PARQUET_CANDIDATES = [TESTS / "clickbench" / "hits.parquet", TESTS / "clickbench" / "clickbench.parquet",
                      TESTS / "click_duckdb" / "clickbench.parquet", Path.home() / "clickbench.parquet",
                      Path.home() / "data" / "clickbench" / "clickbench.parquet", Path.home() / "hits.parquet"]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] [data] {msg}", flush=True)


def rows(db, table):
    if not db.exists():
        return -1
    try:
        con = duckdb.connect(str(db), read_only=True)
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        con.close()
        return n
    except Exception:
        return 0


def csv_rows(path):
    if not path.exists():
        return -1
    return duckdb.connect().execute(f"SELECT count(*) FROM read_csv('{path}', header=true)").fetchone()[0]


def set_aside(db):
    dst = db.with_name(f"{db.name}.noncanonical-{time.strftime('%Y%m%d_%H%M%S')}")
    log(f"  {db.name} has a non-canonical row count; moved aside to {dst.name}")
    db.rename(dst)


def gen(script, *args):
    cmd = [sys.executable, str(GEN / script), *map(str, args)]
    log("  " + " ".join(cmd[1:]))
    subprocess.run(cmd, check=True)


def parquet():
    for p in PARQUET_CANDIDATES:
        if p.exists() and p.stat().st_size > 14e9:
            return p, "local"
    return TESTS / "clickbench" / "clickbench.parquet", "download"   # generate_clickbench.py downloads it there


def prepare(bench, n, want, db, table, build):
    have = rows(db, table)
    if have == want:
        log(f"{bench} {n}: {db.relative_to(REPO)} ok ({have:,} rows)")
        return dict(bench=bench, scale=n, path=str(db), rows=have, canonical_rows=want, source="existing")
    if have >= 0:
        set_aside(db)
    log(f"{bench} {n}: building {db.relative_to(REPO)} (want {want:,} rows)")
    db.parent.mkdir(parents=True, exist_ok=True)
    source = build()
    have = rows(db, table)
    if have != want:
        sys.exit(f"!! {db} has {have:,} rows after building, expected {want:,}")
    log(f"{bench} {n}: built, {have:,} rows ({source})")
    return dict(bench=bench, scale=n, path=str(db), rows=have, canonical_rows=want, source=source)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, type=Path)
    ap.add_argument("--tpch", type=int, nargs="*", default=[1, 5, 10, 20])
    ap.add_argument("--h2o", type=int, nargs="*", default=[1, 2, 4, 8])
    ap.add_argument("--clickbench", type=int, nargs="*", default=[1, 5, 10, 20])
    a = ap.parse_args()
    out = []

    for n in a.tpch:
        db = TESTS / "tpch_duckdb" / f"tpch_sf{n}.duckdb"
        out.append(prepare("tpch", n, TPCH_LINEITEM[n], db, "lineitem",
                           lambda n=n, db=db: (gen("generate_tpch.py", "-o", db.parent, "-sf", n, "--no-run-query",
                                                   "--skip-install", "-q"), f"dbgen(sf={n})")[1]))

    for n in a.h2o:
        db = TESTS / "h2o_duckdb" / f"h2o_{n}gb.duckdb"
        want = H2O_ROWS_PER_GB * n
        def build(n=n, db=db, want=want):
            src = TESTS / "h2o" / f"csv-{n}gb" / "groupby.csv"
            have = csv_rows(src)
            if have == want:            # same rows as Maximus / HeavyDB on this machine
                con = duckdb.connect(str(db))
                con.execute(f"""CREATE TABLE groupby AS SELECT * FROM read_csv('{src}', header=true, columns={{
                    'id1':'VARCHAR','id2':'VARCHAR','id3':'VARCHAR','id4':'INTEGER','id5':'INTEGER','id6':'INTEGER',
                    'v1':'INTEGER','v2':'INTEGER','v3':'DOUBLE'}})""")
                con.close()
                return f"imported {src.relative_to(REPO)}"
            if have >= 0:
                log(f"  {src.relative_to(REPO)} has {have:,} rows, not {want:,}: not used, generating instead")
            gen("generate_h2o.py", f"{n}gb", "--format", "duckdb", "-o", db.parent)
            return "generate_h2o.py (random rows, same distribution)"
        out.append(prepare("h2o", n, want, db, "groupby", build))

    if a.clickbench:
        pq, how = parquet()
        if how == "download":
            free = shutil.disk_usage(TESTS).free / 1e9
            log(f"no local hits.parquet; downloading 14.8 GB to {pq} ({free:.0f} GB free)")
            pq.parent.mkdir(parents=True, exist_ok=True)
            sys.path.insert(0, str(GEN))
            from generate_clickbench import download_parquet
            download_parquet(pq)
        log(f"ClickBench source: {pq}")
        total = duckdb.connect().execute(f"SELECT count(*) FROM read_parquet('{pq}')").fetchone()[0]
        for n in a.clickbench:
            db = TESTS / "click_duckdb" / f"clickbench_{n}.duckdb"
            want = int(total * min(1.0, n / CB_FULL_CSV_GB))
            out.append(prepare("clickbench", n, want, db, "t",
                               lambda n=n, db=db: (gen("generate_clickbench.py", "--scales", n, "--format", "duckdb",
                                                       "-o", db.parent, "--parquet-path", pq), f"first rows of {pq}")[1]))

    a.results.mkdir(parents=True, exist_ok=True)
    with open(a.results / "data_sources.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=out[0].keys())
        w.writeheader()
        w.writerows(out)
    log(f"all {len(out)} datasets ready -> {a.results / 'data_sources.csv'}")


if __name__ == "__main__":
    main()
