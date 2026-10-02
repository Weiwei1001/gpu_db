#!/usr/bin/env python3
"""Category C: two-dimensional energy grid of power limit × SM clock (matching the repo's 5×5).

The repo's run_energy_sweep.py derives the levels from the hardware at run time:
  power = [power.min_limit .. TDP] evenly split into 5 levels
  SM    = [lowest supported clock .. highest supported clock] evenly split into 5 levels (snapped to actually supported clocks)
"""
import argparse, atexit, csv, math, os, subprocess, sys, time
import statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hbench as hb

GPUSTR = ",".join(map(str, hb.GPUS))

def sh(*c):
    return subprocess.run(c, capture_output=True, text=True)

def restore():
    os.environ.pop("HB_EXPECT_PL", None); os.environ.pop("HB_EXPECT_SM", None)
    sh("sudo", "nvidia-smi", "-i", GPUSTR, "-rgc")
    sh("sudo", "nvidia-smi", "-i", GPUSTR, "-pl", str(TDP))
    print(f"[restore] PL={TDP}W, clocks unlocked", flush=True)

def levels(lo, hi, n, snap=None):
    if n <= 1:
        return [hi]
    step = (hi - lo) / (n - 1)
    vs = sorted({int(round(lo + i * step)) for i in range(n)})
    if snap:
        vs = sorted({min(snap, key=lambda s: abs(s - v)) for v in vs})
    return vs

ap = argparse.ArgumentParser()
ap.add_argument("--targets", required=True, help="suite:db:sf:q1,q2,... separated by semicolons")
ap.add_argument("--n-pl", type=int, default=5)
ap.add_argument("--n-sm", type=int, default=5)
ap.add_argument("--trials", type=int, default=3)
ap.add_argument("--window", type=float, default=2.0)
ap.add_argument("--out", default=os.path.join(hb.RESULTS_BASE, "catc.csv"))
a = ap.parse_args()

g0 = str(hb.GPUS[0])
TDP = int(float(sh("nvidia-smi", "-i", g0, "--query-gpu=power.max_limit",
                   "--format=csv,noheader,nounits").stdout.strip()))
PLMIN = int(float(sh("nvidia-smi", "-i", g0, "--query-gpu=power.min_limit",
                     "--format=csv,noheader,nounits").stdout.strip()))
sup = sorted({int(x) for x in sh("nvidia-smi", "-i", g0, "--query-supported-clocks=gr",
                                 "--format=csv,noheader,nounits").stdout.split()
              if x.isdigit()})
PLS = levels(PLMIN, TDP, a.n_pl)
SMS = levels(sup[0], sup[-1], a.n_sm, snap=sup)
print(f"GPU {hb.GPUS}  power levels {PLS}  SM levels {SMS}  = {len(PLS)*len(SMS)} configurations", flush=True)

targets = []
for t in a.targets.split(";"):
    suite, db, sf, qs = t.split(":")
    allq = dict(hb.queries(suite))
    for qn in qs.split(","):
        targets.append((suite, db, sf, qn, allq[qn]))
print(f"{len(targets)} target queries → {len(PLS)*len(SMS)*len(targets)} measurements in total", flush=True)

# Count result rows only once: rows_returned makes heavysql print the entire result set into the pipe
# (h2o q3 has 9.7 million rows); recounting at every configuration would take several minutes each.
pre = []
for suite, db, sf, qn, q in targets:
    _s = hb.Sql(db, "gpu")
    try:
        n = _s.rows_returned(q)
    finally:
        _s.close()
    pre.append((suite, db, sf, qn, hb.Sql.count_wrap(q) if n > 10000 else q, n))
    print(f"  {suite}/{sf}/{qn}: {n:,} rows" + (" (wrapped in COUNT)" if n > 10000 else ""), flush=True)
targets = pre

ok, others = hb.gpu_guard()
if not ok:
    print(f"!! other people's jobs {others} on the GPU", file=sys.stderr)
atexit.register(restore)
try:                               # verify before and after every query: single process, PID unchanged, only on GPUS
    guard = hb.Guard()
except hb.ConfigError as e:
    sys.exit(f"!! configuration check failed before the run, nothing measured: {e}")

done = set()
if os.path.exists(a.out):          # resume support
    for r in csv.DictReader(open(a.out)):
        done.add((r["pl_w"], r["sm_mhz"], r["suite"], r["sf"], r["query"]))
    print(f"{len(done)} rows already present, skipping them", flush=True)

