#!/usr/bin/env python3
"""Shared pieces for the HeavyDB benchmark: persistent heavysql session + NVML energy counter."""
import ctypes, os, queue, re, subprocess, threading, time
import statistics as st

HB_ROOT = os.environ.get("HB_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUILD = os.path.join(os.environ.get("HEAVYDB_HOME", os.path.join(HB_ROOT, "heavydb")), "build")
DEPS = os.environ.get("DEPS_PREFIX", "/usr/local/mapd-deps")
def _server_gpus():
    """Infer which GPUs heavydb uses from its process arguments -- the sampled GPU must match the server,
    otherwise you get the silent "sample on GPU A, execute on GPU B" error (it once ruined a whole batch of data)."""
    try:
        cmd = subprocess.run(["pgrep", "-a", "-x", "heavydb"],
                             capture_output=True, text=True).stdout.split("\n")[0]
        start = int(re.search(r"--start-gpu\s+(\d+)", cmd).group(1))
        n = int(re.search(r"--num-gpus\s+(\d+)", cmd).group(1))
        return list(range(start, start + n))
    except Exception:
        return None

GPUS = ([int(x) for x in os.environ["HB_GPUS"].split(",")]
        if os.environ.get("HB_GPUS") else (_server_gpus() or [4]))
LD = f"{DEPS}/lib:/usr/local/cuda/lib64"

# ---------------- NVML ----------------
_nvml = ctypes.CDLL("libnvidia-ml.so.1")
assert _nvml.nvmlInit_v2() == 0

def _handle(i):
    h = ctypes.c_void_p()
    assert _nvml.nvmlDeviceGetHandleByIndex_v2(i, ctypes.byref(h)) == 0
    return h

HS = [_handle(g) for g in GPUS]

def energy_J():
    """Accumulated energy (J) summed over the GPUs, from the hardware energy counter."""
    tot = 0
    for h in HS:
        v = ctypes.c_ulonglong()
        _nvml.nvmlDeviceGetTotalEnergyConsumption(h, ctypes.byref(v))
        tot += v.value
    return tot / 1000.0

def power_W():
    tot = 0
    for h in HS:
        v = ctypes.c_uint()
        _nvml.nvmlDeviceGetPowerUsage(h, ctypes.byref(v))
        tot += v.value
    return tot / 1000.0

def idle_baseline_W(seconds=4.0):
    """Idle power: energy counter ΔE/Δt over a window >= 4 s."""
    e0, t0 = energy_J(), time.perf_counter()
    time.sleep(seconds)
    e1, t1 = energy_J(), time.perf_counter()
    return (e1 - e0) / (t1 - t0)

# ---------------- heavysql ----------------
class Sql:
    """Persistent heavysql session.

    Output order is [result rows...] ["N rows returned."] ["Execution time: X ms, ..."];
    each query is followed by a sentinel SELECT on its own line (HeavyDB does not allow several
    statements in one request), and the sentinel echo tells us whether the previous statement
    actually finished -- a number of HeavyDB error messages do not contain the word "Error".
    Reading goes through a background thread + queue: stdout is a line-buffered text stream, and
    select() cannot see data already sitting in Python's buffer, so it would hang.
    """
    _seq = 0
    TIMING = re.compile(r"Execution time: ([\d.]+) ms")

    def __init__(self, db="heavyai", mode="gpu"):
        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = LD + ":" + env.get("LD_LIBRARY_PATH", "")
        self.p = subprocess.Popen(
            ["stdbuf", "-oL", "-eL", f"{BUILD}/bin/heavysql", db,
             "-u", "admin", "-p", "HyperInteractive", "--quiet"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=BUILD, env=env)
        self.q = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._w("\\" + mode)
        self._w("\\timing")
        time.sleep(0.8)

    def _pump(self):
        for line in self.p.stdout:
            self.q.put(line)
        self.q.put(None)

    def _w(self, s):
        self.p.stdin.write(s + "\n"); self.p.stdin.flush()

    def _get(self, deadline):
        """Fetch one line; returns "" on timeout; returns None if the server died.

        When HeavyDB crashes, heavysql neither exits nor prints anything, so relying on the
        timeout alone would wait uselessly until the limit; instead, when no data arrives,
        probe the server process every 2 seconds.
        """
        while True:
            left = deadline - time.time()
            if left <= 0:
                return ""
            try:
                return self.q.get(timeout=min(left, 2.0))
            except queue.Empty:
                if not server_alive():
                    return None

    def run(self, sql, timeout=900):
        stmts = [x.strip() for x in sql.strip().split(";") if x.strip()]
        Sql._seq += 1
        token = f"HBOK{Sql._seq}"
        for x in stmts:
            self._w(x.replace("\n", " ") + ";")
        self._w(f"SELECT '{token}';")

        want, got, total, noise, seen_token = len(stmts), 0, 0.0, [], False
        deadline = time.time() + timeout
        while True:
            line = self._get(deadline)
            if line == "":
                raise TimeoutError(f"{timeout}s timeout")
            if line is None:
                raise RuntimeError("server has exited")
            m = self.TIMING.search(line)
            if m:
                if seen_token:                      # the sentinel's own timing -- wrap up
                    if got == want:
                        return total
                    msg = next((l.strip() for l in reversed(noise) if l.strip()), "unknown error")
                    raise RuntimeError(msg[:200])
                if got < want:
                    total += float(m.group(1)); got += 1
                continue
            if token in line:
                seen_token = True
                deadline = min(deadline, time.time() + 30)
                continue
            if not seen_token:
                noise.append(line)

    def rows_returned(self, sql, timeout=900):
        """Run once and return the number of result rows (heavysql prints "N rows returned.")."""
        stmts = [x.strip() for x in sql.strip().split(";") if x.strip()]
        Sql._seq += 1
        token = f"HBROWS{Sql._seq}"
        for x in stmts:
            self._w(x.replace("\n", " ") + ";")
        self._w(f"SELECT '{token}';")
        n, deadline = None, time.time() + timeout
        while True:
            line = self._get(deadline)
            if line == "":
                raise TimeoutError(f"{timeout}s timeout")
            if line is None:
                raise RuntimeError("server has exited")
            m = re.match(r"(\d+) rows returned", line.strip())
            if m and n is None:
                n = int(m.group(1))
            if token in line:
                while True:
                    l2 = self._get(time.time() + 5)
                    if not l2 or self.TIMING.search(l2):
                        break
                return n if n is not None else 0

    @staticmethod
    def count_wrap(sql):
        """Wrap in COUNT(*): the inner scan/aggregation still runs, but the huge result set is not sent back to the client."""
        stmts = [x.strip() for x in sql.strip().split(";") if x.strip()]
        stmts[-1] = f"SELECT COUNT(*) FROM ({stmts[-1]}) AS _hb_wrap"
        return "; ".join(stmts) + ";"

    def run_burst(self, sql, k, timeout=1800):
        """Send the same query k times back to back (without waiting for each reply); returns (list of per-run exec_ms, wall-clock seconds).

        Amplification requires the window to be spent almost entirely executing the query; a per-query run()
        costs a client round trip each time, and on short queries the gaps can take up most of the window,
        diluting E/K.
        """
        stmts = [x.strip() for x in sql.strip().split(";") if x.strip()]
        Sql._seq += 1
        token = f"HBBURST{Sql._seq}"
        t0 = time.perf_counter()
        for _ in range(k):
            for x in stmts:
                self._w(x.replace("\n", " ") + ";")
        self._w(f"SELECT '{token}';")
        want = k * len(stmts)
        ms, seen_token = [], False
        deadline = time.time() + timeout
        while True:
            line = self._get(deadline)
            if line == "":
                raise TimeoutError(f"{timeout}s timeout")
            if line is None:
                raise RuntimeError("server has exited")
            m = self.TIMING.search(line)
            if m:
                if seen_token:
                    break
                ms.append(float(m.group(1)))
                continue
            if token in line:
                seen_token = True
        t1 = time.perf_counter()
        if len(ms) < want:
            raise RuntimeError(f"burst returned only {len(ms)}/{want} timings")
        return ms, t1 - t0

    def cmd(self, c):
        self._w(c); time.sleep(0.3)

    def close(self):
        try:
            self._w("\\q"); self.p.wait(timeout=10)
        except Exception:
            self.p.kill()

# ---------------- Server liveness / restart ----------------
def server_alive():
    return bool(subprocess.run(["pgrep", "-x", "heavydb"],
                               capture_output=True, text=True).stdout.strip())

# ---------------- Per-query configuration check ----------------
class ConfigError(RuntimeError):
    """Server configuration does not match the measurement setup: must abort, must not continue."""

def _heavydb_pids():
    return subprocess.run(["pgrep", "-x", "heavydb"],
                          capture_output=True, text=True).stdout.split()

def _gpus_of_pid(pid):
    """Which GPUs this process has a CUDA context on (measured via nvidia-smi, not trusting the start-up arguments)."""
    idx = {}
    for line in subprocess.run(["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
                               capture_output=True, text=True).stdout.strip().splitlines():
        i, u = [x.strip() for x in line.split(",")]
        idx[u] = int(i)
    out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,gpu_uuid", "--format=csv,noheader"],
                         capture_output=True, text=True).stdout
    return sorted({idx[u.strip()] for p, u in (l.split(",") for l in out.strip().splitlines())
                   if p.strip() == pid})

def _args_of_pid(pid):
    a = open(f"/proc/{pid}/cmdline").read().split("\0")
    get = lambda k: int(a[a.index(k) + 1]) if k in a else None
    return get("--start-gpu"), get("--num-gpus")

class Guard:
    """Call check() before and after every query. It checks:
    1. exactly one heavydb process, and its PID has not changed (no silent restart);
    2. the start-up arguments --start-gpu/--num-gpus match GPUS;
    3. nvidia-smi shows the process has a context only on GPUS -- the GPU sampled for energy == the GPU executing;
    4. power limit / SM clock: if HB_EXPECT_PL / HB_EXPECT_SM are set (Cat C grid point), verify against that level;
       otherwise require the power limit to be the default (Cat A/B must not run under leftover Cat C settings).
    """
    def __init__(self):
        self.pid = None
        self.rebind()

    def rebind(self):
        """After the initial start or a controlled restart, adopt the new PID and check immediately."""
        pids = _heavydb_pids()
        if len(pids) != 1:
            raise ConfigError(f"{len(pids)} heavydb processes, expected 1")
        self.pid = pids[0]
        self.check("rebind")

    def check(self, where=""):
        pids = _heavydb_pids()
        if pids != [self.pid]:
            raise ConfigError(f"[{where}] heavydb PID {self.pid} -> {pids} (restarted or multiple instances)")
        start, num = _args_of_pid(self.pid)
        if (start, num) != (GPUS[0], len(GPUS)):
            raise ConfigError(f"[{where}] start-up arguments start_gpu={start} num_gpus={num}, "
                              f"expected start_gpu={GPUS[0]} num_gpus={len(GPUS)}")
        real = _gpus_of_pid(self.pid)
        if real != sorted(GPUS):
            raise ConfigError(f"[{where}] heavydb is actually on GPU {real}, but energy is sampled on GPU {GPUS}")
        others = _others_on_gpus(self.pid)
        if others and os.environ.get("HB_IGNORE_BUSY", "0") != "1":
            raise ConfigError(f"[{where}] other processes {others} appeared on GPU {GPUS} (energy would be contaminated; HB_IGNORE_BUSY=1 ignores this)")
        check_power_clock(where)
        return ",".join(map(str, real))

def _others_on_gpus(my_pid):
    """Processes on the target GPUs other than our heavydb, as [(pid, MiB)]."""
    idx = {}
    for line in subprocess.run(["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
                               capture_output=True, text=True).stdout.strip().splitlines():
        i, u = [x.strip() for x in line.split(",")]
        idx[u] = int(i)
    out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,gpu_uuid,used_memory",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
    res = []
    for line in out.strip().splitlines():
        p, u, m = [x.strip() for x in line.split(",")]
        if idx.get(u) in GPUS and p != str(my_pid):
            res.append((p, int(m)))
    return res

def check_power_clock(where=""):
    """Re-read the environment variables every time: run_catc.py updates them after switching levels within the process."""
    q = subprocess.run(["nvidia-smi", "-i", str(GPUS[0]),
                        "--query-gpu=power.limit,power.default_limit,clocks.sm",
                        "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
    pl, dpl, sm = [float(v) for v in q.split(",")]
    want_pl = float(os.environ.get("HB_EXPECT_PL") or dpl)
    if abs(pl - want_pl) > 1:
        raise ConfigError(f"[{where}] GPU {GPUS[0]} power limit {pl:.0f} W, expected {want_pl:.0f} W")
    want_sm = os.environ.get("HB_EXPECT_SM")
    if want_sm and sm > float(want_sm) + 20:
        raise ConfigError(f"[{where}] GPU {GPUS[0]} SM clock {sm:.0f} MHz, above the locked {want_sm} MHz")

# Results go in one directory per mode (smoke / full are not mixed); query screening is also done on each mode's own data
RESULTS_BASE = os.environ.get("HB_RESULTS_BASE") or os.path.join(HB_ROOT, "results", os.environ.get("HB_MODE", "smoke"))

IMPORT_PATHS = "[" + ",".join(f'"{p}"' for p in [HB_ROOT, os.environ.get("HB_DATA_DIR")] if p) + "]"

def restart_server(num_gpus=None, start_gpu=None, wait_s=120):
    """Some queries crash HeavyDB outright (e.g. CUDA_ERROR_MISALIGNED_ADDRESS); restart and continue.
    GPU count / start GPU default to GPUS so they match the GPU sampled for energy (the old two-GPU default once contaminated a whole batch of data)."""
    num_gpus = len(GPUS) if num_gpus is None else num_gpus
    start_gpu = GPUS[0] if start_gpu is None else start_gpu
    subprocess.run(["pkill", "-9", "-x", "heavydb"], capture_output=True)
    time.sleep(3)
    subprocess.Popen(
        f"cd {BUILD} && START_GPU={start_gpu} NUM_GPUS={num_gpus} "
        f"nohup {HB_ROOT}/lib/start-heavydb.sh "
        f"--allowed-import-paths='{IMPORT_PATHS}' >> {BUILD}/server.log 2>&1 &",
        shell=True, executable="/bin/bash")
    t_end = time.time() + wait_s
    while time.time() < t_end:
        time.sleep(3)
        try:
            s = Sql(); s.run("SELECT 1;", timeout=10); s.close()
            return True
        except Exception:
            pass
    return False

# ---------------- Busy guard ----------------
def gpu_guard():
    """Confirm that no one else's large job is on GPU 4/5 besides our heavydb; returns (ok, details)."""
    out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,gpu_uuid,used_memory",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
    uuid = subprocess.run(["nvidia-smi", "-i", ",".join(map(str, GPUS)),
                           "--query-gpu=uuid", "--format=csv,noheader"],
                          capture_output=True, text=True).stdout.split()
    mine = {str(p) for p in [os.getpid()]}
    heavy = subprocess.run(["pgrep", "-x", "heavydb"], capture_output=True, text=True).stdout.split()
    others = []
    for line in out.strip().splitlines():
        pid, gid, mem = [x.strip() for x in line.split(",")]
        if gid in uuid and pid not in heavy and pid not in mine and int(mem) > 2000:
            others.append((pid, int(mem)))
    return (not others), others

def queries(suite):
    d = os.path.join(HB_ROOT, "queries", suite)
    fs = sorted(os.listdir(d), key=lambda n: int(re.search(r"\d+", n).group()))
    return [(f[:-4], open(os.path.join(d, f)).read().strip()) for f in fs]
