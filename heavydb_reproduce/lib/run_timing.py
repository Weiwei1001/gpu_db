#!/usr/bin/env python3
"""E1/E2/E3 计时实验：GPU warm / CPU 模式 / 冷 GPU。

用法: run_timing.py --suite tpch --db tpch_sf1 --sf sf1 --mode gpu --reps 20
"""
import argparse, csv, json, os, sys, time
import statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hbench as hb

ap = argparse.ArgumentParser()
ap.add_argument("--suite", required=True)
ap.add_argument("--db", required=True)
ap.add_argument("--sf", required=True)
ap.add_argument("--mode", default="gpu", choices=["gpu", "cpu"])
ap.add_argument("--reps", type=int, default=20)
ap.add_argument("--warmup", type=int, default=3)
ap.add_argument("--cold", action="store_true", help="每次执行前 \\clear_gpu")
ap.add_argument("--timeout", type=int, default=900)
ap.add_argument("--budget", type=float, default=15.0)
ap.add_argument("--min-reps", type=int, default=5)
ap.add_argument("--out", default=os.path.join(hb.RESULTS_BASE, "timing.csv"))
ap.add_argument("--only", default="", help="逗号分隔的 query 名，限定子集")
a = ap.parse_args()

ok, others = hb.gpu_guard()
if not ok:
    print(f"!! GPU {hb.GPUS} 上有别人的任务 {others} —— 结果会被污染", file=sys.stderr)

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

tag = f"{a.suite}/{a.sf}/{a.mode}{'/cold' if a.cold else ''}"
print(f"=== {tag}: {len(qs)} 条 query, {a.reps} reps ===", flush=True)

try:                               # 每条 query 前后核对：单进程、PID 未变、只在 GPUS 上
    guard = hb.Guard()
except hb.ConfigError as e:
    sys.exit(f"!! 开跑前配置检查失败，未测任何数据：{e}")
sql = hb.Sql(a.db, a.mode)
rows = []

def dump(rows):
    if not rows:
        return
    new = not os.path.exists(a.out)
    with open(a.out, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"-> {a.out}  ({len(rows)} 行)")
for name, q in qs:
    try:
        guard.check(f"{name} 前")
        reps, min_reps = a.reps, a.min_reps
        # 结果集很大时客户端打印会淹没执行时间（H2O q10 曾 236 ms 执行 / 140 s 墙钟）。
        # repo 的 maxbench 把结果留在显存、从不回传，包一层 COUNT(*) 反而更贴近其语义。
        nrows = sql.rows_returned(q, a.timeout)
        wrapped = nrows > 10000
        if wrapped:
            q = hb.Sql.count_wrap(q)
        if not a.cold and a.warmup:
            w0 = time.time()
            sql.run(q, a.timeout)                      # 第一次热身顺便探时长
            slow = time.time() - w0 > 3.0
            if slow:                                    # 慢 query：少跑几次，别把预算耗光
                reps, min_reps = min(reps, 3), 2
            else:
                for _ in range(a.warmup - 1):
                    sql.run(q, a.timeout)
        ts, ws = [], []
        t_begin = time.time()
        for i in range(reps):
            if a.cold:
                sql.cmd("\\clear_gpu")
            w0 = time.perf_counter()
            ts.append(sql.run(q, a.timeout))
            ws.append((time.perf_counter() - w0) * 1000)
            if i + 1 >= min_reps and time.time() - t_begin > a.budget:
                break
        gpu = guard.check(f"{name} 后")
        ts_s = sorted(ts)
        rows.append(dict(
            suite=a.suite, sf=a.sf, db=a.db, mode=a.mode,
            cold=a.cold, query=name, reps=len(ts),
            min_ms=round(min(ts), 3), mean_ms=round(st.mean(ts), 3),
            median_ms=round(st.median(ts), 3),
            p95_ms=round(ts_s[max(0, int(0.95 * len(ts)) - 1)], 3),
            max_ms=round(max(ts), 3),
            cv_pct=round(100 * st.pstdev(ts) / st.mean(ts), 2) if st.mean(ts) else "",
            n_rows=nrows, count_wrapped=wrapped,
            wall_min_ms=round(min(ws), 3), wall_mean_ms=round(st.mean(ws), 3),
            gpu_checked=gpu, error=""))
        print(f"  {name:6s} min {min(ts):9.2f}  mean {st.mean(ts):9.2f}  "
              f"CV {rows[-1]['cv_pct']}%", flush=True)
    except hb.ConfigError as e:
        print(f"  {name:6s} !! 配置检查失败，中止：{e}", flush=True)
        sql.close(); dump(rows); sys.exit(2)
    except Exception as e:
        rows.append(dict(suite=a.suite, sf=a.sf, db=a.db, mode=a.mode, cold=a.cold,
                         query=name, reps=0, min_ms="", mean_ms="", median_ms="",
                         p95_ms="", max_ms="", cv_pct="", n_rows="", count_wrapped="",
                         wall_min_ms="", wall_mean_ms="", gpu_checked="", error=str(e)[:200]))
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
                sql = hb.Sql(a.db, a.mode)
            else:
                print("  !! 重启失败，中止", flush=True); dump(rows); sys.exit(2)
sql.close()
dump(rows)
