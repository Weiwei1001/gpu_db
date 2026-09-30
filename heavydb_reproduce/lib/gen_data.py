#!/usr/bin/env python3
"""准备一个数据集并导入 HeavyDB：gen_data.py <suite> <db> <param> [<sf>]
   tpch <db> <sf>        h2o <db> <rows>        clickbench <db> <pct>

先在 HB_DATA_ROOTS（冒号分隔）里找现成数据——别人跑 gpu_db 的 Maximus/Sirius 时留下的：
   TPC-H      tests/tpch/csv-<N>/*.csv | tpch/csv-<N>/ | tests/tpch_duckdb/tpch_sf<N>.duckdb | sirius_db/tpch_<N>.duckdb
   H2O        tests/h2o/csv-<S>/groupby.csv | h2o/csv-<S>/ | tests/h2o_duckdb/h2o_<S>.duckdb | sirius_db/h2o_<S>.duckdb
   ClickBench tests/clickbench/csv-<N>/t.csv | clickbench/csv-<N>/ | tests/click_duckdb/clickbench_<N>.duckdb
              | sirius_db/clickbench_<N>.duckdb | 本地 hits.parquet（按 repo 规则取前 总行数×N/70 行）
找到就从它导入（三个引擎用同一份行）；找不到再生成：TPC-H 用 duckdb dbgen（确定性），H2O 按论文分布生成，
ClickBench 先把 hits.parquet（14.8 GB）下载到 --data-dir 下（只下一次，四个 SF 共用），再按 repo 规则取前 N 行。
一律经 Parquet 导入，列类型按 schemas/*.sql 强制转换；导完即删临时文件。来源记入 results/<mode>/data_sources.csv。"""
import csv, glob, os, re, subprocess, sys, time
import duckdb

suite, db, param = sys.argv[1], sys.argv[2], sys.argv[3]
sf = sys.argv[4] if len(sys.argv) > 4 else ""
HB_ROOT = os.environ["HB_ROOT"]
HSQL = os.path.join(HB_ROOT, "lib", "hsql")
DATA_DIR = os.environ.get("HB_DATA_DIR") or HB_ROOT
STAGE = os.path.join(DATA_DIR, "stage"); os.makedirs(STAGE, exist_ok=True)
RESULTS = os.environ.get("HB_RESULTS_BASE") or os.path.join(HB_ROOT, "results", os.environ.get("HB_MODE", "smoke"))
SCHEMA = {"tpch": "tpch.sql", "h2o": "h2o.sql", "clickbench": "clickbench.sql"}[suite]
CB_URL = "https://datasets.clickhouse.com/hits_compatible/hits.parquet"
CB_FULL_CSV_GB = 70.0                                   # repo generate_clickbench.py 的换算常数

# 搜索根：环境变量 + 常见位置。只查固定几种布局，不递归扫盘。
ROOTS = [p for p in os.environ.get("HB_DATA_ROOTS", "").split(":") if p]
ROOTS += [DATA_DIR, os.path.join(DATA_DIR, "gpudb_data"), os.path.expanduser("~/gpu_db"),
          os.path.join(os.path.dirname(HB_ROOT), "gpu_db"), os.path.join(HB_ROOT, "..", "..", "gpu_db")]
ROOTS = [os.path.realpath(r) for r in ROOTS if os.path.isdir(r)]
ROOTS = list(dict.fromkeys(ROOTS))

