#!/usr/bin/env python3
"""E4 energy experiment: amplification + NVML hardware energy counter (the corrected D3 methodology).

Each query is repeated K times to stretch the window to >= --window seconds; the window energy is
measured with the energy counter and divided by K. The paper's methodology (P_mean x min_ms) is recorded
alongside to quantify the difference between the two.
"""
import argparse, csv, math, os, sys, time
import statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hbench as hb

ap = argparse.ArgumentParser()
ap.add_argument("--suite", required=True)
ap.add_argument("--db", required=True)
ap.add_argument("--sf", required=True)
ap.add_argument("--trials", type=int, default=5)
ap.add_argument("--window", type=float, default=2.0, help="minimum amplification window (seconds)")
ap.add_argument("--max-reps", type=int, default=5000)
ap.add_argument("--timeout", type=int, default=900)
ap.add_argument("--only", default="")
ap.add_argument("--cold", action="store_true",
                help="Cat B: \\clear_gpu before every execution so the data is transferred from CPU memory again")
ap.add_argument("--out", default=os.path.join(hb.RESULTS_BASE, "energy.csv"))
a = ap.parse_args()

ok, others = hb.gpu_guard()
if not ok:
    print(f"!! other people's jobs {others} on the GPU", file=sys.stderr)

passlist = None
scr = os.path.join(hb.RESULTS_BASE, f"query_screen_{a.suite}.csv")
if os.path.exists(scr):
    passlist = {r["query"] for r in csv.DictReader(open(scr)) if r["ok"] == "True"}
qs = hb.queries(a.suite)
if a.only:
    want = set(a.only.split(","))
    qs = [q for q in qs if q[0] in want]
elif passlist is not None:
    qs = [q for q in qs if q[0] in passlist]

print(f"=== energy {a.suite}/{a.sf}: {len(qs)} queries, {a.trials} trials, window>={a.window}s ===", flush=True)
try:                               # verify before and after every query: single process, PID unchanged, only on GPUS
    guard = hb.Guard()
except hb.ConfigError as e:
    sys.exit(f"!! configuration check failed before the run, nothing measured: {e}")
sql = hb.Sql(a.db, "gpu")

p_idle0 = hb.idle_baseline_W(4.0)
print(f"idle baseline {p_idle0:.1f} W (GPU {hb.GPUS} total)", flush=True)
if p_idle0 > 150 * len(hb.GPUS):
    sys.exit(f"idle baseline {p_idle0:.0f} W is far above the expected (~120 W per GPU) -- someone else's load is on GPU {hb.GPUS}; "
             f"measuring energy now would be meaningless, aborting")

rows = []

