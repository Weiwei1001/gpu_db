#!/usr/bin/env python3
"""E4 能耗实验：放大法 + NVML 硬件能量计数器（D3 修正口径）。

每条 query 重复 K 次把窗口撑到 >= --window 秒，用能量计数器测窗口能量，
再除以 K。同时记录论文口径 (P_mean x min_ms) 以量化两者差异。
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
ap.add_argument("--window", type=float, default=2.0, help="放大窗口下限（秒）")
ap.add_argument("--max-reps", type=int, default=5000)
ap.add_argument("--timeout", type=int, default=900)
ap.add_argument("--only", default="")
ap.add_argument("--cold", action="store_true",
                help="Cat B: 每次执行前 \\clear_gpu，让数据重新从 CPU 内存传入")
ap.add_argument("--out", default=os.path.join(hb.RESULTS_BASE, "energy.csv"))
a = ap.parse_args()

ok, others = hb.gpu_guard()
if not ok:
    print(f"!! GPU 上有别人的任务 {others}", file=sys.stderr)

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

print(f"=== 能耗 {a.suite}/{a.sf}: {len(qs)} 条, {a.trials} trials, 窗口>={a.window}s ===", flush=True)
try:                               # 每条 query 前后核对：单进程、PID 未变、只在 GPUS 上
    guard = hb.Guard()
except hb.ConfigError as e:
    sys.exit(f"!! 开跑前配置检查失败，未测任何数据：{e}")
sql = hb.Sql(a.db, "gpu")

p_idle0 = hb.idle_baseline_W(4.0)
print(f"空载基线 {p_idle0:.1f} W（GPU {hb.GPUS} 合计）", flush=True)
if p_idle0 > 150 * len(hb.GPUS):
    sys.exit(f"空载基线 {p_idle0:.0f} W 远高于预期（~120 W/卡）——GPU {hb.GPUS} 上有别人的负载，"
             f"此时测能耗无意义，中止")

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
        guard.check(f"{name} 前")
        nrows = sql.rows_returned(q, a.timeout)
        q_burst, wrapped = q, False
        if nrows > 10000:
            q_burst, wrapped = hb.Sql.count_wrap(q), True
        t_ms = sql.run(q_burst, a.timeout)
        trials = a.trials
        if t_ms > 10000:            # 单次就超过 10s 的慢 query：少做几轮
            trials = min(trials, 3)
        else:
            for _ in range(2):
                t_ms = sql.run(q_burst, a.timeout)
        K = max(1, min(a.max_reps, math.ceil(a.window * 1000.0 / max(t_ms, 0.1))))
        if a.cold:
            K = max(1, min(K, 20))        # 冷缓存每次都要重传，窗口靠次数撑不划算
        per_trial = []
        for _ in range(trials):
            e0 = hb.energy_J(); t0 = time.perf_counter()
            if a.cold:
                # Cat B：冷缓存必须逐条跑（每条前要 clear_gpu），不能 burst
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
        gpu = guard.check(f"{name} 后")
        Eq_tot = [d["E"] / K * 1000 for d in per_trial]                    # mJ/query 总能耗
        Eq_dyn = [(d["E"] - p_idle0 * d["T"]) / K * 1000 for d in per_trial]  # mJ/query 动态
        P = [d["P"] for d in per_trial]
        lat_min = min(d["lat_min"] for d in per_trial)
        lat_mean = st.mean([d["lat_mean"] for d in per_trial])
        cv = 100 * st.pstdev(Eq_tot) / st.mean(Eq_tot) if st.mean(Eq_tot) else 0
        ci = 1.96 * (st.stdev(Eq_tot) / math.sqrt(len(Eq_tot))) if len(Eq_tot) > 1 else 0
        paper_mJ = st.mean(P) * lat_min          # 论文口径: P_steady x min_ms
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
              f"论文口径偏差 {r['paper_err_pct']:+.1f}%", flush=True)
    except hb.ConfigError as e:
        print(f"  {name:6s} !! 配置检查失败，中止：{e}", flush=True)
        sql.close(); dump(rows); sys.exit(2)
    except Exception as e:
        rows.append(dict(suite=a.suite, sf=a.sf, query=name, K="", cold=a.cold, n_rows="",
                         count_wrapped="", window_s="",
                         lat_min_ms="", lat_mean_ms="", P_load_W="", P_idle_W="", duty="",
                         E_total_mJ="", E_dyn_mJ="", cv_pct="", ci95_mJ="",
                         E_paper_mJ="", paper_err_pct="", gpu_checked="", error=str(e)[:200]))
        print(f"  {name:6s} FAIL {str(e)[:110]}", flush=True)
        if not hb.server_alive():
            print("  !! HeavyDB 已崩溃，重启后继续", flush=True)
            rows[-1]["error"] = "服务端崩溃: " + rows[-1]["error"]
            if hb.restart_server():
                try:
                    guard.rebind()         # 重启后的新进程也必须通过检查
                except hb.ConfigError as ce:
                    print(f"  !! 重启后配置检查失败，中止：{ce}", flush=True)
                    dump(rows); sys.exit(2)
                sql = hb.Sql(a.db, "gpu")
            else:
                print("  !! 重启失败，中止", flush=True); dump(rows); sys.exit(2)

sql.close()
print(f"收尾空载 {hb.idle_baseline_W(4.0):.1f} W", flush=True)
dump(rows)
