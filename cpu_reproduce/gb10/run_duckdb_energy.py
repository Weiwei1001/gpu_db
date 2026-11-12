#!/usr/bin/env python3
"""DuckDB (CPU) latency + power/energy benchmark on NVIDIA GB10, using the spbm_telemetry driver.

For each of the 28 datasets (TPC-H SF1/5/10/20, H2O 1/2/4/8gb, ClickBench 1/5/10/20,
case_bench SF1/5/10/20 -- the repo's 5 tiny queries on the TPC-H tables -- and the 120-query
microbenchmark micro_{tpch,h2o,clickbench} at the same scales, cpu_reproduce/queries/microbench_*):
  1. open the DuckDB file and pre-load every column of every table into the buffer pool;
  2. measure idle power for a few seconds (baseline);
  3. for every query:
       - latency phase: run it 3 times, record each wall time;
       - power phase: run it repeatedly for >= 3 runs AND >= 10 s, sampling SPBM power every
         100 ms and reading the SPBM energy counters (mJ) before/after.

Needs the SPBM telemetry module energy.py (SPBM, WRAP, read_counters) in $SPBM_DIR (default ~/spbm),
the databases under gpu_db/tests/{tpch,h2o,click}_duckdb, and python duckdb (1.4.1, as on the x86 CPUs).
Usage:
  python3 run_duckdb_energy.py --config baseline_20c                # all 20 cores, default threads
  python3 run_duckdb_energy.py --config micro_20c --datasets micro  # only the microbenchmark (prefix match)
  python3 run_duckdb_energy.py --config p_cores --cpus 5-9,15-19    # pin to the 10 X925 cores
  python3 run_duckdb_energy.py --config t10 --threads 10 --datasets tpch_sf1 h2o_1gb
Results: <--out, default ~/spbm/results>/<config>/{queries.csv, datasets.csv, samples/<dataset>.csv, machine_info.txt}
"""
import argparse
import csv
import os
import sys
import threading
import time
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "benchmarks" / "scripts"))
sys.path.insert(0, os.environ.get("SPBM_DIR", str(Path.home() / "spbm")))
import generate_sirius_sql as repo_queries  # noqa: E402

from energy import SPBM, WRAP, read_counters  # noqa: E402

DATA = REPO / "tests"
QUERIES = REPO / "cpu_reproduce" / "queries"
POWER_FIELDS = ["te_sys_total", "te_soc_pkg", "te_c_and_g", "te_cpu_p", "te_total_gpu_out"]
TEMP_FIELDS = ["temp_cpu_p_clu0", "temp_cpu_e_clu0", "temp_soc"]

DATASETS = (
    [(f"tpch_sf{s}", "tpch", DATA / "tpch_duckdb" / f"tpch_sf{s}.duckdb") for s in (1, 5, 10, 20)]
    + [(f"h2o_{s}", "h2o", DATA / "h2o_duckdb" / f"h2o_{s}.duckdb") for s in ("1gb", "2gb", "4gb", "8gb")]
    + [(f"clickbench_{s}", "clickbench", DATA / "click_duckdb" / f"clickbench_{s}.duckdb")
       for s in (1, 5, 10, 20)]
    + [(f"case_bench_sf{s}", "case_bench", DATA / "tpch_duckdb" / f"tpch_sf{s}.duckdb") for s in (1, 5, 10, 20)]
    + [(f"micro_tpch_sf{s}", "microbench_tpch", DATA / "tpch_duckdb" / f"tpch_sf{s}.duckdb") for s in (1, 5, 10, 20)]
    + [(f"micro_h2o_{s}", "microbench_h2o", DATA / "h2o_duckdb" / f"h2o_{s}.duckdb")
       for s in ("1gb", "2gb", "4gb", "8gb")]
    + [(f"micro_clickbench_{s}", "microbench_clickbench", DATA / "click_duckdb" / f"clickbench_{s}.duckdb")
       for s in (1, 5, 10, 20)]
)
CASE_BENCH_DIR = REPO / "microbench" / "case_bench"


