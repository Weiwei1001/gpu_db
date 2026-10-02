#!/usr/bin/env python3
"""Cat A: data resident on the GPU, measure 3 latency runs + power sampling (single GPU).

For every query:
  1. warm up to make sure the column data is already in GPU memory (Cat A semantics)
  2. run 3 times, recording the server-side execution time and the client wall clock of each run
  3. power: ΔE/Δt time series in 100 ms bins (matching the NVML energy counter update period)
     a short query yields no samples within a single window, so amplify by sending it K times back to back to reach >=2 s, then sample
"""
import argparse, csv, json, math, os, sys, time, threading
import statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hbench as hb

SAMPLE_DT = 0.1

class Sampler(threading.Thread):
    """Sample the energy counter and the driver power reading on a fixed grid."""
    def __init__(self):
        super().__init__(daemon=True)
        self.rows = []
        self.stop = threading.Event()
    def run(self):
        nxt = time.perf_counter()
        while not self.stop.is_set():
            t = time.perf_counter()
            self.rows.append((t, hb.energy_J(), hb.power_W()))
            nxt += SAMPLE_DT
            time.sleep(max(0.0, nxt - time.perf_counter()))
    def series(self, t0, t1):
        """(relative time, ΔE/Δt power, instantaneous driver power) within the window"""
        r = [x for x in self.rows if t0 - SAMPLE_DT <= x[0] <= t1 + SAMPLE_DT]
        out = []
        for i in range(len(r) - 1):
            dt = r[i + 1][0] - r[i][0]
            if dt <= 0: continue
            out.append((round((r[i][0] + r[i + 1][0]) / 2 - t0, 4),
                        round((r[i + 1][1] - r[i][1]) / dt, 2),
                        round(r[i][2], 2)))
        return out

ap = argparse.ArgumentParser()
ap.add_argument("--suite", required=True)
ap.add_argument("--db", required=True)
ap.add_argument("--sf", required=True)
ap.add_argument("--window", type=float, default=2.0, help="minimum amplification window for short queries (seconds)")
ap.add_argument("--timeout", type=int, default=900)
ap.add_argument("--out", default=os.path.join(hb.RESULTS_BASE, "powersample.csv"))
ap.add_argument("--traces", default=os.path.join(hb.RESULTS_BASE, "traces"))
ap.add_argument("--only", default="", help="run only these queries (comma-separated)")
ap.add_argument("--reps", type=int, default=3)
a = ap.parse_args()
REPS = a.reps

ok, others = hb.gpu_guard()
if not ok:
    print(f"!! other people's jobs {others} on GPU {hb.GPUS}", file=sys.stderr)

scr = os.path.join(hb.RESULTS_BASE, f"query_screen_{a.suite}.csv")
passlist = ({r["query"] for r in csv.DictReader(open(scr)) if r["ok"] == "True"}
            if os.path.exists(scr) else None)
qs = [q for q in hb.queries(a.suite) if passlist is None or q[0] in passlist]
if a.only:
    want = set(a.only.split(","))
    qs = [q for q in qs if q[0] in want]

os.makedirs(a.traces, exist_ok=True)
print(f"=== {a.suite}/{a.sf} (GPU {hb.GPUS}) {len(qs)} queries × {REPS} runs ===", flush=True)

smp = Sampler(); smp.start()
time.sleep(1.0)
p_idle = hb.idle_baseline_W(4.0)
print(f"idle baseline {p_idle:.1f} W", flush=True)

try:          # verify before and after every query: single process, PID unchanged, only on GPUS, power limit/SM clock as expected
    guard = hb.Guard()
except hb.ConfigError as e:
    smp.stop.set(); sys.exit(f"!! configuration check failed before the run, nothing measured: {e}")
sql = hb.Sql(a.db, "gpu")
rows, traces = [], {}

