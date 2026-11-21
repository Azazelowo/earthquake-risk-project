"""大地震剛發生後，風險函數反應得夠不夠快：擬合 alpha、young_days，並比較多組參數。

為什麼要另外做：06 的回測與擬合每 7 天發布一次，主震後幾分鐘到幾小時（APP 最常被打開的時候）幾乎抽不到，
所以 7 天窗的擬合把 alpha 壓到 0，主震後嚴重低估（docs/reports/hazard_v1_2_findings.md）。
這裡改成在每個主震之後的固定延遲發布（2 分鐘、1 小時、1 天），看震央 50 km 內之後 1 天、7 天的 M≥m_ref 地震數。

主震的定義：M≥5，前 30 天、100 km 內沒有更大（或一樣大）的地震。所以一個序列只算第一個最大的，
2024 花蓮 M7.2 之後的 M6.6 不會再算一次——避免少數大序列被重複計入。

預測和即時更新（app/live_update.py）一樣：expected_ref × 滾動校準倍率（trailing_factor，只用過去資料）。

兩種模式：
- --fit-early OUT.json：只用訓練期的淺層主震，格點搜尋 alpha × young_days（其他參數沿用第一組 --params），
  並做「拿掉最大的幾個序列」的穩健性檢查。格點平行計算。
- 預設：在測試期比較各組 --params，報告含逐主震的中位數、依主震重抽樣的信賴區間、排除特定序列後的結果。

輸出：docs/reports/hazard_mainshock_response.md（以及 --out 的明細 CSV）
執行：python pipeline/src/08_mainshock_response.py --events <events.parquet> --params v1.1=<params.json> \\
        --fit-early <v1.2 params.json>
      python pipeline/src/08_mainshock_response.py --events <events.parquet> \\
        --params v1.1=<params.json> --params v1.2=<v1.2 params.json>
"""
import argparse
import json
import os

# 每個工作程序只用 1 個 BLAS 執行緒：平行的是格點，不是矩陣運算（16 程序 × 多執行緒會把記憶體耗光）
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
import sys
from concurrent.futures import ProcessPoolExecutor
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
MAIN_M = 5.0
DECLUSTER_DAYS, DECLUSTER_KM = 30.0, 100.0
DEEP_KM = 40.0
# v1.2 第二輪：第一輪（只有 alpha、young_days）顯示大序列要較陡的 alpha、一般 M5–6 要較小的量，
# 單一 alpha 兩邊顧不到，所以加上倍率 A（boost_amp），並把 alpha 往上放寬
FIT_ALPHAS = (0.8, 1.0, 1.2, 1.4, 1.6)
FIT_YOUNG = (0.1, 0.25, 0.5, 1.0)
FIT_AMPS = (0.1, 0.25, 0.5, 1.0)
N_BOOT = 2000
# 排除檢查用的序列（UTC 日期範圍、中心、半徑）：測試期最大的兩個
SEQUENCES = {"2022 池上": ("2022-09-01", "2022-10-31", 23.0, 121.2, 80.0),
             "2024 花蓮": ("2024-04-01", "2024-05-31", 23.9, 121.6, 80.0)}

_G = {}   # 平行工作程序的共用資料


def km_between(lat1, lon1, lat2, lon2):
    dy = (np.asarray(lat2) - lat1) * hz.KM_PER_DEG
    dx = (np.asarray(lon2) - lon1) * hz.KM_PER_DEG * np.cos(np.radians(lat1))
    return np.hypot(dx, dy)


def disc_mask(grid, lat, lon, radius_km):
    """格中心在震央 radius_km 內的格子（至少包含震央所在格）。"""
    f = grid.frame()
    m = (km_between(lat, lon, f["lat_bin"].to_numpy() + grid.deg / 2,
                    f["lon_bin"].to_numpy() + grid.deg / 2) <= radius_km).reshape(grid.shape)
    i, j, ok = grid.cell_index([lat], [lon])
    if ok[0]:
        m[i[0], j[0]] = True
    return m


