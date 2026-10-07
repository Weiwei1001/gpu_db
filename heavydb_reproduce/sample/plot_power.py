#!/usr/bin/env python3
"""Power-time plot for the sample query: hardware energy counter vs the driver reading."""
import json, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator
matplotlib.rcParams["axes.unicode_minus"] = False

D = os.path.dirname(os.path.abspath(__file__))
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e3e2de"
S1, S2 = "#2a78d6", "#eb6834"      # validated two-colour pair (CVD dE 24.7)

d = json.load(open(f"{D}/trace_tpch_sf10_q1.json"))
t = d["traces"]["q1"]
idle = d["p_idle_W"]
xs = [s[0] for s in t["samples"]]
hw = [s[1] for s in t["samples"]]      # energy counter dE/dt
drv = [s[2] for s in t["samples"]]     # driver power.draw (1 s moving average)

fig, ax = plt.subplots(figsize=(9.6, 5.0), dpi=200)
fig.patch.set_facecolor(SURFACE); ax.set_facecolor(SURFACE)
ax.grid(True, color=GRID, lw=0.8, zorder=0); ax.set_axisbelow(True)
for s in ("top", "right"): ax.spines[s].set_visible(False)
for s in ("left", "bottom"): ax.spines[s].set_color(GRID)
ax.tick_params(colors=INK2, labelsize=9.5, length=0)

# idle baseline
ax.axhline(idle, color=INK2, lw=1.1, ls=(0, (5, 4)), zorder=2)
ax.text(xs[-1] - 0.02, idle + 3, f"idle baseline {idle:.0f} W", color=INK2, fontsize=9.5,
        va="bottom", ha="right")

# the two power curves
ax.plot(xs, hw, "-o", ms=5, lw=2.2, color=S1, zorder=5, label="NVML hardware energy counter (dE/dt)")
ax.plot(xs, drv, "-o", ms=5, lw=2.0, color=S2, zorder=4, label="driver power.draw (1 s moving average)")

# Direct labels on both lines (identity encoded by more than colour), placed where the curves
# are furthest apart
j = max(range(len(xs)), key=lambda i: hw[i] - drv[i])
ax.annotate("hardware counter: tracks the load immediately", xy=(xs[2], hw[2]), xytext=(xs[1], hw[2] + 11),
            color=S1, fontsize=9.8, fontweight="medium", va="bottom", ha="left", zorder=6,
            arrowprops=dict(arrowstyle="-", color=S1, lw=1, alpha=.55))
# the orange label sits in the empty band between the two curves
ax.annotate("driver reading: 1 s average, ramps slowly", xy=(xs[j], drv[j]), xytext=(xs[j] + 0.04, drv[j] + 22),
            color=S2, fontsize=9.8, fontweight="medium", va="bottom", ha="left", zorder=6,
            arrowprops=dict(arrowstyle="-", color=S2, lw=1, alpha=.55))
# time taken to catch up with the true value
i_catch = next((i for i, (h, v) in enumerate(zip(hw, drv)) if v >= h - 5), len(xs) - 1)
ax.annotate("", xy=(xs[0], idle - 6), xytext=(xs[i_catch], idle - 6),
            arrowprops=dict(arrowstyle="<->", color=INK2, lw=1.1), zorder=6)
ax.text((xs[0] + xs[i_catch]) / 2, idle - 8.5,
        f"driver reading lags by ~{xs[i_catch] - xs[0]:.1f} s", color=INK2,
        fontsize=9.5, ha="center", va="top", zorder=6)

ax.set_ylim(idle - 22, max(hw) + 14)
ax.yaxis.set_major_locator(MultipleLocator(20))
ax.set_xlim(xs[0] - 0.05, xs[-1] + 0.07)
ax.xaxis.set_major_locator(MultipleLocator(0.2))
ax.set_xlabel("time within the measurement window (s)", color=INK2, fontsize=10.5, labelpad=6)
ax.set_ylabel("GPU power (W)", color=INK2, fontsize=10.5, labelpad=8)

leg = ax.legend(loc="lower right", frameon=False, fontsize=9.5, handlelength=1.6,
                bbox_to_anchor=(1.0, 0.02), labelspacing=0.35)
for x in leg.get_texts(): x.set_color(INK2)

ax.set_title("TPC-H SF10 / q1 \u2014 power over time for a single query", color=INK,
             fontsize=14, pad=34, loc="left", fontweight="semibold")
lat = t["lat_ms"]
ax.text(0, 1.085, f"single H100 \u00b7 data resident on GPU \u00b7 {sum(lat)/len(lat):.0f} ms per run, "
        f"fired {t['K']}x back to back to fill a {t['window_s']:.2f} s window \u00b7 one sample every 100 ms",
        transform=ax.transAxes, color=INK2, fontsize=9.8)
ax.text(0, 1.028, "the two methods side by side: the hardware counter follows the load at once, "
        "while the driver's 1 s average cannot resolve a millisecond-scale query",
        transform=ax.transAxes, color=INK2, fontsize=9.8)

fig.text(0.008, 0.012,
         f"peak {max(hw):.0f} W \u00b7 window mean {sum(hw)/len(hw):.0f} W \u00b7 "
         f"dynamic rise {sum(hw)/len(hw) - idle:.0f} W \u00b7 1218 mJ of dynamic energy per query "
         f"(37% of the 3301 mJ total; the rest is idle draw)",
         color=INK2, fontsize=9.3)

out = f"{D}/power_time_tpch_sf10_q1.png"
fig.savefig(out, facecolor=SURFACE, bbox_inches="tight", pad_inches=0.35)
print("->", out)
