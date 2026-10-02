# HeavyDB energy/latency benchmark -- one-command reproduction

Runs the three experiment categories (Category A / B / C) of the `gpu_db` paper harness on HeavyDB,
over the four benchmarks TPC-H, H2O, ClickBench and case_bench, measured on a **single GPU**.

**Quick start (one command). If you already ran the Maximus/Sirius benchmarks from this repo, run it inside that checkout so the existing `tests/` data is reused:**

```bash
git pull && heavydb_reproduce/run_full.sh
```

On a fresh machine: `git clone https://github.com/Weiwei1001/gpu_db.git && gpu_db/heavydb_reproduce/run_full.sh`.

It picks an idle GPU, builds HeavyDB, imports the data (generates or downloads whatever is missing, about 70 GB),
and runs Category A, B, and C-lite. About 5.5 h on one H100 when the data is already there. Results land in
`heavydb_reproduce/results/full/<gpu-model>/` (one directory per GPU model, so A100 and H100 runs do not mix). `--dry-run` shows the chosen GPU and the data it found without building anything;
`--data-dir /bigdisk/x` if the root disk is small; `--catc-scope ab` for the full 4–5 day Category C grid.

```bash
./run_full.sh                                  # one-shot full run: auto-pick an idle GPU, auto-find existing data, build, Cat A/B + C lite (default)
./run_full.sh --catc-scope ab --data-dir /bigdisk/hb_repro --data-roots /path/to/gpu_db   # Cat C per repo spec
./reproduce.sh --gpu 4 --smoke                 # end-to-end check on tiny data (about 10 min, excluding compilation)
./reproduce.sh --gpu 4 --full --data-dir /data/$USER/hb_repro   # full: A+B about 4.5 h; Cat C defaults to the repo spec (see below)
./reproduce.sh --gpu 4 --full --catc-scope ab --catc-limit 1      # Cat C test: only 1 query per benchmark×SF (about 7 h)
./reproduce.sh --gpu 4 --full --catc-scope lite                   # Cat C lite: 6 representative queries (about 25 min)
./reproduce.sh --gpu 4 --full --skip-build --heavydb-home /path/to/heavydb   # existing build
HB_SFS_TPCH="1 10" HB_SFS_H2O="1 4" HB_SFS_CB="1 10" ./reproduce.sh --gpu 4 --full   # fewer SFs
```

## Stages

| # | Script | What it does |
|---|---|---|
| 1 | `stages/01_build.sh` | apt dependencies + from-source builds of Thrift/Arrow/CPR/H3 → `/usr/local/mapd-deps`, clone heavydb `b348f14`, compile with gcc-11 + CUDA, `initheavy`. Includes workarounds for 4 upstream pitfalls the official docs do not mention (prebuilt-dependency site is dead, missing `LICENSE.md`, GEOS C++ headers, PROJ/GDAL data paths) |
| 2 | `stages/02_data.sh` | **Look for existing data first, generate only if missing** (see below). Everything is imported via Parquet with column types forced to match `schemas/*.sql`; the source is recorded in `results/full/<gpu-model>/data_sources.csv` |
| 3 | `stages/03_cat_a.sh` | **Category A**: data resident on the GPU. For every query, after warm-up, 3 latency runs + power sampling at 100 ms granularity (NVML energy counter ΔE/Δt); short queries are amplified to a ≥2 s window |
| 4 | `stages/04_cat_b.sh` | **Category B**: data in CPU memory, every run includes the PCIe transfer (`\clear_gpu` before each query); timing + energy |
| 5 | `stages/05_cat_c.sh` | **Category C**: grid of 5 power levels × 5 SM clocks, **stage 3 (and 4) rerun at every grid point**, matching the repo's `run_energy_sweep.py`. Needs passwordless `sudo nvidia-smi`; resumable (finished grid points leave a `.done` marker); default power/clocks are restored however it exits |

**Category C options**

| Option | Meaning | Full-run time (single H100) |
|---|---|---|
| `--catc-scope ab` (`reproduce.sh` default) | rerun A + B at every grid point (repo spec) | about 4–5 days |
| `--catc-scope a` | rerun only A at every grid point | about 1 day |
| `--catc-scope lite` (`run_full.sh` default) | 6 representative queries (tpch q1/q6, h2o q1/q3, clickbench q0/q31) | about 25 min |
| `--catc-limit N` | with ab/a: only the first N queries per benchmark×SF that passed screening (for testing) | N=1, ab: about 7 h |

**Automatic data discovery (full mode)**: when someone has already run the gpu_db Maximus/Sirius benchmarks, the data is usually on the machine. Stage 2 looks under
`--data-roots` (colon-separated) as well as `--data-dir`, `~/gpu_db`, and a `gpu_db` next to this package, using fixed layouts (no recursive disk scan):

| Dataset | Existing data it recognizes | When none is found |
|---|---|---|
| TPC-H sf N | `tests/tpch/csv-N/`, `tpch/csv-N/`, `tests/tpch_duckdb/tpch_sfN.duckdb`, `sirius_db/tpch_N.duckdb` | duckdb `dbgen` (deterministic, same as the repo) |
| H2O S | `tests/h2o/csv-S/groupby.csv`, `h2o/csv-S/`, `tests/h2o_duckdb/h2o_S.duckdb`, `sirius_db/h2o_S.duckdb` | generated with the paper's distribution (the repo generator has no random seed, so **only existing CSVs give the same rows as the other engines**) |
| ClickBench N | `tests/clickbench/csv-N/t.csv`, `clickbench/csv-N/`, `tests/click_duckdb/clickbench_N.duckdb`, `sirius_db/clickbench_N.duckdb`, a local `hits.parquet` | download `hits.parquet` (14.8 GB, once) to `--data-dir`, take the first total_rows×N/70 rows per the repo rule |

