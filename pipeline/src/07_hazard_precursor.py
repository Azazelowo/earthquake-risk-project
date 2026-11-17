"""檢驗地磁特徵能不能讓風險函數更準（全部規模＋分規模），並做循環時間平移檢定。

做法見 eqforecast/modifier.py 開頭。流程：
1. 讀 06_hazard_model.py 估好的參數，對每個發布時間算風險函數的期望次數 N0（含滾動校準）。
2. 從 features_magnet.parquet 取每格在發布時間「之前」7 天與 30 天的平均 z 值。
3. 訓練期擬合「只有截距」與「截距＋地磁」，在測試期比較每個地震的資訊增益。
4. 把地磁特徵沿時間整段循環平移後重做，得到「碰巧」的分布與 p 值。
5. 規模門檻 M≥3.5（主要檢定，事先指定）、4、5、5.5 各做一次；另外三種地磁指標各自單獨檢定。

輸出：pipeline/data/hazard/precursor_mag.csv、docs/reports/hazard_precursor_mag/README.md
執行：python pipeline/src/07_hazard_precursor.py --features <features_magnet.parquet>
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import safe_console  # noqa: E402,F401  Windows cp950 終端機遇到 ⚠、≥ 等字元時不會中斷
from eqforecast import hazard as hz  # noqa: E402
from eqforecast import modifier as md  # noqa: E402

BASE_DIR = Path(__file__).resolve().parents[2]
DATA = BASE_DIR / "pipeline" / "data"
REPORT = BASE_DIR / "docs" / "reports" / "hazard_precursor_mag"

COLS = ["mag_sznet_z", "mag_szres_z", "mag_pol_z"]
COL_LABEL = {"mag_sznet_z": "相對其他測站", "mag_szres_z": "扣除 Kp、Dst", "mag_pol_z": "極化比"}
LOOKBACKS = [7, 30]
MAGS = [3.5, 4.0, 5.0, 5.5]
HORIZON = 7.0
CLIP_Z = 8.0                  # 每日 z 值先截在 ±8，避免單日極端值主導平均


def daily_cube(feat, grid, cols):
    """網格日特徵 → (天, 格, 欄) 的陣列與對應日期。"""
    t = pd.to_datetime(feat["time_bin"]).dt.normalize()
    days = pd.date_range(t.min(), t.max(), freq="D")
    i = np.rint((feat["lat_bin"].to_numpy() - grid.lat_min) / grid.deg).astype(int)
    j = np.rint((feat["lon_bin"].to_numpy() - grid.lon_min) / grid.deg).astype(int)
    ni, nj = grid.shape
    ok = (i >= 0) & (i < ni) & (j >= 0) & (j < nj)
    d = ((t - days[0]).dt.days).to_numpy()
    cube = np.full((len(days), ni * nj, len(cols)), np.nan)
    for k, c in enumerate(cols):
        cube[d[ok], (i * nj + j)[ok], k] = np.clip(feat[c].to_numpy(float)[ok], -CLIP_Z, CLIP_Z)
    return cube, days


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", default=str(DATA / "events.parquet"))
    ap.add_argument("--features", default=str(DATA / "features_magnet.parquet"))
    ap.add_argument("--params", default=str(DATA / "hazard" / "params.json"))
    ap.add_argument("--start", default="2020-11-01")
    ap.add_argument("--split", default="2024-01-01", help="之前為訓練、之後為測試")
    ap.add_argument("--n-shifts", type=int, default=200)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--event-quality", choices=hz.EVENT_QUALITIES, default=None,
                    help="預設沿用 params.json 記錄的事件母體，讓前兆檢定和風險函數用同一批地震")
    args = ap.parse_args()

    meta = json.loads(Path(args.params).read_text(encoding="utf-8"))
    # params.json 沒有記錄母體的（v1）是用全部事件擬合的，要沿用同一個母體，檢定才公平
    quality = args.event_quality or meta.get("event_quality", {}).get("quality", "all")
    if "event_quality" not in meta and args.event_quality is None:
        print("注意：params.json 是 v1 產生的（沒有記錄事件母體），沿用 all。建議先重跑 06_hazard_model.py。")
    events, ev_info = hz.load_events(args.events, quality)
    print(f"事件：{ev_info['quality']}，使用 {ev_info['n_used']:,} / {ev_info['n_all']:,} 筆")
    prm = hz.HazardParams(**meta["params"])
    grid = hz.Grid()
    cat = hz.Catalog(events, grid, prm.m_ref)
    feat = pd.read_parquet(args.features, columns=["lat_bin", "lon_bin", "time_bin"] + COLS)
    cube, days = daily_cube(feat, grid, COLS)

    end = min(events["time"].max().normalize(), days[-1] + pd.Timedelta(days=1))
    t0s = hz.issue_times(args.start, end, HORIZON)
    h = np.timedelta64(int(HORIZON * 86400), "s")
    t0s = t0s[(t0s + h <= np.datetime64(end, "us")) & (t0s >= np.datetime64(days[0] + pd.Timedelta(days=max(LOOKBACKS)), "us"))]
    T, C = len(t0s), grid.shape[0] * grid.shape[1]
    print(f"發布時間 {T} 個（{pd.Timestamp(t0s[0]).date()} ～ {pd.Timestamp(t0s[-1]).date()}），每個 {HORIZON:g} 天、{C} 格")

    n0 = np.empty((T, C))
    for k, t in enumerate(t0s):
        a, b = hz.expected_ref(cat, t, HORIZON, prm)
        n0[k] = (a + b).ravel() * hz.trailing_factor(cat, t, HORIZON, prm)
    # 發布時間對應到每日陣列的列：第 d 列的滾動平均只用到 d 之前的日子
    row = ((pd.DatetimeIndex(t0s).normalize() - days[0]).days).to_numpy()
    feats = np.concatenate([md.rolling_before(cube, L)[row] for L in LOOKBACKS], axis=2)   # (T, C, 欄×視窗)
    names = [f"{c}｜前 {L} 天" for L in LOOKBACKS for c in COLS]
    is_train = t0s < np.datetime64(args.split, "us")
    covered = np.isfinite(feats).all(axis=2)
    print(f"有完整地磁特徵的樣本：{covered.mean() * 100:.1f}%（訓練 {covered[is_train].sum()}、測試 {covered[~is_train].sum()} 個格窗）")

    rows, betas = [], {}
    tests = [(m, "全部 6 個特徵", list(range(len(names)))) for m in MAGS]
    tests += [(MAGS[0], f"只用「{COL_LABEL[c]}」", [k for k, n in enumerate(names) if n.startswith(c)]) for c in COLS]
    for m, label, idx in tests:
        y = np.stack([cat.counts(t, t + h, min_ml=m) for t in t0s]).reshape(T, C)
        off = np.log(np.clip(hz.scale_magnitude(n0, m, prm.m_ref, prm.b), 1e-300, None))
        real, null, p = md.circular_shift_test(feats[:, :, idx], y, off, is_train, ~is_train,
                                               n_shifts=args.n_shifts, seed=args.seed)
        r = {"m": m, "features": label, "train_events": real["n_train_events"], "test_events": real["n_test_events"],
             "gain_test": real["gain"], "gain_train": real.get("train_gain", np.nan),
             "null_median": float(np.median(null)) if len(null) else np.nan,
             "null_p95": float(np.quantile(null, 0.95)) if len(null) else np.nan, "n_shifts": len(null), "p": p}
        rows.append(r)
        if real["beta"] is not None:
            betas[(m, label)] = dict(zip(["截距"] + [names[k] for k in idx], np.round(real["beta"], 4)))
        print(f"M≥{m:g}｜{label}：測試地震 {r['test_events']}、增益 {r['gain_test']:+.4f}"
              f"（平移中位數 {r['null_median']:+.4f}、95% {r['null_p95']:+.4f}）p = {p:.3f}")
    res = pd.DataFrame(rows)
    out = Path(args.params).parent
    res.to_csv(out / "precursor_mag.csv", index=False)

    def f(v, nd=4):
        return "—" if pd.isna(v) else f"{v:+.{nd}f}"

    L = ["# 地磁能不能讓風險函數更準：檢定報告", "",
         f"> 由 `pipeline/src/07_hazard_precursor.py` 產生（{pd.Timestamp.now():%Y-%m-%d %H:%M}）。重新執行會覆寫本檔。", "",
         "## 問題與做法", "",
         "把地磁特徵當成風險函數發生率的倍率，看測試期的預報有沒有變準。樣本是全部 M≥3.5 的地震（不是只有大地震），",
         "所以有足夠的數量做檢定；再把規模門檻提高，檢查「只有大地震才看得到地磁影響」的猜想。", "",
         f"- 預報窗 {HORIZON:g} 天；訓練 {pd.Timestamp(t0s[0]).date()} ～ {args.split}，測試 {args.split} ～ {pd.Timestamp(t0s[-1]).date()}",
         f"- 事件母體：{ev_info['quality']}（{ev_info['n_used']:,} / {ev_info['n_all']:,} 筆），和風險函數的參數一致",
         f"- 特徵：每格在發布時間之前 7 天、30 天的平均 z 值（{'、'.join(COL_LABEL.values())}），共 {len(names)} 個",
         f"- 有完整地磁特徵的格窗：{covered.mean() * 100:.1f}%（其餘離測站超過 150 km 或資料不足，不納入）",
         "- **資訊增益**：每個測試期地震平均多得到的資訊（nat）。0 = 和沒加地磁一樣；負值 = 加了反而變差。",
         f"- **p 值**：把地磁特徵沿時間整段循環平移 {args.n_shifts} 次後重做，平移結果不輸真實結果的比例。"
         "p 小於 0.05 才算不像碰巧。", "",
         "## 結果", "",
         "| 規模 | 特徵 | 訓練地震 | 測試地震 | 測試增益 | 訓練增益 | 平移中位數 | 平移 95% | p |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in res.itertuples():
        L.append(f"| M≥{r.m:g} | {r.features} | {r.train_events} | {r.test_events} | {f(r.gain_test)} | {f(r.gain_train)} | "
                 f"{f(r.null_median)} | {f(r.null_p95)} | {'—' if pd.isna(r.p) else f'{r.p:.3f}'} |")
    L += ["", "第一列（M≥3.5、全部特徵）是事先指定的主要檢定；其餘各列是輔助，共做了 "
          f"{len(res)} 個檢定，看到個別的小 p 值時要考慮多重比較。", "",
          "## 係數（標準化後；正值代表該特徵越高、發生率越高）", ""]
    for (m, label), b in betas.items():
        L.append(f"- M≥{m:g}｜{label}：" + "、".join(f"{k} {v:+.3f}" for k, v in b.items()))
    L += ["", "## 限制", "",
          "1. 地磁只有 3–5 個測站，每格取最近的測站，所以同一測站涵蓋的格子特徵幾乎相同，獨立資訊比格窗數少得多。",
          "2. 測試期包含 2024-04 花蓮地震序列，地震高度群聚；增益可能被少數幾週主導。循環平移保留了這個結構，"
          "所以 p 值仍可參考，但增益的大小不穩定。",
          "3. 風險函數本身的偏差（例如餘震期低估）可能被任何有時間結構的特徵「撿到」，這不代表該特徵是前兆。",
          "4. 「相對其他測站」會扣掉涵蓋全台的訊號；用遠端參考站（鹿屋）的版本還沒做。",
          "5. 花蓮、台東測站不能用，東部外海離測站遠，覆蓋不到。", ""]
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "README.md").write_text("\n".join(L), encoding="utf-8")
    print(f"報告：{REPORT / 'README.md'}")


if __name__ == "__main__":
    main()
