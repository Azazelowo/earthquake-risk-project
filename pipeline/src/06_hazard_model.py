"""連續式風險函數（eqforecast/hazard.py）：估參數、回測、輸出最新一期的風險表。

流程：
1. 用訓練期（預設 1995–2014）估 b 值、mu、K 與形狀參數。
2. 在測試期（預設 2015 起）對多組「規模門檻 × 時間長度」回測，和兩個對照比較：
   - 均勻：全區每格機率相同（完全亂猜）
   - 只有背景：以前常震的地方（不含近期加成）
3. 以目錄最後一天為發布時間，輸出每格在多個時間長度下的期望次數，供 APP 換算任意規模的機率。

輸出：
    pipeline/data/hazard/params.json、fit_grid.csv、backtest.csv、hazard_latest.parquet
    docs/reports/hazard_v1/README.md 與地圖

執行：python pipeline/src/06_hazard_model.py
"""
import argparse
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eqforecast import hazard as hz  # noqa: E402

BASE_DIR = Path(__file__).resolve().parents[2]
DATA = BASE_DIR / "pipeline" / "data"
REPORT = BASE_DIR / "docs" / "reports" / "hazard_v1"

MAGS = [3.0, 3.5, 4.0, 5.0, 6.0]
HORIZONS = [1 / 24, 1.0, 7.0, 30.0]
EXPORT_HORIZONS = [1 / 24, 3 / 24, 6 / 24, 0.5, 1, 2, 3, 7, 14, 30]

plt.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "Noto Sans CJK TC", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def horizon_label(days):
    return f"{round(days * 24)} 小時" if days < 1 else f"{days:g} 天"


def backtest(events, grid, prm, info, test_start, test_end, mags=MAGS, horizons=HORIZONS):
    """每組（規模、時間長度）回傳一列指標。發布時間每 7 天一次；時間長度超過 7 天時拉長到不重疊。"""
    cat = hz.Catalog(events, grid, prm.m_ref)
    cat_obs = hz.Catalog(events, grid, min(min(mags), prm.m_ref))
    bg_only = hz.HazardParams(**{**prm.to_dict(), "mu": info["mu_bg_only"], "K": 0.0})
    rows = []
    for T in horizons:
        step = max(7, int(np.ceil(T / 7) * 7))
        h = np.timedelta64(int(round(T * 86400)), "s")
        t0s = hz.issue_times(test_start, test_end, step)
        t0s = t0s[t0s + h <= np.datetime64(test_end, "us")]
        full, bgo = [], []
        for t in t0s:
            a, b = hz.expected_ref(cat, t, T, prm)
            full.append(a + b)
            bgo.append(hz.expected_ref(cat, t, T, bg_only)[0])
        full, bgo = np.stack(full), np.stack(bgo)
        uni = np.full_like(full, info["uniform_rate_per_cell_day"] * T)
        for m in mags:
            obs = np.stack([cat_obs.counts(t, t + h, min_ml=m) for t in t0s])
            n_ev = float(obs.sum())
            pos = obs > 0
            row = {"m": m, "horizon_days": T, "n_windows": len(t0s), "n_events": int(n_ev),
                   "positive_cell_windows": int(pos.sum()), "base_rate": float(pos.mean())}
            ll = {}
            for name, ref in (("full", full), ("bg", bgo), ("uniform", uni)):
                n_exp = hz.scale_magnitude(ref, m, prm.m_ref, prm.b)
                ll[name] = hz.poisson_ll(obs, n_exp)
                row[f"n_ratio_{name}"] = float(n_exp.sum() / n_ev) if n_ev else np.nan
                if name != "uniform":
                    row[f"auc_{name}"] = (float(roc_auc_score(pos.ravel(), n_exp.ravel()))
                                          if 0 < pos.sum() < pos.size else np.nan)
            row["gain_vs_uniform"] = (ll["full"] - ll["uniform"]) / n_ev if n_ev else np.nan
            row["gain_vs_bg"] = (ll["full"] - ll["bg"]) / n_ev if n_ev else np.nan
            row["gain_bg_vs_uniform"] = (ll["bg"] - ll["uniform"]) / n_ev if n_ev else np.nan
            rows.append(row)
    return pd.DataFrame(rows)


def export_latest(events, grid, prm, t0, horizons=EXPORT_HORIZONS):
    cat = hz.Catalog(events, grid, prm.m_ref)
    out = grid.frame()
    for T in horizons:
        a, b = hz.expected_ref(cat, t0, T, prm)
        out[f"n_ref_{T:.5f}d"] = (a + b).ravel()
        out[f"n_bg_{T:.5f}d"] = a.ravel()
    return out


