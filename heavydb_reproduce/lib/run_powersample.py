#!/usr/bin/env python3
"""Cat A：数据常驻 GPU，测 3 次延迟 + 功率采样（单卡）。

每条 query：
  1. 预热，确保列数据已进显存（Cat A 语义）
  2. 跑 3 次，记每次的服务端 execution time 和客户端墙钟
  3. 功率：100 ms 一个 bin 的 ΔE/Δt 时间序列（对齐 NVML 能量计数器更新周期）
     短 query 单次窗口采不到点，用放大法连发 K 次撑到 >=2 s 再采
"""
import argparse, csv, json, math, os, sys, time, threading
import statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hbench as hb

SAMPLE_DT = 0.1

class Sampler(threading.Thread):
    """按固定网格采能量计数器与驱动功率读数。"""
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
        """窗口内的 (相对时刻, ΔE/Δt 功率, 驱动瞬时功率)"""
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
ap.add_argument("--window", type=float, default=2.0, help="短 query 的放大窗口下限（秒）")
ap.add_argument("--timeout", type=int, default=900)
ap.add_argument("--out", default=os.path.join(hb.RESULTS_BASE, "powersample.csv"))
ap.add_argument("--traces", default=os.path.join(hb.RESULTS_BASE, "traces"))
ap.add_argument("--only", default="", help="只跑这些 query（逗号分隔）")
ap.add_argument("--reps", type=int, default=3)
a = ap.parse_args()
REPS = a.reps

ok, others = hb.gpu_guard()
if not ok:
    print(f"!! GPU {hb.GPUS} 上有别人的任务 {others}", file=sys.stderr)

scr = os.path.join(hb.RESULTS_BASE, f"query_screen_{a.suite}.csv")
passlist = ({r["query"] for r in csv.DictReader(open(scr)) if r["ok"] == "True"}
            if os.path.exists(scr) else None)
qs = [q for q in hb.queries(a.suite) if passlist is None or q[0] in passlist]
if a.only:
    want = set(a.only.split(","))
    qs = [q for q in qs if q[0] in want]

os.makedirs(a.traces, exist_ok=True)
print(f"=== {a.suite}/{a.sf} (GPU {hb.GPUS}) {len(qs)} 条 × {REPS} 次 ===", flush=True)

smp = Sampler(); smp.start()
time.sleep(1.0)
p_idle = hb.idle_baseline_W(4.0)
print(f"空载基线 {p_idle:.1f} W", flush=True)

try:          # 每条 query 前后核对：单进程、PID 未变、只在 GPUS 上、功率上限/SM 时钟符合预期
    guard = hb.Guard()
except hb.ConfigError as e:
    smp.stop.set(); sys.exit(f"!! 开跑前配置检查失败，未测任何数据：{e}")
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
    """返回 (结果行, sql 会话)；调用方负责异常处理。"""
    return sql

for name, q in qs:
    attempt = 0
    while True:
      attempt += 1
      try:
          guard.check(f"{name} 前")
          nrows = sql.rows_returned(q, a.timeout)
          qx = hb.Sql.count_wrap(q) if nrows > 10000 else q
          for _ in range(3):                      # 预热：把列数据拉进显存
              t_ms = sql.run(qx, a.timeout)

          # --- 3 次计时 ---
          lat, wall, wins = [], [], []
          for _ in range(REPS):
              w0 = time.perf_counter()
              lat.append(sql.run(qx, a.timeout))
              w1 = time.perf_counter()
              wall.append((w1 - w0) * 1000)
              wins.append((w0, w1))

          # --- 功率采样：够长就用单次窗口，太短就放大 ---
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

          gpu = guard.check(f"{name} 后")
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
                f"(墙钟 {r['wall_mean_ms']:8.1f})  K={K:4d}  {len(ser):3d} 个采样点  "
                f"峰值 {r['P_peak_W']:6.1f} W  E {r['E_total_mJ']:9.1f} mJ", flush=True)
          break
      except hb.ConfigError as e:
        print(f"  {name:6s} !! 配置检查失败，中止：{e}", flush=True)
        smp.stop.set(); sql.close(); dump(); sys.exit(2)
      except Exception as e:
        if not hb.server_alive():
            print(f"  {name:6s} 服务端崩溃，重启{'并重试' if attempt == 1 else ''}", flush=True)
            if hb.restart_server(num_gpus=1, start_gpu=hb.GPUS[0]):
                try:
                    guard.rebind()                # 重启后的新进程也必须通过检查
                except hb.ConfigError as ce:
                    print(f"  !! 重启后配置检查失败，中止：{ce}", flush=True)
                    smp.stop.set(); dump(); sys.exit(2)
                sql = hb.Sql(a.db, "gpu")
                if attempt == 1:
                    continue                      # 崩溃后重试一次再判失败
            else:
                print("  !! 重启失败，中止", flush=True)
                rows.append(dict(suite=a.suite, sf=a.sf, db=a.db, query=name, n_rows="",
                                 count_wrapped="", reps=0, lat_ms="", lat_min_ms="",
                                 lat_mean_ms="", wall_min_ms="", wall_mean_ms="",
                                 amplified="", K="", window_s="", duty="", n_samples="",
                                 P_idle_W="", P_win_W="", P_peak_W="", E_total_mJ="",
                                 E_dyn_mJ="", gpu_checked="", error="重启失败: " + str(e)[:150]))
                break
        rows.append(dict(suite=a.suite, sf=a.sf, db=a.db, query=name, n_rows="",
                         count_wrapped="", reps=0, lat_ms="", lat_min_ms="",
                         lat_mean_ms="", wall_min_ms="", wall_mean_ms="",
                         amplified="", K="", window_s="", duty="", n_samples="",
                         P_idle_W="", P_win_W="", P_peak_W="", E_total_mJ="",
                         E_dyn_mJ="", gpu_checked="", error=str(e)[:200]))
        print(f"  {name:6s} FAIL{'（重试后仍失败）' if attempt > 1 else ''} {str(e)[:90]}", flush=True)
        break

smp.stop.set(); smp.join(); sql.close()
with open(os.path.join(a.traces, f"{a.suite}_{a.sf}.json"), "w") as f:
    json.dump(dict(suite=a.suite, sf=a.sf, gpus=hb.GPUS, sample_dt_s=SAMPLE_DT,
                   p_idle_W=p_idle, traces=traces), f)
dump()
print(f"-> {a.out} ({len(rows)} 行) + traces/{a.suite}_{a.sf}.json", flush=True)