def select_mainshocks(events, grid, start, end, min_m=MAIN_M, shallow_only=False):
    """[start, end) 內 M≥min_m、前 DECLUSTER_DAYS 天 DECLUSTER_KM 內沒有更大或一樣大地震的事件。"""
    _, _, ok = grid.cell_index(events["lat"], events["lon"])
    ev = events[ok].sort_values("time")
    big = ev[ev["ml"] >= min_m]
    cand = big[(big["time"] >= start) & (big["time"] < end)]
    if shallow_only and "depth" in cand.columns:
        cand = cand[~(cand["depth"] > DEEP_KM)]
    keep = []
    for r in cand.itertuples():
        prior = big[(big["time"] < r.time) & (big["time"] >= r.time - pd.Timedelta(days=DECLUSTER_DAYS))
                    & (big["ml"] >= r.ml)]
        if len(prior) == 0 or (km_between(r.lat, r.lon, prior["lat"], prior["lon"]) > DECLUSTER_KM).all():
            keep.append(r.Index)
    return ev.loc[keep]


def build_cases(cat, grid, mains):
    """每個（主震、延遲）一個 case：發布時間、50 km 範圍、之後各時間長度的實際數。"""
    cases = []
    for k, ev in enumerate(mains.itertuples()):
        mask = disc_mask(grid, ev.lat, ev.lon, RADIUS_KM)
        for dlabel, delay in DELAYS:
            t0 = np.datetime64(ev.time, "us") + np.timedelta64(int(round(delay * 86400)), "s")
            obs = [float(cat.counts(t0, t0 + np.timedelta64(int(T * 86400), "s"))[mask].sum()) for T in COUNT_HORIZONS]
            cases.append({"main": k, "delay": dlabel, "t0": t0, "mask": mask, "obs": obs})
    return cases


def calibrated(cat, t0, T, prm):
    a, b = hz.expected_ref(cat, t0, T, prm)
    return (a + b) * hz.trailing_factor(cat, t0, T, prm)


def predict_cases(cat, cases, prm):
    """回傳 (case × 時間長度) 的預測數。"""
    return np.array([[float(calibrated(cat, c["t0"], T, prm)[c["mask"]].sum()) for T in COUNT_HORIZONS] for c in cases])


def _init(events_path, m_ref, cases):
    events, _ = hz.load_events(events_path)
    _G["cat"] = hz.Catalog(events, hz.Grid(), m_ref)
    _G["cases"] = cases


def _grid_point(args):
    base, alpha, yd, amp = args
    prm = hz.HazardParams(**{**base, "alpha": alpha, "young_days": yd, "deep_km": DEEP_KM, "boost_amp": amp})
    return alpha, yd, amp, predict_cases(_G["cat"], _G["cases"], prm)


def case_ll(pred, obs):
    """每個 case 的卜瓦松對數概似（各時間長度加總）。"""
    pred = np.clip(pred, 1e-300, None)
    return (obs * np.log(pred) - pred).sum(axis=1)


