"""把地磁測站的異常指標整理成 APP 可以「展示」的小檔 precursors.json。**不計入任何機率。**

為什麼是獨立的檔：手機端對 feed.json 有格式檢查，前兆資料不應該動到 feed 的格式；而且它的角色不同——
風險函數的機率只用地震目錄；前兆資料要「回測證明有增益」才能計入（規格 forecast_card_spec.md §10），
目前的回測是負結果（docs/reports/hazard_precursor_mag/README.md），所以這裡只展示。

輸出的每個數字都帶「相對自己歷史分布的位置」，不下任何「異常＝快要地震」的結論：
    - 測站層級指標：station_anomalies.parquet 的 mag_sznet_z、mag_szres_z、mag_pol_z
      （相對該站近期背景的穩健 z 值，磁暴日已設為缺值；定義見 pipeline/src/02_clean_features.py）
    - rank_pct：最近 7 天平均值，在該站「歷史上所有 7 天平均值」中的百分位（0–100）。只是位置，不是機率
    - 資料終點與延遲：GDMS 目前要手動下載，資料終點常落後現在 1 個多月；延遲天數一定要顯示

回測的數字不寫死：每次從報告讀（regex），讀不到就留空並在 notes 說明，避免報告重跑後這裡還放舊數字。

用法：
    python app/export_precursors.py                       # 預設讀 E:/123/processed/features/station_anomalies.parquet
    python app/export_precursors.py --anomalies <路徑> --out app/data/precursors.json
環境變數 PRECURSOR_STATION_ANOMALIES 可覆寫預設輸入路徑。
"""
import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
sys.path.insert(0, str(ROOT / "pipeline" / "src"))
import safe_console  # noqa: E402,F401

DEFAULT_ANOMALIES = os.environ.get("PRECURSOR_STATION_ANOMALIES", "E:/123/processed/features/station_anomalies.parquet")
REPORT = ROOT / "docs" / "reports" / "hazard_precursor_mag" / "README.md"
INDICES = ("mag_sznet_z", "mag_szres_z", "mag_pol_z")
INDEX_LABELS = {
    "mag_sznet_z": "垂直分量 ULF 相對其他測站",
    "mag_szres_z": "垂直分量 ULF 扣除 Kp、Dst 後的殘差",
    "mag_pol_z": "極化比（垂直÷水平）",
}
SERIES_DAYS = 30
ACTIVE_DAYS = 30            # 最後資料日距資料終點不超過這麼多天才算「仍在運作」
ROLL_DAYS = 7
MIN_ROLL = 4                # 7 天內至少 4 天有值才算一個 7 天平均
MIN_HISTORY = 60            # 歷史 7 天平均至少這麼多個，才給百分位
STALE_DAYS = 14             # 資料終點落後現在超過這麼多天，標 stale
MAX_BYTES = 50 * 1024

DISCLAIMER = ("展示用，未計入任何機率。這些是各測站相對自己近期背景的指標（已排除磁暴日）。"
              "指標偏離不代表地震即將發生，指標正常也不代表不會發生；回測沒有檢出它能讓預報變準。")


def sig(x, digits=3):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(f"{float(x):.{digits}g}")


def read_backtest(report_path=REPORT):
    """從回測報告讀主要檢定（M≥3.5、全部特徵）。讀不到時回傳 None 欄位，不猜。"""
    out = {"report": "docs/reports/hazard_precursor_mag/README.md", "primary_test": "M≥3.5、全部 6 個特徵",
           "test_gain_nat": None, "p_value": None, "n_test_events": None}
    try:
        text = Path(report_path).read_text(encoding="utf-8")
    except OSError:
        return out
    m = re.search(r"^\|\s*M≥3\.5\s*\|\s*全部 6 個特徵\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([+\-−]?\d+\.\d+)\s*\|"
                  r"[^|]*\|[^|]*\|[^|]*\|\s*(\d\.\d+)\s*\|", text, re.MULTILINE)
    if m:
        out.update(n_test_events=int(m.group(2)), test_gain_nat=float(m.group(3).replace("−", "-")),
                   p_value=float(m.group(4)))
    return out


