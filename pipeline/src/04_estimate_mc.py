"""分段完備規模（Mc）估計，產生「隨年份變動的目錄特徵門檻」（FR-B2-20、PROJECT_OVERVIEW 3.2）。

為什麼：GDMS 分析報告發現 Mc 隨年代變動（1990s ≈ 2.25、2010s ≈ 1.35），而且外海比本島高。
B3 目錄特徵原本全期間固定用 ML ≥ 3，2026-11-10 快速檢查發現東部外海 1990–2000 年代 Mc ≈ 3.3，
ML ≥ 3 在那裡不完整，而 ML ≥ 5.5 目標有一半在東部外海。

輸入：EQ_DATA_DIR 底下的 GDMS 原始目錄（earthquake_data/raw/GDMS地震目錄_*.csv、earthquake_data/GDMScatalog*.csv）。
    不用 events.parquet：它已經刪掉 ML < 2.0，估不出低於 2.2 的 Mc。
    篩選和特徵事件相同：研究區內、Gap ≤ 110 或未知；來源內去重。

方法（每個「年 × 區」）：
    MAXC：0.1 級距直方圖的眾數 + 0.2（Woessner & Wiemer 2005 的修正）
    GFT90：擬合度法（Wiemer & Wyss 2000），觀測與 GR 理論分布的差異 ≤ 10% 的最小 Mc；達不到時為缺值
    Mc = max(MAXC, GFT90)（保守）；自助法 200 次估 MAXC 的標準差

分區（粗略，用經緯度切）：東部外海 lon ≥ 121.8；西部外海 lon < 120.2；南部外海 lat < 21.9；其餘為本島。

特徵門檻（不偷看未來）：
    事件不到 MIN_EVENTS 的「年 × 區」往前併年（最多 5 年；外海事件少，常需要併年）。
    第 Y 年事件的門檻 = max(FLOOR, 第 Y−1 年各區 Mc 的最大值，取到 0.1 無條件進位)
    只用「之前」的目錄估 Mc，所以 t0 當年後面的事件不會影響當年的門檻。
    第一年（沒有前一年）用當年；B3 發布時間從 1991 起，1990 的事件全都在 t0 之前，不會洩漏。
    FLOOR = 3.0：維持原本設計（ML ≥ 3 在各年代可以比較），只在 Mc 較高的年份往上調。

輸出：
    docs/reports/catalog_mc/README.md、mc_by_year_zone.csv
    pipeline/configs/feature_min_ml_by_year.csv   （year, threshold, mc_max, zone）→ B3 設定檔的 feature_min_ml_by_year

用法：
    $env:EQ_DATA_DIR = "C:\\...\\破專題"
    python pipeline/src/04_estimate_mc.py
"""
import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path(__file__).resolve().parent
BASE_DIR = SRC.parents[1]
DATA_DIR = Path(os.environ.get("EQ_DATA_DIR", BASE_DIR))
CONFIG = BASE_DIR / "pipeline" / "configs" / "b3_baseline.json"
OUT_REPORT = BASE_DIR / "docs" / "reports" / "catalog_mc"
OUT_TABLE = BASE_DIR / "pipeline" / "configs" / "feature_min_ml_by_year.csv"

BIN = 0.1
MAXC_CORR = 0.2
GFT_R = 90.0
MIN_EVENTS = 200
N_BOOT = 200
GAP_MAX = 110
FLOOR = 3.0
MAX_POOL_YEARS = 5
ZONES = ["本島", "東部外海", "西部外海", "南部外海"]


def zone_of(lat, lon):
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    return np.where(lon >= 121.8, "東部外海",
                    np.where(lon < 120.2, "西部外海", np.where(lat < 21.9, "南部外海", "本島")))


def _bins(m):
    return np.round(np.floor(np.asarray(m, float) / BIN + 1e-6) * BIN, 1)


def mc_maxc(m):
    """最大曲率法：級距直方圖的眾數 + 0.2。"""
    b = _bins(m)
    vals, cnt = np.unique(b, return_counts=True)
    return round(float(vals[cnt.argmax()]) + MAXC_CORR, 1)


