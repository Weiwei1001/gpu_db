#!/usr/bin/env python3
"""Dialect screening: run each of the 75 queries once on HeavyDB and record pass/fail + the error reason."""
import csv, os, re, subprocess, sys, time

import os as _os
HB_ROOT = _os.environ.get("HB_ROOT", _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
HSQL = _os.path.join(HB_ROOT, "lib", "hsql")
QDIR = _os.path.join(HB_ROOT, "queries")
SUITES = _os.environ.get("HB_SCREEN_DBS") and dict(
    kv.split("=") for kv in _os.environ["HB_SCREEN_DBS"].split(",")) or {  # suite -> (database, query directory)
    "tpch":       "tpch_sf1",
    "h2o":        "h2o_1gb",
    "clickbench": "cb_sf10",
    "case_bench": "tpch_sf1",
    "clickbench_approx": "cb_sf10",
}
ERR_PAT = re.compile(r"(SQL Error|Exception|not supported|Error:|Failed|Cannot |failed to|Query execution failed)", re.I)

def run(db, sql, timeout=600):
    p = subprocess.run([HSQL, db], input="\\timing\n" + sql, capture_output=True,
                       text=True, timeout=timeout)
    return p.stdout + p.stderr

def qsort(n):
    return int(re.search(r"\d+", n).group())

only = sys.argv[1:] or list(SUITES)
rows = []
for suite, db in [(k, v) for k, v in SUITES.items() if k in only]:
    d = os.path.join(QDIR, suite)
    files = sorted(os.listdir(d), key=qsort)
    print(f"\n===== {suite}  ({db})  {len(files)} queries =====")
    for fn in files:
        sql = open(os.path.join(d, fn)).read()
        t0 = time.time()
        try:
            out = run(db, sql)
        except subprocess.TimeoutExpired:
            out, wall = "TIMEOUT (>600s)", 600.0
        wall = time.time() - t0
        nstmt = len([x for x in sql.split(";") if x.strip()])
        ms = [float(m) for m in re.findall(r"Execution time: ([\d.]+) ms", out)]
        ok = len(ms) >= nstmt
        errline = None
        if not ok:
            cand = [l.strip() for l in out.splitlines()
                    if l.strip() and "Execution time" not in l and "Total time" not in l]
            errline = next((l for l in cand if ERR_PAT.search(l)), cand[-1] if cand else "no output")
        rows.append(dict(suite=suite, query=fn[:-4], db=db, ok=ok,
                         exec_ms=round(sum(ms), 2) if ms else "",
                         wall_s=round(wall, 2), error=(errline or "")[:200]))
        flag = "ok  " if ok else "FAIL"
        print(f"  {flag} {fn[:-4]:6s} {rows[-1]['exec_ms'] or '-':>10} ms   {rows[-1]['error'][:90]}")

out_csv = _os.path.join(_os.environ.get("HB_RESULTS_BASE") or _os.path.join(HB_ROOT, "results", _os.environ.get("HB_MODE", "smoke")), "query_screen_%s.csv" % "_".join(only))
with open(out_csv, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader(); w.writerows(rows)

print("\n===== Summary =====")
for suite in only:
    r = [x for x in rows if x["suite"] == suite]
    ok = sum(1 for x in r if x["ok"])
    print(f"  {suite:11s} {ok}/{len(r)} runnable")
    for x in r:
        if not x["ok"]:
            print(f"      {x['query']}: {x['error'][:110]}")
print(f"\n-> {out_csv}")
