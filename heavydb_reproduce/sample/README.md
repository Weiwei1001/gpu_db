# Sample: every metric for TPC-H SF10 / q1

Everything a single query produces in this harness. The SQL is in `q1.sql`: six aggregates and
a two-key group-by over `lineitem`, filtered by `l_shipdate <= DATE '1998-09-02'`, scanning
59,986,052 rows.

Environment: single H100 80GB, HeavyDB built from source (master b348f14), power taken from the
NVML hardware energy counter at 10 Hz.

## 1. Dialect screening — `query_screen_tpch.csv`

| | |
|---|---|
| Runs as written | yes |
| First execution (includes compilation) | 10 ms |

## 2. Category A: data resident on GPU — `catA_powersample.csv`

| Column | Value | Meaning |
|---|---|---|
| `lat_ms` | **16;15;14** | the three raw execution times (ms) as reported by the server |
| `lat_min_ms` / `lat_mean_ms` | 14.0 / 15.0 | min and mean |
| `wall_min_ms` / `wall_mean_ms` | 23.306 / 24.126 | client wall clock. The ~10 ms gap is the client round trip; the server reports whole milliseconds only, so this column is the cross-check |
| `n_rows` / `count_wrapped` | 4 / False | 4 result rows, no `COUNT(*)` wrapper needed (only applied above 10000 rows) |
| `P_idle_W` | 118.19 | idle baseline: server up, data in device memory, no query running |
| `P_win_W` | 187.33 | mean power over the measurement window |
| `P_peak_W` | **199.11** | peak power, 81 W above idle |
| `E_total_mJ` | 3301.264 | total energy per query = window energy / K |
| `E_dyn_mJ` | **1218.385** | dynamic energy = total − idle power × duration. Only 37% of the total is what the query actually costs; the rest is the H100 idling |
| `amplified` / `K` | True / 87 | a single run is only ~15.0 ms, too short to sample at 100 ms, so it is fired 87 times to stretch the window |
| `window_s` / `duty` | 1.5332 / 0.86 | a 1.5332 s window, 86% of which is spent actually executing |
| `n_samples` | 16 | power samples inside that window |

## 3. Category B: data in CPU memory, PCIe transfer included

`\clear_gpu` before each run forces the data back across PCIe.

### Latency — `catB_timing.csv`

| Column | Value |
|---|---|
| `reps` | 3 |
| `min_ms` | 142.0 |
| `mean_ms` | 147.0 |
| `median_ms` | 148.0 |
| `p95_ms` | 148.0 |
| `max_ms` | 151.0 |
| `cv_pct` | 2.55 |
| `wall_mean_ms` | 158.972 |

**Against Cat A**: 142.0 ms vs 14.0 ms — a cold cache is **10.1x** slower, and that gap is the cost of moving the lineitem columns back into device memory.

### Energy — `catB_energy.csv`

| Column | Value | Meaning |
|---|---|---|
| `E_dyn_mJ` | **2163.908** | dynamic energy, against 1218.385 for Cat A (1.8x) |
| `E_total_mJ` | 55801.733 | **not usable directly**: duty is only 0.315, so most of the window is `clear_gpu` and this figure is inflated by idle draw |
| `duty` | 0.315 | fraction of the window actually executing |
| `ci95_mJ` | 463.908 | 95% confidence interval (± mJ) |
| `cv_pct` | 0.6 | coefficient of variation across repetitions |
| `E_paper_mJ` | 17023.299 | what the paper's `P_steady × min_ms` formula gives |
| `paper_err_pct` | **-69.49%** | that formula's deviation from the hardware counter |

## 4. Power time series — `trace_tpch_sf10_q1.json`

16 samples, one every 100 ms. Plotted in `power_time_tpch_sf10_q1.png`.

| Time (s) | dE/dt power (W) | driver power.draw (W) |
|---|---|---|
| -0.05 | 169.3 | 118.7 |
| 0.05 | 185.3 | 123.4 |
| 0.15 | 189.7 | 126.0 |
| 0.25 | 192.3 | 134.5 |
| 0.35 | 191.8 | 141.1 |
| 0.45 | 191.7 | 148.5 |
| 0.55 | 192.1 | 156.4 |
| 0.65 | 192.2 | 161.9 |
| 0.75 | 186.8 | 168.5 |
| 0.85 | 193.7 | 176.4 |
| 0.95 | 186.0 | 183.5 |
| 1.05 | 196.3 | 185.4 |
| 1.15 | 193.0 | 189.2 |
| 1.25 | 199.1 | 188.1 |
| 1.35 | 186.1 | 188.4 |
| 1.45 | 196.2 | 189.3 |

Both columns are recorded deliberately:
- **dE/dt**, from the hardware energy counter, jumps to ~199 W as soon as the load starts
- **power.draw**, the driver's 1 s moving average, crawls from 119 W to 189 W and needs about a second to catch up

This is the direct evidence that the driver reading cannot resolve a millisecond-scale query —
exactly what is needed to answer a reviewer challenging the energy measurement methodology.

## 5. Category C: this query across the 5x5 grid — `catC_grid.csv`

Five power limits x five SM clocks, 25 configurations. `catC_A.csv`, `catC_B_timing.csv` and
`catC_B_energy.csv` hold the full Cat A and Cat B re-runs at each of those points (25 rows each).

| Configuration | Dynamic energy | Mean latency |
|---|---|---|
| default, 700 W / 1980 MHz | 1279.842 mJ | 13.193 ms |
| **lowest energy, 575 W / 750 MHz** | **760.766 mJ** | 19.481 ms |

→ **41% less energy** for **+48%** latency.

## Files

| File | Contents |
|---|---|
| `q1.sql` | the SQL |
| `catA_powersample.csv` | Cat A, 1 row x 23 columns |
| `catB_timing.csv` | Cat B latency, 1 row x 19 columns |
| `catB_energy.csv` | Cat B energy, 1 row x 21 columns |
| `trace_tpch_sf10_q1.json` | the power time series |
| `power_time_tpch_sf10_q1.png` | the plot |
| `plot_power.py` | the plotting script |
| `catC_grid.csv` | the 5x5 grid, 25 rows |
| `catC_A.csv` / `catC_B_timing.csv` / `catC_B_energy.csv` | A/B re-run at each grid point, 25 rows each |
