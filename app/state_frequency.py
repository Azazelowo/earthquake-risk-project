"""狀態門檻的回測統計：測試期每天、各縣市、APP 預設條件下，「和平常比」各狀態出現的頻率。

為什麼需要：APP 首頁用倍數（現在÷平常）決定狀態，門檻 1.5／3／10 倍是暫定值。設計書的目標是
「比平常高不少」以上不要天天出現——要用回測期間實際算出來的倍數分布來檢查。

做法（和 APP 首頁同一套計算）：
- 發布時間：測試期（2015-01-01 起）每天台灣時間 08:00（UTC 00:00）。
- 地點：APP 的 20 個縣市代表點（金門、連江在網格外），周圍 50 km；條件 3 天、M5 以上（首頁預設）。
- 期望數：hazard.expected_ref（只用發布時間之前的地震）；校準倍率每月初用 trailing_factor 算一次、當月沿用
  （只用月初之前的資料；倍率對「倍數」的影響很小，因為現在與平常乘同一個倍率）。
- 「平常」：n_bg 乘 background_rescale（和 hazard_core.load_snapshot 相同）。
- 倍數＝現在的機率 ÷ 平常的機率（和 APP assess 相同）。

輸出：docs/reports/state_frequency.md（報告）；加 --csv 另存每天每縣市的倍數（約 5 MB，不進 git）。

用法：
    python app/state_frequency.py                    # 測試期全部（約 4,300 天）
    python app/state_frequency.py --start 2024-01-01 --end 2024-12-31
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
import live_update as lu  # noqa: E402
from eqforecast import hazard as hz  # noqa: E402

BASE_DIR = APP_DIR / "data"
REPORT_DIR = APP_DIR.parent / "docs" / "reports"

# APP 的縣市代表點（earthquake-risk-app/src/state.ts 的 COUNTIES，去掉網格外的金門、連江）
COUNTIES = [
    ("基隆市", 25.13, 121.74), ("臺北市", 25.04, 121.56), ("新北市", 25.01, 121.47), ("桃園市", 24.99, 121.3),
    ("新竹市", 24.8, 120.97), ("新竹縣", 24.83, 121.01), ("苗栗縣", 24.56, 120.82), ("臺中市", 24.15, 120.68),
    ("彰化縣", 24.08, 120.54), ("南投縣", 23.91, 120.68), ("雲林縣", 23.71, 120.43), ("嘉義市", 23.48, 120.45),
    ("嘉義縣", 23.46, 120.25), ("臺南市", 22.99, 120.21), ("高雄市", 22.63, 120.3), ("屏東縣", 22.67, 120.49),
    ("宜蘭縣", 24.75, 121.75), ("花蓮縣", 23.99, 121.6), ("臺東縣", 22.76, 121.14), ("澎湖縣", 23.57, 119.58),
]
RADIUS_KM, T_DAYS, M = 50.0, 3.0, 5.0
EDGES = (1.5, 3.0, 10.0)            # APP 目前的門檻（format.ts 的 LEVEL_EDGES）
LEVELS = ("和平常差不多", "比平常高一些", "比平常高不少", "遠高於平常")


def level_of(ratio, edges=EDGES):
    """倍數 → 狀態編號 0–3（和 APP levelOf 相同的切法：< 1.5、1.5–3、3–10、≥ 10）"""
    return int(np.searchsorted(edges, ratio, side="right"))


def county_weights(params):
    """每個縣市代表點周圍 50 km 的面積比例（hazard_core.point_weights，和 APP pointWeights 相同）"""
    grid = lu.grid_of(params)
    cells = grid.frame()[["lat_bin", "lon_bin"]]
    snap = hc.Snapshot(params, cells, np.array([T_DAYS]), np.zeros((len(cells), 1)), np.zeros((len(cells), 1)),
                       pd.DataFrame(), pd.DataFrame())
    return {name: hc.point_weights(snap, lat, lon, RADIUS_KM) for name, lat, lon in COUNTIES}


def ratios(start, end, base_dir=BASE_DIR, hour_utc=0, log=print):
    """每天每縣市的（現在機率、平常機率、倍數）。回傳 DataFrame。"""
    params = lu.load_params(base_dir)
    prm, grid = lu.hazard_params(params), lu.grid_of(params)
    cat = hz.Catalog(lu.read_base_catalog(base_dir), grid, prm.m_ref)
    # 「平常」＝只有背景的模型、用它自己的滾動校準倍率，和 APP 的 n_bg（06 匯出、審查 M5）相同。
    # 2026-11-21 修正：原本是「完整模型的背景項 × 完整模型的倍率 × background_rescale」，新版快照的
    # background_rescale 回傳 1（它假設 n_bg 已經換好），這裡卻是自己重算的，「平常」因此低估約 2.6 倍。
    bgp = hz.bg_only_params(prm, params["fit_info"]["mu_bg_only"])
    trailing = float(params.get("calibration_trailing_days", 730))
    scale = 10.0 ** (-prm.b * (M - prm.m_ref))
    weights = county_weights(params)
    days = pd.date_range(pd.Timestamp(start) + pd.Timedelta(hours=hour_utc), pd.Timestamp(end), freq="D")
    rows, factor, factor_bg, month = [], None, None, None
    for i, t0 in enumerate(days):
        if (t0.year, t0.month) != month:
            month = (t0.year, t0.month)
            factor = hz.trailing_factor(cat, t0, T_DAYS, prm, trailing_days=trailing)
            factor_bg = hz.trailing_factor(cat, t0, T_DAYS, bgp, trailing_days=trailing)
            if t0.month == 1:
                log(f"{t0.date()}：校準倍率 {factor:.3f}（{i}/{len(days)} 天）")
        a, b = hz.expected_ref(cat, t0, T_DAYS, prm)
        full = (a + b).ravel() * factor
        bg = hz.expected_ref(cat, t0, T_DAYS, bgp)[0].ravel() * factor_bg
        for name, w in weights.items():
            p = float(-np.expm1(-(w * full).sum() * scale))
            pb = float(-np.expm1(-(w * bg).sum() * scale))
            rows.append((t0, name, p, pb, p / pb if pb > 0 else np.nan))
    return pd.DataFrame(rows, columns=["issue_time", "county", "p", "p_bg", "ratio"])


def summarize(df, edges=EDGES):
    """各狀態出現的比例（全部縣市合計）、每個縣市「高不少」以上的天數比例、全台任一縣市達到各狀態的天數比例"""
    lv = df["ratio"].map(lambda r: level_of(r, edges))
    overall = lv.value_counts(normalize=True).reindex(range(4), fill_value=0.0)
    per_county = (lv >= 2).groupby(df["county"]).mean().sort_values(ascending=False)
    daily_max = df.groupby("issue_time")["ratio"].max().map(lambda r: level_of(r, edges))
    any_county = daily_max.value_counts(normalize=True).reindex(range(4), fill_value=0.0)
    q = df["ratio"].quantile([0.5, 0.9, 0.99, 0.999]).to_dict()
    return {"overall": overall.tolist(), "any_county": any_county.tolist(),
            "per_county_high": per_county.to_dict(), "quantiles": q, "n_days": int(df["issue_time"].nunique())}


def write_report(df, s, path_md, start, end):
    pct = lambda x: f"{x * 100:.2f}%"  # noqa: E731
    lines = [
        "# 狀態門檻的回測統計",
        "",
        "> 由 `app/state_frequency.py` 產生。重新執行會覆寫本檔。",
        "",
        f"期間 {start} ～ {end}（測試期），每天台灣時間 08:00 發布一次，共 {s['n_days']} 天；",
        f"20 個縣市代表點、周圍 {RADIUS_KM:g} km、{T_DAYS:g} 天內、M{M:g} 以上（APP 首頁預設）。",
        "計算和 APP 首頁相同：倍數＝現在的機率 ÷ 平常的機率；只用每個發布時間之前的資料。",
        "",
        f"## 目前門檻（{' / '.join(f'{e:g}' for e in EDGES)} 倍）下各狀態出現的比例",
        "",
        "| 狀態 | 所有縣市×天 | 全台任一縣市（排行第一名） |",
        "|---|---|---|",
    ]
    for i, name in enumerate(LEVELS):
        lines.append(f"| {name} | {pct(s['overall'][i])} | {pct(s['any_county'][i])} |")
    lines += ["", "## 倍數的分布（所有縣市×天）", "",
              "| 百分位 | 倍數 |", "|---|---|"]
    lines += [f"| {k * 100:g}% | {v:.2f} |" for k, v in s["quantiles"].items()]
    lines += ["", "## 各縣市「比平常高不少」以上的天數比例", "", "| 縣市 | 比例 |", "|---|---|"]
    lines += [f"| {k} | {pct(v)} |" for k, v in s["per_county_high"].items()]
    lines += ["", "## 怎麼看", "",
              "- 倍數小於 1 是正常的：「平常」是包含過去大地震餘震期的長期平均，平靜時「現在」比它低（APP 畫面寫「約 1 倍」）。",
              "- 設計書的目標是「比平常高不少」以上不要天天出現；上表的「所有縣市×天」就是一般使用者打開 APP 時看到各狀態的機會。",
              "- 「全台任一縣市」是全台排行的第一名達到該狀態的天數比例。", ""]
    Path(path_md).write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    params = lu.load_params(BASE_DIR)
    ap.add_argument("--start", default=params["test"][0])
    ap.add_argument("--end", default=params["test"][1])
    ap.add_argument("--out", default=str(REPORT_DIR))
    ap.add_argument("--csv", action="store_true", help="另存每天每縣市的倍數（state_frequency.csv）")
    args = ap.parse_args()
    df = ratios(args.start, args.end)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.csv:
        df.to_csv(out / "state_frequency.csv", index=False, float_format="%.6g", date_format="%Y-%m-%d %H:%M")
    s = summarize(df)
    write_report(df, s, out / "state_frequency.md", args.start, args.end)
    print(json.dumps(s, ensure_ascii=False, indent=1, default=float))


if __name__ == "__main__":
    main()
