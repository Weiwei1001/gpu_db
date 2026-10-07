#!/usr/bin/env python3
"""Regenerate sample/README.md from the CSVs in this directory."""
import csv, json, os
D = os.path.dirname(os.path.abspath(__file__))
def row(fn, **kw):
    rs = list(csv.DictReader(open(f"{D}/{fn}")))
    for r in rs:
        if all(r.get(k) == v for k, v in kw.items()):
            return r
    return rs[0]
A, Bt, Be = row("catA_powersample.csv"), row("catB_timing.csv"), row("catB_energy.csv")
tr = json.load(open(f"{D}/trace_tpch_sf10_q1.json")); T = tr["traces"]["q1"]
C = list(csv.DictReader(open(f"{D}/catC_grid.csv")))
best = min(C, key=lambda r: float(r["E_dyn_mJ"]))
dflt = [r for r in C if r["pl_w"] == "700" and r["sm_mhz"] == "1980"][0]

L = []; w = L.append
w("# Sample: every metric for TPC-H SF10 / q1\n")
w("Everything a single query produces in this harness. The SQL is in `q1.sql`: six aggregates and")
w("a two-key group-by over `lineitem`, filtered by `l_shipdate <= DATE '1998-09-02'`, scanning")
w("59,986,052 rows.\n")
w("Environment: single H100 80GB, HeavyDB built from source (master b348f14), power taken from the")
w("NVML hardware energy counter at 10 Hz.\n")

w("## 1. Dialect screening — `query_screen_tpch.csv`\n")
w("| | |\n|---|---|")
w("| Runs as written | yes |")
w("| First execution (includes compilation) | 10 ms |\n")

w("## 2. Category A: data resident on GPU — `catA_powersample.csv`\n")
w("| Column | Value | Meaning |\n|---|---|---|")
w(f"| `lat_ms` | **{A['lat_ms']}** | the three raw execution times (ms) as reported by the server |")
w(f"| `lat_min_ms` / `lat_mean_ms` | {A['lat_min_ms']} / {A['lat_mean_ms']} | min and mean |")
w(f"| `wall_min_ms` / `wall_mean_ms` | {A['wall_min_ms']} / {A['wall_mean_ms']} | client wall clock. The ~10 ms gap is the client round trip; the server reports whole milliseconds only, so this column is the cross-check |")
w(f"| `n_rows` / `count_wrapped` | {A['n_rows']} / {A['count_wrapped']} | {A['n_rows']} result rows, no `COUNT(*)` wrapper needed (only applied above 10000 rows) |")
w(f"| `P_idle_W` | {A['P_idle_W']} | idle baseline: server up, data in device memory, no query running |")
w(f"| `P_win_W` | {A['P_win_W']} | mean power over the measurement window |")
w(f"| `P_peak_W` | **{A['P_peak_W']}** | peak power, {float(A['P_peak_W'])-float(A['P_idle_W']):.0f} W above idle |")
w(f"| `E_total_mJ` | {A['E_total_mJ']} | total energy per query = window energy / K |")
w(f"| `E_dyn_mJ` | **{A['E_dyn_mJ']}** | dynamic energy = total − idle power × duration. Only {100*float(A['E_dyn_mJ'])/float(A['E_total_mJ']):.0f}% of the total is what the query actually costs; the rest is the H100 idling |")
w(f"| `amplified` / `K` | {A['amplified']} / {A['K']} | a single run is only ~{A['lat_mean_ms']} ms, too short to sample at 100 ms, so it is fired {A['K']} times to stretch the window |")
w(f"| `window_s` / `duty` | {A['window_s']} / {A['duty']} | a {A['window_s']} s window, {100*float(A['duty']):.0f}% of which is spent actually executing |")
w(f"| `n_samples` | {A['n_samples']} | power samples inside that window |\n")

w("## 3. Category B: data in CPU memory, PCIe transfer included\n")
w("`\\clear_gpu` before each run forces the data back across PCIe.\n")
w("### Latency — `catB_timing.csv`\n")
w("| Column | Value |\n|---|---|")
for k in ("reps", "min_ms", "mean_ms", "median_ms", "p95_ms", "max_ms", "cv_pct", "wall_mean_ms"):
    w(f"| `{k}` | {Bt[k]} |")