def dump():
    if not rows:
        return
    new = not os.path.exists(a.out)
    with open(a.out, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        if new: w.writeheader()
        w.writerows(rows)
def measure_one(sql, name, q):
    """Returns (result row, sql session); the caller handles exceptions."""
    return sql

for name, q in qs:
    attempt = 0
    while True:
      attempt += 1
      try:
          guard.check(f"before {name}")
          nrows = sql.rows_returned(q, a.timeout)
          qx = hb.Sql.count_wrap(q) if nrows > 10000 else q
          for _ in range(3):                      # warm-up: pull the column data into GPU memory
              t_ms = sql.run(qx, a.timeout)

          # --- 3 timed runs ---
          lat, wall, wins = [], [], []
          for _ in range(REPS):
              w0 = time.perf_counter()
              lat.append(sql.run(qx, a.timeout))
              w1 = time.perf_counter()
              wall.append((w1 - w0) * 1000)
              wins.append((w0, w1))

          # --- power sampling: a single window if long enough, amplify if too short ---
          amplified = t_ms < a.window * 1000 / 2
          if amplified:
              K = max(1, min(5000, math.ceil(a.window * 1000 / max(t_ms, 0.1))))
              e0 = hb.energy_J(); t0 = time.perf_counter()
              bl, _ = sql.run_burst(qx, K, timeout=max(a.timeout, 60 + K * 2))
              t1 = time.perf_counter(); e1 = hb.energy_J()
              ser = smp.series(t0, t1)
              E_win, T_win, duty = e1 - e0, t1 - t0, sum(bl) / 1000 / (t1 - t0)
              E_q = E_win / K * 1000
          else:
              K = 1
              e0 = hb.energy_J(); t0 = time.perf_counter()
              l1 = sql.run(qx, a.timeout)
              t1 = time.perf_counter(); e1 = hb.energy_J()
              ser = smp.series(t0, t1)
              E_win, T_win, duty = e1 - e0, t1 - t0, l1 / 1000 / (t1 - t0)
              E_q = E_win * 1000

          gpu = guard.check(f"after {name}")
          P_win = E_win / T_win if T_win else 0
          traces[name] = dict(sf=a.sf, amplified=amplified, K=K,
                              window_s=round(T_win, 4), lat_ms=lat,
                              samples=ser)
          rows.append(dict(
              suite=a.suite, sf=a.sf, db=a.db, query=name, n_rows=nrows,
              count_wrapped=nrows > 10000, reps=REPS,
              lat_ms=";".join(f"{x:g}" for x in lat),
              lat_min_ms=round(min(lat), 3), lat_mean_ms=round(st.mean(lat), 3),
              wall_min_ms=round(min(wall), 3), wall_mean_ms=round(st.mean(wall), 3),
              amplified=amplified, K=K, window_s=round(T_win, 4),
              duty=round(duty, 3), n_samples=len(ser),
              P_idle_W=round(p_idle, 2), P_win_W=round(P_win, 2),
              P_peak_W=round(max((s[1] for s in ser), default=0), 2),
              E_total_mJ=round(E_q, 3),
              E_dyn_mJ=round(E_q - p_idle * T_win / K * 1000, 3),
              gpu_checked=gpu, error=""))
          r = rows[-1]
          print(f"  {name:6s} lat {r['lat_min_ms']:8.1f}/{r['lat_mean_ms']:8.1f} ms "
                f"(wall {r['wall_mean_ms']:8.1f})  K={K:4d}  {len(ser):3d} samples  "
                f"peak {r['P_peak_W']:6.1f} W  E {r['E_total_mJ']:9.1f} mJ", flush=True)
          break
      except hb.ConfigError as e:
        print(f"  {name:6s} !! configuration check failed, aborting: {e}", flush=True)
        smp.stop.set(); sql.close(); dump(); sys.exit(2)
      except Exception as e:
        if not hb.server_alive():
            print(f"  {name:6s} server crashed, restarting{' and retrying' if attempt == 1 else ''}", flush=True)
            if hb.restart_server(num_gpus=1, start_gpu=hb.GPUS[0]):
                try:
                    guard.rebind()                # the new process after restart must pass the check too
                except hb.ConfigError as ce:
                    print(f"  !! configuration check failed after restart, aborting: {ce}", flush=True)
                    smp.stop.set(); dump(); sys.exit(2)
                sql = hb.Sql(a.db, "gpu")
                if attempt == 1:
                    continue                      # after a crash retry once before declaring failure
            else:
                print("  !! restart failed, aborting", flush=True)
                rows.append(dict(suite=a.suite, sf=a.sf, db=a.db, query=name, n_rows="",
                                 count_wrapped="", reps=0, lat_ms="", lat_min_ms="",
                                 lat_mean_ms="", wall_min_ms="", wall_mean_ms="",
                                 amplified="", K="", window_s="", duty="", n_samples="",
                                 P_idle_W="", P_win_W="", P_peak_W="", E_total_mJ="",
                                 E_dyn_mJ="", gpu_checked="", error="restart failed: " + str(e)[:150]))
                break
        rows.append(dict(suite=a.suite, sf=a.sf, db=a.db, query=name, n_rows="",
                         count_wrapped="", reps=0, lat_ms="", lat_min_ms="",
                         lat_mean_ms="", wall_min_ms="", wall_mean_ms="",
                         amplified="", K="", window_s="", duty="", n_samples="",
                         P_idle_W="", P_win_W="", P_peak_W="", E_total_mJ="",
                         E_dyn_mJ="", gpu_checked="", error=str(e)[:200]))
        print(f"  {name:6s} FAIL{' (still failing after retry)' if attempt > 1 else ''} {str(e)[:90]}", flush=True)
        break

smp.stop.set(); smp.join(); sql.close()
with open(os.path.join(a.traces, f"{a.suite}_{a.sf}.json"), "w") as f:
    json.dump(dict(suite=a.suite, sf=a.sf, gpus=hb.GPUS, sample_dt_s=SAMPLE_DT,
                   p_idle_W=p_idle, traces=traces), f)
dump()
print(f"-> {a.out} ({len(rows)} rows) + traces/{a.suite}_{a.sf}.json", flush=True)
