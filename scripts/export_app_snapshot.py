"""把連續式風險函數的最新輸出整理成 APP 用的小快照（app/data/）。

為什麼要另存一份：`pipeline/data/` 不進 git（體積大、可重產），但 APP 必須在任何電腦
（包括成果展現場、雲端部署）都能打開。快照只含 APP 需要的東西，總共約 0.2 MB，
全部用 CSV／JSON，方便檢查與版本控制。

輸入（06_hazard_model.py 的輸出）：
    <hazard-dir>/hazard_latest.parquet   每格、每個時間長度的期望地震數（M≥m_ref，已校準）
    <hazard-dir>/params.json             參數、發布時間、校準倍率
    <hazard-dir>/backtest.csv            回測結果（APP 的「可信度」頁）
    <events>                             地震目錄：發布時間前 365 天、M≥3 的地震畫在地圖上；
                                         另存 M≥m_ref 的完整目錄（catalog_base.csv）給即時更新用

用法：
    python scripts/export_app_snapshot.py --hazard-dir pipeline/data/hazard \
        --events pipeline/data/events.parquet --out app/data
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline" / "src"))
import safe_console  # noqa: E402,F401
from eqforecast import hazard as hz  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hazard-dir", default="pipeline/data/hazard")
    ap.add_argument("--events", default="pipeline/data/events.parquet")
    ap.add_argument("--out", default="app/data")
    ap.add_argument("--event-days", type=float, default=365.0)
    ap.add_argument("--event-min-ml", type=float, default=3.0)
    ap.add_argument("--kappa", default="pipeline/data/probability_calibration/kappa.json",
                    help="機率換算的群聚係數（app/probability_calibration.py 的輸出）；檔案不存在就用卜瓦松")
    args = ap.parse_args()

    src, out = Path(args.hazard_dir), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    params = json.loads((src / "params.json").read_text(encoding="utf-8"))
    issue = pd.Timestamp(params["issue_time"])

    latest = pd.read_parquet(src / "hazard_latest.parquet")
    latest.to_csv(out / "hazard_latest.csv", index=False, float_format="%.6g")
    shutil.copyfile(src / "backtest.csv", out / "backtest.csv")

    ev = pd.read_parquet(args.events, columns=["time", "lat", "lon", "depth", "ml"])
    ev["time"] = pd.to_datetime(ev["time"])
    g = params["grid"]
    m_ref = float(params["params"]["m_ref"])
    # 即時更新（app/live_update.py）要在任意時間重算發生率，需要完整的 M≥m_ref 目錄（背景項用整段歷史）。
    # 必須和擬合參數時的事件母體相同（審查 M1），否則即時版的發生率和回測過的模型不是同一個東西。
    quality = params.get("event_quality", {}).get("quality", "all")
    model_ev, _ = hz.load_events(args.events, quality)
    in_grid = model_ev["lat"].between(g["lat_min"], g["lat_max"]) & model_ev["lon"].between(g["lon_min"], g["lon_max"])
    base = model_ev[in_grid & (model_ev["ml"] >= m_ref) & (model_ev["time"] < issue)].sort_values("time")
    # depth：v1.2 起深源地震（> deep_km）不套用規模加權，即時更新需要深度
    base[["time", "lat", "lon", "ml", "depth"]].to_csv(out / "catalog_base.csv", index=False, float_format="%.4f",
                                              date_format="%Y-%m-%d %H:%M:%S")
    catalog_end = ev.loc[ev["time"] < issue, "time"].max()
    keep = ((ev["time"] < issue) & (ev["time"] >= issue - pd.Timedelta(days=args.event_days))
            & (ev["ml"] >= args.event_min_ml)
            & ev["lat"].between(g["lat_min"], g["lat_max"]) & ev["lon"].between(g["lon_min"], g["lon_max"]))
    ev = ev[keep].sort_values("time").assign(source="目錄")
    ev.to_csv(out / "recent_events.csv", index=False, float_format="%.4f", date_format="%Y-%m-%d %H:%M:%S")

    # 機率換算的群聚係數（app/probability_calibration.py 用訓練期估）；沒有就維持卜瓦松（kappa = 1）
    kfile = Path(args.kappa)
    if kfile.exists():
        k = json.loads(kfile.read_text(encoding="utf-8"))
        params["probability"] = {"kappa": round(float(k["kappa"]), 4), "method": "compound_poisson",
                                 "train": k["train"], "source": "app/probability_calibration.py"}
    params["snapshot"] = {"exported_from": str(src).replace("\\", "/"), "recent_events_days": args.event_days,
                          "recent_events_min_ml": args.event_min_ml, "n_recent_events": int(len(ev)),
                          "n_catalog_base": int(len(base)), "event_quality": quality}
    params["catalog_end"] = str(catalog_end)
    params["data_status"] = {"mode": "base", "catalog_end": str(catalog_end)}
    (out / "params.json").write_text(json.dumps(params, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"快照已寫到 {out}：{len(latest)} 格、{len(ev)} 個近期地震，發布時間 {issue}")


if __name__ == "__main__":
    main()
