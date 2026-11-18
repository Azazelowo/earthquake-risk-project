"""歷史回放：重算過去某個時間點「當時」的風險資料，輸出成和 feed.json 同格式的檔案（APP Phase 5 的歷史頁）。

為什麼可以拿來展示（不偷看未來）：
- 參數用 v1.1，訓練期 1995–2014；回放的時間點都在 2015 年之後的測試期，參數沒有看過那段資料。
- 每格期望數只用 t0 之前的地震（hazard.expected_ref）；校準倍率用 t0 之前 730 天內「已經結束」的預報窗
  （hazard.trailing_factor），和正式匯出（06_hazard_model.export_latest）同一個做法。
- 計算用的目錄是 app/data/catalog_base.csv（M≥m_ref、定位良好，和擬合時同一個母體）。

和「當時真的跑」的差別（APP 畫面要寫清楚）：
- 用的是事後整理好的正式目錄。當時即時能拿到的資料（有感報告、USGS）比較不完整，小地震會少，
  所以當時真的跑的話，近期加成會比這裡低一些。
- 列表上的地震（recent）來自完整目錄（含定位較差的事件），只用來顯示，不影響數字。

輸出：
    <out>/<快照 id>.json   每個時間點一份 feed（check_feed 檢查過）
    <out>/index.json       清單：地震、時間點、檔名（APP 的歷史頁讀這個）

用法：
    python app/export_history.py --events <pipeline/data/events.parquet> --out <earthquake-risk-app/public/history>
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR.parent / "pipeline" / "src"))
import safe_console  # noqa: E402,F401
import hazard_core as hc  # noqa: E402
import live_update as lu  # noqa: E402
from eqforecast import hazard as hz  # noqa: E402
from export_feed import build_feed, check_feed  # noqa: E402

BASE_DIR = APP_DIR / "data"

# 回放的地震：名稱與日期（台灣時間）。主震的時間、規模、位置從目錄找：那一天前後一天內規模最大的。
QUAKES = [
    {"id": "2016-meinong", "name": "2016 美濃地震", "date": "2016-02-06"},
    {"id": "2018-hualien", "name": "2018 花蓮地震", "date": "2018-02-06"},
    {"id": "2022-chishang", "name": "2022 池上地震", "date": "2022-09-18"},
    {"id": "2024-hualien", "name": "2024 花蓮地震", "date": "2024-04-03"},
]

# 每個地震的時間點（相對主震）。主震前 1 小時：模型只會因為已經發生的地震而升高——2018 花蓮、2022 池上
# 之前都有前震（池上是前一天的關山 ML 6.6），2016 美濃之前很平靜；它不知道哪一次會變成更大的主震（不是預報）。
OFFSETS = [
    ("before1h", pd.Timedelta(hours=-1), "主震前 1 小時"),
    ("after1h", pd.Timedelta(hours=1), "主震後 1 小時"),
    ("after1d", pd.Timedelta(days=1), "主震後 1 天"),
    ("after7d", pd.Timedelta(days=7), "主震後 7 天"),
]


def find_mainshock(base, date_tw, window_days=1):
    """台灣時間的日期 → 目錄裡那一天前後 window_days 天內規模最大的地震（時間為不帶時區的 UTC）。"""
    day = pd.Timestamp(date_tw) - pd.Timedelta(hours=8)
    w = base[(base["time"] >= day - pd.Timedelta(days=window_days)) & (base["time"] < day + pd.Timedelta(days=1 + window_days))]
    if w.empty:
        raise ValueError(f"{date_tw} 前後找不到地震")
    return w.loc[w["ml"].idxmax()]


def hazard_at(base_events, params, t0, trailing_days=None):
    """t0 當時的每格期望數與校準倍率；只用 t0 之前的資料。"""
    prm, grid = lu.hazard_params(params), lu.grid_of(params)
    trailing = float(trailing_days if trailing_days is not None else params.get("calibration_trailing_days", 730))
    cat = hz.Catalog(base_events, grid, prm.m_ref)
    out = grid.frame()
    factors = {}
    for T in params["export_horizons_days"]:
        a, b = hz.expected_ref(cat, t0, T, prm)
        f = hz.trailing_factor(cat, t0, T, prm, trailing_days=trailing)
        factors[f"{T:.5f}"] = f
        out[f"n_ref_{T:.5f}d"] = (a + b).ravel() * f
        out[f"n_bg_{T:.5f}d"] = a.ravel() * f
    return out, factors


def feed_at(base_events, shown_events, params, t0, backtest_csv, trailing_days=None):
    """t0 的 feed dict：寫成和 app/data 相同的快照資料夾，再走正式的 load_snapshot → build_feed → check_feed。"""
    latest, factors = hazard_at(base_events, params, t0, trailing_days)
    t0 = pd.Timestamp(t0)
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        snap_params = dict(params, issue_time=str(t0), catalog_end=str(t0), calibration_factors=factors,
                           data_status={"mode": "history", "catalog_end": str(t0), "gap_days": 0.0, "n_provisional": 0})
        (d / "params.json").write_text(json.dumps(snap_params, ensure_ascii=False), encoding="utf-8")
        latest.to_csv(d / "hazard_latest.csv", index=False, float_format="%.6g")
        ev = shown_events[(shown_events["time"] < t0) & (shown_events["time"] >= t0 - pd.Timedelta(days=31))]
        ev.assign(source="目錄")[["time", "lat", "lon", "depth", "ml", "source"]].to_csv(
            d / "recent_events.csv", index=False, float_format="%.4f", date_format="%Y-%m-%d %H:%M:%S")
        (d / "backtest.csv").write_bytes(Path(backtest_csv).read_bytes())
        return check_feed(build_feed(hc.load_snapshot(d)))


def _iso(ts):
    return pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def export(events_path, out_dir, quakes=QUAKES, offsets=OFFSETS, base_dir=BASE_DIR, log=print, trailing_days=None):
    """產生全部回放檔與 index.json，回傳 index dict。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    params = lu.load_params(base_dir)
    base = lu.read_base_catalog(base_dir)
    shown = pd.read_parquet(events_path) if str(events_path).endswith(".parquet") else pd.read_csv(events_path, parse_dates=["time"])
    shown = shown[["time", "lat", "lon", "depth", "ml"]].dropna()
    shown["time"] = pd.to_datetime(shown["time"])
    # 列表只放模型網格內的地震：網格外的（例如 2016-02-02 東經 123.5° 的深源 M6.7）模型本來就不算，
    # 放進來會讓 APP 的餘震卡挑到它，而不是回放的那個主震
    g = params["grid"]
    shown = shown[shown["lat"].between(g["lat_min"], g["lat_max"]) & shown["lon"].between(g["lon_min"], g["lon_max"])]
    index = {"version": 1, "model_version": params.get("model_version"), "train": params.get("train"),
             "note": "用事後整理好的正式目錄與 v1.1 參數重算；只用每個時間點之前的資料。", "quakes": []}
    for q in quakes:
        ms = find_mainshock(base, q["date"])
        full = shown[(shown["time"] - ms["time"]).abs() < pd.Timedelta(seconds=5)]
        depth = float(full["depth"].iloc[0]) if len(full) else None
        entry = {"id": q["id"], "name": q["name"], "time": _iso(ms["time"]), "ml": round(float(ms["ml"]), 1),
                 "lat": round(float(ms["lat"]), 3), "lon": round(float(ms["lon"]), 3), "snapshots": []}
        if depth is not None:
            entry["depth_km"] = round(depth, 1)
        for key, delta, label in offsets:
            t0 = (ms["time"] + delta).floor("min")
            sid = f"{q['id']}-{key}"
            feed = feed_at(base, shown, params, t0, base_dir / "backtest.csv", trailing_days)
            (out_dir / f"{sid}.json").write_text(json.dumps(feed, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            entry["snapshots"].append({"id": sid, "label": label, "issued_at": feed["issued_at"]})
            log(f"{sid}：{feed['issued_at']}，近期地震 {len(feed['recent'])} 筆")
        index["quakes"].append(entry)
    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    return index


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--events", required=True, help="完整目錄（pipeline/data/events.parquet），列表的地震與深度從這裡來")
    ap.add_argument("--out", required=True, help="輸出資料夾（APP 的 public/history）")
    args = ap.parse_args()
    idx = export(args.events, args.out)
    print(f"完成：{sum(len(q['snapshots']) for q in idx['quakes'])} 個時間點 → {args.out}")


if __name__ == "__main__":
    main()
