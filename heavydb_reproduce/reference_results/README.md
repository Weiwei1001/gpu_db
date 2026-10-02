Results of `./reproduce.sh --gpu 6 --full` on a single GPU (GPU 6) of an 8×H100 GCP VM on 2026-09-30, for comparison:
- `catA_powersample.csv` / `catB_timing.csv` / `catB_energy.csv`: Category A / B, 12 datasets + case_bench + clickbench_approx
- `catC_grid.csv`: Category C lite (6 representative queries × 25 grid points)
- `catC_A.csv` / `catC_B_*.csv`: Category C test (`--catc-scope ab --catc-limit 1`, 1 query per benchmark×SF × 25 grid points)
- `query_screen_*.csv`: HeavyDB dialect screening (which queries run, and why the others fail)
- `data_sources.csv`: data source of every database
All rows have `gpu_checked=6`; no check aborted the run.
