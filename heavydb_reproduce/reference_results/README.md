Results of `./reproduce.sh --gpu 6 --full` on a single GPU (GPU 6) of an 8×H100 GCP VM on 2026-09-30, for comparison:
- `catA_powersample.csv` / `catB_timing.csv` / `catB_energy.csv`: Category A / B, 12 datasets + case_bench + clickbench_approx
- `catC_grid.csv`: Category C lite (6 representative queries × 25 grid points)
- `catC_A.csv` / `catC_B_*.csv`: Category C test (`--catc-scope ab --catc-limit 1`, 1 query per benchmark×SF × 25 grid points)
- `query_screen_*.csv`: HeavyDB dialect screening (which queries run, and why the others fail)
- `data_sources.csv`: data source of every database
No check aborted the run.

**Repaired 2026-10-09** with `./fix_results.sh --gpu 7` (same machine, another H100): `queries/tpch/q14.sql` and
`queries/clickbench/q28.sql` had been empty, so their rows were 0 ms measurements of nothing. TPC-H q14 was re-screened
and re-measured (Cat A + B, 4 SFs; those rows have `gpu_checked=7`); ClickBench q28 now fails screening ("Aggregate on TEXT
is not supported yet") and has no A/B rows. The `duty` column of `catC_grid.csv` was divided by 3 (it summed 3 trials'
latencies over one trial's window). `catA_powersample.csv` had been written before the `gpu_checked` column existed; the
column is now present and blank for the original rows. `catC_A.csv` / `catC_B_*.csv` contain neither query and are unchanged.
