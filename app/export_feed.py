"""把 APP 快照（app/data/）或即時更新的結果（live_update.py 的輸出）轉成手機 PWA 讀的 feed.json。

格式見 earthquake-risk-app/src/data/schema.ts；手機端在 src/data/feed.ts 用 validateFeed 再檢查一次。

為什麼要這一步：
    手機只需要「每格 × 10 個時間長度的期望數（現在、平常各一份）」加上少量的說明資料，
    規模換算、範圍加總、卜瓦松都在手機上算。所以這裡只做格式轉換與檢查，不重算模型。
    寫出之前先用 check_feed 檢查一遍：每 15 分鐘自動發布，格式錯了不能直接送到使用者手機上。

用法：
    python app/export_feed.py --out feed.json                  # 自動選比較新的：app/live/ 或 app/data/
    python app/export_feed.py --data <live 資料夾> --out feed.json
"""
import argparse
import json
import math
import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR.parent / "pipeline" / "src"))
import safe_console  # noqa: E402,F401
import hazard_core as hc  # noqa: E402

RECENT_DAYS = 30          # 近期地震只放發布前 30 天
RECENT_MIN_ML = 3.0
RECENT_MAX = 400          # 最多幾筆（控制檔案大小）
SOURCE_NAMES = {"CWA": "中央氣象署有感地震報告（暫時）", "USGS": "美國地質調查所 USGS（暫時）"}


def _iso(ts):
    """快照裡的時間都是不帶時區的 UTC → ISO 8601（Z）。"""
    return pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sig(values, digits=6):
    """有效位數 6 位：數值本身是估計值，再多位數只會讓檔案變大。"""
    return [float(f"{float(v):.{digits}g}") for v in values]


def _source(label):
    s = str(label)
    if s.startswith("CWA"):
        return "CWA"
    if s.startswith("USGS"):
        return "USGS"
    return "GDMS"   # 「目錄」：正式目錄（CWA／GDMS 整併）


def build_feed(snap):
    """Snapshot → feed dict。"""
    params = snap.params
    status = params.get("data_status") or {}
    issue = snap.issue_time
    catalog_end = pd.Timestamp(params.get("catalog_end") or status.get("catalog_end"))
    gap_days = status.get("gap_days")
    if gap_days is None:
        gap_days = max((issue - catalog_end) / pd.Timedelta(days=1), 0.0)

    sources = ["中央氣象署地震目錄（GDMS 正式目錄）"]
    sources += [SOURCE_NAMES[k] for k in (status.get("n_provisional_by_source") or {}) if k in SOURCE_NAMES]
    # 缺口評估：live_update 給的是「缺口版總數 ÷ 完整版」，APP 要的是「可能低估的比例」
    undercount = {f"{float(k):g}": round(min(max(1.0 - float(v), 0.0), 1.0), 4)
                  for k, v in (status.get("gap_ratio") or {}).items()}

    ev = snap.events.copy()
    ev["time"] = pd.to_datetime(ev["time"])
    keep = (ev["time"] < issue) & (ev["time"] >= issue - pd.Timedelta(days=RECENT_DAYS)) & (ev["ml"] >= RECENT_MIN_ML)
    ev = ev[keep].dropna(subset=["lat", "lon", "ml", "depth"]).sort_values("time", ascending=False).head(RECENT_MAX)
    recent = [{"time": _iso(r.time), "lat": round(float(r.lat), 4), "lon": round(float(r.lon), 4),
               "depth_km": round(float(r.depth), 1), "ml": round(float(r.ml), 1), "source": _source(r.source)}
              for r in ev.itertuples()]

    bt = snap.backtest.dropna(subset=["auc_full", "n_ratio_full"])
    backtest = [{"m": float(r.m), "horizon_days": float(r.horizon_days), "auc": round(float(r.auc_full), 4),
                 "ratio": round(float(r.n_ratio_full), 4), "n_events": int(r.n_events)} for r in bt.itertuples()]

    g = params["grid"]
    return {
        "version": 1,
        "mock": False,
        "issued_at": _iso(issue),
        "grid": {k: float(g[k]) for k in ("lat_min", "lat_max", "lon_min", "lon_max", "deg")},
        "params": {"b": round(snap.b, 4), "m_ref": snap.m_ref},
        "horizons_days": [float(h) for h in snap.horizons],
        "cells": {"lat": [round(float(v), 4) for v in snap.cells["lat_bin"]],
                  "lon": [round(float(v), 4) for v in snap.cells["lon_bin"]]},
        "n_full": [_sig(row) for row in snap.n_ref],
        "n_bg": [_sig(row) for row in snap.n_bg],
        "data_status": {"catalog_end": _iso(catalog_end), "gap_days": round(float(gap_days), 2),
                        "n_provisional": int(status.get("n_provisional", 0)), "sources": sources,
                        "undercount_by_horizon": undercount},
        "recent": recent,
        "backtest": backtest,
    }


def check_feed(feed):
    """和手機端 validateFeed 相同的重點檢查。有問題就丟出 ValueError（不要發布壞掉的資料）。"""
    def fin(x, where, lo=-math.inf):
        if not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) or x < lo:
            raise ValueError(f"{where} 不是 ≥ {lo} 的有限數字：{x!r}")

    if feed.get("version") != 1 or feed.get("mock") is not False:
        raise ValueError("version 或 mock 不對")
    h = feed["horizons_days"]
    if len(h) < 2 or any(b <= a for a, b in zip(h, h[1:])):
        raise ValueError("horizons_days 必須遞增")
    n = len(feed["cells"]["lat"])
    if n == 0 or len(feed["cells"]["lon"]) != n:
        raise ValueError("cells 長度不一致")
    for name in ("n_full", "n_bg"):
        rows = feed[name]
        if len(rows) != n:
            raise ValueError(f"{name} 列數 {len(rows)} ≠ {n}")
        for i, row in enumerate(rows):
            if len(row) != len(h):
                raise ValueError(f"{name}[{i}] 長度不對")
            for j, v in enumerate(row):
                fin(v, f"{name}[{i}][{j}]", 0.0)
    for i, e in enumerate(feed["recent"]):
        if e["source"] not in ("GDMS", "CWA", "USGS"):
            raise ValueError(f"recent[{i}].source 不允許：{e['source']!r}")
        for k in ("lat", "lon", "depth_km", "ml"):
            fin(e[k], f"recent[{i}].{k}")
    for i, r in enumerate(feed["backtest"]):
        fin(r["auc"], f"backtest[{i}].auc", 0.0)
        if r["auc"] > 1:
            raise ValueError(f"backtest[{i}].auc > 1")
    for k, v in feed["data_status"]["undercount_by_horizon"].items():
        fin(float(k), f"undercount 的鍵 {k}", 0.0)
        fin(v, f"undercount[{k}]", 0.0)
    return feed


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default=None, help="快照資料夾；預設自動選 app/live/（較新時）或 app/data/")
    ap.add_argument("--out", default=str(APP_DIR / "feed" / "feed.json"))
    args = ap.parse_args()
    data_dir = Path(args.data) if args.data else hc.pick_data_dir(APP_DIR / "data", APP_DIR / "live")
    feed = check_feed(build_feed(hc.load_snapshot(data_dir)))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(feed, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"feed.json：{out}（{out.stat().st_size / 1024:.0f} KB）；發布時間 {feed['issued_at']}、"
          f"{len(feed['cells']['lat'])} 格、近期地震 {len(feed['recent'])} 筆、資料來源 {data_dir}")


if __name__ == "__main__":
    main()