def fit_early(events_path, events, grid, meta, train_start, train_end):
    """只用訓練期淺層主震選 alpha 與 young_days；回傳 (結果表, 主震表, 穩健性表)。"""
    base = meta["params"]
    cat = hz.Catalog(events, grid, base["m_ref"])
    mains = select_mainshocks(events, grid, train_start, pd.Timestamp(train_end) - pd.Timedelta(days=7),
                              shallow_only=True)
    cases = build_cases(cat, grid, mains)
    obs = np.array([c["obs"] for c in cases])
    jobs = [(base, a, y, A) for a in FIT_ALPHAS for y in FIT_YOUNG for A in FIT_AMPS]
    print(f"訓練期淺層 M≥{MAIN_M:g} 主震 {len(mains)} 個、case {len(cases)} 個；格點 {len(jobs)} 組，平行計算", flush=True)
    with ProcessPoolExecutor(max_workers=min(len(jobs), 8, os.cpu_count() or 4),
                             initializer=_init, initargs=(events_path, base["m_ref"], cases)) as ex:
        results = list(ex.map(_grid_point, jobs))
    main_of = np.array([c["main"] for c in cases])
    rows, ll_by_main = [], {}
    for alpha, yd, amp, pred in results:
        ll = case_ll(pred, obs)
        ll_by_main[(alpha, yd, amp)] = np.bincount(main_of, weights=ll, minlength=len(mains))
        rows.append({"alpha": alpha, "young_days": yd, "boost_amp": amp, "ll": float(ll.sum()),
                     "pred_over_obs": float(pred.sum() / obs.sum()),
                     "median_log2_ratio": float(np.median(np.log2((pred + 1) / (obs + 1))))})
    res = pd.DataFrame(rows).sort_values("ll", ascending=False).reset_index(drop=True)
    # 穩健性：依序拿掉實際餘震最多的主震（＝最大的序列），看最佳值會不會變
    per_main_obs = np.bincount(main_of, weights=obs.sum(axis=1), minlength=len(mains))
    order = np.argsort(-per_main_obs)
    robust = []
    for n_drop in (0, 1, 2, 3, 5):
        keep = np.ones(len(mains), bool)
        keep[order[:n_drop]] = False
        best = max(ll_by_main, key=lambda k: ll_by_main[k][keep].sum())
        dropped = "、".join(f"{pd.Timestamp(mains.iloc[i].time):%Y-%m-%d} M{mains.iloc[i].ml:.1f}" for i in order[:n_drop])
        robust.append({"dropped": n_drop, "which": dropped or "—", "alpha": best[0], "young_days": best[1],
                       "boost_amp": best[2]})
    return res, mains, pd.DataFrame(robust)


def evaluate(events, grid, metas, test_start, test_end):
    """測試期每組參數、每個 case 的預測、實際與 3 天 M≥5 的倍數。"""
    mains = select_mainshocks(events, grid, test_start, test_end)
    rows = []
    for name, meta in metas.items():
        prm = hz.HazardParams(**meta["params"])
        bgp = hz.bg_only_params(prm, meta["fit_info"]["mu_bg_only"])
        cat = hz.Catalog(events, grid, prm.m_ref)
        cases = build_cases(cat, grid, mains)
        pred = predict_cases(cat, cases, prm)
        for c, p in zip(cases, pred):
            ev = mains.iloc[c["main"]]
            n_full = hz.scale_magnitude(calibrated(cat, c["t0"], 3.0, prm)[c["mask"]].sum(), 5.0, prm.m_ref, prm.b)
            a_bg, _ = hz.expected_ref(cat, c["t0"], 3.0, bgp)
            n_bg = hz.scale_magnitude(a_bg[c["mask"]].sum() * hz.trailing_factor(cat, c["t0"], 3.0, bgp), 5.0,
                                      prm.m_ref, prm.b)
            row = {"model": name, "time": ev.time, "ml": ev.ml, "lat": ev.lat, "lon": ev.lon,
                   "depth": getattr(ev, "depth", np.nan), "delay": c["delay"],
                   "ratio_m5_3d": float(hz.probability(n_full) / hz.probability(n_bg)),
                   "p_m5_3d": float(hz.probability(n_full))}
            for T, pv, ov in zip(COUNT_HORIZONS, p, c["obs"]):
                row[f"pred_{T:g}d"], row[f"obs_{T:g}d"] = float(pv), ov
            rows.append(row)
        print(f"{name}：測試期主震 {len(mains)} 個")
    return pd.DataFrame(rows), mains


def in_sequence(df, name):
    a, b, lat, lon, r = SEQUENCES[name]
    t = pd.to_datetime(df["time"])
    return (t >= a) & (t < b) & (km_between(lat, lon, df["lat"], df["lon"]) <= r)