def strip_gpu_call(line: str) -> str:
    """'call gpu_processing("SELECT ...;");' -> 'SELECT ...'"""
    s = line.strip()
    s = s[len('call gpu_processing("'):-len('");')]
    return s.replace("\\'", "'").rstrip(";")


def load_queries(bench: str, con) -> list:
    if bench == "tpch":  # DuckDB's own standard TPC-H text (repo q11 is split for the GPU engine)
        return [(f"q{n}", q.strip().rstrip(";"))
                for n, q in con.execute("SELECT query_nr, query FROM tpch_queries()").fetchall()]
    if bench == "case_bench" or bench.startswith("microbench_"):
        # repo microbench/case_bench/q*.sql or the x86 runs' cpu_reproduce/queries/microbench_*/*.sql
        # (one statement per file, query name = file stem), '--' comment lines dropped
        files = (sorted(CASE_BENCH_DIR.glob("q*.sql")) if bench == "case_bench"
                 else sorted((QUERIES / bench).glob("*.sql")))
        return [(f.stem, "\n".join(l for l in f.read_text().splitlines()
                                    if not l.strip().startswith("--")).strip().rstrip(";"))
                for f in files]
    src = repo_queries.H2O_QUERIES if bench == "h2o" else repo_queries.CLICK_QUERIES
    return [(fname.removesuffix(".sql"), strip_gpu_call(calls[-1])) for fname, calls in src]


def read_fields(fields):
    return [int(open(SPBM + f).read()) for f in fields]


class PowerSampler:
    """Background thread sampling SPBM instantaneous power (mW) every `period` seconds."""

    def __init__(self, period=0.1):
        self.period, self.samples, self._stop = period, [], threading.Event()

    def __enter__(self):
        self._t0 = time.perf_counter()
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            self.samples.append([time.perf_counter() - self._t0] + read_fields(POWER_FIELDS + TEMP_FIELDS))
            self._stop.wait(self.period)

    def __exit__(self, *exc):
        self._stop.set()
        self._th.join()
        return False

    def mean(self, field):
        i = 1 + (POWER_FIELDS + TEMP_FIELDS).index(field)
        return sum(s[i] for s in self.samples) / max(1, len(self.samples))


def run_timed(con, sql, timeout_s):
    """Run one query, materializing its full result inside DuckDB (H2O db-benchmark style,
    no Python row conversion); interrupt it after timeout_s. Returns seconds."""
    timer = threading.Timer(timeout_s, con.interrupt)
    timer.start()
    try:
        t0 = time.perf_counter()
        con.execute(f"CREATE OR REPLACE TEMP TABLE ans AS {sql}")
        return time.perf_counter() - t0
    finally:
        timer.cancel()


def energy_delta(c0, c1):
    return {k: ((c1[k] - c0[k]) % WRAP) / 1000.0 for k in c0}


