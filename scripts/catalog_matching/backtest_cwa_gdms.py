"""CWA XML x GDMS matching backtest (same agency, two catalog products).

Why: 01_build_catalog.py removes CWA XML rows that duplicate GDMS using
60 s / 0.3 deg. That rule was never tested. Both products come from the CWA
network, so true pairs should agree far more tightly than CWA x USGS.

Method is the same as backtest_cwa_usgs.py: one-to-one greedy matching, false
matches estimated by shifting the query catalog's origin times.

Usage:
    python backtest_cwa_gdms.py --data-dir <folder with earthquake_data/ and E-A0073-002/> --out <dir>
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

import backtest_cwa_usgs as core

LAT_MIN, LAT_MAX = 20.0, 27.0     # CWA XML published box
LON_MIN, LON_MAX = 118.0, 124.0
GDMS_ML_MIN = 2.5                 # XML is ML>=3; keep slack for ML differences

DT_GRID = [0.5, 1, 2, 3, 5, 10, 30, 60]
DIST_GRID = [1, 2, 5, 10, 20, 33]


def load_gdms(data_dir: Path) -> pd.DataFrame:
    frames = []
    for f in sorted((data_dir / "earthquake_data" / "raw").glob("GDMS地震目錄_cwa_earthquake_*.csv")):
        g = pd.read_csv(f, dtype={"fixed": str})
        frames.append(g)
    g = pd.concat(frames, ignore_index=True)
    out = pd.DataFrame({
        "t": pd.to_datetime(g["date"] + " " + g["time"], utc=True, errors="coerce"),  # GDMS is UTC
        "lat": g["lat"], "lon": g["lon"], "depth": g["depth"], "ml": g["ML"],
        "fixed": g["fixed"],
    })
    out = out.dropna(subset=["t", "lat", "lon"])
    out = out[out.ml >= GDMS_ML_MIN]
    # Exact duplicates across files would inflate matches.
    return out.drop_duplicates(subset=["t", "lat", "lon", "ml"])


def clip(df: pd.DataFrame) -> pd.DataFrame:
    m = (df.lat.between(LAT_MIN, LAT_MAX) & df.lon.between(LON_MIN, LON_MAX)
         & (df.t >= core.T_START) & (df.t < core.T_END))
    return df[m].sort_values("t").reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    q = clip(core.load_cwa_xml(args.data_dir))   # query: CWA XML
    r = clip(load_gdms(args.data_dir))           # reference: GDMS
    print(f"CWA XML: {len(q)}   GDMS (ML>={GDMS_ML_MIN}): {len(r)}")

    # core.candidates(u, c) treats u as query and c as reference; dt = ref - query.
    real = core.add_deg(core.candidates(q, r), q, r)
    nulls = [core.add_deg(core.candidates(q, r, s), q, r) for s in core.NULL_SHIFTS_S]

    rows = []
    for dt in DT_GRID:
        for d in DIST_GRID:
            row, _ = core.evaluate("grid", real, nulls, len(q), max_dt=dt, max_dist=d)
            rows.append(row)
    pd.DataFrame(rows).to_csv(args.out / "gdms_grid.csv", index=False)

    named = []
    for name, kw in {
        "current_60s_0.3deg": dict(max_dt=60, max_dist=None, box_deg=0.3),
        "tight_2s_5km": dict(max_dt=2, max_dist=5),
        "usgs_rule_10s_75km": dict(max_dt=10, max_dist=75),
    }.items():
        row, _ = core.evaluate(name, real, nulls, len(q), **kw)
        named.append(row)
    pd.DataFrame(named).to_csv(args.out / "gdms_named_rules.csv", index=False)

    wide = core.match_one_to_one(real, max_dt=60, max_dist=100)
    pairs = wide.merge(q.add_prefix("q_"), left_on="ui", right_index=True) \
                .merge(r.add_prefix("r_"), left_on="ci", right_index=True)
    pairs["dml"] = pairs.r_ml - pairs.q_ml
    pairs.to_csv(args.out / "gdms_pairs_wide.csv", index=False)
    q.drop(index=wide.ui.unique()).to_csv(args.out / "gdms_cwa_unmatched_wide.csv", index=False)
    print("done ->", args.out)


if __name__ == "__main__":
    main()