def b_value(m, mc):
    """Aki–Utsu 最大概似 b 值（含級距修正）。"""
    m = np.asarray(m, float)
    m = m[m >= mc - 1e-9]
    if len(m) < 2:
        return np.nan
    mean = m.mean()
    return float(np.log10(np.e) / (mean - (mc - BIN / 2)))


def mc_gft(m, r_target=GFT_R, min_n=50):
    """擬合度法：從低往高試 Mc，第一個讓 R ≥ r_target 的值；都達不到時回傳 NaN。"""
    b = _bins(m)
    lo = float(np.min(b)) if len(b) else np.nan
    for mc in np.round(np.arange(lo, float(np.max(b)) if len(b) else lo, BIN), 1):
        sel = b[b >= mc - 1e-9]
        if len(sel) < min_n:
            break
        bv = b_value(sel, mc)
        if not np.isfinite(bv) or bv <= 0:
            continue
        edges = np.round(np.arange(mc, sel.max() + BIN / 2, BIN), 1)
        obs = np.array([(np.abs(sel - e) < 1e-6).sum() for e in edges], float)
        a = np.log10(len(sel)) + bv * mc            # 累積 GR：log10 N(≥M) = a − bM
        cum = 10 ** (a - bv * edges)
        syn = cum - np.append(cum[1:], 0.0)
        r = 100.0 - 100.0 * np.abs(obs - syn).sum() / obs.sum()
        if r >= r_target:
            return float(mc)
    return np.nan


def estimate(m, rng=None, n_boot=N_BOOT):
    """回傳 dict：n、MAXC、GFT90、Mc、MAXC 自助法標準差、b 值。"""
    m = np.asarray(m, float)
    out = {"n": int(len(m)), "mc_maxc": np.nan, "mc_gft90": np.nan, "mc": np.nan, "mc_maxc_sd": np.nan,
           "b_value": np.nan}
    if len(m) < MIN_EVENTS:
        return out
    out["mc_maxc"] = mc_maxc(m)
    out["mc_gft90"] = mc_gft(m)
    out["mc"] = float(np.nanmax([out["mc_maxc"], out["mc_gft90"]]))
    rng = rng or np.random.default_rng(0)
    boots = [mc_maxc(rng.choice(m, len(m))) for _ in range(n_boot)]
    out["mc_maxc_sd"] = round(float(np.std(boots)), 2)
    out["b_value"] = round(b_value(m, out["mc"]), 3)
    return out


def mc_table(events, seed=0, max_pool=MAX_POOL_YEARS):
    """每個「年 × 區」的 Mc。events 需有 time、lat、lon、ml。

    外海事件少（Gap 篩選後每區每年常不到 200 筆），所以事件不夠時往前多併幾年：
    第 Y 年用 [Y−w+1, Y] 的事件，w 從 1 加到 max_pool，取第一個事件數足夠的 w（只往前併，不用未來）。
    """
    rng = np.random.default_rng(seed)
    e = events.assign(year=pd.to_datetime(events["time"]).dt.year, zone=zone_of(events["lat"], events["lon"]))
    rows = []
    years = sorted(e["year"].unique())
    for z in ZONES + ["全區"]:
        ez = e if z == "全區" else e[e["zone"] == z]
        by_year = {y: g["ml"].to_numpy() for y, g in ez.groupby("year")}
        for y in years:
            for w in range(1, max_pool + 1):
                m = np.concatenate([by_year.get(k, np.empty(0)) for k in range(y - w + 1, y + 1)])
                if len(m) >= MIN_EVENTS:
                    break
            rows.append({"year": int(y), "zone": z, "pool_years": w, **estimate(m, rng)})
    return pd.DataFrame(rows).sort_values(["year", "zone"]).reset_index(drop=True)