rows, t_start = [], time.time()
for pl in PLS:
    if sh("sudo", "nvidia-smi", "-i", GPUSTR, "-pl", str(pl)).returncode != 0:
        print(f"  PL={pl} could not be set, skipping", flush=True); continue
    for sm in SMS:
        if sh("sudo", "nvidia-smi", "-i", GPUSTR, "-lgc", f"{sm},{sm}").returncode != 0:
            print(f"  SM={sm} could not be set, skipping", flush=True); continue
        os.environ["HB_EXPECT_PL"], os.environ["HB_EXPECT_SM"] = str(pl), str(sm)   # for Guard to verify
        time.sleep(2)
        p_idle = hb.idle_baseline_W(3.0)
        print(f"--- PL={pl}W SM={sm}MHz  idle {p_idle:.1f} W "
              f"(elapsed {(time.time()-t_start)/60:.0f} min) ---", flush=True)
        for suite, db, sf, qn, qb, nrows in targets:
            if (str(pl), str(sm), suite, sf, qn) in done:
                continue
            sql = None
            try:
                guard.check(f"before {suite}/{sf}/{qn} PL={pl} SM={sm}")
                sql = hb.Sql(db, "gpu")
                for _ in range(2):
                    t_ms = sql.run(qb)
                K = max(1, min(3000, math.ceil(a.window * 1000 / max(t_ms, 0.1))))
                Es, Ts, lats = [], [], []
                for _ in range(a.trials):
                    e0 = hb.energy_J(); t0 = time.perf_counter()
                    ls, _ = sql.run_burst(qb, K, timeout=120 + K * 2)
                    t1 = time.perf_counter(); e1 = hb.energy_J()
                    Es.append((e1 - e0) / K * 1000); Ts.append(t1 - t0); lats += ls
                gpu = guard.check(f"after {suite}/{sf}/{qn} PL={pl} SM={sm}")
                E = st.mean(Es); T = st.mean(Ts)
                row = dict(pl_w=pl, sm_mhz=sm, suite=suite, sf=sf, query=qn, K=K,
                           lat_min_ms=round(min(lats), 3),
                           lat_mean_ms=round(st.mean(lats), 3),
                           P_idle_W=round(p_idle, 2), P_load_W=round(E * K / T / 1000, 2),
                           E_total_mJ=round(E, 3),
                           E_dyn_mJ=round(E - p_idle * T / K * 1000, 3),
                           duty=round(sum(lats) / 1000 / T, 3),
                           cv_pct=round(100 * st.pstdev(Es) / E, 3) if E else "",
                           gpu_checked=gpu, error="")
                rows.append(row)
                print(f"    {suite}/{sf}/{qn}: {row['lat_mean_ms']:8.2f} ms  "
                      f"E {row['E_total_mJ']:9.1f} mJ  dyn {row['E_dyn_mJ']:8.1f}", flush=True)
            except hb.ConfigError as e:
                print(f"    {suite}/{sf}/{qn}: !! configuration check failed, aborting: {e}", flush=True)
                sys.exit(2)                # finished rows are already on disk; atexit restores power/clocks
            except Exception as e:
                rows.append(dict(pl_w=pl, sm_mhz=sm, suite=suite, sf=sf, query=qn, K="",
                                 lat_min_ms="", lat_mean_ms="", P_idle_W="", P_load_W="",
                                 E_total_mJ="", E_dyn_mJ="", duty="", cv_pct="",
                                 gpu_checked="", error=str(e)[:150]))
                print(f"    {suite}/{sf}/{qn}: FAIL {str(e)[:90]}", flush=True)
                if not hb.server_alive():
                    print("    !! server crashed, restarting", flush=True)
                    if hb.restart_server():
                        try:
                            guard.rebind() # the new process after restart must pass the check too
                        except hb.ConfigError as ce:
                            print(f"    !! configuration check failed after restart, aborting: {ce}", flush=True)
                            sys.exit(2)
                    else:
                        print("    !! restart failed, aborting", flush=True); sys.exit(2)
            finally:
                if sql:
                    sql.close()
            if rows:                       # write as we go, so the run can be resumed
                new = not os.path.exists(a.out)
                with open(a.out, "a", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    if new: w.writeheader()
                    w.writerows(rows[-1:])

restore(); atexit.unregister(restore)
print(f"-> {a.out}  took {(time.time()-t_start)/60:.0f} min")
