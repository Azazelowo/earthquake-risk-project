"""機率換算的群聚修正：用真的發生的地震估 κ，讓「至少發生一次」的機率不再系統性偏高。

為什麼需要（docs/reports/state_frequency.md 的實際倍數檢查）：模型的期望數 N 大致校準過，但地震成群發生——
有地震的 3 天裡平均有 1.5 個 M5——用卜瓦松 P = 1 − exp(−N) 換算「至少一次」會高估（2015–2026：模型平均 1.56%、
平常 1.85%，真的發生 0.99%）。

修正：複合卜瓦松（地震以「群」為單位發生，每群平均 κ 個）：P = 1 − exp(−N / κ)。κ = 1 就是原本的公式。
κ 只用訓練期（1995–2014）估：每天 UTC 00:00 發布、20 個縣市代表點、多組（規模、時間、半徑），
對「之後真的有沒有發生」做伯努利最大概似。再拿測試期（2015 起）檢驗：平均機率 vs 實際比例、log loss、Brier。

「現在」與「平常」用同一個 κ（同一套換算；分開估會讓倍數多一個自由度，也比較難解釋）。

輸出：pipeline/data/probability_calibration/{train,test}.parquet、kappa.json、docs/reports/probability_calibration.md
用法：python app/probability_calibration.py [--step-days 1]
"""
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar

for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "4")

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR.parent / "pipeline" / "src"))
import safe_console  # noqa: E402,F401
import hazard_core as hc  # noqa: E402
import live_update as lu  # noqa: E402
import state_frequency as sf  # noqa: E402
from eqforecast import hazard as hz  # noqa: E402

BASE_DIR = APP_DIR / "data"
OUT = APP_DIR.parent / "pipeline" / "data" / "probability_calibration"
REPORT = APP_DIR.parent / "docs" / "reports" / "probability_calibration.md"

MAGS = (4.0, 5.0, 6.0)
HORIZONS = (1.0, 3.0, 7.0, 30.0)
RADII = (25.0, 50.0, 100.0)
TRAIN = ("1995-01-01", "2015-01-01")
TEST = ("2015-01-01", None)            # None＝目錄最後一天往前推最長的時間長度


def build(start, end, step_days, log=print):
    """每個（發布時間、縣市、T、R、m）一列：模型期望數 n、平常期望數 nb、之後真的發生的數量 k。"""
    params = lu.load_params(BASE_DIR)
    prm, grid = lu.hazard_params(params), lu.grid_of(params)
    ev = lu.read_base_catalog(BASE_DIR)
    cat = hz.Catalog(ev, grid, prm.m_ref)
    bgp = hz.bg_only_params(prm, params["fit_info"]["mu_bg_only"])
    trailing = float(params.get("calibration_trailing_days", 730))
    cells = grid.frame()[["lat_bin", "lon_bin"]]
    snap = hc.Snapshot(params, cells, np.array([1.0]), np.zeros((len(cells), 1)), np.zeros((len(cells), 1)),
                       pd.DataFrame(), pd.DataFrame())
    W = {(name, R): hc.point_weights(snap, lat, lon, R).ravel() for name, lat, lon in sf.COUNTIES for R in RADII}
    # 實際地震：和模型同一個母體，事先依縣市、半徑分好
    t_ev = ev["time"].to_numpy("datetime64[s]")
    lat, lon, ml = ev["lat"].to_numpy(float), ev["lon"].to_numpy(float), ev["ml"].to_numpy(float)
    near = {}
    for name, clat, clon in sf.COUNTIES:
        d = hc.haversine_km(clat, clon, lat, lon)
        for R in RADII:
            for m in MAGS:
                near[(name, R, m)] = np.sort(t_ev[(d <= R) & (ml >= m)])
    days = pd.date_range(start, end, freq=f"{step_days}D", inclusive="left")
    combos = [(T, ci, R, m) for T in HORIZONS for ci in range(len(sf.COUNTIES)) for R in RADII for m in MAGS]
    per_day = len(combos)
    total = len(days) * per_day
    col_t = np.empty(total, "datetime64[s]")
    col_c, col_T, col_R, col_m = (np.empty(total, np.int16), np.empty(total), np.empty(total), np.empty(total))
    col_n, col_nb, col_k = np.empty(total), np.empty(total), np.empty(total, np.int32)
    names = [c[0] for c in sf.COUNTIES]
    fac, month, pos = {}, None, 0
    for i, t0 in enumerate(days):
        if (t0.year, t0.month) != month:
            month = (t0.year, t0.month)
            fac = {T: (hz.trailing_factor(cat, t0, T, prm, trailing_days=trailing),
                       hz.trailing_factor(cat, t0, T, bgp, trailing_days=trailing)) for T in HORIZONS}
            if t0.month == 1:
                log(f"{t0.date()}（{i}/{len(days)}）")
        t0s = np.datetime64(t0, "s")
        for T in HORIZONS:
            a, b = hz.expected_ref(cat, t0, T, prm)
            full = (a + b).ravel() * fac[T][0]
            bg = hz.expected_ref(cat, t0, T, bgp)[0].ravel() * fac[T][1]
            t1 = t0s + np.timedelta64(int(T * 86400), "s")
            for ci, name in enumerate(names):
                for R in RADII:
                    w = W[(name, R)]
                    nf, nb = float(w @ full), float(w @ bg)
                    for m in MAGS:
                        s = 10.0 ** (-prm.b * (m - prm.m_ref))
                        tt = near[(name, R, m)]
                        col_t[pos], col_c[pos], col_T[pos], col_R[pos], col_m[pos] = t0s, ci, T, R, m
                        col_n[pos], col_nb[pos] = nf * s, nb * s
                        col_k[pos] = np.searchsorted(tt, t1) - np.searchsorted(tt, t0s)
                        pos += 1
    return pd.DataFrame({"issue_time": col_t[:pos], "county": np.array(names)[col_c[:pos]], "T": col_T[:pos],
                         "R": col_R[:pos], "m": col_m[:pos], "n": col_n[:pos], "nb": col_nb[:pos], "k": col_k[:pos]})