def plot_map(latest, grid, prm, m, T, t0, path):
    n = hz.scale_magnitude(latest[f"n_ref_{T:.5f}d"].to_numpy(), m, prm.m_ref, prm.b).reshape(grid.shape)
    p = hz.probability(n) * 100
    lat_b = np.arange(grid.lat_min, grid.lat_max + 1e-9, grid.deg)
    lon_b = np.arange(grid.lon_min, grid.lon_max + 1e-9, grid.deg)
    fig, ax = plt.subplots(figsize=(6, 7))
    pm = ax.pcolormesh(lon_b, lat_b, p, cmap="YlOrRd", shading="flat",
                       norm=matplotlib.colors.LogNorm(vmin=max(p.max() / 300, 1e-6), vmax=p.max()))
    fig.colorbar(pm, ax=ax, shrink=0.75, label="機率（%）",
                 format=matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
    ax.set_aspect(1 / np.cos(np.deg2rad(23.5)))
    ax.set_xlabel("經度")
    ax.set_ylabel("緯度")
    ax.set_title(f"{pd.Timestamp(t0):%Y-%m-%d} 起 {horizon_label(T)}內，每格發生 M {m:g} 以上地震的機率\n"
                 f"全區至少一次：{hz.probability(n.sum()) * 100:.1f}%", fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def write_report(prm, info, bt, latest_t0, args, maps):
    def fmt(v, nd=2):
        return "—" if pd.isna(v) else f"{v:.{nd}f}"

    L = ["# 連續式風險函數 v1：回測報告", "",
         f"> 由 `pipeline/src/06_hazard_model.py` 產生（{pd.Timestamp.now():%Y-%m-%d %H:%M}）。重新執行會覆寫本檔。", "",
         "## 這個模型回答什麼", "",
         "「從現在起 T 這麼久之內，這一格發生規模 m 以上地震的機率是多少？」m 與 T 都可以任意指定。",
         "做法是先估每格的發生率（以前常震的地方 ＋ 最近剛震過的地方），再用規模分布與時間長度換算。",
         "公式與限制見 `pipeline/src/eqforecast/hazard.py` 開頭。", "",
         "**和 B3 的差別**：這裡預報的是全部地震（含餘震），B3 只算去群聚後的主震。兩者的分數不能互相比較。", "",
         "## 參數", "",
         f"- 訓練期 {args.train_start} ～ {args.train_end}：{info['n_windows']} 個 {args.fit_horizon:g} 天的窗、"
         f"{info['n_events']} 個 M≥{prm.m_ref:g} 地震",
         f"- 測試期 {args.test_start} ～ {pd.Timestamp(args.test_end):%Y-%m-%d}",
         f"- b 值 {prm.b:.3f}（只用訓練期結束前的地震）",
         f"- 背景倍率 mu = {prm.mu:.3f}；近期加成 K = {prm.K:.4g}、alpha = {prm.alpha:g}、p = {prm.p:g}、c = {prm.c:g} 天",
         f"- 平滑寬度：背景 {prm.sigma_bg:g} 格、近期 {prm.sigma_st:g} 格（一格 0.2°）", "",
         "## 回測結果（測試期）", "",
         "- **資訊增益**：每個地震平均多得到多少資訊（單位 nat；0 = 和對照一樣，越大越好）。",
         "- **AUC**：把所有「格 × 窗」依機率排序，有地震的排在前面的程度（0.5 = 亂猜，1 = 完美）。",
         "- **預測／實際**：模型預測的地震總數 ÷ 實際發生數（1 = 剛好，> 1 = 高估）。",
         "- **基準率**：隨便挑一格一窗，剛好有地震的比例。任何「命中」都要和它比。", "",
         "| 規模 | 時間 | 地震數 | 基準率 | 增益（對均勻） | 增益（對只有背景） | AUC 完整 | AUC 只有背景 | 預測／實際 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in bt.itertuples():
        note = "（外推）" if r.m < prm.m_ref else ""
        L.append(f"| M≥{r.m:g}{note} | {horizon_label(r.horizon_days)} | {r.n_events} | {r.base_rate * 100:.3f}% | "
                 f"{fmt(r.gain_vs_uniform)} | {fmt(r.gain_vs_bg)} | {fmt(r.auc_full, 3)} | {fmt(r.auc_bg, 3)} | "
                 f"{fmt(r.n_ratio_full)} |")
    L += ["", "地震數少於約 20 的列，數字只能當參考。M 小於參考規模的列是往下外推，那個範圍的目錄並不完整。", "",
          f"## 最新一期（發布時間 {pd.Timestamp(latest_t0):%Y-%m-%d}，即目錄最後一天的隔日）", ""]
    L += [f"![{m}]({m})\n" for m in maps]
    L += ["## 已知限制", "",
          "1. 近期加成的技巧主要來自「大地震後會有餘震」，這是已知規律，不是預測主震。",
          "2. **總數校準不準**：參數只用 7 天的窗擬合，再套到 1 小時與 30 天。上表「預測／實際」若明顯小於 1，"
          "代表模型低估地震總數（排序能力不受影響，但機率的絕對值偏低）。可能原因：測試期的地震活動比訓練期多、"
          "餘震再觸發的餘震沒有算進去、近期活動只回看一年。下一版要依時間長度重新校準。",
          "3. 大森定律的 c 固定，沒有擬合；形狀參數只做粗略的格點搜尋，選到的值若在搜尋範圍邊界，代表範圍要再放寬"
          "（見 `pipeline/data/hazard/fit_grid.csv`）。",
          "4. `events.parquet` 已先以測站包覆角（Gap）篩過，外海事件偏少，所以外海的發生率偏低。",
          "5. 尚未加入地磁、TEC 等前兆；這一版是之後檢驗前兆時的對照基準。", ""]
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "README.md").write_text("\n".join(L), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", default=str(DATA / "events.parquet"))
    ap.add_argument("--train-start", default="1995-01-01")
    ap.add_argument("--train-end", default="2015-01-01")
    ap.add_argument("--test-start", default="2015-01-01")
    ap.add_argument("--test-end", default=None, help="預設為目錄最後一筆的隔日")
    ap.add_argument("--m-ref", type=float, default=3.5)
    ap.add_argument("--fit-horizon", type=float, default=7.0)
    ap.add_argument("--out", default=str(DATA / "hazard"))
    args = ap.parse_args()

    events = pd.read_parquet(args.events, columns=["time", "lat", "lon", "ml"])
    events["time"] = pd.to_datetime(events["time"])
    grid = hz.Grid()
    last = (events["time"].max() + pd.Timedelta(days=1)).normalize()
    args.test_end = str(last.date()) if args.test_end is None else args.test_end
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    prm, grid_log, info = hz.fit(events, grid, args.train_start, args.train_end, m_ref=args.m_ref,
                                 horizon_days=args.fit_horizon)
    print(f"b = {prm.b:.3f}；mu = {prm.mu:.3f}、K = {prm.K:.4g}、alpha = {prm.alpha}、p = {prm.p}、"
          f"sigma_bg = {prm.sigma_bg}、sigma_st = {prm.sigma_st}")
    print(f"訓練期對數概似：完整 {info['ll_full']:.1f}、只有背景 {info['ll_bg_only']:.1f}（{info['n_events']} 個地震）")
    grid_log.to_csv(out / "fit_grid.csv", index=False)

    bt = backtest(events, grid, prm, info, args.test_start, args.test_end)
    bt.to_csv(out / "backtest.csv", index=False)
    print(bt[["m", "horizon_days", "n_events", "base_rate", "gain_vs_uniform", "gain_vs_bg", "auc_full", "auc_bg",
              "n_ratio_full"]].round(4).to_string(index=False))

    latest = export_latest(events, grid, prm, last)
    latest.to_parquet(out / "hazard_latest.parquet", index=False)
    (out / "params.json").write_text(json.dumps(
        {"params": prm.to_dict(), "fit_info": info, "issue_time": str(last), "export_horizons_days": EXPORT_HORIZONS,
         "train": [args.train_start, args.train_end], "test": [args.test_start, args.test_end],
         "grid": {"lat_min": grid.lat_min, "lat_max": grid.lat_max, "lon_min": grid.lon_min, "lon_max": grid.lon_max,
                  "deg": grid.deg}}, ensure_ascii=False, indent=1), encoding="utf-8")
    REPORT.mkdir(parents=True, exist_ok=True)
    maps = []
    for m, T in ((4.0, 7), (5.5, 30)):
        name = f"map_M{m:g}_{T}d.png"
        plot_map(latest, grid, prm, m, float(T), last, REPORT / name)
        maps.append(name)
    write_report(prm, info, bt, last, args, maps)
    print(f"輸出：{out}；報告：{REPORT / 'README.md'}")


if __name__ == "__main__":
    main()
