# Reproduction Issues Log (question.md)

This file records every problem encountered while following the repo's setup flow on a
**fresh machine**, plus the fix applied so that anyone in the same environment can reproduce.

## Target Environment (verified 2026-05-25)

| Item | Value |
|------|-------|
| OS | Ubuntu 22.04.5 LTS (jammy) |
| GPU | NVIDIA H100 NVL (95 GB), driver 580.159.03, CUDA 13.0 (driver-level) |
| CPU | 40 cores |
| RAM | 314 GB |
| Disk | 287 GB free on / |
| gcc | 11.4.0 |
| python | 3.10.12 |
| sudo | passwordless |

> Note: repo's `CLAUDE.md` documents the *author's* hardware (RTX 5080 + T400, Maximus at
> `/home/xzw/...`). The actual reproduction machine is an H100 NVL Azure VM — paths and GPU
> differ, which is itself a source of several issues below.

Scope of this run: follow `setup.sh` end-to-end (deps → build → data) but **STOP before any
benchmark query is executed** (i.e. before `setup.sh` Step 11 smoke test and before
`run_all_benchmarks.sh`).

---

## Issue Index

| # | Step | Severity | Status |
|---|------|----------|--------|
| 1 | Prereqs | Blocker | fixed |
| 2 | Step 3/4 pip | Resolved (no fix needed) | verified |
| 3 | Step 2 cmake | Risk (not triggered) | noted |
| 4 | Docs | Inconsistency | noted |
| 5 | CUDA PATH | Minor | fixed |
| 6 | Maximus build | Inconsistency | noted |
| 7 | Step 9 Sirius | Blocker | fixed |
| 8 | Sirius runners | Latent bug | fixed |

(Plus a `--toy` experiment mode added on request — see end of file.)

### Build verification (no benchmark query run — per scope)

| Component | Result |
|-----------|--------|
| Apache Arrow 17.0.0 | built + installed (`~/arrow_install/lib/libarrow.so` etc.) |
| Taskflow | built + installed (`~/taskflow_install`) |
| cuDF (pip) | `import cudf` → 26.02.01 |
| Maximus `maxbench` | built; `ldd` clean; `--help` exits 0 |
| Sirius `duckdb` | built (v1.4.4); `ldd` clean; `--version` exits 0 |
| `setup_env.sh` | generated; sourcing it resolves nvcc + all `LD_LIBRARY_PATH` libs |

> All four build artifacts compiled cleanly under **CMake 4.3.2 + CUDA 12.6 + GCC 11.4** on
> **H100 (arch 90)**. No query/benchmark was executed (stopped before Step 11 smoke test).

---

## Issue #1 — CUDA toolkit / cmake / ninja missing, but `setup.sh` does not install them

**Where:** `setup.sh` Step 1 (Check prerequisites), lines ~90-96.

**Symptom:** On a fresh machine, `nvcc`, `cmake`, and `ninja` are not present.
`setup.sh` Step 1 does:

```bash
check_cmd nvcc || { log "FATAL: CUDA toolkit required (nvcc not found)"; exit 1; }
```

So the script **aborts immediately** with `FATAL: CUDA toolkit required (nvcc not found)`.
It only *checks* for `nvcc` — it never installs the CUDA toolkit. `cmake`/`ninja` are
installed later in Step 2 (`apt-get install ... ninja-build`, and a kitware upgrade for
cmake), but `nvcc` is a hard prerequisite the script assumes is already present.

This contradicts the script's own header which lists "CUDA toolkit installed (nvcc available)"
as a *prerequisite* — meaning a truly fresh machine cannot run `setup.sh` as documented in
the README's "Quick Start" (`./setup.sh`).

**Fix (applied):** Edited `setup.sh` Step 1 so that instead of `FATAL`-ing when `nvcc`
is absent, it:
1. First probes `/usr/local/cuda*/bin` in case the toolkit is installed but off PATH.
2. If still missing, adds the NVIDIA CUDA apt repo keyring for the detected distro
   (`${ID}${VERSION_ID//./}`, e.g. `ubuntu2204`) and runs
   `sudo apt-get install -y cuda-toolkit-12-6`.
3. Adds the toolkit `bin` dir to `PATH` for the rest of the script, then re-verifies.

On the repro machine the toolkit was installed manually first (mirrors what the patched
script now does):
```bash
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb && sudo apt-get update
sudo apt-get install -y cuda-toolkit-12-6   # installs nvcc to /usr/local/cuda-12.6
```
**Also note:** `nvcc` is not on `PATH` by default after install — it lives in
`/usr/local/cuda-12.6/bin`. `setup_env.sh` / the user's shell must add it (the patched
setup.sh handles it for the build process).

---

## Issue #2 — `pip` not present and Step 4 cuDF install assumes `pip` on PATH