w(f"\n**Against Cat A**: {Bt['min_ms']} ms vs {A['lat_min_ms']} ms — a cold cache is "
  f"**{float(Bt['min_ms'])/float(A['lat_min_ms']):.1f}x** slower, and that gap is the cost of moving "
  f"the lineitem columns back into device memory.\n")
w("### Energy — `catB_energy.csv`\n")
w("| Column | Value | Meaning |\n|---|---|---|")
w(f"| `E_dyn_mJ` | **{Be['E_dyn_mJ']}** | dynamic energy, against {A['E_dyn_mJ']} for Cat A ({float(Be['E_dyn_mJ'])/float(A['E_dyn_mJ']):.1f}x) |")
w(f"| `E_total_mJ` | {Be['E_total_mJ']} | **not usable directly**: duty is only {Be['duty']}, so most of the window is `clear_gpu` and this figure is inflated by idle draw |")
w(f"| `duty` | {Be['duty']} | fraction of the window actually executing |")
w(f"| `ci95_mJ` | {Be['ci95_mJ']} | 95% confidence interval (± mJ) |")
w(f"| `cv_pct` | {Be['cv_pct']} | coefficient of variation across repetitions |")
w(f"| `E_paper_mJ` | {Be['E_paper_mJ']} | what the paper's `P_steady × min_ms` formula gives |")
w(f"| `paper_err_pct` | **{Be['paper_err_pct']}%** | that formula's deviation from the hardware counter |\n")

w("## 4. Power time series — `trace_tpch_sf10_q1.json`\n")
w(f"{len(T['samples'])} samples, one every 100 ms. Plotted in `power_time_tpch_sf10_q1.png`.\n")
w("| Time (s) | dE/dt power (W) | driver power.draw (W) |\n|---|---|---|")
for s in T["samples"]:
    w(f"| {s[0]:.2f} | {s[1]:.1f} | {s[2]:.1f} |")
w("\nBoth columns are recorded deliberately:")
w(f"- **dE/dt**, from the hardware energy counter, jumps to ~{max(s[1] for s in T['samples']):.0f} W as soon as the load starts")
w(f"- **power.draw**, the driver's 1 s moving average, crawls from {T['samples'][0][2]:.0f} W to {T['samples'][-1][2]:.0f} W and needs about a second to catch up\n")
w("This is the direct evidence that the driver reading cannot resolve a millisecond-scale query —")
w("exactly what is needed to answer a reviewer challenging the energy measurement methodology.\n")

w("## 5. Category C: this query across the 5x5 grid — `catC_grid.csv`\n")
w("Five power limits x five SM clocks, 25 configurations. `catC_A.csv`, `catC_B_timing.csv` and")
w("`catC_B_energy.csv` hold the full Cat A and Cat B re-runs at each of those points (25 rows each).\n")
w("| Configuration | Dynamic energy | Mean latency |\n|---|---|---|")
w(f"| default, 700 W / 1980 MHz | {dflt['E_dyn_mJ']} mJ | {dflt['lat_mean_ms']} ms |")
w(f"| **lowest energy, {best['pl_w']} W / {best['sm_mhz']} MHz** | **{best['E_dyn_mJ']} mJ** | {best['lat_mean_ms']} ms |")
de = 100*(float(dflt['E_dyn_mJ'])-float(best['E_dyn_mJ']))/float(dflt['E_dyn_mJ'])
dl = 100*(float(best['lat_mean_ms'])-float(dflt['lat_mean_ms']))/float(dflt['lat_mean_ms'])
w(f"\n→ **{de:.0f}% less energy** for **{dl:+.0f}%** latency.\n")

w("## Files\n")
w("| File | Contents |\n|---|---|")
w("| `q1.sql` | the SQL |")
w("| `catA_powersample.csv` | Cat A, 1 row x 23 columns |")
w("| `catB_timing.csv` | Cat B latency, 1 row x 19 columns |")
w("| `catB_energy.csv` | Cat B energy, 1 row x 21 columns |")
w("| `trace_tpch_sf10_q1.json` | the power time series |")
w("| `power_time_tpch_sf10_q1.png` | the plot |")
w("| `plot_power.py` | the plotting script |")
w("| `catC_grid.csv` | the 5x5 grid, 25 rows |")
w("| `catC_A.csv` / `catC_B_timing.csv` / `catC_B_energy.csv` | A/B re-run at each grid point, 25 rows each |")
open(f"{D}/README.md", "w").write("\n".join(L) + "\n")
print("-> sample/README.md")