def dump(rows):
    if not rows:
        return
    new = not os.path.exists(a.out)
    with open(a.out, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        if new: w.writeheader()
        w.writerows(rows)
    print(f"-> {a.out}")
for name, q in qs:
    try:
        guard.check(f"before {name}")
        nrows = sql.rows_returned(q, a.timeout)
        q_burst, wrapped = q, False
        if nrows > 10000:
            q_burst, wrapped = hb.Sql.count_wrap(q), True
        t_ms = sql.run(q_burst, a.timeout)
        trials = a.trials
        if t_ms > 10000:            # slow query (a single run already exceeds 10 s): do fewer trials
            trials = min(trials, 3)
        else:
            for _ in range(2):
                t_ms = sql.run(q_burst, a.timeout)
        K = max(1, min(a.max_reps, math.ceil(a.window * 1000.0 / max(t_ms, 0.1))))
        if a.cold:
            K = max(1, min(K, 20))        # cold cache re-transfers every time; stretching the window by repetition is not worth it
        per_trial = []
        for _ in range(trials):
            e0 = hb.energy_J(); t0 = time.perf_counter()
            if a.cold:
                # Cat B: cold cache must run one by one (clear_gpu before each), no burst
                lat = []
                for _ in range(K):
                    sql.cmd("\\clear_gpu")
                    lat.append(sql.run(q_burst, a.timeout))
            else:
                lat, _ = sql.run_burst(q_burst, K, timeout=max(a.timeout, 60 + K * 2))
            t1 = time.perf_counter(); e1 = hb.energy_J()
            T = t1 - t0
            E = e1 - e0
            per_trial.append(dict(E=E, T=T, P=E / T, lat_min=min(lat),
                                  lat_mean=st.mean(lat),
                                  duty=sum(lat) / 1000.0 / T))
        gpu = guard.check(f"after {name}")
        Eq_tot = [d["E"] / K * 1000 for d in per_trial]                    # mJ/query total energy
        Eq_dyn = [(d["E"] - p_idle0 * d["T"]) / K * 1000 for d in per_trial]  # mJ/query dynamic
        P = [d["P"] for d in per_trial]
        lat_min = min(d["lat_min"] for d in per_trial)
        lat_mean = st.mean([d["lat_mean"] for d in per_trial])
        cv = 100 * st.pstdev(Eq_tot) / st.mean(Eq_tot) if st.mean(Eq_tot) else 0
        ci = 1.96 * (st.stdev(Eq_tot) / math.sqrt(len(Eq_tot))) if len(Eq_tot) > 1 else 0
        paper_mJ = st.mean(P) * lat_min          # paper methodology: P_steady x min_ms
        rows.append(dict(
            suite=a.suite, sf=a.sf, query=name, K=K, cold=a.cold,
            n_rows=nrows, count_wrapped=wrapped,
            window_s=round(st.mean([d["T"] for d in per_trial]), 3),
            lat_min_ms=round(lat_min, 3), lat_mean_ms=round(lat_mean, 3),
            P_load_W=round(st.mean(P), 2), P_idle_W=round(p_idle0, 2),
            duty=round(st.mean([d["duty"] for d in per_trial]), 3),
            E_total_mJ=round(st.mean(Eq_tot), 3), E_dyn_mJ=round(st.mean(Eq_dyn), 3),
            cv_pct=round(cv, 3), ci95_mJ=round(ci, 3),
            E_paper_mJ=round(paper_mJ, 3),
            paper_err_pct=round(100 * (paper_mJ - st.mean(Eq_tot)) / st.mean(Eq_tot), 2),
            gpu_checked=gpu, error=""))
        r = rows[-1]
        print(f"  {name:6s} K={K:5d} {r['lat_mean_ms']:8.2f} ms  "
              f"E={r['E_total_mJ']:9.2f} mJ (dyn {r['E_dyn_mJ']:8.2f})  "
              f"duty {r['duty']:.2f}  CV {r['cv_pct']:.2f}%  "
              f"paper-method deviation {r['paper_err_pct']:+.1f}%", flush=True)
    except hb.ConfigError as e:
        print(f"  {name:6s} !! configuration check failed, aborting: {e}", flush=True)
        sql.close(); dump(rows); sys.exit(2)
    except Exception as e:
        rows.append(dict(suite=a.suite, sf=a.sf, query=name, K="", cold=a.cold, n_rows="",
                         count_wrapped="", window_s="",
                         lat_min_ms="", lat_mean_ms="", P_load_W="", P_idle_W="", duty="",
                         E_total_mJ="", E_dyn_mJ="", cv_pct="", ci95_mJ="",
                         E_paper_mJ="", paper_err_pct="", gpu_checked="", error=str(e)[:200]))
        print(f"  {name:6s} FAIL {str(e)[:110]}", flush=True)
        if not hb.server_alive():
            print("  !! HeavyDB crashed, restarting and continuing", flush=True)
            rows[-1]["error"] = "server crashed: " + rows[-1]["error"]
            if hb.restart_server():
                try:
                    guard.rebind()         # the new process after restart must pass the check too
                except hb.ConfigError as ce:
                    print(f"  !! configuration check failed after restart, aborting: {ce}", flush=True)
                    dump(rows); sys.exit(2)
                sql = hb.Sql(a.db, "gpu")
            else:
                print("  !! restart failed, aborting", flush=True); dump(rows); sys.exit(2)

sql.close()
print(f"final idle {hb.idle_baseline_W(4.0):.1f} W", flush=True)
dump(rows)
