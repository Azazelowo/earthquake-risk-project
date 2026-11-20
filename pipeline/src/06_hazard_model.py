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
import safe_console  # noqa: E402,F401  Windows cp950 終端機遇到 ⚠、≥ 等字元時不會中斷
from eqforecast import hazard as hz  # noqa: E402

BASE_DIR = Path(__file__).resolve().parents[2]
DATA = BASE_DIR / "pipeline" / "data"
REPORT = BASE_DIR / "docs" / "reports" / "hazard_v1"

MAGS = [3.0, 3.5, 4.0, 5.0, 6.0]
HORIZONS = [1 / 24, 1.0, 7.0, 30.0]
EXPORT_HORIZONS = [1 / 24, 3 / 24, 6 / 24, 0.5, 1, 2, 3, 7, 14, 30]
TRAILING_DAYS = 730            # 滾動校準回看的天數
MODEL_VERSION = "v1.1"         # v1.2 研究中（見 docs/reports/hazard_v1_2_findings.md），尚未套用；v1.1：事件母體統一、搜尋範圍放寬、均勻對照校準

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
        raw_total = float(full.sum())
        # 滾動校準：每週一個「校準用」的回顧窗，t0 只取在它之前已經結束、且在回看期間內的窗
        lookback = np.timedelta64(int(TRAILING_DAYS), "D")
        G = hz.issue_times(pd.Timestamp(test_start) - pd.Timedelta(days=TRAILING_DAYS + np.ceil(T) + 7), test_end, 7)
        g_obs = np.array([cat.counts(g, g + h).sum() for g in G])
        g_full = np.array([sum(x.sum() for x in hz.expected_ref(cat, g, T, prm)) for g in G])
        g_bg = np.array([hz.expected_ref(cat, g, T, bg_only)[0].sum() for g in G])
        # 均勻對照也走同一套滾動校準（審查 M4）：v1 的均勻對照固定用訓練期的發生率，測試期總數改變時
        # 只有完整模型與「只有背景」會被校正，「增益（對均勻）」因此被高估。
        uni = np.full_like(full, info["uniform_rate_per_cell_day"] * T)
        g_uni = np.full(len(G), float(uni[0].sum()) if len(uni) else 0.0)
        factors, factors_uni = [], []
        for k, t in enumerate(t0s):
            use = (G + h <= t) & (G >= t - lookback)
            f_full = hz.recalibration_factor(g_full[use].sum(), g_obs[use].sum())
            full[k] *= f_full
            bgo[k] *= hz.recalibration_factor(g_bg[use].sum(), g_obs[use].sum())
            f_uni = hz.recalibration_factor(g_uni[use].sum(), g_obs[use].sum())
            uni[k] *= f_uni
            factors.append(f_full)
            factors_uni.append(f_uni)
        for m in mags:
            obs = np.stack([cat_obs.counts(t, t + h, min_ml=m) for t in t0s])
            n_ev = float(obs.sum())
            pos = obs > 0
            row = {"m": m, "horizon_days": T, "n_windows": len(t0s), "n_events": int(n_ev),
                   "positive_cell_windows": int(pos.sum()), "base_rate": float(pos.mean()),
                   "calib_factor_median": float(np.median(factors)),
                   "calib_factor_uniform_median": float(np.median(factors_uni)),
                   "n_ratio_uncalibrated": (float(hz.scale_magnitude(raw_total, m, prm.m_ref, prm.b) / n_ev)
                                            if n_ev else np.nan)}
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


def export_latest(events, grid, prm, t0, mu_bg_only, horizons=EXPORT_HORIZONS):
    """每格、每個時間長度的期望數（M≥m_ref）。回傳 (表格, 完整模型的校準倍率, 「平常」的校準倍率)。

    「平常」＝只有背景的模型，用它自己的滾動校準倍率（審查 M5；理由見 hazard.background_only_export）。
    """
    cat = hz.Catalog(events, grid, prm.m_ref)
    out = grid.frame()
    factors, factors_bg = {}, {}
    for T in horizons:
        a, b = hz.expected_ref(cat, t0, T, prm)
        f = hz.trailing_factor(cat, t0, T, prm, trailing_days=TRAILING_DAYS)
        n_bg, f_bg = hz.background_only_export(cat, t0, T, prm, mu_bg_only, trailing_days=TRAILING_DAYS)
        factors[f"{T:.5f}"] = f
        factors_bg[f"{T:.5f}"] = f_bg
        out[f"n_ref_{T:.5f}d"] = (a + b).ravel() * f
        out[f"n_bg_{T:.5f}d"] = n_bg.ravel()
    return out, factors, factors_bg


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


