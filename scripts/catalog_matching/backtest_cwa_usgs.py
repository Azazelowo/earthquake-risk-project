"""CWA x USGS cross-agency event matching backtest.

Why: the matching thresholds in the catalog proposal (10 s / 30 km / 30 km depth)
and in 01_build_catalog.py (60 s / 0.3 deg) are guesses. There is no ground-truth
list of "same event" pairs, so we estimate:

- matched rate : share of USGS events that find a CWA partner under a rule
- false matches: run the same matcher after shifting all USGS origin times by
                 +-10 min ... +-3 h. Anything that still matches is a coincidence
                 (clustering of seismicity is preserved, so this is a realistic
                 null, not a uniform-random one). Mean over shifts = expected
                 false matches at zero shift.
- miss proxy   : USGS events with no CWA partner. CWA XML covers ML>=3 in the
                 whole overlap box, so nearly every USGS M4+ event should exist.

Usage:
    python backtest_cwa_usgs.py --data-dir <folder with earthquake_data/ and E-A0073-002/> --out <dir>
"""
from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd

EARTH_RADIUS_KM = 6371.0
# Overlap of the two catalogs' published coverage boxes.
LAT_MIN, LAT_MAX = 21.0, 25.0
LON_MIN, LON_MAX = 119.0, 124.0
T_START = pd.Timestamp("1990-01-01", tz="UTC")
T_END = pd.Timestamp("2026-01-01", tz="UTC")  # CWA XML ends 2025-12-31 local

CAND_DT_S = 120.0     # candidate search window (wider than any rule tested)
CAND_DIST_KM = 200.0
NULL_SHIFTS_S = [s * k for s in (600, 1800, 3600, 7200, 10800) for k in (1, -1)]
FIXED_DEPTHS_USGS = (10.0, 33.0, 35.0)

DT_GRID = [2, 3, 5, 10, 15, 20, 30, 60]
DIST_GRID = [10, 20, 30, 50, 75, 100]


# ---------------------------------------------------------------- loading
def load_usgs(data_dir: Path) -> pd.DataFrame:
    f = data_dir / "earthquake_data" / "raw" / "地震紀錄_usgs_earthquake_1990_2026.csv"
    u = pd.read_csv(f)
    out = pd.DataFrame({
        "uid": u["id"],
        "t": pd.to_datetime(u["time"], utc=True),
        "lat": u["latitude"], "lon": u["longitude"], "depth": u["depth"],
        "mag": u["mag"], "mag_type": u["magType"].str.lower(),
        "depth_err": u["depthError"], "h_err": u["horizontalError"],
    })
    out["depth_fixed_guess"] = out["depth"].isin(FIXED_DEPTHS_USGS)
    return out


def load_cwa_xml(data_dir: Path) -> pd.DataFrame:
    rows = []
    for f in sorted((data_dir / "E-A0073-002").glob("CWA-EQ-Catalog-*.xml")):
        # iter() does not accept the {*} namespace wildcard; iterfind() does.
        for eq in ET.parse(f).getroot().iterfind(".//{*}EarthquakeInfo"):
            rows.append((
                eq.findtext("{*}OriginTime"),
                eq.findtext("{*}EpicenterLatitude"),
                eq.findtext("{*}EpicenterLongitude"),
                eq.findtext("{*}FocalDepth"),
                eq.findtext("{*}LocalMagnitude"),
            ))
    c = pd.DataFrame(rows, columns=["t", "lat", "lon", "depth", "ml"])
    # Times carry +08:00; convert explicitly to UTC (the bug fixed in 01_build_catalog.py).
    c["t"] = pd.to_datetime(c["t"], utc=True)
    for k in ("lat", "lon", "depth", "ml"):
        c[k] = pd.to_numeric(c[k].str.strip(), errors="coerce")
    c["cid"] = np.arange(len(c))
    return c


def clip(df: pd.DataFrame) -> pd.DataFrame:
    m = (df.lat.between(LAT_MIN, LAT_MAX) & df.lon.between(LON_MIN, LON_MAX)
         & (df.t >= T_START) & (df.t < T_END))
    return df[m].sort_values("t").reset_index(drop=True)


# ---------------------------------------------------------------- matching
def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


def epoch_s(t: pd.Series) -> np.ndarray:
    # Unit-agnostic: pandas 3 may store datetimes as [s], [ms] or [us], so astype(int64)
    # is not safe to divide by a fixed constant.
    return ((t - pd.Timestamp("1970-01-01", tz="UTC")) / pd.Timedelta(seconds=1)).to_numpy()


