"""產生「TS／Python 同一組輸入結果一致」的對照資料（手機 APP 的 vitest 會讀它）。

做法：拿一份 feed.json（手機實際讀的格式）當唯一輸入，用 hazard_core 算一批條件
（不同地點、半徑、規模、時間長度、現在／平常），把輸入與答案一起寫成 JSON。
APP 端 src/core/parity.test.ts 用同一份 feed 跑 src/core/hazard.ts，要求相對誤差 < 1e-9。

為什麼從 feed.json 建 Snapshot、而不是讀 app/data：手機拿到的就是 feed.json（數值已取 6 位有效數字、
n_bg 已做 background_rescale），兩邊吃一模一樣的數字，比對才有意義。

用法：
    python app/export_feed.py --data app/data --out feed.json
    python app/export_parity.py --feed feed.json --out ../earthquake-risk-app/src/core/__fixtures__/parity.json
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR.parent / "pipeline" / "src"))
import safe_console  # noqa: E402,F401
import hazard_core as hc  # noqa: E402

POINTS = [  # (名稱, 緯度, 經度)：本島西南／東部／北部、網格邊緣（覆蓋率不足）、網格外
    ("高雄", 22.627, 120.301), ("花蓮", 23.992, 121.601), ("臺北", 25.033, 121.565),
    ("鵝鑾鼻外海", 21.20, 120.85), ("金門", 24.43, 118.32),
]
RADII = [10.0, 50.0, 100.0]
MAGS = [3.5, 5.0, 6.2]
HORIZONS = [1 / 24, 0.37, 3.0, 30.0]   # 0.37 天不在匯出刻度上，會走內插
P_TARGETS = [0.01, 0.1]


def snapshot_from_feed(feed):
    """feed.json → hazard_core.Snapshot（n_bg 已在匯出時換算過，不再乘 background_rescale）。"""
    params = {"grid": feed["grid"], "params": feed["params"], "issue_time": feed["issued_at"],
              # 機率換算的群聚係數在 feed.params.kappa；舊 feed 沒有＝1（和 TS validateFeed 相同）
              "probability": {"kappa": float(feed["params"].get("kappa", 1.0))}}
    cells = pd.DataFrame({"lat_bin": feed["cells"]["lat"], "lon_bin": feed["cells"]["lon"]})
    return hc.Snapshot(params, cells, np.asarray(feed["horizons_days"], float),
                       np.asarray(feed["n_full"], float), np.asarray(feed["n_bg"], float),
                       pd.DataFrame(), pd.DataFrame())


def _num(x):
    """JSON 沒有 NaN：範圍內沒有格子時反查規模沒有定義，寫成 null（TS 端對應 NaN）。"""
    x = float(x)
    return None if np.isnan(x) else x


def region_case(snap, w):
    out = {"area_km2": hc.region_area_km2(snap, w), "rows": []}
    for T in HORIZONS:
        for m in MAGS:
            out["rows"].append({"m": m, "T": T,
                                "p_full": hc.region_probability(snap, w, m, T, "full"),
                                "p_bg": hc.region_probability(snap, w, m, T, "bg")})
        for p in P_TARGETS:
            out["rows"].append({"T": T, "p_target": p,
                                "m_full": _num(hc.magnitude_at_probability(snap, w, T, p, "full")),
                                "m_bg": _num(hc.magnitude_at_probability(snap, w, T, p, "bg"))})
    return out


def build(feed):
    snap = snapshot_from_feed(feed)
    cases = []
    for name, lat, lon in POINTS:
        for r in RADII:
            w = hc.point_weights(snap, lat, lon, r)
            nz = np.flatnonzero(w)
            c = {"name": name, "lat": lat, "lon": lon, "radius_km": r,
                 "weights": [[int(i), float(w[i])] for i in nz]}
            c.update(region_case(snap, w))
            cases.append(c)
    whole = {"name": "全網格"}
    whole.update(region_case(snap, hc.all_weights(snap)))
    cells = {"T": 0.37, "m": 5.0,
             "p_full": hc.probability(hc.expected(snap, 5.0, 0.37, "full"), snap.kappa).tolist(),
             "p_bg": hc.probability(hc.expected(snap, 5.0, 0.37, "bg"), snap.kappa).tolist()}
    return {"generated_by": "破專題 app/export_parity.py", "feed": feed,
            "point_cases": cases, "whole_grid": whole, "cells": cells}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--feed", required=True, help="export_feed.py 產生的 feed.json")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    feed = json.loads(Path(a.feed).read_text(encoding="utf-8"))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build(feed), ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    print(f"對照資料：{out}（{out.stat().st_size // 1024} KB）")


if __name__ == "__main__":
    main()