**Where:** `setup.sh` Step 3 (`pip install ... || pip3 install ...`) and Step 4
(`pip install 'cudf-cu12==26.2.1' ...`, lines ~159, 185).

**Symptom:** On a fresh Ubuntu 22.04 machine neither `pip`, `pip3`, nor `python3 -m pip`
exists until `python3-pip` is apt-installed (Step 2). Step 3 is resilient
(`pip ... || pip3 ...`), but **Step 4 calls bare `pip`**, which Ubuntu's `python3-pip`
package does **not** always provide (it ships `pip3`; `pip` only appears with
`python-is-python3` or a user pip install). If only `pip3` exists, Step 4's cuDF install
silently fails its first branch and may fall through to the (heavier) conda path
unnecessarily.

**Resolution (verified, no code change needed):** On Ubuntu 22.04, the `python3-pip`
apt package (installed in Step 2) *does* provide `/usr/bin/pip` as well as `/usr/bin/pip3`.
After Step 2 both exist, so Step 4's bare `pip install` works. cuDF installed cleanly:

```
pip install 'cudf-cu12==26.2.1' 'libcudf-cu12==26.2.1'   # -> Successfully installed cudf-cu12-26.2.1 ...
python3 -c "import cudf; print(cudf.__version__)"          # -> 26.02.01
```

cuDF (pip, non-root) lands in **user-site**: `~/.local/lib/python3.10/site-packages`, with
`libcudf/lib64/cmake/cudf/cudf-config.cmake` present there. `configure_with_gpu_pip_cudf.sh`
checks user-site first, so detection works. *Recommendation* (defensive, not required here):
Step 4 could use `python3 -m pip` to be robust on distros where bare `pip` is absent.

---

## Issue #3 — `setup.sh` Step 2 installs the *latest* CMake (4.x), a compatibility risk

**Where:** `setup.sh` Step 2, `apt-get install -y cmake` from the kitware repo (no version pin).

**Symptom:** kitware's apt repo serves **CMake 4.3.2** on jammy. CMake 4.x removes
compatibility with `cmake_minimum_required(VERSION < 3.5)`, which can hard-fail older
bundled third-party projects. This is a latent reproducibility risk: a future bundled dep
that declares an old minimum will fail to configure.

**Status:** Did **not** trigger for Arrow 17.0.0 or Taskflow — both configured and compiled
cleanly under CMake 4.3.2. Noted as a risk; if a future component breaks, pin with
`apt-get install -y cmake=3.31.*` (kitware keeps 3.31.x available). No change applied since
nothing is currently broken.

---

## Issue #4 — Documentation says cuDF 24.12 / CCCL 2.5.0, but the build uses cuDF 26.2.1

**Where:** `README.md` (lines ~186, 204-209), `CLAUDE.md` (lines ~7, 141) vs. `setup.sh`
Step 4 (`cudf-cu12==26.2.1`) and Step 6.5 comments ("cuDF 26.2 compatibility").

**Symptom:** The prose docs describe a cuDF **24.12 + CCCL 2.5.0** toolchain and warn about a
"segfault in PinnedMemoryPool::do_allocate" from CCCL ABI mismatch. The actual scripted path
installs **cuDF 26.2.1** (pulling CCCL 12.9.x) and the source has already been migrated to the
26.x API (per Step 6.5 comment). The CLAUDE.md "Hardware Environment" also lists RTX 5080 +
`/home/xzw/...` paths that do not match this machine. These are stale docs, not blockers, but
they mislead anyone troubleshooting. Recommend updating README/CLAUDE.md to 26.2.1.

---

## Issue #5 — `nvcc` not on default PATH after toolkit install

**Where:** post-install of `cuda-toolkit-12-6`.

**Symptom:** The toolkit installs to `/usr/local/cuda-12.6/bin`, which is **not** on the
default `PATH`. So `nvcc` is invisible to new shells and to `setup_env.sh` (which does not add
it). Any later step that shells out and expects `nvcc` (e.g. `build_sirius.sh`'s nvcc version
check) would not find it.