def preload(con):
    """Scan every column of every table once so all blocks sit in DuckDB's buffer pool."""
    for (table,) in con.execute("SHOW TABLES").fetchall():
        cols = [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]
        agg = ", ".join(f'sum(hash("{c}"))' for c in cols)
        con.execute(f"SELECT count(*), {agg} FROM {table}").fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="name of this configuration (results subdir)")
    ap.add_argument("--threads", type=int, default=None, help="DuckDB threads (default: #cpus allowed)")
    ap.add_argument("--cpus", default=None, help="CPU affinity list, e.g. 5-9,15-19")
    ap.add_argument("--datasets", nargs="*", default=None,
                    help="subset: dataset names or prefixes (e.g. micro, tpch_sf1, h2o)")
    ap.add_argument("--out", type=Path, default=Path.home() / "spbm" / "results", help="results root")
    ap.add_argument("--latency-runs", type=int, default=3)
    ap.add_argument("--power-min-runs", type=int, default=3)
    ap.add_argument("--power-min-seconds", type=float, default=10.0)
    ap.add_argument("--idle-seconds", type=float, default=5.0)
    ap.add_argument("--timeout", type=float, default=300.0, help="per-run timeout (s)")
    ap.add_argument("--memory-limit", default="14GB")
    args = ap.parse_args()

    if args.cpus:
        cpus = set()
        for part in args.cpus.split(","):
            lo, _, hi = part.partition("-")
            cpus.update(range(int(lo), int(hi or lo) + 1))
        os.sched_setaffinity(0, cpus)
    threads = args.threads or len(os.sched_getaffinity(0))

    out = args.out / args.config
    (out / "samples").mkdir(parents=True, exist_ok=True)
    qcsv_path, dcsv_path = out / "queries.csv", out / "datasets.csv"
    done = set()
    if qcsv_path.exists():  # resume support: skip (dataset, query) already recorded
        with open(qcsv_path) as f:
            done = {(r["dataset"], r["query"]) for r in csv.DictReader(f)}

    qfields = ["config", "dataset", "query", "status", "lat1_s", "lat2_s", "lat3_s", "lat_min_s",
               "lat_median_s", "power_runs", "power_window_s", "energy_per_query_pkg_j",
               "energy_per_query_cpu_j", "energy_per_query_cpu_p_j", "energy_per_query_cpu_e_j",
               "energy_per_query_gpu_j", "avg_pkg_w", "avg_cpu_p_w", "avg_cpu_e_w", "avg_sys_total_w",
               "avg_soc_pkg_w_inst", "idle_pkg_w", "dyn_energy_per_query_pkg_j", "avg_temp_cpu_p_c",
               "error"]
    dfields = ["config", "dataset", "threads", "cpus", "preload_s", "idle_pkg_w", "idle_cpu_p_w",
               "idle_cpu_e_w", "idle_sys_total_w", "n_queries", "n_ok"]
    qnew, dnew = not qcsv_path.exists(), not dcsv_path.exists()
    qf, df = open(qcsv_path, "a", newline=""), open(dcsv_path, "a", newline="")
    qw, dw = csv.DictWriter(qf, qfields), csv.DictWriter(df, dfields)
    if qnew:
        qw.writeheader()
    if dnew:
        dw.writeheader()

    names = {d[0] for d in DATASETS}
    pick = lambda n: any(n == p or (p not in names and n.startswith(p)) for p in args.datasets)
    datasets = [d for d in DATASETS if not args.datasets or pick(d[0])]
    with open(out / "machine_info.txt", "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} config={args.config} duckdb={duckdb.__version__} "
                f"python={sys.version.split()[0]} threads={threads} cpus={args.cpus or 'all'} "
                f"datasets={' '.join(d[0] for d in datasets)}\n")
    print(f"[{args.config}] duckdb {duckdb.__version__} threads={threads} cpus={sorted(os.sched_getaffinity(0))} datasets={len(datasets)}",
          flush=True)

    for name, bench, path in datasets:
        con = duckdb.connect(str(path), read_only=True)
        con.execute(f"SET threads={threads}")
        con.execute(f"SET memory_limit='{args.memory_limit}'")
        con.execute(f"SET temp_directory='{Path.home() / 'spbm' / 'duckdb_tmp'}'")
        queries = load_queries(bench, con)
        if all((name, q) in done for q, _ in queries):
            print(f"== {name}: already complete, skipping", flush=True)
            continue

        t0 = time.perf_counter()
        preload(con)
        preload_s = time.perf_counter() - t0
        time.sleep(2)
        c0 = read_counters()
        time.sleep(args.idle_seconds)
        idle = {k: v / args.idle_seconds for k, v in energy_delta(c0, read_counters()).items()}
        idle_sys = sum(read_fields(["te_sys_total"])) / 1000.0
        print(f"== {name}: preload {preload_s:.1f}s, idle pkg {idle['pkg_j']:.1f} W", flush=True)

        n_ok = 0
        with open(out / "samples" / f"{name}.csv", "a", newline="") as sf:
            sw = csv.writer(sf)
            sw.writerow(["query", "t_s"] + POWER_FIELDS + TEMP_FIELDS)
            for qname, sql in queries:
                if (name, qname) in done:
                    continue
                row = {"config": args.config, "dataset": name, "query": qname, "status": "ok",
                       "idle_pkg_w": round(idle["pkg_j"], 3)}
                try:
                    lats = [run_timed(con, sql, args.timeout) for _ in range(args.latency_runs)]
                    for i, v in enumerate(lats[:3]):
                        row[f"lat{i + 1}_s"] = round(v, 6)
                    row["lat_min_s"] = round(min(lats), 6)
                    row["lat_median_s"] = round(sorted(lats)[len(lats) // 2], 6)

                    runs = 0
                    with PowerSampler() as ps:
                        c0 = read_counters()
                        tw0 = time.perf_counter()
                        while runs < args.power_min_runs or time.perf_counter() - tw0 < args.power_min_seconds:
                            run_timed(con, sql, args.timeout)
                            runs += 1
                        window = time.perf_counter() - tw0
                        e = energy_delta(c0, read_counters())
                    for s in ps.samples:
                        sw.writerow([qname] + [round(s[0], 3)] + s[1:])
                    row.update({
                        "power_runs": runs, "power_window_s": round(window, 3),
                        "energy_per_query_pkg_j": round(e["pkg_j"] / runs, 4),
                        "energy_per_query_cpu_j": round((e["cpu_p_j"] + e["cpu_e_j"]) / runs, 4),
                        "energy_per_query_cpu_p_j": round(e["cpu_p_j"] / runs, 4),
                        "energy_per_query_cpu_e_j": round(e["cpu_e_j"] / runs, 4),
                        "energy_per_query_gpu_j": round(e["gpu_j"] / runs, 4),
                        "avg_pkg_w": round(e["pkg_j"] / window, 3),
                        "avg_cpu_p_w": round(e["cpu_p_j"] / window, 3),
                        "avg_cpu_e_w": round(e["cpu_e_j"] / window, 3),
                        "avg_sys_total_w": round(ps.mean("te_sys_total") / 1000, 3),
                        "avg_soc_pkg_w_inst": round(ps.mean("te_soc_pkg") / 1000, 3),
                        "dyn_energy_per_query_pkg_j": round((e["pkg_j"] - idle["pkg_j"] * window) / runs, 4),
                        "avg_temp_cpu_p_c": round(ps.mean("temp_cpu_p_clu0") / 100, 2),
                    })
                    n_ok += 1
                    print(f"  {name} {qname:4s} lat_min {row['lat_min_s'] * 1000:9.1f} ms | "
                          f"{runs:4d} runs/{window:5.1f}s | pkg {row['avg_pkg_w']:5.1f} W | "
                          f"E/q pkg {row['energy_per_query_pkg_j']:8.3f} J cpu {row['energy_per_query_cpu_j']:8.3f} J",
                          flush=True)
                except Exception as ex:  # timeout (interrupt) or SQL error: record and move on
                    row["status"] = "timeout" if isinstance(ex, duckdb.InterruptException) else "error"
                    row["error"] = str(ex).splitlines()[0][:200]
                    print(f"  {name} {qname:4s} {row['status'].upper()}: {row['error']}", flush=True)
                con.execute("DROP TABLE IF EXISTS ans")
                qw.writerow(row)
                qf.flush()

        dw.writerow({"config": args.config, "dataset": name, "threads": threads,
                     "cpus": args.cpus or "all", "preload_s": round(preload_s, 2),
                     "idle_pkg_w": round(idle["pkg_j"], 3), "idle_cpu_p_w": round(idle["cpu_p_j"], 3),
                     "idle_cpu_e_w": round(idle["cpu_e_j"], 3), "idle_sys_total_w": round(idle_sys, 3),
                     "n_queries": len(queries), "n_ok": n_ok})
        df.flush()
        con.close()

    qf.close()
    df.close()
    print(f"[{args.config}] done -> {out}", flush=True)


if __name__ == "__main__":
    main()
