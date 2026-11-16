#!/usr/bin/env python3
"""Translate the repo's 120-query microbenchmark (gpu_db/microbench/{tpch,h2o,clickbench}/*.sql, DuckDB dialect)
into HeavyDB SQL under queries/microbench_{tpch,h2o,clickbench}/<id>.sql.

Five files under microbench/tpch/ (w2_018, w2_019, w3_027, w4_058, w6_022) read the ClickBench table, and Maximus
runs them on ClickBench (src/maximus/microbench/microbench_clickbench.cpp), so they go to microbench_clickbench:
55 TPC-H + 35 H2O + 30 ClickBench = 120.
<id> is the Maximus/Sirius query id (the first two "_" fields of the file name, e.g. w1_002, w5a_029), so the
HeavyDB rows join directly with the other engines' microbench results. The rewrites are the same ones the
full-query HeavyDB suites use:
  * ClickBench: table hits -> t; EventDate is stored as INTEGER days since 1970-01-01 (lib/gen_data.py),
    so a 'YYYY-MM-DD' literal compared with EventDate becomes that day number.
  * TPC-H: a 'YYYY-MM-DD' literal becomes CAST('YYYY-MM-DD' AS date) (as in queries/tpch/q6.sql).
  * H2O: unchanged.

    gen_microbench.py [--src DIR] [--check]     --check: exit 1 if the committed files differ from a fresh translation
"""
import argparse, datetime, os, re, sys

HB_ROOT = os.environ.get("HB_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(os.path.dirname(HB_ROOT), "microbench")
EPOCH = datetime.date(1970, 1, 1)
DATE = r"'(\d{4})-(\d{2})-(\d{2})'"


def translate(bench, sql):
    if bench == "clickbench":
        sql = re.sub(r"\bhits\b", "t", sql)
        sql = re.sub(r"(\bEventDate\s*(?:>=|<=|<>|!=|=|<|>)\s*)" + DATE,
                     lambda m: m.group(1) + str((datetime.date(*map(int, m.groups()[1:])) - EPOCH).days), sql)
        if re.search(r"EventDate\s*(?:>=|<=|<>|!=|=|<|>)\s*'", sql):
            raise SystemExit(f"!! untranslated EventDate literal: {sql}")
    elif bench == "tpch":
        sql = re.sub(DATE, lambda m: f"CAST('{m.group(1)}-{m.group(2)}-{m.group(3)}' AS date)", sql)
    return sql


def build(src):
    out = {}
    for bench in ("tpch", "h2o", "clickbench"):
        d = os.path.join(src, bench)
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".sql") or fn.startswith("_"):
                continue
            qid = "_".join(fn[:-4].split("_")[:2])
            body = "\n".join(l for l in open(os.path.join(d, fn)).read().splitlines()
                             if l.strip() and not l.lstrip().startswith("--"))
            body = " ".join(body.split()).rstrip(";") + ";"
            dest = "clickbench" if re.search(r"\bhits\b", body) else bench   # w2_018 ... w6_022 sit in tpch/ but read hits
            key = (f"microbench_{dest}", qid)
            if key in out:
                raise SystemExit(f"!! duplicate query id {qid} in {d}")
            out[key] = translate(dest, body) + "\n"      # no "--" header: Sql.run joins lines, a comment would swallow the query
    return out


ap = argparse.ArgumentParser()
ap.add_argument("--src", default=SRC)
ap.add_argument("--check", action="store_true")
a = ap.parse_args()
qs = build(a.src)
stale = []
for (suite, qid), text in qs.items():
    p = os.path.join(HB_ROOT, "queries", suite, qid + ".sql")
    if a.check:
        if not os.path.exists(p) or open(p).read() != text:
            stale.append(p)
        continue
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(text)
if a.check:
    if stale:
        sys.exit("!! microbench queries out of date (rerun lib/gen_microbench.py): " + ", ".join(stale))
    print(f"microbench queries up to date ({len(qs)})")
else:
    n = {}
    for s, _ in qs:
        n[s] = n.get(s, 0) + 1
    print("written", ", ".join(f"{s}: {k}" for s, k in sorted(n.items())))