**Fix (applied, two places):**
1. The Step-1 patch (Issue #1) adds `/usr/local/cuda*/bin` to `PATH` for the setup process.
2. Added a CUDA block to the `setup_env.sh` heredoc inside `setup.sh` (and regenerated the
   actual `setup_env.sh`), so `source setup_env.sh` now prepends the CUDA bin dir:
   ```bash
   for _cudadir in /usr/local/cuda/bin /usr/local/cuda-12.6/bin /usr/local/cuda-12/bin; do
       if [ -x "$_cudadir/nvcc" ]; then export PATH="$_cudadir:$PATH"; break; fi
   done
   ```
   Verified: after `source setup_env.sh`, `nvcc` resolves to `/usr/local/cuda/bin/nvcc` (V12.6.85).

---

## Issue #6 — Generator mismatch: pip-cuDF configure makes Makefiles, docs say `ninja`

**Where:** `scripts/configure_with_gpu_pip_cudf.sh` (cmake call, lines ~100-118) vs.
`README.md` "Manual Build" (`cmake -B build -GNinja ... ninja -C build`).

**Symptom:** The pip-cuDF configure script's `cmake` invocation has **no `-G` generator
flag**, so it defaults to **Unix Makefiles** — there is no `build.ninja`. Following the
README literally (`ninja -C build`) on the pip path fails with
`ninja: error: loading 'build.ninja': No such file or directory`.

`setup.sh` itself is correct — its pip branch builds via `cmake --build . -j$(nproc)`
(generator-agnostic). Only the README's manual instructions and the conda branch assume
Ninja. **Always build with `cmake --build <dir> -j$(nproc)`**, which works for both
generators. (No code change required; documented here so reproducers don't hit it.)

---

## Issue #7 — `build_sirius.sh` installs source-built abseil/spdlog to /usr/local WITHOUT sudo

**Where:** `sirius_patches/build_sirius.sh`, the abseil build (`ninja install`, ~line 91) and
the spdlog build (`ninja install`, ~line 111).

**Symptom:** On a non-root sudo user (the documented target — Ubuntu workstation with sudo,
not a root container), the abseil install fails:

```
CMake Error at absl/base/cmake_install.cmake:46 (file):
  file cannot create directory: /usr/local/lib/pkgconfig.  Maybe need administrative privileges.
```

The script *does* define a `$SUDO` helper (lines ~45-48: `SUDO="sudo"` when `id -u != 0`)
and uses it for `apt-get`, **but the two `ninja ... install` calls that write to
`/usr/local` omit `$SUDO`.** A non-root user cannot create directories under
`/usr/local/lib`, so the build aborts (set -e). This only works on root containers.

**Fix (applied):** Prefixed both install steps with `$SUDO`:
```bash
$SUDO ninja -C "$ABSL_TMP/absl/build" install   >/dev/null   # abseil
$SUDO ninja -C "$SPDLOG_TMP/spdlog/build" install >/dev/null   # spdlog
```
(The configure/compile steps stay unprivileged — they write only to the temp dirs.
nvcomp symlinks and the cmake-module copy target user-owned paths, so they don't need sudo.)

---

## Issue #8 — Sirius runners only honor query reduction under `--test`, not `--minimum`

**Where:** `run_sirius_benchmark.py`, `run_sirius_metrics.py`, `run_sirius_cpu_data.py` —
each loaded ALL queries from the query dir and only filtered to `cfg["queries"]` when
`args.test` was set (`if args.test:`).

**Symptom (latent bug):** In `--minimum` mode, `get_benchmark_config(minimum_mode=True)`
correctly reduces the *scale factors* and the cfg query list, but the Sirius scripts ignored
the cfg query list (the `if args.test:` filter didn't fire), so Sirius would still execute the
**full** per-benchmark query set in minimum mode — inconsistent with Maximus (whose runners
read `cfg["queries"]` directly) and with the intent of `--minimum`.

**Fix (applied):** Broadened the filter in all three Sirius runners to
`if args.test or args.minimum or args.toy:` so any reduced mode restricts Sirius to the
configured queries. (Discovered while wiring up the new `--toy` mode below.)

---

## Feature added (per request) — `--toy` experiment mode

Not an issue, but recorded for reproducibility. Added a mode **smaller than `--test`** that
still exercises every category A1→C1 end-to-end, targeting ~1 hour total:

- **Scope:** `tpch` only, **1 query (q1)**, **largest SF (sf20)**.
- **Metrics** (`A3`,`A4`,`B2`, and Category C metrics): sampling window **target-time = 120s (2 min)**.
- **Category C** energy sweep: **2 power-limits × 2 SM-clocks = 4 configs** (vs 25 full / 9 minimum).

**Files touched:**
- `hw_detect.py`: `get_benchmark_config(..., toy_mode=True)` → `{"tpch": {queries:[q1], scale_factors:[sf_max]}}`.
- All 6 runners (`run_{maximus,sirius}_{benchmark,metrics,cpu_data}.py`): new `--toy` arg, threaded
  into `get_benchmark_config(toy_mode=...)`.
- `run_energy_sweep.py`: new `--toy` → 2×2 grid, `benchmarks=["tpch"]`, target-time=120s, and
  propagates `--toy` to its metrics subprocesses.
- `run_all_benchmarks.sh`: `--toy` flag → `MODE=TOY`, `METRICS_TT=120` for A3/A4/B2, `--toy`
  carried via `$EXTRA_FLAGS` into Category C.

**Run with:** `bash benchmarks/scripts/run_all_benchmarks.sh --toy`

**Verified (no queries executed):** all 8 scripts `py_compile`/`bash -n` clean; every runner's
`--help` shows `--toy`; `get_benchmark_config(95830, toy_mode=True)` returns exactly
`tpch / [q1] / [20]`.