def thresholds(tab, floor=FLOOR):
    """第 Y 年的特徵門檻：第 Y−1 年各區 Mc（已往前併年）的最大值，無條件進位到 0.1，不低於 floor。

    只用 Y−1 以前的事件，所以 t0 當年後面的事件不會影響當年的門檻。第一年用當年。
    """
    z = tab[tab["zone"] != "全區"]
    years = sorted(z["year"].unique())
    rows = []
    for y in years:
        src = y - 1 if (y - 1) in years else y
        win = z[z["year"] == src]
        if win["mc"].notna().any():
            k = win["mc"].idxmax()
            mc_max, zone, pool = float(win.loc[k, "mc"]), win.loc[k, "zone"], int(win.loc[k, "pool_years"])
            basis = f"{src - pool + 1}–{src}" if pool > 1 else f"{src}"
        else:
            mc_max, zone, basis = np.nan, "", ""
        if src == y:
            basis += "（第一年）"
        thr = floor if not np.isfinite(mc_max) else max(floor, np.ceil(mc_max * 10 - 1e-6) / 10)
        rows.append({"year": int(y), "threshold": round(float(thr), 1), "mc_max": mc_max,
                     "zone": zone, "basis_years": basis})
    return pd.DataFrame(rows)


def load_gdms(data_dir, region):
    spec = importlib.util.spec_from_file_location("build_catalog", SRC / "01_build_catalog.py")
    bc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bc)
    files = sorted((Path(data_dir) / "earthquake_data" / "raw").glob("GDMS地震目錄_*.csv"))
    files += sorted((Path(data_dir) / "earthquake_data").glob("GDMScatalog*.csv"))
    if not files:
        raise SystemExit(f"找不到 GDMS 目錄 CSV，請設定 EQ_DATA_DIR（目前 {data_dir}）")
    df = pd.concat([bc.parse_gdms_csv(f) for f in files], ignore_index=True).dropna(subset=["time", "lat", "lon", "ml"])
    df = bc.dedup_within_source(df)
    r = region
    df = df[(df["lat"] >= r["lat_min"]) & (df["lat"] < r["lat_max"]) &
            (df["lon"] >= r["lon_min"]) & (df["lon"] < r["lon_max"])]
    df = df[df["gap"].isna() | (df["gap"] <= GAP_MAX)]
    return df, files


def _md(df):
    head = "| " + " | ".join(map(str, df.columns)) + " |\n|" + "---|" * len(df.columns) + "\n"
    return head + "".join("| " + " | ".join("" if pd.isna(v) else str(v) for v in r) + " |\n"
                          for r in df.itertuples(index=False))


