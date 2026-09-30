# HeavyDB 能耗/延迟基准 —— 一键复现

对齐 `gpu_db` 论文 harness 的三类实验（Category A / B / C），在 HeavyDB 上跑
TPC-H、H2O、ClickBench、case_bench 四个 benchmark，**单卡**测量。

**Quick start (one command, fresh Ubuntu machine with an NVIDIA GPU and sudo):**

```bash
git clone -b heavydb-reproduce https://github.com/Weiwei1001/gpu_db.git && gpu_db/heavydb_reproduce/run_full.sh
```

It picks an idle GPU, builds HeavyDB, prepares the data (about 70 GB), and runs Category A, B, and C-lite.
About 5.5 h on one H100. Results land in `gpu_db/heavydb_reproduce/results/full/`. Add `--data-dir /bigdisk/x`
if the root disk is small, and `--catc-scope ab` for the full 4–5 day Category C grid.

```bash
./run_full.sh                                  # 一键全量：自动选空闲 GPU、自动找现成数据、构建、Cat A/B + C lite（默认）
./run_full.sh --catc-scope ab --data-dir /bigdisk/hb_repro --data-roots /path/to/gpu_db   # Cat C 按 repo 规格
./reproduce.sh --gpu 4 --smoke                 # 极小数据全链路验证（不含编译约 10 分钟）
./reproduce.sh --gpu 4 --full --data-dir /data/$USER/hb_repro   # 全量：A+B 约 4.5 h；Cat C 默认按 repo 规格（见下）
./reproduce.sh --gpu 4 --full --catc-scope ab --catc-limit 1      # Cat C 测试：每个 benchmark×SF 只跑 1 条（约 7 h）
./reproduce.sh --gpu 4 --full --catc-scope lite                   # Cat C 轻量版：6 条代表 query（约 25 min）
./reproduce.sh --gpu 4 --full --skip-build --heavydb-home /path/to/heavydb   # 已有构建
HB_SFS_TPCH="1 10" HB_SFS_H2O="1 4" HB_SFS_CB="1 10" ./reproduce.sh --gpu 4 --full   # 缩减 SF
```

## 阶段

| # | 脚本 | 做什么 |
|---|---|---|
| 1 | `stages/01_build.sh` | apt 依赖 + 源码构建 Thrift/Arrow/CPR/H3 → `/usr/local/mapd-deps`，克隆 heavydb `b348f14`，gcc-11 + CUDA 编译，`initheavy`。含官方文档没写的 4 处上游坑的绕法（预编译依赖站点已死、缺 `LICENSE.md`、GEOS C++ 头、PROJ/GDAL 数据路径） |
| 2 | `stages/02_data.sh` | **先找现成数据，再生成**（见下）。全部经 Parquet 导入，列类型按 `schemas/*.sql` 强制转换；来源记入 `results/full/data_sources.csv` |
| 3 | `stages/03_cat_a.sh` | **Category A**：数据常驻 GPU。每条 query 预热后 3 次延迟 + 100 ms 粒度功率采样（NVML 能量计数器 ΔE/Δt）；短 query 放大到 ≥2 s 窗口 |
| 4 | `stages/04_cat_b.sh` | **Category B**：数据在 CPU 内存、每次含 PCIe 传输（每条前 `\clear_gpu`），计时 + 能耗 |
| 5 | `stages/05_cat_c.sh` | **Category C**：5 功率档 × 5 SM 时钟网格，**每个网格点重跑一遍阶段 3（和 4）**，对齐 repo 的 `run_energy_sweep.py`。需 passwordless `sudo nvidia-smi`；断点续跑（完成的网格点留 `.done`）；无论怎么退出都恢复默认功率/时钟 |

**Category C 选项**

| 选项 | 含义 | 全量耗时（单卡 H100） |
|---|---|---|
| `--catc-scope ab`（`reproduce.sh` 默认） | 每个网格点重跑 A + B（repo 规格） | 约 4–5 天 |
| `--catc-scope a` | 每个网格点只重跑 A | 约 1 天 |
| `--catc-scope lite`（`run_full.sh` 默认） | 6 条代表 query（tpch q1/q6、h2o q1/q3、clickbench q0/q31） | 约 25 min |
| `--catc-limit N` | 配合 ab/a：每个 benchmark×SF 只跑筛查可跑的前 N 条（测试用） | N=1、ab：约 7 h |

**数据自动发现（full 模式）**：别人跑过 gpu_db 的 Maximus/Sirius 时，机器上通常已有数据。阶段 2 会在
`--data-roots`（冒号分隔）以及 `--data-dir`、`~/gpu_db`、本包同级 `gpu_db` 下按固定布局查找（不递归扫盘）：

| 数据集 | 认得的现成数据 | 都没有时 |
|---|---|---|
| TPC-H sf N | `tests/tpch/csv-N/`、`tpch/csv-N/`、`tests/tpch_duckdb/tpch_sfN.duckdb`、`sirius_db/tpch_N.duckdb` | duckdb `dbgen`（确定性，与 repo 相同） |
| H2O S | `tests/h2o/csv-S/groupby.csv`、`h2o/csv-S/`、`tests/h2o_duckdb/h2o_S.duckdb`、`sirius_db/h2o_S.duckdb` | 按论文分布生成（repo 生成器无随机种子，**只有用现成 CSV 才与其他引擎同一份行**） |
| ClickBench N | `tests/clickbench/csv-N/t.csv`、`clickbench/csv-N/`、`tests/click_duckdb/clickbench_N.duckdb`、`sirius_db/clickbench_N.duckdb`、本地 `hits.parquet` | 下载 `hits.parquet`（14.8 GB，一次）到 `--data-dir`，按 repo 规则取前 总行数×N/70 行 |

