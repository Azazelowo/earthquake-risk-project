"""大地震剛發生後，風險函數反應得夠不夠快：比較兩組參數（例如 v1.1 與 v1.2）。

為什麼要另外檢查：06 的回測每 7 天發布一次，主震後幾分鐘到幾小時這段「APP 最常被打開的時候」
幾乎抽不到。這裡改成在每個測試期 M≥6 主震之後的固定延遲發布，直接看：
- 震央 50 km 內，之後 1 天、7 天的 M≥3.5 地震數：預測 vs 實際（預測÷實際 < 1 代表低估）
- 震央 50 km 內，3 天、M≥5 的機率相對「只有背景」的倍數（APP 首頁的數字）

預測和即時更新（app/live_update.py）一樣：expected_ref × 滾動校準倍率（trailing_factor，只用過去資料）。
主震本身已在目錄裡（發布時間在主震之後）。

輸出：docs/reports/hazard_mainshock_response.md（以及 --out 的 CSV）
執行：python pipeline/src/08_mainshock_response.py --events <events.parquet> \\
        --params A=<v1.1 params.json> --params B=<v1.2 params.json>
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import safe_console  # noqa: E402,F401
from eqforecast import hazard as hz  # noqa: E402

BASE_DIR = Path(__file__).resolve().parents[2]
REPORT = BASE_DIR / "docs" / "reports" / "hazard_mainshock_response.md"

DELAYS = [("2 分鐘", 2 / 1440), ("1 小時", 1 / 24), ("1 天", 1.0)]
COUNT_HORIZONS = [1.0, 7.0]
RADIUS_KM = 50.0
MAIN_M = 6.0


def disc_mask(grid, lat, lon, radius_km):
    """格中心在震央 radius_km 內的格子（至少包含震央所在格）。"""
    f = grid.frame()
    clat = f["lat_bin"].to_numpy() + grid.deg / 2
    clon = f["lon_bin"].to_numpy() + grid.deg / 2
    dy = (clat - lat) * hz.KM_PER_DEG
    dx = (clon - lon) * hz.KM_PER_DEG * np.cos(np.radians(lat))
    m = (np.hypot(dx, dy) <= radius_km).reshape(grid.shape)
    i, j, ok = grid.cell_index([lat], [lon])
    if ok[0]:
        m[i[0], j[0]] = True
    return m


def calibrated(cat, t0, T, prm):
    a, b = hz.expected_ref(cat, t0, T, prm)
    return (a + b) * hz.trailing_factor(cat, t0, T, prm)


def evaluate(events, grid, name, meta, mains, test_start):
    prm = hz.HazardParams(**meta["params"])
    bgp = hz.bg_only_params(prm, meta["fit_info"]["mu_bg_only"])
    cat = hz.Catalog(events, grid, prm.m_ref)
    rows = []
    for ev in mains.itertuples():
        mask = disc_mask(grid, ev.lat, ev.lon, RADIUS_KM)
        for dlabel, delay in DELAYS:
            t0 = np.datetime64(ev.time, "us") + np.timedelta64(int(round(delay * 86400)), "s")
            row = {"model": name, "time": ev.time, "ml": ev.ml, "lat": ev.lat, "lon": ev.lon, "delay": dlabel}
            for T in COUNT_HORIZONS:
                h = np.timedelta64(int(round(T * 86400)), "s")
                pred = float(calibrated(cat, t0, T, prm)[mask].sum())
                obs = float(cat.counts(t0, t0 + h)[mask].sum())
                row[f"pred_{T:g}d"], row[f"obs_{T:g}d"] = pred, obs
            n_full = hz.scale_magnitude(calibrated(cat, t0, 3.0, prm)[mask].sum(), 5.0, prm.m_ref, prm.b)
            a_bg, _ = hz.expected_ref(cat, t0, 3.0, bgp)
            n_bg = hz.scale_magnitude(a_bg[mask].sum() * hz.trailing_factor(cat, t0, 3.0, bgp), 5.0, prm.m_ref, prm.b)
            row["p_m5_3d"] = float(hz.probability(n_full))
            row["ratio_m5_3d"] = float(hz.probability(n_full) / hz.probability(n_bg))
            rows.append(row)
    return rows


FIT_ALPHAS = (0.0, 0.4, 0.6, 0.8, 1.0, 1.2)
FIT_YOUNG = (0.1, 0.25, 0.5, 1.0, 3.0)
FIT_MAIN_M = 5.5
DEEP_KM = 40.0


def fit_early(events, grid, meta, train_start, train_end):
    """只用訓練期的淺層主震，選 alpha 與 young_days（其他參數沿用 meta，也就是 v1.1）。

    目標：主震後 2 分鐘、1 小時、1 天發布時，震央 50 km 內之後 1 天與 7 天 M≥m_ref 地震數的卜瓦松對數概似
    （含滾動校準，和即時運作相同）。young_days 之後模型與 v1.1 完全相同，所以 7 天窗的擬合結果不受影響。
    """
    base = meta["params"]
    cat = hz.Catalog(events, grid, base["m_ref"])
    i, j, ok = grid.cell_index(events["lat"], events["lon"])
    deep = events["depth"] > DEEP_KM if "depth" in events.columns else False
    mains = events[ok & ~deep & (events["ml"] >= FIT_MAIN_M) & (events["time"] >= train_start)
                   & (events["time"] < pd.Timestamp(train_end) - pd.Timedelta(days=7))]
    cases = []
    for ev in mains.itertuples():
        mask = disc_mask(grid, ev.lat, ev.lon, RADIUS_KM)
        for _, delay in DELAYS:
            t0 = np.datetime64(ev.time, "us") + np.timedelta64(int(round(delay * 86400)), "s")
            obs = {T: float(cat.counts(t0, t0 + np.timedelta64(int(T * 86400), "s"))[mask].sum()) for T in COUNT_HORIZONS}
            cases.append((t0, mask, obs))
    rows = []
    for alpha in FIT_ALPHAS:
        for yd in FIT_YOUNG:
            if alpha == 0 and yd != FIT_YOUNG[0]:
                continue   # alpha = 0 時 young_days 沒有作用
            prm = hz.HazardParams(**{**base, "alpha": alpha, "young_days": yd, "deep_km": DEEP_KM})
            ll, pred_tot, obs_tot = 0.0, 0.0, 0.0
            for t0, mask, obs in cases:
                for T in COUNT_HORIZONS:
                    n = float(calibrated(cat, t0, T, prm)[mask].sum())
                    ll += hz.poisson_ll(np.array([obs[T]]), np.array([n]))
                    pred_tot += n
                    obs_tot += obs[T]
            rows.append({"alpha": alpha, "young_days": yd, "ll": ll, "pred_over_obs": pred_tot / obs_tot})
            print(f"alpha={alpha:g} young={yd:g}：ll={ll:.1f} 預測÷實際={pred_tot / obs_tot:.2f}")
    return pd.DataFrame(rows).sort_values("ll", ascending=False).reset_index(drop=True), len(mains)


def write_report(df, names, metas, n_main, test_start):
    L = ["# 大地震剛發生後，模型反應得夠不夠快", "",
         "> 由 `pipeline/src/08_mainshock_response.py` 產生。重新執行會覆寫本檔。", "",
         f"測試期（{test_start} 起）每個 M≥{MAIN_M:g} 地震（{n_main} 個，定位良好的母體），在主震後固定延遲發布一次，"
         f"看震央 {RADIUS_KM:g} km 內的預測。預測含滾動校準，和 APP 的即時更新相同。", "",
         "- **預測÷實際**：之後 1 天／7 天的 M≥3.5 地震數，所有主震加總後相除。< 1 = 低估。",
         "- **倍數**：3 天、M≥5 的機率 ÷ 只有背景的機率（APP 首頁的數字），取所有主震的中位數。", "",
         "## 參數", ""]
    for n in names:
        p = metas[n]["params"]
        L.append(f"- **{n}**：alpha = {p['alpha']:g}、d_frac = {p.get('d_frac', 0.0):g}、p = {p['p']:g}、"
                 f"sigma_st = {p['sigma_st']:g}、K = {p['K']:.4g}")
    L += ["", "## 彙總", "",
          "| 延遲 | 模型 | 預測÷實際（1 天） | 預測÷實際（7 天） | 倍數中位數 | 機率中位數 |", "|---|---|---|---|---|---|"]
    for dlabel, _ in DELAYS:
        for n in names:
            d = df[(df.delay == dlabel) & (df.model == n)]
            r1 = d["pred_1d"].sum() / d["obs_1d"].sum() if d["obs_1d"].sum() else np.nan
            r7 = d["pred_7d"].sum() / d["obs_7d"].sum() if d["obs_7d"].sum() else np.nan
            L.append(f"| {dlabel} | {n} | {r1:.2f} | {r7:.2f} | {d['ratio_m5_3d'].median():.1f} 倍 | "
                     f"{d['p_m5_3d'].median() * 100:.1f}% |")
    L += ["", "## 每個主震（主震後 2 分鐘發布）", "",
          "| 時間（UTC） | ML | " + " | ".join(f"{n} 預測／實際（1 天）| {n} 倍數" for n in names) + " |",
          "|---|---|" + "---|---|" * len(names)]
    first = df[df.delay == DELAYS[0][0]]
    for t, g in first.groupby("time", sort=True):
        cells = []
        for n in names:
            r = g[g.model == n].iloc[0]
            cells.append(f"{r.pred_1d:.0f}／{r.obs_1d:.0f} | {r.ratio_m5_3d:.1f}")
        L.append(f"| {pd.Timestamp(t):%Y-%m-%d %H:%M} | {g.ml.iloc[0]:.1f} | " + " | ".join(cells) + " |")
    L += ["", "## 解讀時要注意", "",
          "- 主震數量少，彙總數字受最大的幾個序列主導（2018、2022、2024 年）。",
          "- 實際數量來自目前的完整目錄；即時運作時主震後的目錄還沒更新，APP 實際看到的會更低，"
          "因為即時資料只補得到有感報告與 USGS（見資料缺口評估）。",
          "- 這只檢查主震之後；主震之前模型沒有能力指出哪一次地震會成為大地震。", ""]
    REPORT.write_text("\n".join(L), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", required=True)
    ap.add_argument("--params", action="append", required=True, help="名稱=params.json，可給多次")
    ap.add_argument("--test-start", default="2015-01-01")
    ap.add_argument("--out", default=None, help="明細 CSV")
    ap.add_argument("--fit-early", default=None, metavar="OUT_JSON",
                    help="改成擬合模式：用第一組 --params 在訓練期主震上選 alpha、young_days，輸出新的 params.json")
    ap.add_argument("--train-start", default="1995-01-01")
    ap.add_argument("--train-end", default="2015-01-01")
    args = ap.parse_args()

    events, _ = hz.load_events(args.events)
    grid = hz.Grid()
    metas = {}
    for item in args.params:
        name, path = item.split("=", 1)
        metas[name] = json.loads(Path(path).read_text(encoding="utf-8"))
    if args.fit_early:
        name, meta = next(iter(metas.items()))
        res, n = fit_early(events, grid, meta, args.train_start, args.train_end)
        best = res.iloc[0]
        print(f"訓練期淺層 M≥{FIT_MAIN_M:g} 主震 {n} 個；最佳 alpha={best.alpha:g}、young_days={best.young_days:g}")
        new = json.loads(json.dumps(meta))
        new["params"].update(alpha=float(best.alpha), young_days=float(best.young_days), deep_km=DEEP_KM)
        new["early_fit"] = {"train": [args.train_start, args.train_end], "n_mainshocks": n, "main_m": FIT_MAIN_M,
                            "radius_km": RADIUS_KM, "grid": res.to_dict("records")}
        Path(args.fit_early).write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
        return
    last = pd.Timestamp(events["time"].max()) - pd.Timedelta(days=7)
    i, j, ok = grid.cell_index(events["lat"], events["lon"])
    mains = events[ok & (events["ml"] >= MAIN_M) & (events["time"] >= args.test_start) & (events["time"] <= last)]
    rows = []
    for name, meta in metas.items():
        rows += evaluate(events, grid, name, meta, mains, args.test_start)
        print(f"{name}：完成 {len(mains)} 個主震")
    df = pd.DataFrame(rows)
    if args.out:
        df.to_csv(args.out, index=False)
    write_report(df, list(metas), metas, len(mains), args.test_start)
    print(f"報告：{REPORT}")


if __name__ == "__main__":
    main()