def hsql(d, sql):
    r = subprocess.run([HSQL, d], input=sql, capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()

def schema_types():
    """schemas/<suite>.sql -> {表: [(列, duckdb 类型)]}，导入前按它 CAST，保证 parquet 类型与 HeavyDB 表一致。"""
    out = {}
    for m in re.finditer(r"CREATE TABLE (\w+)\s*\((.*?)\);", open(os.path.join(HB_ROOT, "schemas", SCHEMA)).read(), re.S):
        cols = []
        for c in re.split(r",\s*(?![^()]*\))", m.group(2).strip()):
            name, typ = c.strip().split(None, 1)
            typ = re.sub(r"\s+ENCODING.*", "", typ).strip()
            cols.append((name, {"TEXT": "VARCHAR"}.get(typ, typ)))
        out[m.group(1)] = cols
    return out

TYPES = schema_types()

def cast_select(table, src):
    sel = ", ".join(f"CAST({c} AS {t}) AS {c}" for c, t in TYPES[table])
    return f"SELECT {sel} FROM {src}"

def imp(table, path):
    out = hsql(db, f"COPY {table} FROM '{path}' WITH (source_type='parquet_file');")
    if "Loaded" not in out:
        sys.exit(f"导入 {table} 失败: {out[:300]}")
    os.remove(path)

def first(paths):
    for p in paths:
        hits = sorted(glob.glob(p))
        if hits:
            return hits[0]
    return None

# ---------- 找现成数据 ----------
def n(s):                                   # "sf10" -> "10", "4gb" -> "4gb"
    return s[2:] if s.startswith("sf") else s

def discover():
    if os.environ.get("HB_MODE", "smoke") != "full" or not sf:
        return None
    N = n(sf)
    cands = []
    for r in ROOTS:
        if suite == "tpch":
            cands += [(f"{r}/tests/tpch/csv-{N}/lineitem.csv", "csvdir"), (f"{r}/tpch/csv-{N}/lineitem.csv", "csvdir"),
                      (f"{r}/csv-{N}/lineitem.csv", "csvdir"),
                      (f"{r}/tests/tpch_duckdb/tpch_sf{N}.duckdb", "duckdb"), (f"{r}/sirius_db/tpch_{N}.duckdb", "duckdb")]
        elif suite == "h2o":
            cands += [(f"{r}/tests/h2o/csv-{N}/groupby.csv", "csv"), (f"{r}/h2o/csv-{N}/groupby.csv", "csv"),
                      (f"{r}/csv-{N}/groupby.csv", "csv"),
                      (f"{r}/tests/h2o_duckdb/h2o_{N}.duckdb", "duckdb"), (f"{r}/sirius_db/h2o_{N}.duckdb", "duckdb")]
        else:
            cands += [(f"{r}/tests/clickbench/csv-{N}/t.csv", "csv"), (f"{r}/clickbench/csv-{N}/t.csv", "csv"),
                      (f"{r}/csv-{N}/t.csv", "csv"),
                      (f"{r}/tests/click_duckdb/clickbench_{N}.duckdb", "duckdb"),
                      (f"{r}/sirius_db/clickbench_{N}.duckdb", "duckdb"),
                      (f"{r}/tests/clickbench/hits.parquet", "parquet"), (f"{r}/clickbench/hits.parquet", "parquet"),
                      (f"{r}/hits.parquet", "parquet")]
    for p, kind in cands:
        if os.path.exists(p):
            return p, kind
    return None

def cb_source_sql(src):
    """repo 的 CSV 把 EventTime/EventDate 转成了时间戳；HeavyDB schema 与 query 用的是整数秒/天，按源类型转回去。"""
    typed = dict(TYPES["t"])
    desc = {c[0]: c[1].upper() for c in con.execute(f"DESCRIBE SELECT * FROM {src} LIMIT 1").fetchall()}
    sel = []
    for c, t in TYPES["t"]:
        if c not in desc:
            sys.exit(f"来源缺列 {c}")
        st = desc[c]
        if c == "EventTime" and st.startswith("TIMESTAMP"):
            sel.append("CAST(epoch(EventTime) AS BIGINT) AS EventTime")
        elif c == "EventDate" and (st.startswith("TIMESTAMP") or st == "DATE"):
            sel.append("CAST(date_diff('day', DATE '1970-01-01', CAST(EventDate AS DATE)) AS INTEGER) AS EventDate")
        else:
            sel.append(f"CAST({c} AS {t}) AS {c}")
    return f"SELECT {', '.join(sel)} FROM {src}"

# ---------- 主流程 ----------
t0 = time.time()
con = duckdb.connect(":memory:")
hsql("heavyai", f"DROP DATABASE IF EXISTS {db};")
hsql("heavyai", f"CREATE DATABASE {db};")
hsql(db, open(os.path.join(HB_ROOT, "schemas", SCHEMA)).read())

found = discover()
source = ""
if found:
    path, kind = found
    source = path
    print(f"  {db}: 用现成数据 {path}", flush=True)
    if kind == "duckdb":
        con.execute(f"ATTACH '{path}' AS src (READ_ONLY)")

if suite == "tpch":
    tables = ["region", "nation", "supplier", "customer", "part", "partsupp", "orders", "lineitem"]
    if not found:
        source = f"duckdb dbgen(sf={param})"
        con.execute("INSTALL tpch; LOAD tpch;")
        con.execute(f"CALL dbgen(sf={param})")
    for t in tables:
        if found and kind == "csvdir":
            src = f"read_csv_auto('{os.path.dirname(path)}/{t}.csv', header=true)"
        elif found:
            src = f"src.{t}"
        else:
            src = t
        p = f"{STAGE}/{t}.parquet"
        con.execute(f"COPY ({cast_select(t, src)}) TO '{p}' (FORMAT PARQUET)")
        imp(t, p)
    nrows = hsql(db, "SELECT COUNT(*) FROM lineitem;").split("\n")[0]
elif suite == "h2o":
    p = f"{STAGE}/groupby.parquet"
    if found:
        src = f"read_csv_auto('{path}', header=true)" if kind == "csv" else "src.groupby"
        con.execute(f"COPY ({cast_select('groupby', src)}) TO '{p}' (FORMAT PARQUET)")
    else:
        source = f"generate_h2o 同款分布，{int(param)} 行（random() 无种子）"
        con.execute(f"""COPY (SELECT
          'id'||LPAD(CAST(1+(random()*99)::int AS VARCHAR),3,'0') AS id1,
          'id'||LPAD(CAST(1+(random()*99)::int AS VARCHAR),3,'0') AS id2,
          'id'||LPAD(CAST((random()*9999999)::int AS VARCHAR),10,'0') AS id3,
          CAST(1+(random()*99)::int AS INTEGER) AS id4, CAST(1+(random()*99)::int AS INTEGER) AS id5,
          CAST(1+(random()*99999)::int AS INTEGER) AS id6, CAST(1+(random()*4)::int AS INTEGER) AS v1,
          CAST(1+(random()*14)::int AS INTEGER) AS v2, CAST(random()*100 AS DOUBLE) AS v3
          FROM generate_series(1,{int(param)})) TO '{p}' (FORMAT PARQUET)""")
    imp("groupby", p)
    nrows = hsql(db, "SELECT COUNT(*) FROM groupby;").split("\n")[0]
else:
    p = f"{STAGE}/hits.parquet"
    if found and kind == "csv":
        src = f"read_csv_auto('{path}', header=true, sample_size=200000)"
        con.execute(f"COPY ({cb_source_sql(src)}) TO '{p}' (FORMAT PARQUET)")
    elif found and kind == "duckdb":
        con.execute(f"COPY ({cb_source_sql('src.t')}) TO '{p}' (FORMAT PARQUET)")
    else:
        # repo 规则：取前 总行数 × SF/70 行（generate_clickbench.py 的 LIMIT）
        if found:                                   # 本地 hits.parquet
            pq = path
        elif os.environ.get("HB_MODE", "smoke") == "full":
            # 远程按行读太慢（httpfs 取 140 万行超过 15 分钟），全量模式下载一次到本地，四个 SF 共用
            pq = os.path.join(DATA_DIR, "clickbench", "hits.parquet")
            if not os.path.exists(pq):
                os.makedirs(os.path.dirname(pq), exist_ok=True)
                print(f"  下载 {CB_URL}（14.8 GB）-> {pq}", flush=True)
                r = subprocess.run(["wget", "-q", "-c", "-O", pq + ".part", CB_URL])
                if r.returncode != 0:
                    sys.exit("下载 hits.parquet 失败（需要能访问 datasets.clickhouse.com）")
                os.rename(pq + ".part", pq)
            source = f"{pq}（下载自 {CB_URL}）"
        else:
            pq = CB_URL
            con.execute("INSTALL httpfs; LOAD httpfs;")
            source = f"{CB_URL} 远程随机采样 {param}%"
        if os.environ.get("HB_MODE", "smoke") == "full":
            total = con.execute(f"SELECT count(*) FROM read_parquet('{pq}')").fetchone()[0]   # 只读元数据
            limit = max(1, int(total * min(1.0, float(n(sf)) / CB_FULL_CSV_GB)))
            con.execute(f"COPY ({cb_source_sql(f'(SELECT * FROM read_parquet(\'{pq}\') LIMIT {limit}) AS h')}) "
                        f"TO '{p}' (FORMAT PARQUET)")
        else:                                       # smoke：随机小样本
            con.execute(f"COPY (SELECT * FROM read_parquet('{pq}') USING SAMPLE {float(param)} PERCENT (bernoulli, 42)) "
                        f"TO '{p}' (FORMAT PARQUET)")
    imp("t", p)
    nrows = hsql(db, "SELECT COUNT(*) FROM t;").split("\n")[0]

os.makedirs(RESULTS, exist_ok=True)
log = os.path.join(RESULTS, "data_sources.csv")
new = not os.path.exists(log)
with open(log, "a", newline="") as f:
    w = csv.writer(f)
    if new:
        w.writerow(["db", "suite", "sf", "rows", "source"])
    w.writerow([db, suite, sf, nrows, source])
print(f"  {db}: {nrows} 行，{time.time()-t0:.0f}s  <- {source}", flush=True)