def boot_ratio(d, col_p, col_o, rng):
    """依主震重抽樣，加總預測÷加總實際的 95% 區間。"""
    g = d.groupby("time")[[col_p, col_o]].sum().to_numpy()
    if len(g) < 3 or g[:, 1].sum() == 0:
        return np.nan, np.nan
    idx = rng.integers(0, len(g), (N_BOOT, len(g)))
    s = g[idx].sum(axis=1)
    r = s[:, 0] / np.where(s[:, 1] > 0, s[:, 1], np.nan)
    return np.nanquantile(r, 0.025), np.nanquantile(r, 0.975)


def summary_rows(df, names, label_filter=None):
    rng = np.random.default_rng(0)
    out = []
    for dlabel, _ in DELAYS:
        for n in names:
            d = df[(df.delay == dlabel) & (df.model == n)]
            if label_filter is not None:
                d = d[label_filter(d)]
            r = {}
            for T in COUNT_HORIZONS:
                p, o = f"pred_{T:g}d", f"obs_{T:g}d"
                r[T] = (d[p].sum() / d[o].sum() if d[o].sum() else np.nan, *boot_ratio(d, p, o, rng),
                        float(np.median(np.log2((d[p] + 1) / (d[o] + 1)))) if len(d) else np.nan)
            out.append((dlabel, n, len(d), r, d["ratio_m5_3d"].median() if len(d) else np.nan))
    return out


def fmt_rows(rows):
    L = ["| 延遲 | 模型 | 主震 | 預測÷實際（1 天）[95%] | 逐主震中位數（1 天） | 預測÷實際（7 天）[95%] | "
         "逐主震中位數（7 天） | 倍數中位數 |", "|---|---|---|---|---|---|---|---|"]
    for dlabel, n, k, r, ratio in rows:
        cells = []
        for T in COUNT_HORIZONS:
            v, lo, hi, med = r[T]
            cells += [f"{v:.2f} [{lo:.2f}–{hi:.2f}]", f"×{2 ** med:.2f}"]
        L.append(f"| {dlabel} | {n} | {k} | " + " | ".join(cells) + f" | {ratio:.1f} |")
    return L


