#!/usr/bin/env python3
"""Sanity check of the Category A / B results before starting Category C.

    check_ab.py <results-dir>            e.g. results/full/h100-80gb-hbm3

Prints, per result file, the row count, the number of rows that carry an error, and a per-suite x SF
breakdown; then lists every failed query with its error text. Exit status 1 when a result file is
missing or has no rows (nothing to validate), 0 otherwise -- rows with errors are reported but do not
fail the check, since some are expected (ClickBench q27 string encoding, H2O q10 OOM at 4/8 GB).
"""
import csv
import os
import sys
from collections import Counter, defaultdict

FILES = ["catA_powersample.csv", "catB_timing.csv", "catB_energy.csv"]


def load(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def main(rdir):
    missing, failures, flagged, empty, unlisted = [], [], [], [], []
    print(f"== Category A/B results in {rdir}")
    for name in FILES:
        p = os.path.join(rdir, name)
        if not os.path.isfile(p):
            missing.append(name)
            print(f"  {name:<24} MISSING")
            continue
        rows = load(p)
        if not rows:
            missing.append(name)
            print(f"  {name:<24} 0 rows")
            continue
        errs = [r for r in rows if (r.get("error") or "").strip()]
        per = Counter((r["suite"], r["sf"]) for r in rows)
        per_err = Counter((r["suite"], r["sf"]) for r in errs)
        print(f"  {name:<24} {len(rows):5d} rows, {len(errs):3d} with errors")
        for (suite, sf) in sorted(per):
            e = per_err.get((suite, sf), 0)
            mark = f"  ({e} failed)" if e else ""
            print(f"      {suite:<18} {sf:<6} {per[(suite, sf)]:3d} queries{mark}")
        for r in errs:
            failures.append((name, r["suite"], r["sf"], r["query"], r["error"].strip()))
        # Cat A runs with the data already on the GPU, so a negative dynamic energy there means the
        # idle baseline was higher than the load window: worth a look before spending days on Cat C.
        if name == "catA_powersample.csv":
            # every query returning 0 rows on one dataset = the table is empty (an import that died halfway)
            okrows = defaultdict(list)
            for r in rows:
                if not (r.get("error") or "").strip():
                    okrows[(r["suite"], r["sf"], r.get("db", ""))].append(r)
            for (suite, sf, db), rs in sorted(okrows.items()):
                if len(rs) >= 3 and all((r.get("n_rows") or "0") in ("0", "0.0") for r in rs):
                    empty.append((suite, sf, db))
            srcp = os.path.join(rdir, "data_sources.csv")
            if os.path.isfile(srcp):
                have = {r["db"] for r in load(srcp)}
                unlisted = sorted({r.get("db", "") for r in rows} - have - {""})
            for r in rows:
                try:
                    if float(r.get("E_dyn_mJ") or "nan") < 0:
                        flagged.append((r["suite"], r["sf"], r["query"], r["E_dyn_mJ"]))
                except ValueError:
                    pass

    if failures:
        print(f"\n== {len(failures)} failed query runs")
        for name, suite, sf, q, err in failures:
            print(f"  [{name.split('_')[0]}] {suite}/{sf} {q}: {err}")
    if flagged:
        print(f"\n== {len(flagged)} Cat A rows with negative E_dyn_mJ (idle baseline above load window)")
        for suite, sf, q, e in flagged:
            print(f"  {suite}/{sf} {q}: E_dyn_mJ={e}")
    if unlisted:     # a hint only: data_sources.csv may simply be missing or from another run
        print(f"\n!! warning: not in data_sources.csv (an import that never finished?): {', '.join(unlisted)}")
    if empty:
        print(f"\n!! {len(empty)} dataset(s) look empty -- results on them are meaningless; ./fix_results.sh re-imports and re-measures them")
        for suite, sf, db in dict.fromkeys(empty):
            print(f"  {suite}/{sf} {db}")
        return 1
    if missing:
        print(f"\n!! incomplete: {', '.join(missing)} -- rerun run_ab.sh (finished parts are skipped) before Category C")
        return 1
    print("\nCategory A/B results are complete" + (" (with the failures listed above)" if failures else "") + ".")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__.strip())
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