def nll(kappa, n, hit):
    """伯努利負對數概似，P = 1 − exp(−n/κ)。"""
    p = np.clip(-np.expm1(-n / kappa), 1e-12, 1 - 1e-12)
    return -float(np.sum(hit * np.log(p) + (~hit) * np.log1p(-p)))


def fit_kappa(n, hit, bounds=(0.3, 20.0)):
    return float(minimize_scalar(nll, bounds=bounds, args=(n, hit), method="bounded").x)


def scores(df, kappa):
    """平均機率、實際比例、log loss、Brier（模型與平常各一組）。"""
    hit = (df["k"] > 0).to_numpy()
    out = {"n_rows": len(df), "obs": float(hit.mean())}
    for col, tag in (("n", "full"), ("nb", "bg")):
        for kk, lab in ((1.0, "poisson"), (kappa, "kappa")):
            p = np.clip(-np.expm1(-df[col].to_numpy() / kk), 1e-12, 1 - 1e-12)
            out[f"{tag}_{lab}_mean"] = float(p.mean())
            out[f"{tag}_{lab}_logloss"] = float(-np.mean(hit * np.log(p) + (~hit) * np.log1p(-p)))
            out[f"{tag}_{lab}_brier"] = float(np.mean((p - hit) ** 2))
    return out