def write_report(df, mains, metas, test_start, fit_info=None):
    names = list(metas)
    L = ["# 大地震剛發生後，模型反應得夠不夠快", "",
         "> 由 `pipeline/src/08_mainshock_response.py` 產生。重新執行會覆寫本檔。", "",
         f"測試期（{test_start} 起）的主震：M≥{MAIN_M:g}，前 {DECLUSTER_DAYS:g} 天、{DECLUSTER_KM:g} km 內沒有更大的地震，"
         f"共 **{len(mains)} 個**（深度 > {DEEP_KM:g} km 的 {int((mains['depth'] > DEEP_KM).sum()) if 'depth' in mains else '?'} 個）。"
         f"每個主震在 2 分鐘、1 小時、1 天後各發布一次，看震央 {RADIUS_KM:g} km 內之後的 M≥3.5 地震數。", "",
         "- **預測÷實際**：所有主震加總後相除（大序列影響大）；方括號是依主震重抽樣 2000 次的 95% 區間。",
         "- **逐主震中位數**：每個主震的 (預測+1)÷(實際+1) 取中位數——每個主震權重相同，不會被花蓮、池上主導。",
         "- **倍數**：3 天、M≥5 的機率 ÷ 只有背景的機率（APP 首頁的數字），取中位數。", "",
         "## 參數", ""]
    for n in names:
        p = metas[n]["params"]
        L.append(f"- **{n}**：alpha = {p['alpha']:g}、A = {p.get('boost_amp', 1.0):g}、young_days = {p.get('young_days', 0.0):g}、"
                 f"deep_km = {p.get('deep_km', 0.0):g}、p = {p['p']:g}、K = {p['K']:.4g}")
    if fit_info:
        L += ["", f"v1.2 的 alpha、young_days 只用訓練期（{fit_info['train'][0]} ～ {fit_info['train'][1]}）"
              f"{fit_info['n_mainshocks']} 個淺層主震選出；拿掉最大的序列後的最佳值：", "",
              "| 拿掉 | 哪些 | alpha | young_days | A |", "|---|---|---|---|---|"]
        L += [f"| {r['dropped']} | {r['which']} | {r['alpha']:g} | {r['young_days']:g} | {r.get('boost_amp', 1.0):g} |"
              for r in fit_info["robustness"]]
    L += ["", "## 全部主震", ""] + fmt_rows(summary_rows(df, names))
    excl = lambda d: ~(in_sequence(d, "2022 池上") | in_sequence(d, "2024 花蓮"))  # noqa: E731
    L += ["", "## 排除 2022 池上、2024 花蓮兩個序列", ""] + fmt_rows(summary_rows(df, names, excl))
    shallow = lambda d: ~(d["depth"] > DEEP_KM)  # noqa: E731
    L += ["", f"## 只看淺層（深度 ≤ {DEEP_KM:g} km）", ""] + fmt_rows(summary_rows(df, names, shallow))
    L += ["", "## 每個主震（主震後 2 分鐘發布，1 天）", "",
          "| 時間（UTC） | ML | 深度 | " + " | ".join(f"{n} 預測／實際 | {n} 倍數" for n in names) + " |",
          "|---|---|---|" + "---|---|" * len(names)]
    first = df[df.delay == DELAYS[0][0]]
    for t, g in first.groupby("time", sort=True):
        cells = []
        for n in names:
            r = g[g.model == n].iloc[0]
            cells.append(f"{r.pred_1d:.0f}／{r.obs_1d:.0f} | {r.ratio_m5_3d:.1f}")
        L.append(f"| {pd.Timestamp(t):%Y-%m-%d %H:%M} | {g.ml.iloc[0]:.1f} | {g.depth.iloc[0]:.0f} | "
                 + " | ".join(cells) + " |")
    L += ["", "## 解讀時要注意", "",
          "- 實際數量來自目前的完整目錄；即時運作時主震後的目錄只補得到有感報告與 USGS，APP 實際看到的會更低。",
          "- 這只檢查主震之後；主震之前模型沒有能力指出哪一次地震會成為大地震。", ""]
    REPORT.write_text("\n".join(L), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", required=True)
    ap.add_argument("--params", action="append", required=True, help="名稱=params.json，可給多次")
    ap.add_argument("--test-start", default="2015-01-01")
    ap.add_argument("--out", default=None, help="明細 CSV")
    ap.add_argument("--fit-early", default=None, metavar="OUT_JSON",
                    help="擬合模式：用第一組 --params 在訓練期主震上選 alpha、young_days，輸出新的 params.json")
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
        res, mains, robust = fit_early(args.events, events, grid, meta, args.train_start, args.train_end)
        print(res.head(10).round(4).to_string(index=False))
        print(robust.to_string(index=False))
        best = res.iloc[0]
        new = json.loads(json.dumps(meta))
        new["params"].update(alpha=float(best.alpha), young_days=float(best.young_days), deep_km=DEEP_KM,
                             boost_amp=float(best.boost_amp))
        new["early_fit"] = {"train": [args.train_start, args.train_end], "n_mainshocks": int(len(mains)),
                            "main_m": MAIN_M, "radius_km": RADIUS_KM, "decluster": [DECLUSTER_DAYS, DECLUSTER_KM],
                            "grid": res.to_dict("records"), "robustness": robust.to_dict("records")}
        Path(args.fit_early).write_text(json.dumps(new, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
        print(f"最佳 alpha={best.alpha:g}、young_days={best.young_days:g}、A={best.boost_amp:g} → {args.fit_early}")
        return
    end = pd.Timestamp(events["time"].max()) - pd.Timedelta(days=7)
    df, mains = evaluate(events, grid, metas, args.test_start, end)
    if args.out:
        df.to_csv(args.out, index=False)
    fit_info = next((m.get("early_fit") for m in metas.values() if m.get("early_fit")), None)
    write_report(df, mains, metas, args.test_start, fit_info)
    print(f"報告：{REPORT}")


if __name__ == "__main__":
    main()
