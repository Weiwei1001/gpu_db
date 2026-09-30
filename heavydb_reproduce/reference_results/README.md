2026-09-30 在 8×H100 GCP VM 上单卡（GPU 6）跑 `./reproduce.sh --gpu 6 --full` 得到的结果，供对照：
- `catA_powersample.csv` / `catB_timing.csv` / `catB_energy.csv`：Category A / B，12 个数据集 + case_bench + clickbench_approx
- `catC_grid.csv`：Category C lite（6 条代表 query × 25 网格点）
- `catC_A.csv` / `catC_B_*.csv`：Category C 测试（`--catc-scope ab --catc-limit 1`，每个 benchmark×SF 1 条 × 25 网格点）
- `query_screen_*.csv`：HeavyDB 方言筛查（哪些 query 能跑、失败原因）
- `data_sources.csv`：每个库的数据来源
所有行 `gpu_checked=6`，全程无检查中止。