repo 的 ClickBench CSV 把 `EventTime`/`EventDate` 转成了时间戳，导入时会转回整数秒/天（HeavyDB 的 schema 与 query 用整数）。
已验证：同一 SF 从 repo CSV、Sirius `.duckdb`、`hits.parquet` 前 N 行三种来源导入，行数与内容一致。

每个阶段幂等：已存在的库会跳过，可用 `--stages "3 5"` 单独重跑。

**单卡保证**：阶段 3/4/5 的每条 query 前后都检查——只有一个 heavydb、PID 没变（没被悄悄重启）、
启动参数与实测 CUDA context 都只在 `--gpu` 那张卡上、功率上限为默认值（Cat C 网格点上则核对为该档功率与 SM 时钟）。
任何一项不符立即中止并保留已测数据；每行结果带 `gpu_checked` 列。

**结果目录**：`results/<smoke|full>/`（两种模式分开，query 筛查也在各自模式的数据上做）
- `catA_powersample.csv`、`catB_timing.csv`、`catB_energy.csv`、`traces/`
- Cat C：`catC/pl<W>_sm<MHz>/` 每个网格点一份，汇总 `catC_A.csv`、`catC_B_timing.csv`、`catC_B_energy.csv`（带 `pl_w`/`sm_mhz`）；lite 为 `catC_grid.csv`

**读数注意**：H100 的 NVML 能量计数器约 100 ms 才更新一次，所以短 query 一律放大到 ≥2 s 窗口。
Cat B 每条前要 `\clear_gpu`，窗口里真正执行 query 的时间只占约 5–15%（`duty` 列），**执行时间很短的 query
其 `E_dyn_mJ` 会被待机扣除误差淹没、甚至为负**（低频档尤甚）——Cat B 能耗只对长 query（如 TPC-H SF≥5、H2O 4/8 GB）有意义。

### 环境要求

- Ubuntu 22.04+，`sudo`（apt 与 `nvidia-smi` 改功率/时钟需要；Cat C 需 passwordless）
- CUDA Toolkit（默认 `/usr/local/cuda`，否则设 `CUDA_HOME`）。构建阶段显式把 `$CUDA_HOME/bin/nvcc`
  传给 cmake——不在 PATH 里的 nvcc 会让 `enable_language(CUDA)` 直接失败
- Python 3.10+（脚本自建 venv）
- 磁盘：smoke 约 1 GB；full 约 70 GB（数据）+ 3 GB（构建）
- 网络：ClickBench 从 `datasets.clickhouse.com` 远程采样（smoke 也要扫一遍 14.8 GB，约 8 分钟）

### 验证记录（2026-09-29，本机 8×H100 GCP VM，Ubuntu 24.04 / CUDA 12.9）

三轮 `--smoke`，逐项一致（筛查结果、各 CSV 行数、0 失败、采样 GPU 与服务端一致、Cat C 后功率/时钟还原）：

| 轮 | 条件 | 耗时 | 结果 |
|---|---|---|---|
| 1 | 现有二进制，GPU 6 | 14 min | 5 阶段通过 |
| 2 | 干净目录真实构建 → 新二进制，GPU 5 | 构建 7 min + smoke 16 min | 第一次 cmake 因 nvcc 不在 PATH 失败，已修 |
| 3 | **删掉 `/usr/local/mapd-deps`、源码依赖、构建目录、venv 后从零全装** | **21 min**（依赖编译 2 min、HeavyDB 2.5 min、smoke 16 min） | 5 阶段通过 |

第 3 轮没删 apt 包（共享机器，会影响他人），改用两种方式验证 apt 清单：45 个包名 `--dry-run` 全部可解析；
新二进制 `DT_NEEDED` 直接链接的 10 个系统库，其 -dev 包全部在清单的依赖闭包内。
未能验证的只剩"另一台干净 Ubuntu 上 apt 清单是否有遗漏"——这只能在容器或新机器上确认。

### 全量验证（2026-09-30，GPU 6，`--full --skip-build --data-dir /data/...`）

| 内容 | 耗时 | 结果 |
|---|---|---|
| 数据：TPC-H 1/5/10/20、H2O 1/2/4/8 GB、ClickBench 1/5/10/20 | 44 min | 12 库 |
| Cat A | 33 min | 304 行；失败 6（ClickBench q27 字符串编码转换 ×4、H2O q10 在 4/8 GB 显存不足 ×2） |
| Cat B | 2 h 50 min | 计时/能耗各 304 行，失败同上 |
| Cat C lite | 25 min | 150 行（6 条 × 25 网格点），0 失败 |
| Cat C 测试（`ab --catc-limit 1`） | 7 h | A / B 计时 / B 能耗各 500 行（20 × 25 网格点），0 失败，全部 `gpu_checked=6` 且功率/时钟核对通过 |

全程无检查中止。Cat C 默认档（700 W/1980 MHz）与主实验 Cat A 同 query 的动态能耗相差 2–26%（短 query 波动大）。

## 目录

```
run_full.sh       一键全量入口（选 GPU、找数据、检查磁盘/sudo，然后调 reproduce.sh --full）
reproduce.sh      分阶段入口
stages/           5 个阶段脚本
lib/              hbench.py（NVML + heavysql 会话 + 崩溃恢复）、各 runner、hsql、start-heavydb.sh、gen_data.py、datasets.sh
queries/          tpch(22) h2o(10) clickbench(43) clickbench_approx(6) case_bench(5)
schemas/          tpch.sql h2o.sql clickbench.sql
```
