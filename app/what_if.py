"""「如果真的發生會怎樣」第一階段：歷史類比（規格 docs/phase2/forecast_card_spec.md §8）。

只回答一件事：歷史目錄裡，規模和 m 相近、震央落在所選地點附近的地震有哪些。
不是預報、不是預警，不推估震度或 PGA（專案沒有驗證過的地動衰減式）。

為什麼這樣設計：
- 輸入是純表格（time, lat, lon, ml；depth、located 選填），不綁定特定目錄，方便測試與換母體。
- 距離用 hazard_core.haversine_km，和 point_weights 的「點＋半徑」同一套幾何，邊界一律「含等號」。
- 「同規模以上的全台最大幾筆」不受地點限制，當作比例尺（目錄裡實際存在的事件，不憑記憶補）。
"""
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hazard_core as hc  # noqa: E402

EPS = 1e-9  # 規模與距離比較的浮點容差，讓「剛好等於」的事件算在內
TOL_DEFAULT = 0.3
NEAR_MARGIN_DEFAULT_KM = 50.0
SCOPE_IN, SCOPE_NEAR = "區域內", "附近"


@dataclass
class WhatIfResult:
    m: float
    tol: float
    lat: float
    lon: float
    radius_km: float
    near_margin_km: float
    analogs: pd.DataFrame      # 規模在 m±tol、距離 ≤ radius+margin；含 scope、dist_km
    largest: pd.DataFrame      # 全目錄 ml ≥ m 中最大的 n 筆（不限地點），含 dist_km
    n_in: int                  # 區域內（dist ≤ radius）的類比事件數
    n_near: int                # 附近（radius < dist ≤ radius+margin）的類比事件數
    n_catalog: int
    span: tuple                # (最早時間, 最晚時間)；UTC
    has_depth: bool


def _clean(events):
    need = ["time", "lat", "lon", "ml"]
    miss = [c for c in need if c not in events.columns]
    if miss:
        raise ValueError(f"目錄缺少欄位 {miss}")
    df = events.copy()
    df["time"] = pd.to_datetime(df["time"])
    for c in ("lat", "lon", "ml"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if "depth" in df.columns:
        df["depth"] = pd.to_numeric(df["depth"], errors="coerce")
    return df.dropna(subset=need).reset_index(drop=True)


def find_analogs(events, lat, lon, radius_km, m, tol=TOL_DEFAULT,
                 near_margin_km=NEAR_MARGIN_DEFAULT_KM, n_largest=5):
    """找歷史類比事件。

    analogs：|ml − m| ≤ tol 且距離 ≤ radius_km + near_margin_km；距離 ≤ radius_km 標「區域內」，其餘標「附近」。
    排序：先看規模差（越小越前），再看距離（越近越前），最後時間（新的在前）。
    largest：全目錄 ml ≥ m 的最大 n_largest 筆（規模由大到小，同規模時間新的在前），不限地點。
    """
    if radius_km < 0 or near_margin_km < 0 or tol < 0:
        raise ValueError("radius_km、near_margin_km、tol 不可為負")
    df = _clean(events)
    has_depth = "depth" in df.columns and bool(df["depth"].notna().any())
    if len(df) == 0:
        empty = df.assign(dist_km=[], scope=[], dm=[])
        return WhatIfResult(m, tol, lat, lon, radius_km, near_margin_km, empty, empty.drop(columns=["scope", "dm"]),
                            0, 0, 0, (None, None), has_depth)
    df["dist_km"] = hc.haversine_km(lat, lon, df["lat"].to_numpy(), df["lon"].to_numpy())
    df["dm"] = (df["ml"] - m).abs()
    sel = (df["dm"] <= tol + EPS) & (df["dist_km"] <= radius_km + near_margin_km + EPS)
    an = df[sel].copy()
    an["scope"] = np.where(an["dist_km"] <= radius_km + EPS, SCOPE_IN, SCOPE_NEAR)
    an = an.sort_values(["dm", "dist_km", "time"], ascending=[True, True, False]).reset_index(drop=True)
    big = df[df["ml"] >= m - EPS].drop(columns=["dm"])
    big = big.sort_values(["ml", "time"], ascending=[False, False]).head(n_largest).reset_index(drop=True)
    return WhatIfResult(m, tol, lat, lon, radius_km, near_margin_km, an, big,
                        int((an["scope"] == SCOPE_IN).sum()), int((an["scope"] == SCOPE_NEAR).sum()),
                        len(df), (df["time"].min(), df["time"].max()), has_depth)


def years_covered(res):
    """目錄涵蓋的年數（時間跨度／365.25 天）；沒有資料回 0。"""
    lo, hi = res.span
    return 0.0 if lo is None else float((hi - lo).days / 365.25)


def display_table(df, has_depth):
    """轉成給人看的表（台灣時間、繁體欄名）。深度缺值顯示「—」；沒有深度欄就不放這欄。"""
    out = pd.DataFrame({"時間（台灣）": [f"{hc.to_taipei(t):%Y-%m-%d %H:%M}" for t in df["time"]],
                        "規模 ML": df["ml"].round(2).to_numpy()})
    if has_depth:
        out["深度 km"] = ["—" if pd.isna(d) else f"{d:.0f}" for d in df["depth"]]
    out["距所選地點 km"] = df["dist_km"].round(0).astype(int).to_numpy() if len(df) else []
    if "scope" in df.columns:
        out["位置"] = df["scope"].to_numpy()
    out["震央"] = [f"{la:.2f}°N, {lo:.2f}°E" for la, lo in zip(df["lat"], df["lon"])]
    return out


def load_catalog(data_dir, full_events_path=None):
    """回傳 (events, 說明文字)。有完整目錄（parquet，含深度）就用它，否則用 APP 內的 catalog_base.csv（無深度）。

    完整目錄含定位品質差的外海事件（use_for_features=False，約 0.2%）：類比時保留，欄位 located 供標示。
    """
    if full_events_path and Path(full_events_path).exists():
        df = pd.read_parquet(full_events_path)
        keep = [c for c in ("time", "lat", "lon", "depth", "ml", "use_for_features") if c in df.columns]
        df = df[keep].rename(columns={"use_for_features": "located"})
        return df, f"完整目錄（含深度，{len(df):,} 筆，含定位品質較差的外海事件）"
    df = pd.read_csv(Path(data_dir) / "catalog_base.csv", parse_dates=["time"])
    return df, f"APP 內建目錄 catalog_base（M≥3.5、定位品質良好，{len(df):,} 筆；無深度、不含定位較差的外海事件）"