QUALITY_TEXT = {
    "located": "只用定位品質良好的事件（方位角空隙 gap ≤ 110° 或未知，`use_for_features`）；擬合、回測、匯出都用同一個母體",
    "all": "`events.parquet` 全部事件（v1 的舊行為：gap ≤ 110° 的事件加上 ML ≥ 5.5 不論 gap，兩種母體混在一起）",
}


def write_report(prm, info, bt, latest_t0, args, maps, ev_info):
    def fmt(v, nd=2):
        return "—" if pd.isna(v) else f"{v:.{nd}f}"

    grid_txt = "、".join(f"{k} {'／'.join(f'{v:g}' for v in vals)}" for k, vals in info["search_grid"].items())
    hits = info["boundary_hits"]
    hit_txt = ("⚠ 最佳值落在搜尋範圍邊界：" + "、".join(f"{n} = {v:g}（{side}）" for n, v, side in hits)
               + "。真正的最佳值可能在範圍外，或模型形式本身需要調整（見已知限制 3）。") if hits else "最佳值都在搜尋範圍內部。"
    L = [f"# 連續式風險函數 {MODEL_VERSION}：回測報告", "",
         f"> 由 `pipeline/src/06_hazard_model.py` 產生（{pd.Timestamp.now():%Y-%m-%d %H:%M}）。重新執行會覆寫本檔。", "",
         "> **v1.1 的修正（2026-11-17 審查）**：① 事件母體統一（M1）② 形狀參數搜尋範圍放寬，並檢查是否落在邊界（M3）"
         "③ 均勻對照也做滾動校準（M4）。v1 的數字不能和本版直接比較。", "",
         "## 這個模型回答什麼", "",
         "「從現在起 T 這麼久之內，這一格發生規模 m 以上地震的機率是多少？」m 與 T 都可以任意指定。",
         "做法是先估每格的發生率（以前常震的地方 ＋ 最近剛震過的地方），再用規模分布與時間長度換算。",
         "公式與限制見 `pipeline/src/eqforecast/hazard.py` 開頭。", "",
         "**和 B3 的差別**：這裡預報的是全部地震（含餘震），B3 只算去群聚後的主震。兩者的分數不能互相比較。", "",
         "## 參數", "",
         f"- 事件母體：{QUALITY_TEXT[ev_info['quality']]}（{ev_info['n_used']:,} / {ev_info['n_all']:,} 筆，各規模合計）",
         f"- 訓練期 {args.train_start} ～ {args.train_end}：{info['n_windows']} 個 {args.fit_horizon:g} 天的窗、"
         f"{info['n_events']} 個 M≥{prm.m_ref:g} 地震",
         f"- 測試期 {args.test_start} ～ {pd.Timestamp(args.test_end):%Y-%m-%d}",
         f"- b 值 {prm.b:.3f}（只用訓練期結束前的地震）",
         f"- 背景倍率 mu = {prm.mu:.3f}；近期加成 K = {prm.K:.4g}、alpha = {prm.alpha:g}、p = {prm.p:g}、c = {prm.c:g} 天",
         f"- 平滑寬度：背景 {prm.sigma_bg:g} 格、近期 {prm.sigma_st:g} 格（一格 0.2°）；"
         f"近期加成隨規模放大 d_frac = {prm.d_frac:g}（寬度² = 近期² + (d_frac × 破裂長度)²，"
         "破裂長度 L(M) = 0.01×10^(0.5M) km；d_frac = 0 即 v1.1 的固定寬度）",
         f"- 深源地震：深度 > {prm.deep_km:g} km 的地震不套用 alpha（0 = 全部套用，即 v1.1）" if prm.deep_km > 0
         else "- 深源地震：全部地震都套用 alpha（v1.1 的作法）",
         f"- 形狀參數的搜尋範圍：{grid_txt}。{hit_txt}", "",
         "## 回測結果（測試期）", "",
         "- **三個模型（完整、只有背景、均勻）都用同一套滾動校準**，所以增益比較的是空間與時間的分布，"
         "不是「誰的總數剛好比較準」。",
         "- **資訊增益**：每個地震平均多得到多少資訊（單位 nat；0 = 和對照一樣，越大越好）。",
         "- **AUC**：把所有「格 × 窗」依機率排序，有地震的排在前面的程度（0.5 = 亂猜，1 = 完美）。",
         "- **預測／實際**：模型預測的地震總數 ÷ 實際發生數（1 = 剛好，> 1 = 高估）。",
         "- **基準率**：隨便挑一格一窗，剛好有地震的比例。任何「命中」都要和它比。", "",
         "- **校準前**：沒有滾動校準時的「預測／實際」，用來看校準修正了多少。", "",
         "| 規模 | 時間 | 地震數 | 基準率 | 增益（對均勻） | 增益（對只有背景） | AUC 完整 | AUC 只有背景 | 預測／實際 | 校準前 |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for r in bt.itertuples():
        note = "（外推）" if r.m < prm.m_ref else ""
        L.append(f"| M≥{r.m:g}{note} | {horizon_label(r.horizon_days)} | {r.n_events} | {r.base_rate * 100:.3f}% | "
                 f"{fmt(r.gain_vs_uniform)} | {fmt(r.gain_vs_bg)} | {fmt(r.auc_full, 3)} | {fmt(r.auc_bg, 3)} | "
                 f"{fmt(r.n_ratio_full)} | {fmt(r.n_ratio_uncalibrated)} |")
    L += ["", "地震數少於約 20 的列，數字只能當參考。M 小於參考規模的列是往下外推，那個範圍的目錄並不完整。", "",
          f"## 最新一期（發布時間 {pd.Timestamp(latest_t0):%Y-%m-%d}，即目錄最後一天的隔日）", ""]
    L += [f"![{m}]({m})\n" for m in maps]
    L += ["## 已知限制", "",
          "1. 近期加成的技巧主要來自「大地震後會有餘震」，這是已知規律，不是預測主震。",
          f"2. **總數校準**：參數只用 7 天的窗擬合，直接套到其他時間長度會低估地震總數（見「校準前」欄）。"
          f"現在每次發布前，會回看過去 {TRAILING_DAYS} 天內已經結束的預報窗，用「實際 ÷ 預測」修正整體倍率"
          "（限制在 0.5–3 倍，只用過去資料）。這只修正全區總量，不改變各格的相對高低；"
          "大地震剛發生後的幾週，倍率來不及反應，仍可能低估。",
          "3. 大森定律的 c 固定，沒有擬合；形狀參數用格點搜尋（全部組合見 `pipeline/data/hazard/fit_grid.csv`）。"
          "v1.1 的近期加成平滑寬度和規模無關，alpha 落在下限 0；v1.2 讓寬度隨破裂長度放大（d_frac），"
          "擬合仍選 d_frac = 0、alpha = 0（假設被否定）；主震後低估改由 young_days 處理，見 `docs/reports/hazard_v1_2_findings.md`。",
          "4. 事件母體只含地震網能良好定位的地震（gap ≤ 110° 或未知）。外海 gap 大的地震（含部分大地震）"
          "不在模型裡，所以外海的機率代表「能被良好定位的地震」，實際會偏低。",
          "5. 尚未加入地磁、TEC 等前兆；這一版是之後檢驗前兆時的對照基準。", ""]
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "README.md").write_text("\n".join(L), encoding="utf-8")