def candidates(u: pd.DataFrame, c: pd.DataFrame, shift_s: float = 0.0) -> pd.DataFrame:
    """All (USGS, CWA) pairs within the wide candidate window. dt = CWA - USGS."""
    ut = epoch_s(u.t) + shift_s
    ct = epoch_s(c.t)
    lo = np.searchsorted(ct, ut - CAND_DT_S, "left")
    hi = np.searchsorted(ct, ut + CAND_DT_S, "right")
    ui = np.repeat(np.arange(len(u)), hi - lo)
    ci = np.concatenate([np.arange(a, b) for a, b in zip(lo, hi)]) if len(ui) else np.array([], int)
    dist = haversine_km(u.lat.to_numpy()[ui], u.lon.to_numpy()[ui],
                        c.lat.to_numpy()[ci], c.lon.to_numpy()[ci])
    p = pd.DataFrame({"ui": ui, "ci": ci, "dt": ct[ci] - ut[ui], "dist": dist,
                      "ddepth": np.abs(c.depth.to_numpy()[ci] - u.depth.to_numpy()[ui])})
    return p[p.dist <= CAND_DIST_KM].reset_index(drop=True)


def match_one_to_one(p: pd.DataFrame, max_dt: float, max_dist: float,
                     box_deg: float | None = None) -> pd.DataFrame:
    """Greedy one-to-one assignment by normalized score.

    box_deg reproduces 01_build_catalog.py's |dlat|,|dlon| <= deg rule instead of a km radius.
    """
    q = p[p.dt.abs() <= max_dt]
    if box_deg is None:
        q = q[q.dist <= max_dist]
        score = q.dt.abs() / max_dt + q.dist / max_dist
    else:
        q = q[(q.dlat.abs() <= box_deg) & (q.dlon.abs() <= box_deg)]
        score = q.dt.abs() / max_dt + q.dist / (box_deg * 111.0)
    q = q.assign(score=score).sort_values("score")
    used_u, used_c, keep = set(), set(), []
    for idx, a, b in zip(q.index, q.ui, q.ci):
        if a in used_u or b in used_c:
            continue
        used_u.add(a); used_c.add(b); keep.append(idx)
    return q.loc[keep]


def add_deg(p: pd.DataFrame, u: pd.DataFrame, c: pd.DataFrame) -> pd.DataFrame:
    return p.assign(dlat=c.lat.to_numpy()[p.ci] - u.lat.to_numpy()[p.ui],
                    dlon=c.lon.to_numpy()[p.ci] - u.lon.to_numpy()[p.ui])


# ---------------------------------------------------------------- main
def evaluate(rule_name, real_p, null_ps, n_u, **kw):
    m = match_one_to_one(real_p, **kw)
    nulls = [len(match_one_to_one(q, **kw)) for q in null_ps]
    fp = float(np.mean(nulls))
    return {"rule": rule_name, **{k: v for k, v in kw.items() if v is not None},
            "matched": len(m), "matched_rate": len(m) / n_u,
            "exp_false": fp, "exp_false_rate": fp / max(len(m), 1),
            "unmatched": n_u - len(m)}, m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    u = clip(load_usgs(args.data_dir))
    c = clip(load_cwa_xml(args.data_dir))
    print(f"USGS in overlap: {len(u)}   CWA XML in overlap: {len(c)}")

    real = add_deg(candidates(u, c), u, c)
    nulls = [add_deg(candidates(u, c, s), u, c) for s in NULL_SHIFTS_S]

    rows = []
    for dt in DT_GRID:
        for d in DIST_GRID:
            r, _ = evaluate("grid", real, nulls, len(u), max_dt=dt, max_dist=d)
            rows.append(r)
    grid = pd.DataFrame(rows)
    grid.to_csv(args.out / "grid.csv", index=False)

    named = {}
    for name, kw in {
        "proposal_10s_30km": dict(max_dt=10, max_dist=30),
        "current_60s_0.3deg": dict(max_dt=60, max_dist=None, box_deg=0.3),
    }.items():
        r, m = evaluate(name, real, nulls, len(u), **kw)
        named[name] = (r, m)
        rows.append(r)
    pd.DataFrame([v[0] for v in named.values()]).to_csv(args.out / "named_rules.csv", index=False)

    # Detailed pairs under a generous rule, for distribution plots / inspection.
    wide = match_one_to_one(real, max_dt=60, max_dist=100)
    pairs = wide.merge(u.add_prefix("u_"), left_on="ui", right_index=True) \
                .merge(c.add_prefix("c_"), left_on="ci", right_index=True)
    pairs["dmag"] = pairs.c_ml - pairs.u_mag
    pairs.to_csv(args.out / "pairs_wide_60s_100km.csv", index=False)
    unmatched = u.drop(index=wide.ui.unique())
    unmatched.to_csv(args.out / "usgs_unmatched_wide.csv", index=False)

    # Null pairs under the same generous rule: what coincidences look like.
    null_pairs = pd.concat([match_one_to_one(q, max_dt=60, max_dist=100).assign(shift_s=s)
                            for q, s in zip(nulls, NULL_SHIFTS_S)])
    null_pairs.to_csv(args.out / "null_pairs_wide.csv", index=False)
    print("done ->", args.out)


if __name__ == "__main__":
    main()
