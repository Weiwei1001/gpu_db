#!/usr/bin/env python3
"""HeavyDB benchmark 公用件：持久 heavysql 会话 + NVML 能量计数器。"""
import ctypes, os, queue, re, subprocess, threading, time
import statistics as st

HB_ROOT = os.environ.get("HB_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUILD = os.path.join(os.environ.get("HEAVYDB_HOME", os.path.join(HB_ROOT, "heavydb")), "build")
DEPS = os.environ.get("DEPS_PREFIX", "/usr/local/mapd-deps")
def _server_gpus():
    """从 heavydb 进程参数推断它用了哪些 GPU —— 采样卡必须和服务端一致，
    否则会出现"在 A 卡采样、B 卡执行"的静默错误（曾因此报废一整批数据）。"""
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
    """两张卡累计能量之和（J），来自硬件能量计数器。"""
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
    """空载功率：能量计数器 ΔE/Δt，窗口 >=4s。"""
    e0, t0 = energy_J(), time.perf_counter()
    time.sleep(seconds)
    e1, t1 = energy_J(), time.perf_counter()
    return (e1 - e0) / (t1 - t0)

# ---------------- heavysql ----------------
class Sql:
    """持久 heavysql 会话。

    输出顺序是 [结果行...] ["N rows returned."] ["Execution time: X ms, ..."]，
    每条 query 后面跟一条单独成行的哨兵 SELECT（HeavyDB 不允许一次请求多条语句），
    用哨兵回显判定前一条到底跑完没有 —— HeavyDB 有一批错误信息不含 "Error" 字样。
    读取走后台线程 + 队列：stdout 是行缓冲的文本流，select() 看不到已经进了
    Python 缓冲区的数据，会假死。
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
        """取一行；超时返回 ""；服务端死了返回 None。

        HeavyDB 崩溃时 heavysql 不会退出、也不吐任何东西，只靠超时会白等到
        timeout 上限，所以没数据时每 2 秒探一次服务端进程。
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
                raise TimeoutError(f"{timeout}s 超时")
            if line is None:
                raise RuntimeError("服务端已退出")
            m = self.TIMING.search(line)
            if m:
                if seen_token:                      # 哨兵自己的 timing —— 收尾
                    if got == want:
                        return total
                    msg = next((l.strip() for l in reversed(noise) if l.strip()), "未知错误")
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
        """跑一次，返回结果行数（heavysql 会打印 "N rows returned."）。"""
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
                raise TimeoutError(f"{timeout}s 超时")
            if line is None:
                raise RuntimeError("服务端已退出")
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
        """包一层 COUNT(*)：内层扫描/聚合照做，但不把海量结果集传回客户端。"""
        stmts = [x.strip() for x in sql.strip().split(";") if x.strip()]
        stmts[-1] = f"SELECT COUNT(*) FROM ({stmts[-1]}) AS _hb_wrap"
        return "; ".join(stmts) + ";"

    def run_burst(self, sql, k, timeout=1800):
        """把同一条 query 连发 k 次（不逐条等回包），返回 (每次 exec_ms 列表, 墙钟秒)。

        放大法要求窗口里基本都在执行 query；逐条 run() 每次都要一个客户端往返，
        短 query 上空隙能占到窗口的一大半，会把 E/K 稀释掉。
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
                raise TimeoutError(f"{timeout}s 超时")
            if line is None:
                raise RuntimeError("服务端已退出")
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
            raise RuntimeError(f"burst 只回了 {len(ms)}/{want} 条计时")
        return ms, t1 - t0

    def cmd(self, c):
        self._w(c); time.sleep(0.3)

    def close(self):
        try:
            self._w("\\q"); self.p.wait(timeout=10)
        except Exception:
            self.p.kill()

# ---------------- 服务端存活/重启 ----------------
def server_alive():
    return bool(subprocess.run(["pgrep", "-x", "heavydb"],
                               capture_output=True, text=True).stdout.strip())

# ---------------- 逐 query 配置检查 ----------------
class ConfigError(RuntimeError):
    """服务端配置与测量口径不一致：必须中止，不能续跑。"""

def _heavydb_pids():
    return subprocess.run(["pgrep", "-x", "heavydb"],
                          capture_output=True, text=True).stdout.split()

def _gpus_of_pid(pid):
    """该进程在哪几块卡上有 CUDA context（nvidia-smi 实测，不信启动参数）。"""
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
    """每条 query 前后调用 check()。检查：
    1. 只有一个 heavydb 进程，且 PID 没变（没被悄悄重启过）；
    2. 启动参数 --start-gpu/--num-gpus 与 GPUS 一致；
    3. nvidia-smi 实测该进程只在 GPUS 上有 context —— 能耗采样的卡 == 执行的卡；
    4. 功率上限 / SM 时钟：设了 HB_EXPECT_PL / HB_EXPECT_SM（Cat C 网格点）就核对为该档，
       没设就要求功率上限为默认值（Cat A/B 不能在 Cat C 残留的设置下跑）。
    """
    def __init__(self):
        self.pid = None
        self.rebind()

    def rebind(self):
        """初次启动或受控重启之后，认下新的 PID 并立即检查。"""
        pids = _heavydb_pids()
        if len(pids) != 1:
            raise ConfigError(f"heavydb 进程数 {len(pids)}，应为 1")
        self.pid = pids[0]
        self.check("rebind")

    def check(self, where=""):
        pids = _heavydb_pids()
        if pids != [self.pid]:
            raise ConfigError(f"[{where}] heavydb PID {self.pid} -> {pids}（被重启或多开）")
        start, num = _args_of_pid(self.pid)
        if (start, num) != (GPUS[0], len(GPUS)):
            raise ConfigError(f"[{where}] 启动参数 start_gpu={start} num_gpus={num}，"
                              f"应为 start_gpu={GPUS[0]} num_gpus={len(GPUS)}")
        real = _gpus_of_pid(self.pid)
        if real != sorted(GPUS):
            raise ConfigError(f"[{where}] heavydb 实际在 GPU {real} 上，能耗采的是 GPU {GPUS}")
        others = _others_on_gpus(self.pid)
        if others and os.environ.get("HB_IGNORE_BUSY", "0") != "1":
            raise ConfigError(f"[{where}] GPU {GPUS} 上出现了别的进程 {others}（能耗会被污染；HB_IGNORE_BUSY=1 可忽略）")
        check_power_clock(where)
        return ",".join(map(str, real))

def _others_on_gpus(my_pid):
    """目标卡上除本 heavydb 之外的进程 [(pid, MiB)]。"""
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
    """每次都重新读环境变量：run_catc.py 在进程内切档后会更新它们。"""
    q = subprocess.run(["nvidia-smi", "-i", str(GPUS[0]),
                        "--query-gpu=power.limit,power.default_limit,clocks.sm",
                        "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
    pl, dpl, sm = [float(v) for v in q.split(",")]
    want_pl = float(os.environ.get("HB_EXPECT_PL") or dpl)
    if abs(pl - want_pl) > 1:
        raise ConfigError(f"[{where}] GPU {GPUS[0]} 功率上限 {pl:.0f} W，应为 {want_pl:.0f} W")
    want_sm = os.environ.get("HB_EXPECT_SM")
    if want_sm and sm > float(want_sm) + 20:
        raise ConfigError(f"[{where}] GPU {GPUS[0]} SM 时钟 {sm:.0f} MHz，高于锁定的 {want_sm} MHz")

# 结果按模式分目录（smoke / full 不混在一起），query 筛查也在各自模式的数据上做
RESULTS_BASE = os.environ.get("HB_RESULTS_BASE") or os.path.join(HB_ROOT, "results", os.environ.get("HB_MODE", "smoke"))

IMPORT_PATHS = "[" + ",".join(f'"{p}"' for p in [HB_ROOT, os.environ.get("HB_DATA_DIR")] if p) + "]"

def restart_server(num_gpus=None, start_gpu=None, wait_s=120):
    """HeavyDB 会被某些 query 直接打挂（如 CUDA_ERROR_MISALIGNED_ADDRESS），重启续跑。
    卡数/起始卡默认取 GPUS，保证与能耗采样的卡一致（曾因默认双卡污染整批数据）。"""
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

# ---------------- 占用守卫 ----------------
def gpu_guard():
    """确认 GPU 4/5 上除了我们的 heavydb 没有别人的大任务；返回 (ok, 说明)。"""
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