def render(tab, thr, files, n_events, events_per_year=None):
    pv = tab.pivot(index="year", columns="zone", values="mc").reindex(columns=["全區"] + ZONES)
    pv = pv.map(lambda v: "" if pd.isna(v) else (f"**{v:.1f}**" if v > FLOOR else f"{v:.1f}"))
    pv.insert(0, "年", pv.index)
    era = tab[tab["zone"] != "全區"].assign(era=lambda d: pd.cut(d["year"], [1989, 1999, 2009, 2017, 2026],
                                                                 labels=["1990s", "2000s", "2010–17", "2018–26"]))
    above = (thr["threshold"] > FLOOR).sum()
    raw = events_per_year if events_per_year is not None else pd.DataFrame(columns=ZONES)
    counts = (raw.groupby(pd.cut(raw.index, [1989, 1999, 2009, 2017, 2026],
                                 labels=["1990s", "2000s", "2010–17", "2018–26"]), observed=True).sum()
              .reindex(columns=ZONES).fillna(0).astype(int))
    counts.insert(0, "年代", counts.index.astype(str))
    L = ["# 分段完備規模（Mc）\n\n",
         f"> 由 `pipeline/src/04_estimate_mc.py` 產生（{pd.Timestamp.now():%Y-%m-%d}）。對應 FR-B2-20（PROJECT_OVERVIEW 3.2）。\n\n",
         "## 1. 結論\n\n",
         f"- 目錄特徵門檻改成**隨年份變動**：{len(thr)} 年中有 {above} 年要高於 ML {FLOOR}，"
         f"最高 {thr['threshold'].max():.1f}（見 `pipeline/configs/feature_min_ml_by_year.csv`）。\n",
         "- 第 Y 年的門檻由第 Y−1 年（事件不夠時往前併到最多 5 年）各區 Mc 的最大值決定，不使用當年之後的資料。\n",
         f"- 粗體 = Mc 高於原本的固定門檻 {FLOOR}：這些「年 × 區」的 ML ≥ {FLOOR} 事件不完整。\n\n",
         "## 2. 資料與方法\n\n",
         f"- GDMS 原始目錄 {len(files)} 個檔，研究區內、Gap ≤ {GAP_MAX} 或未知、來源內去重後 {n_events:,} 筆（**不**先刪 ML < 2）。\n",
         f"- MAXC = 0.1 級距直方圖的眾數 + {MAXC_CORR}；GFT90 = 擬合度 R ≥ {GFT_R:.0f}% 的最小 Mc（Wiemer & Wyss 2000）。"
         "Mc = 兩者較大者。某「年 × 區」事件不到 200 筆時往前併年（最多 5 年，見 CSV 的 `pool_years`），仍不夠則不估。\n",
         "- 分區（粗略）：東部外海 lon ≥ 121.8；西部外海 lon < 120.2；南部外海 lat < 21.9；其餘為本島。"
         "東部外海的界線會包到宜蘭、花蓮海岸的一小部分陸地。\n",
         f"- MAXC 自助法 {N_BOOT} 次的標準差見 CSV 的 `mc_maxc_sd`，多數在 0.1 左右。\n\n",
         "## 3. 每年 × 區的 Mc\n\n", _md(pv.reset_index(drop=True)),
         "\n## 4. 各年代中位數\n\n",
         _md(era.groupby(["era", "zone"], observed=True)["mc"].median().unstack().reindex(columns=ZONES)
             .round(2).reset_index().rename(columns={"era": "年代"})),
         "\n## 5. 各年代的事件數（Gap ≤ 110 或未知，全部規模）\n\n",
         _md(counts),
         "\n**外海早期事件太少**：1990 年代東部外海通過 Gap ≤ 110 的事件十年不到 30 筆，"
         "併 5 年也估不出 Mc。這時外海的問題不是「Mc 偏高」，而是**定位品質篩選把大部分外海事件刪掉了**，"
         "影響比 Mc 大得多。目錄特徵用同一套篩選，所以早期外海網格的特徵主要來自鄰近的陸地事件；"
         "B3 用「占全區比例」特徵、目標事件不看 Gap，就是為了減輕這個偏差。\n",
         "\n## 6. 每年的特徵門檻\n\n",
         _md(thr.rename(columns={"year": "年", "threshold": "門檻", "mc_max": "依據的最大 Mc", "zone": "來自",
                                 "basis_years": "依據年份"})),
         "\n## 7. 限制\n\n",
         "- MAXC 在事件少或分布不平滑時會低估 Mc，所以加 0.2，並取和 GFT90 的較大者。\n",
         "- 分區是經緯度直線切，不是依測網幾何；更細的空間 Mc（例如 0.5° 格）事件數不夠，不估。\n",
         "- 門檻隨年份改變後，同一時間點的各網格仍用同一個門檻，「占全區比例」特徵才可以比較。"
         "只有視窗跨年（例如 365 天）時，一個視窗內會混到兩個門檻。\n"]
    return "".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    ap.add_argument("--out-report", default=str(OUT_REPORT))
    ap.add_argument("--out-table", default=str(OUT_TABLE))
    args = ap.parse_args()

    region = json.loads(CONFIG.read_text(encoding="utf-8"))["region"]
    ev, files = load_gdms(args.data_dir, region)
    print(f"GDMS {len(files)} 個檔；研究區、Gap 篩選、去重後 {len(ev):,} 筆，"
          f"{ev['time'].min():%Y-%m-%d} ～ {ev['time'].max():%Y-%m-%d}")
    tab = mc_table(ev)
    thr = thresholds(tab)
    out = Path(args.out_report)
    out.mkdir(parents=True, exist_ok=True)
    tab.to_csv(out / "mc_by_year_zone.csv", index=False, encoding="utf-8-sig")
    thr.to_csv(args.out_table, index=False, encoding="utf-8")
    per_year = (ev.assign(year=ev["time"].dt.year, zone=zone_of(ev["lat"], ev["lon"]))
                .pivot_table(index="year", columns="zone", values="ml", aggfunc="size", fill_value=0))
    (out / "README.md").write_text(render(tab, thr, files, len(ev), per_year), encoding="utf-8")
    print(tab.pivot(index="year", columns="zone", values="mc").to_string())
    print(thr[["year", "threshold", "mc_max", "zone"]].to_string(index=False))
    print(f"報告：{out / 'README.md'}；門檻表：{args.out_table}")


if __name__ == "__main__":
    main()
