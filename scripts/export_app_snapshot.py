"""把連續式風險函數的最新輸出整理成 APP 用的小快照（app/data/）。

為什麼要另存一份：`pipeline/data/` 不進 git（體積大、可重產），但 APP 必須在任何電腦
（包括成果展現場、雲端部署）都能打開。快照只含 APP 需要的東西，總共約 0.2 MB，
全部用 CSV／JSON，方便檢查與版本控制。

輸入（06_hazard_model.py 的輸出）：
    <hazard-dir>/hazard_latest.parquet   每格、每個時間長度的期望地震數（M≥m_ref，已校準）
    <hazard-dir>/params.json             參數、發布時間、校準倍率
    <hazard-dir>/backtest.csv            回測結果（APP 的「可信度」頁）
    <events>                             地震目錄，只取發布時間前 365 天、M≥3 的地震畫在地圖上

用法：
    python scripts/export_app_snapshot.py --hazard-dir pipeline/data/hazard \
        --events pipeline/data/events.parquet --out app/data
"""
import argparse
import json
import shutil
from pathlib import Path

import pandas as pd


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hazard-dir", default="pipeline/data/hazard")
    ap.add_argument("--events", default="pipeline/data/events.parquet")
    ap.add_argument("--out", default="app/data")
    ap.add_argument("--event-days", type=float, default=365.0)
    ap.add_argument("--event-min-ml", type=float, default=3.0)
    args = ap.parse_args()

    src, out = Path(args.hazard_dir), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    params = json.loads((src / "params.json").read_text(encoding="utf-8"))
    issue = pd.Timestamp(params["issue_time"])

    latest = pd.read_parquet(src / "hazard_latest.parquet")
    latest.to_csv(out / "hazard_latest.csv", index=False, float_format="%.6g")
    shutil.copyfile(src / "backtest.csv", out / "backtest.csv")

    ev = pd.read_parquet(args.events, columns=["time", "lat", "lon", "depth", "ml"])
    g = params["grid"]
    keep = ((ev["time"] < issue) & (ev["time"] >= issue - pd.Timedelta(days=args.event_days))
            & (ev["ml"] >= args.event_min_ml)
            & ev["lat"].between(g["lat_min"], g["lat_max"]) & ev["lon"].between(g["lon_min"], g["lon_max"]))
    ev = ev[keep].sort_values("time")
    ev.to_csv(out / "recent_events.csv", index=False, float_format="%.4f", date_format="%Y-%m-%d %H:%M:%S")

    params["snapshot"] = {"exported_from": str(src).replace("\\", "/"), "recent_events_days": args.event_days,
                          "recent_events_min_ml": args.event_min_ml, "n_recent_events": int(len(ev))}
    (out / "params.json").write_text(json.dumps(params, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"快照已寫到 {out}：{len(latest)} 格、{len(ev)} 個近期地震，發布時間 {issue}")


if __name__ == "__main__":
    main()