def by_condition(df, fn):
    return [{"m": m, "T": T, "R": R, **fn(g)} for (m, T, R), g in df.groupby(["m", "T", "R"])]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--step-days", type=int, default=1)
    ap.add_argument("--reuse", action="store_true", help="沿用已存的 train/test.parquet，只重做擬合與報告")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    params = lu.load_params(BASE_DIR)
    if args.reuse and (OUT / "train.parquet").exists():
        train, test = pd.read_parquet(OUT / "train.parquet"), pd.read_parquet(OUT / "test.parquet")
    else:
        test_end = (pd.Timestamp(params["catalog_end"]) - pd.Timedelta(days=max(HORIZONS))).normalize()
        train = build(*TRAIN, args.step_days)
        train.to_parquet(OUT / "train.parquet", index=False)
        test = build(TEST[0], test_end, args.step_days)
        test.to_parquet(OUT / "test.parquet", index=False)

    hit_tr = (train["k"] > 0).to_numpy()
    kappa = fit_kappa(train["n"].to_numpy(), hit_tr)
    kappa_bg = fit_kappa(train["nb"].to_numpy(), hit_tr)
    per_cond = by_condition(train, lambda g: {"kappa": fit_kappa(g["n"].to_numpy(), (g["k"] > 0).to_numpy()),
                                              "hits": int((g["k"] > 0).sum()),
                                              "cluster": float(g.loc[g["k"] > 0, "k"].mean())})
    test_all = scores(test, kappa)
    test_cond = by_condition(test, lambda g: scores(g, kappa))
    res = {"kappa": kappa, "kappa_bg_separate": kappa_bg, "train": list(TRAIN), "per_condition_train": per_cond,
           "test_overall": test_all, "test_by_condition": test_cond}
    (OUT / "kappa.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(json.dumps({"kappa": kappa, "kappa_bg_separate": kappa_bg, "test_overall": test_all}, indent=1))
    pd.DataFrame(per_cond).to_csv(OUT / "kappa_by_condition.csv", index=False)
    pd.DataFrame(test_cond).to_csv(OUT / "test_by_condition.csv", index=False)
    write_report(res, len(train), len(test))


def write_report(res, n_train, n_test):
    t = res["test_overall"]
    pc = pd.DataFrame(res["per_condition_train"])
    tc = pd.DataFrame(res["test_by_condition"])
    tc["po"], tc["ka"] = tc["full_poisson_mean"] / tc["obs"], tc["full_kappa_mean"] / tc["obs"]
    better = int((tc["full_poisson_logloss"] > tc["full_kappa_logloss"]).sum())
    L = ["# 機率換算的群聚修正（κ）", "", "> 由 `app/probability_calibration.py` 產生。重新執行會覆寫本檔。", "",
         "## 為什麼", "",
         "期望數 N 大致校準過，但地震成群發生，用卜瓦松 P = 1 − exp(−N) 換算「至少一次」會高估。"
         "改成複合卜瓦松 **P = 1 − exp(−N / κ)**（地震以群為單位發生，每群平均 κ 個；κ = 1 就是原本的公式）。", "",
         "## 怎麼估", "",
         f"只用訓練期 {res['train'][0]} ～ {res['train'][1]}：每天 UTC 00:00 發布、20 個縣市代表點、"
         f"規模 {'/'.join(f'{m:g}' for m in MAGS)}、時間 {'/'.join(f'{T:g}' for T in HORIZONS)} 天、半徑 "
         f"{'/'.join(f'{R:g}' for R in RADII)} km（{n_train:,} 列），對「之後真的有沒有發生」做伯努利最大概似。"
         "「現在」與「平常」用同一個 κ。", "",
         f"**κ = {res['kappa']:.3f}**（「平常」單獨估是 {res['kappa_bg_separate']:.3f}，接近，所以共用）。", "",
         "## 測試期檢驗（2015 起，沒有參與估計）", "",
         f"{n_test:,} 列，真的發生的比例 {t['obs'] * 100:.2f}%。", "",
         "| | 平均機率 | log loss | Brier |", "|---|---|---|---|",
         f"| 現在・卜瓦松 | {t['full_poisson_mean'] * 100:.2f}% | {t['full_poisson_logloss']:.4f} | {t['full_poisson_brier']:.4f} |",
         f"| 現在・κ | {t['full_kappa_mean'] * 100:.2f}% | {t['full_kappa_logloss']:.4f} | {t['full_kappa_brier']:.4f} |",
         f"| 平常・卜瓦松 | {t['bg_poisson_mean'] * 100:.2f}% | {t['bg_poisson_logloss']:.4f} | {t['bg_poisson_brier']:.4f} |",
         f"| 平常・κ | {t['bg_kappa_mean'] * 100:.2f}% | {t['bg_kappa_logloss']:.4f} | {t['bg_kappa_brier']:.4f} |", "",
         f"36 組條件中 {better} 組的 log loss 變好。各條件「預測平均 ÷ 實際比例」（1＝剛好）：", "",
         "| 規模 | 時間（天） | 半徑（km） | 卜瓦松 | κ |", "|---|---|---|---|---|"]
    L += [f"| M{r.m:g} | {r.T:g} | {r.R:g} | {r.po:.2f} | {r.ka:.2f} |" for r in tc.itertuples()]
    L += ["", "## 訓練期各條件單獨估的 κ（參考）", "", "| 規模 | 時間（天） | 半徑（km） | κ | 有發生的列 |", "|---|---|---|---|---|"]
    L += [f"| M{r.m:g} | {r.T:g} | {r.R:g} | {r.kappa:.2f} | {r.hits:,} |" for r in pc.itertuples()]
    L += ["", "## 限制", "",
          "- κ 隨條件變（訓練期 1.35–2.52，半徑、規模越大越大）；用單一 κ 是為了簡單、可解釋、少一些過度擬合的風險。"
          "殘差：M6 仍偏高約 1.1–1.3 倍（推測與 b 值偏低有關），短時間＋小半徑（1 天、25 km）偏低到約 0.7–0.8。",
          "- 縣市代表點都在陸上或近岸；外海的群聚可能不同。", "- 倍數（現在÷平常）受的影響小：兩者用同一個 κ。", ""]
    REPORT.write_text(chr(10).join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
