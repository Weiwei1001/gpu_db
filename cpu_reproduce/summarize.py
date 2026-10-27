#!/usr/bin/env python3
"""Per benchmark and scale: query count, failures, and geometric means of both latency definitions and their energy.
  timed : single_time_s, one fresh duckdb process per timed run (includes process start and reloading the data);
          energy = package average power x single_time_s (the definition of the submitted paper)
  warm  : the in-process energy loop, latency = wall_s / n_reps, energy = pkg_total_j / n_reps
Package power is total (idle included); the idle baseline of each run is in */idle_baseline.csv.
Usage: summarize.py <run dir>"""
import csv, glob, math, os, sys
from collections import defaultdict

run = sys.argv[1]
gm = lambda xs: math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else float("nan")
print(f"{'part/benchmark':34s}{'sf':>5}{'ok':>5}{'fail':>5}{'timed ms':>10}{'warm ms':>9}{'timed J':>9}{'warm J':>8}{'pkg W':>7}")
for f in sorted(glob.glob(f"{run}/*/*_energy_summary.csv")):
    part, bench = f.split("/")[-2], os.path.basename(f).replace("_energy_summary.csv", "")
    tf = f.replace("_energy_summary.csv", "_timing.csv")
    fails = defaultdict(int)
    if os.path.exists(tf):
        for r in csv.DictReader(open(tf)):
            if r.get("status") != "OK":
                fails[r["sf"]] += 1
    by = defaultdict(list)
    for r in csv.DictReader(open(f)):
        try:
            t, n = float(r["single_time_s"]), float(r["n_reps"])
            if t > 0 and n > 0:
                by[r["sf"]].append((t, float(r["wall_s"]) / n, float(r["pkg_avg_w"]) * t, float(r["pkg_total_j"]) / n,
                                   float(r["pkg_avg_w"])))
        except (KeyError, ValueError):
            continue
    for sf in sorted(by, key=lambda s: float(s)):
        v = by[sf]
        print(f"{part + '/' + bench:34s}{sf:>5}{len(v):5d}{fails[sf]:5d}{gm([x[0] for x in v]) * 1e3:10.1f}"
              f"{gm([x[1] for x in v]) * 1e3:9.1f}{gm([x[2] for x in v]):9.2f}{gm([x[3] for x in v]):8.2f}"
              f"{gm([x[4] for x in v]):7.1f}")