def main():
    global REPORT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", default=str(DATA / "events.parquet"))
    ap.add_argument("--train-start", default="1995-01-01")
    ap.add_argument("--train-end", default="2015-01-01")
    ap.add_argument("--test-start", default="2015-01-01")
    ap.add_argument("--test-end", default=None, help="預設為目錄最後一筆的隔日")
    ap.add_argument("--m-ref", type=float, default=3.5)
    ap.add_argument("--fit-horizon", type=float, default=7.0)
    ap.add_argument("--out", default=str(DATA / "hazard"))
    ap.add_argument("--d-fracs", default="0",
                    help="d_frac 的搜尋值（逗號分隔），例如 0,0.5,1,2；預設 0 = v1.1 的固定寬度")
    ap.add_argument("--deep-kms", default="0",
                    help="deep_km 的搜尋值（逗號分隔），例如 0,40；預設 0 = 全部地震都套用 alpha（v1.1）")
    ap.add_argument("--report", default=str(REPORT), help="報告資料夾")
    ap.add_argument("--event-quality", choices=hz.EVENT_QUALITIES, default="located",
                    help="located（預設）＝只用 gap ≤ 110° 的事件；all＝v1 的舊行為，只供比較")
    args = ap.parse_args()
    REPORT = Path(args.report)

    events, ev_info = hz.load_events(args.events, args.event_quality)
    print(f"事件：{ev_info['quality']}，使用 {ev_info['n_used']:,} / {ev_info['n_all']:,} 筆")
    grid = hz.Grid()
    last = (pd.Timestamp(ev_info["time_max"]) + pd.Timedelta(days=1)).normalize()
    args.test_end = str(last.date()) if args.test_end is None else args.test_end
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    prm, grid_log, info = hz.fit(events, grid, args.train_start, args.train_end, m_ref=args.m_ref,
                                 horizon_days=args.fit_horizon,
                                 d_fracs=tuple(float(x) for x in args.d_fracs.split(",")),
                                 deep_kms=tuple(float(x) for x in args.deep_kms.split(",")))
    print(f"b = {prm.b:.3f}；mu = {prm.mu:.3f}、K = {prm.K:.4g}、alpha = {prm.alpha}、p = {prm.p}、"
          f"sigma_bg = {prm.sigma_bg}、sigma_st = {prm.sigma_st}、d_frac = {prm.d_frac}、deep_km = {prm.deep_km}")
    print(f"訓練期對數概似：完整 {info['ll_full']:.1f}、只有背景 {info['ll_bg_only']:.1f}（{info['n_events']} 個地震）")
    if info["boundary_hits"]:
        print("注意：最佳值落在搜尋範圍邊界：" + "、".join(f"{n}={v:g}（{s}）" for n, v, s in info["boundary_hits"]))
    grid_log.to_csv(out / "fit_grid.csv", index=False)

    bt = backtest(events, grid, prm, info, args.test_start, args.test_end)
    bt.to_csv(out / "backtest.csv", index=False)
    print(bt[["m", "horizon_days", "n_events", "base_rate", "gain_vs_uniform", "gain_vs_bg", "auc_full", "auc_bg",
              "n_ratio_full"]].round(4).to_string(index=False))

    latest, latest_factors, latest_factors_bg = export_latest(events, grid, prm, last, info["mu_bg_only"])
    latest.to_parquet(out / "hazard_latest.parquet", index=False)
    print("最新一期的校準倍率：" + "、".join(f"{horizon_label(float(k))} ×{v:.2f}" for k, v in latest_factors.items()))
    print("「平常」自己的校準倍率：" + "、".join(f"{horizon_label(float(k))} ×{v:.2f}" for k, v in latest_factors_bg.items()))
    (out / "params.json").write_text(json.dumps(
        {"model_version": MODEL_VERSION, "event_quality": ev_info,
         "params": prm.to_dict(), "fit_info": info, "issue_time": str(last), "export_horizons_days": EXPORT_HORIZONS,
         "calibration_factors": latest_factors, "calibration_factors_bg": latest_factors_bg,
         "calibration_trailing_days": TRAILING_DAYS,
         "train": [args.train_start, args.train_end], "test": [args.test_start, args.test_end],
         "grid": {"lat_min": grid.lat_min, "lat_max": grid.lat_max, "lon_min": grid.lon_min, "lon_max": grid.lon_max,
                  "deg": grid.deg}}, ensure_ascii=False, indent=1), encoding="utf-8")
    REPORT.mkdir(parents=True, exist_ok=True)
    maps = []
    for m, T in ((4.0, 7), (5.5, 30)):
        name = f"map_M{m:g}_{T}d.png"
        plot_map(latest, grid, prm, m, float(T), last, REPORT / name)
        maps.append(name)
    write_report(prm, info, bt, last, args, maps, ev_info)
    print(f"輸出：{out}；報告：{REPORT / 'README.md'}")


if __name__ == "__main__":
    main()