The repo's ClickBench CSV converted `EventTime`/`EventDate` to timestamps; the import converts them back to integer seconds/days (the HeavyDB schema and queries use integers).
Verified: for the same SF, importing from the repo CSV, from the Sirius `.duckdb`, and from the first N rows of `hits.parquet` gives identical row counts and content.

Every stage is idempotent: existing databases are skipped, and `--stages "3 5"` reruns stages individually.

**Single-GPU guarantee**: before and after every query in stages 3/4/5 we check that there is exactly one heavydb, its PID has not changed (no silent restart),
both its start-up arguments and its measured CUDA context are only on the `--gpu` GPU, and the power limit is the default (at a Cat C grid point, that it matches that level's power and SM clock).
Any mismatch aborts immediately while keeping the data measured so far; every result row carries a `gpu_checked` column.

**Results directory**: `results/<smoke|full>/<gpu-model>/` (e.g. `results/full/a100-sxm4-80gb/`; mode and GPU model are kept separate, and query screening is done per directory)
- `catA_powersample.csv`, `catB_timing.csv`, `catB_energy.csv`, `traces/`
- Cat C: `catC/pl<W>_sm<MHz>/` one per grid point, aggregated into `catC_A.csv`, `catC_B_timing.csv`, `catC_B_energy.csv` (with `pl_w`/`sm_mhz`); lite writes `catC_grid.csv`

**Reading the numbers**: the H100 NVML energy counter only updates about every 100 ms, so short queries are always amplified to a ≥2 s window.
Cat B issues `\clear_gpu` before every query, so actual query execution takes only about 5–15% of the window (`duty` column); **for queries with a very short execution time,
`E_dyn_mJ` is swamped by the idle-subtraction error and can even go negative** (especially at low-frequency levels) -- Cat B energy is only meaningful for long queries (e.g. TPC-H SF≥5, H2O 4/8 GB).

### Requirements

- Ubuntu 22.04+, `sudo` (needed for apt and for `nvidia-smi` power/clock changes; Cat C needs it passwordless)
- CUDA Toolkit (default `/usr/local/cuda`, otherwise set `CUDA_HOME`). The build stage explicitly passes `$CUDA_HOME/bin/nvcc`
  to cmake -- an nvcc that is not in PATH makes `enable_language(CUDA)` fail outright
- Python 3.10+ (the scripts create their own venv)
- Disk: smoke about 1 GB; full about 70 GB (data) + 3 GB (build)
- Network: ClickBench is sampled remotely from `datasets.clickhouse.com` (even smoke scans the whole 14.8 GB once, about 8 min)

### Validation log (2026-09-29, local 8×H100 GCP VM, Ubuntu 24.04 / CUDA 12.9)

Three `--smoke` rounds, consistent in every respect (screening results, row counts of each CSV, 0 failures, sampled GPU matches the server, power/clocks restored after Cat C):

| Round | Conditions | Time | Result |
|---|---|---|---|
| 1 | existing binary, GPU 6 | 14 min | 5 stages passed |
| 2 | real build in a clean directory → new binary, GPU 5 | build 7 min + smoke 16 min | first cmake failed because nvcc was not in PATH; fixed |
| 3 | **deleted `/usr/local/mapd-deps`, dependency sources, build directory and venv, then full install from scratch** | **21 min** (dependency compile 2 min, HeavyDB 2.5 min, smoke 16 min) | 5 stages passed |

Round 3 did not remove the apt packages (shared machine, it would affect others); the apt list was verified in two other ways instead: all 45 package names resolve with `--dry-run`;
the -dev packages of the 10 system libraries directly linked by the new binary's `DT_NEEDED` are all in the dependency closure of the list.
The only thing left unverified is "whether the apt list is missing something on another clean Ubuntu" -- that can only be confirmed in a container or on a new machine.

### Full-run validation (2026-09-30, GPU 6, `--full --skip-build --data-dir /data/...`)

| Item | Time | Result |
|---|---|---|
| Data: TPC-H 1/5/10/20, H2O 1/2/4/8 GB, ClickBench 1/5/10/20 | 44 min | 12 databases |
| Cat A | 33 min | 304 rows; 6 failures (ClickBench q27 string encoding conversion ×4, H2O q10 out of GPU memory at 4/8 GB ×2) |
| Cat B | 2 h 50 min | 304 rows each for timing/energy, same failures as above |
| Cat C lite | 25 min | 150 rows (6 queries × 25 grid points), 0 failures |
| Cat C test (`ab --catc-limit 1`) | 7 h | 500 rows each for A / B timing / B energy (20 × 25 grid points), 0 failures, all `gpu_checked=6`, power/clock checks passed |

No check aborted at any point. At the Cat C default level (700 W/1980 MHz), the dynamic energy of the same queries differs from the main Cat A experiment by 2–26% (short queries fluctuate more).

## Layout

```
run_full.sh       one-shot full-run entry point (picks the GPU, finds data, checks disk/sudo, then calls reproduce.sh --full)
reproduce.sh      stage-by-stage entry point
stages/           the 5 stage scripts
lib/              hbench.py (NVML + heavysql session + crash recovery), the runners, hsql, start-heavydb.sh, gen_data.py, datasets.sh
queries/          tpch(22) h2o(10) clickbench(43) clickbench_approx(6) case_bench(5)
schemas/          tpch.sql h2o.sql clickbench.sql
```