def station_summary(g, data_end):
    """一個測站的輸出：期間、是否仍在運作、各指標最近 30 天與位置。"""
    g = g.sort_values("date")
    first, last = g["date"].min(), g["date"].max()
    active = (data_end - last) <= pd.Timedelta(days=ACTIVE_DAYS)
    out = {"code": str(g["station"].iloc[0]), "lat": sig(g["lat"].iloc[0], 5), "lon": sig(g["lon"].iloc[0], 6),
           "first_date": first.strftime("%Y-%m-%d"), "last_date": last.strftime("%Y-%m-%d"),
           "n_days": int(len(g)), "active": bool(active)}
    if not active:
        return out
    days = pd.date_range(first, data_end, freq="D")
    indices = {}
    for col in INDICES:
        s = g.set_index("date")[col].astype(float).reindex(days)
        roll = s.rolling(ROLL_DAYS, min_periods=MIN_ROLL).mean()
        current = roll.iloc[-1]
        hist = roll.iloc[:-ROLL_DAYS].dropna()
        rank = float((hist <= current).mean() * 100) if np.isfinite(current) and len(hist) >= MIN_HISTORY else None
        tail = s.iloc[-SERIES_DAYS:]
        indices[col] = {"label": INDEX_LABELS[col], "mean7": sig(current), "rank_pct": sig(rank, 3),
                        "n_hist": int(len(hist)), "n_valid_30d": int(tail.notna().sum()),
                        "series_30d": [sig(v, 3) for v in tail.to_numpy()]}
    out["indices"] = indices
    out["series_start"] = days[-SERIES_DAYS].strftime("%Y-%m-%d")
    return out


def build_precursors(anoms, generated_at, backtest):
    """station_anomalies 表 → precursors dict。anoms 欄位：date、station、lat、lon、mag_*。"""
    df = anoms.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    data_end = df["date"].max()
    gen = pd.Timestamp(generated_at).tz_localize(None) if pd.Timestamp(generated_at).tzinfo else pd.Timestamp(generated_at)
    lag = int((gen.normalize() - data_end).days)
    stations = [station_summary(g, data_end) for _, g in df.groupby("station")]
    stations.sort(key=lambda s: (not s["active"], s["code"]))
    notes = ["資料來自 GDMS 手動下載的地磁資料，資料終點通常落後現在 1 個月以上；請以 data_end 與 lag_days 為準，不是今天的狀態。",
             "rank_pct 是最近 7 天平均值在該站歷史所有 7 天平均值中的百分位，只是位置，不是機率。",
             "測站只有少數幾個，且集中在西部與北部；花蓮、台東沒有可用測站。"]
    if backtest.get("test_gain_nat") is None:
        notes.append("無法從回測報告讀到主要檢定的數字；請以報告原文為準。")
    return {"version": 1, "kind": "display_only", "in_probability": False,
            "generated_at": gen.strftime("%Y-%m-%dT%H:%M:%SZ"), "data_end": data_end.strftime("%Y-%m-%d"),
            "lag_days": lag, "stale": lag > STALE_DAYS, "disclaimer": DISCLAIMER, "backtest": backtest,
            "thresholds": {"series_days": SERIES_DAYS, "active_days": ACTIVE_DAYS, "roll_days": ROLL_DAYS,
                           "min_history": MIN_HISTORY},
            "stations": stations, "notes": notes}


def check(out):
    """輸出前的檢查：格式、不得宣稱計入機率、大小。"""
    if out.get("in_probability") is not False or out.get("kind") != "display_only":
        raise ValueError("前兆輸出必須標明 display_only 且 in_probability=false")
    for s in out["stations"]:
        for name, idx in (s.get("indices") or {}).items():
            if len(idx["series_30d"]) != SERIES_DAYS:
                raise ValueError(f"{s['code']} {name} 的序列長度不是 {SERIES_DAYS}")
            r = idx["rank_pct"]
            if r is not None and not (0.0 <= r <= 100.0):
                raise ValueError(f"{s['code']} {name} 的百分位超出範圍：{r}")
    size = len(json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > MAX_BYTES:
        raise ValueError(f"precursors.json 太大：{size} 位元組（上限 {MAX_BYTES}）")
    return size


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--anomalies", default=DEFAULT_ANOMALIES)
    ap.add_argument("--out", default=str(APP_DIR / "data" / "precursors.json"))
    ap.add_argument("--report", default=str(REPORT))
    ap.add_argument("--now", default=None, help="產生時間（UTC），預設為現在；測試用")
    args = ap.parse_args()
    if not Path(args.anomalies).exists():
        sys.exit(f"找不到測站異常指標：{args.anomalies}\n（地磁資料不進版控；用 --anomalies 或環境變數 PRECURSOR_STATION_ANOMALIES 指定）")
    now = pd.Timestamp.now("UTC").tz_localize(None) if args.now is None else pd.Timestamp(args.now)
    out = build_precursors(pd.read_parquet(args.anomalies), now, read_backtest(args.report))
    size = check(out)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8", newline="\n")
    act = [s["code"] for s in out["stations"] if s["active"]]
    print(f"precursors.json：{path}（{size / 1024:.1f} KB）；資料終點 {out['data_end']}、落後 {out['lag_days']} 天"
          f"{'（過期）' if out['stale'] else ''}；運作中 {len(act)} 站：{'、'.join(act)}")


if __name__ == "__main__":
    main()
