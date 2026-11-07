"""個案分析：NDH5 GNSS 站的 TEC，2024-04-03 花蓮地震前後。

⚠ 這是單一測站、單一事件的展示案例，不能當作「TEC 前兆存在」的證據。
  要回答前兆是否有效，必須用多個事件並和基準模型比較（B3 框架），不能只看單一事件。

流程：
    RINEX 觀測檔（地震預測/測地法/GNSS_ETEC/*.YYo）
      → 廣播星曆與衛星 DCB（BKG 下載，--download）
      → 斜向 TEC、相位平滑、衛星 DCB 修正（符號由資料決定）
      → 接收機偏差（最小標準差法 + 平面梯度，當地夜間）
      → 垂直 TEC（仰角 ≥ 30°）→ 15 分鐘序列
      → 背景值：前 15 天中位數 ± 1.5 IQR；前期不足時改用「其他所有可用天」（暫定）
      → 與 Kp、Dst 並列，排除磁暴干擾
輸出：
    pipeline/data/tec/ndh5_vtec_15min.parquet（與 .csv）
    docs/reports/case_ndh5_2024hualien/README.md 與圖

用法：
    python pipeline/src/case_ndh5_tec.py --download
"""
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eqforecast.decluster import haversine_km  # noqa: E402
from gnss_tec import aux_data, orbit, rinex, tec  # noqa: E402

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("EQ_DATA_DIR", BASE_DIR))
RINEX_DIR = DATA_DIR / "地震預測" / "測地法" / "GNSS_ETEC"
AUX_DIR = DATA_DIR / "地震預測" / "測地法" / "aux"
OUT_DIR = BASE_DIR / "pipeline" / "data" / "tec"
REP_DIR = BASE_DIR / "docs" / "reports" / "case_ndh5_2024hualien"

EQ_TIME_UTC = pd.Timestamp("2024-04-02 23:58:09")   # 以 events.parquet 中的 GDMS 紀錄為準，執行時會再核對
# 接收機偏差的估計時段：當地黎明前 04:00–06:59（UTC 20–22 時）。
# 2024 年接近太陽極大期，當地午夜前後常有電漿不規則結構，用 00–06 時估計的逐日全距達 14 TECU；
# 黎明前電離層最平滑，逐日全距約 4 TECU。兩者都會在報告中列出比較。
NIGHT_UTC_HOURS = range(20, 23)
BIAS_WINDOWS = {"當地 00–06 時": range(16, 22), "當地 02–06 時": range(18, 22), "當地 04–07 時（採用）": range(20, 23)}
REF_DAYS = 15
MIN_REF_DAYS = 10
IQR_K = 1.5
FREQ = "15min"


def rinex_date(path):
    """由 RINEX 2 檔名 ssssDDDf.YYo 取得日期。"""
    name = path.name[:-3] if path.name.endswith(".gz") else path.name
    doy, yy = int(name[4:7]), int(name[-3:-1])
    return pd.Timestamp(year=2000 + yy, month=1, day=1) + pd.Timedelta(days=doy - 1)


def process_day(path, download, log):
    date = rinex_date(path)
    if download:
        nav_p, dcb_p, msgs = aux_data.fetch_day(date, AUX_DIR)
        log += msgs
    else:
        nav_p = AUX_DIR / "brdc" / aux_data.brdc_url(date).rsplit("/", 1)[1]
        dcb_p = AUX_DIR / "dcb" / aux_data.cas_dcb_url(date).rsplit("/", 1)[1]
    nav = rinex.read_gps_nav(nav_p) if nav_p and Path(nav_p).exists() else None
    if nav is None and download:
        # BKG 有些日子沒有 RINEX 2 星曆（例如 2024-03-22），改用多來源的 RINEX 3 混合星曆
        nav, msg = aux_data.get_nav(date, AUX_DIR)
        log.append(msg)
    if nav is None or not (dcb_p and Path(dcb_p).exists()):
        log.append(f"{path.name}: 缺少星曆或 DCB，略過（請加 --download）")
        return None
    h, obs = rinex.read_obs(path)
    dsb = aux_data.read_cas_dsb(dcb_p)
    st = tec.slant_tec(obs, nav, h["approx_xyz"])
    # 衛星 DCB 符號：兩種都試，取全天衛星間殘差較小者（全天資料一定有，區別力也足夠），兩者都記錄
    sign_check = {}
    for sign in (+1, -1):
        s = tec.apply_sat_dcb(st, dsb, sign)
        _, rs, n = tec.estimate_receiver_bias(s, hours_utc=None)
        sign_check[sign] = {"resid": rs, "n_epochs": n}
    sign = min(sign_check, key=lambda k: np.inf if np.isnan(sign_check[k]["resid"]) else sign_check[k]["resid"])
    s = tec.apply_sat_dcb(st, dsb, sign)
    bias, resid, n_ep = tec.estimate_receiver_bias(s, hours_utc=NIGHT_UTC_HOURS)
    window_check = {name: tec.estimate_receiver_bias(s, hours_utc=hrs)[:2] for name, hrs in BIAS_WINDOWS.items()}
    return {
        "window_check": window_check,
        "date": date, "file": path.name, "header": h, "slant": s, "sign": sign, "sign_check": sign_check,
        "first_obs": obs["time"].min(), "last_obs": obs["time"].max(),
        "n_obs_epochs": int(obs["time"].nunique()),
        "bias": bias, "resid": resid, "bias_epochs": n_ep,
    }


def reference(series, exclude=(), min_days=MIN_REF_DAYS):
    """每個 15 分鐘格的背景中位數與 IQR。回傳加上 ref_med、ref_iqr、ref_mode、n_ref 的表。

    exclude：不當背景的日子（例如地磁擾動日）。
    """
    s = series.copy()
    s["date"] = s.index.floor("D")
    s["slot"] = s.index - s["date"]
    table = s.pivot_table(index="date", columns="slot", values="vtec")
    days = list(table.index)
    meds, iqrs, modes, nref = [], [], [], []
    for d in days:
        prev = [x for x in days if d - pd.Timedelta(days=REF_DAYS) <= x < d and x not in exclude]
        if len(prev) >= min_days:
            ref_days, mode = prev, f"前 {REF_DAYS} 天"
        else:
            ref_days, mode = [x for x in days if x != d and x not in exclude], "其他所有可用天（暫定）"
        ref = table.loc[ref_days]
        q1, med, q3 = ref.quantile(0.25), ref.median(), ref.quantile(0.75)
        meds.append(med)
        iqrs.append(q3 - q1)
        modes.append(mode)
        nref.append(len(ref_days))
    med = pd.DataFrame(meds, index=days)
    iqr = pd.DataFrame(iqrs, index=days)
    # 整天都沒有有效 VTEC 的日子不會出現在 pivot 表裡，查不到就給缺值
    def look(tab, d, sl):
        return tab.at[d, sl] if d in tab.index and sl in tab.columns else np.nan
    s["ref_med"] = [look(med, d, sl) for d, sl in zip(s["date"], s["slot"])]
    s["ref_iqr"] = [look(iqr, d, sl) for d, sl in zip(s["date"], s["slot"])]
    mode_map = dict(zip(days, modes))
    nref_map = dict(zip(days, nref))
    s["ref_mode"] = s["date"].map(mode_map)
    s["n_ref_days"] = s["date"].map(nref_map)
    s["dvtec"] = s["vtec"] - s["ref_med"]
    s["upper"] = s["ref_med"] + IQR_K * s["ref_iqr"]
    s["lower"] = s["ref_med"] - IQR_K * s["ref_iqr"]
    s["anom"] = np.where(s["vtec"] > s["upper"], 1, np.where(s["vtec"] < s["lower"], -1, 0))
    s.loc[s["vtec"].isna() | s["ref_med"].isna(), "anom"] = 0
    return s.drop(columns=["slot"])


def anomaly_runs(s, min_bins=4):
    """連續 ≥ min_bins 個 15 分鐘格（預設 1 小時）同方向超出界線的時段。"""
    runs = []
    a = s["anom"].to_numpy()
    idx = s.index
    k = 0
    while k < len(a):
        if a[k] == 0:
            k += 1
            continue
        j = k
        while j + 1 < len(a) and a[j + 1] == a[k] and idx[j + 1] - idx[j] == pd.Timedelta(FREQ):
            j += 1
        if j - k + 1 >= min_bins:
            seg = s.iloc[k:j + 1]
            runs.append({"start_utc": idx[k], "end_utc": idx[j] + pd.Timedelta(FREQ),
                         "sign": "正（偏高）" if a[k] > 0 else "負（偏低）",
                         "hours": (j - k + 1) / 4, "max_abs_dvtec": float(seg["dvtec"].abs().max())})
        k = j + 1
    return pd.DataFrame(runs)


def main():
    ap = argparse.ArgumentParser(description="NDH5 TEC 個案分析")
    ap.add_argument("--download", action="store_true", help="下載缺少的廣播星曆與 DCB")
    ap.add_argument("--rinex-dir", default=str(RINEX_DIR),
                    help="NDH5 觀測檔資料夾（.YYo 或 .YYo.gz），例如 E:\123\GNSS_ETEC")
    args = ap.parse_args()

    rdir = Path(args.rinex_dir)
    files = sorted(list(rdir.glob("ndh5*.[0-9][0-9]o")) + list(rdir.glob("ndh5*.[0-9][0-9]o.gz")),
                   key=lambda f: rinex_date(f))
    if not files:
        sys.exit(f"{rdir} 沒有 NDH5 的 RINEX 觀測檔")
    log = []
    days = [d for d in (process_day(f, args.download, log) for f in files) if d is not None]
    print("\n".join(log))
    h = days[0]["header"]
    rx_lat, rx_lon, rx_h = orbit.ecef_to_geodetic(*h["approx_xyz"])

    # 接收機偏差：各日估計值的中位數（假設一週內不變，並檢查穩定度）
    daily_bias = pd.DataFrame([{k: d[k] for k in ("date", "file", "sign", "bias", "resid", "bias_epochs",
                                                   "first_obs", "last_obs", "n_obs_epochs")} for d in days])
    rx_bias = float(daily_bias["bias"].median())
    vt = pd.concat([tec.vertical_tec(d["slant"], rx_bias) for d in days], ignore_index=True)
    series = tec.station_series(vt, FREQ)
    s = reference(series)
    runs = anomaly_runs(s)
    sens = None

    # 太空天氣
    sw_path = BASE_DIR / "pipeline" / "data" / "space_weather_hourly.parquet"
    sw = pd.read_parquet(sw_path) if sw_path.exists() else None
    t_lo, t_hi = s.index.min().floor("D"), s.index.max().ceil("D")
    if sw is not None:
        sw = sw[(sw["time"] >= t_lo - pd.Timedelta(days=3)) & (sw["time"] < t_hi)]
        swd = sw.assign(date=sw["time"].dt.floor("D")).groupby("date").agg(
            kp_max=("kp_gfz", "max"), dst_min=("dst_nT", "min"), f107=("f107_omni", "mean"))

    # 地震資訊（從 events.parquet 核對）
    ev_path = BASE_DIR / "pipeline" / "data" / "events.parquet"
    eq = None
    if ev_path.exists():
        ev = pd.read_parquet(ev_path)
        near = ev[(ev["time"] - EQ_TIME_UTC).abs() < pd.Timedelta(minutes=5)].sort_values("ml", ascending=False)
        if not near.empty:
            eq = near.iloc[0]
    eq_time = eq["time"] if eq is not None else EQ_TIME_UTC
    eq_dist = float(haversine_km(rx_lat, rx_lon, eq["lat"], eq["lon"])) if eq is not None else np.nan

    # 每日超出界線比例（判斷地震前是否「特別」：和其他日子比）
    s["day"] = s.index.floor("D")
    daily = s.groupby("day").agg(
        bins=("vtec", "count"),
        frac_above=("anom", lambda a: float((a == 1).mean())),
        frac_below=("anom", lambda a: float((a == -1).mean())),
        max_dvtec=("dvtec", "max"), min_dvtec=("dvtec", "min"),
        ref_mode=("ref_mode", "first"), n_ref=("n_ref_days", "first"))
    if sw is not None:
        daily = daily.join(swd, how="left")
        # 敏感度：擾動日（Kp ≥ 4 或 Dst ≤ −30 nT）不當背景。寧靜日較少，最少 5 天即可（標示於報告）
        dist_days = set(swd.index[(swd["kp_max"] >= 4) | (swd["dst_min"] <= -30)])
        q = reference(series, exclude=dist_days, min_days=5)
        q["day"] = q.index.floor("D")
        sens = q.groupby("day").agg(
            frac_above=("anom", lambda a: float((a == 1).mean())),
            frac_below=("anom", lambda a: float((a == -1).mean())),
            max_dvtec=("dvtec", "max"), min_dvtec=("dvtec", "min"),
            ref_mode=("ref_mode", "first"), n_ref=("n_ref_days", "first"))
        sens["disturbed"] = sens.index.isin(dist_days)

    # ---------- 輸出 ----------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REP_DIR.mkdir(parents=True, exist_ok=True)
    out = s.reset_index().rename(columns={"time": "time_utc"})
    out.insert(1, "station", h.get("marker", "NDH5"))
    out.insert(2, "lat", rx_lat)
    out.insert(3, "lon", rx_lon)
    out.to_parquet(OUT_DIR / "ndh5_vtec_15min.parquet", index=False)
    out.to_csv(OUT_DIR / "ndh5_vtec_15min.csv", index=False, encoding="utf-8")
    vt.to_parquet(OUT_DIR / "ndh5_vtec_per_satellite.parquet", index=False)

    figs = make_figures(s, sw, eq_time, rx_bias)
    meta = {
        "station": h.get("marker"), "receiver": h.get("receiver"), "interval_s": h.get("interval"),
        "lat": rx_lat, "lon": rx_lon, "height_m": rx_h, "rx_bias_tecu": rx_bias,
        "eq": None if eq is None else {k: (str(eq[k]) if k == "time" else float(eq[k]))
                                       for k in ("time", "lat", "lon", "depth", "ml")},
        "eq_station_distance_km": eq_dist,
    }
    (OUT_DIR / "ndh5_case_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str),
                                                 encoding="utf-8")
    write_report(meta, daily_bias, daily, runs, s, figs, days, sens)
    print(f"接收機偏差（中位數）：{rx_bias:.2f} TECU；各日：{daily_bias['bias'].round(2).tolist()}")
    print(daily.round(3).to_string())
    print(runs.to_string() if not runs.empty else "沒有連續 1 小時以上的超界時段")
    print(f"報告：{REP_DIR / 'README.md'}")


def make_figures(s, sw, eq_time, rx_bias):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    from eqforecast.report import setup_matplotlib_cjk
    setup_matplotlib_cjk()

    lt = pd.Timedelta(hours=8)
    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True,
                             gridspec_kw={"height_ratios": [2.2, 1.6, 1.2]})
    x = s.index + lt
    ax = axes[0]
    ax.fill_between(x, s["lower"], s["upper"], color="#bee3f8", label=f"背景 中位數 ± {IQR_K} IQR")
    ax.plot(x, s["ref_med"], color="#2b6cb0", lw=0.8, label="背景中位數")
    ax.plot(x, s["vtec"], color="k", lw=1.0, label="NDH5 VTEC")
    ax.set_ylabel("VTEC (TECU)")
    ax.set_ylim(0, np.nanmax(s["upper"].to_numpy(float)) * 1.25)
    ax.legend(fontsize=8, loc="upper left", ncol=3)
    ax = axes[1]
    up = s["anom"] == 1
    lo = s["anom"] == -1
    ax.axhline(0, color="gray", lw=0.6)
    ax.fill_between(x, s["lower"] - s["ref_med"], s["upper"] - s["ref_med"], color="#e2e8f0")
    ax.plot(x, s["dvtec"], color="k", lw=0.8)
    ax.scatter(x[up], s.loc[up, "dvtec"], s=8, color="#c53030", label="高於上界")
    ax.scatter(x[lo], s.loc[lo, "dvtec"], s=8, color="#2f855a", label="低於下界")
    ax.set_ylabel("ΔVTEC (TECU)")
    ax.legend(fontsize=8, loc="upper left")
    ax = axes[2]
    if sw is not None:
        k = sw.dropna(subset=["kp_gfz"])
        ax.bar(k["time"] + lt, k["kp_gfz"], width=1 / 24, color="#a0aec0", label="Kp（GFZ）")
        ax.axhline(4, color="#dd6b20", lw=0.8, ls="--")
        ax.set_ylabel("Kp")
        ax.set_ylim(0, 9)
        ax2 = ax.twinx()
        ax2.plot(sw["time"] + lt, sw["dst_nT"], color="#6b46c1", lw=1, label="Dst")
        ax2.axhline(-30, color="#6b46c1", lw=0.6, ls=":")
        ax2.set_ylabel("Dst (nT)")
        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, fontsize=8, loc="lower left")
    for a in axes:
        a.axvline(eq_time + lt, color="#c53030", lw=1.2)
        a.grid(True, lw=0.3)
    axes[1].text(eq_time + lt, 0.97, f" 地震 {eq_time + lt:%m-%d %H:%M}", color="#c53030", fontsize=9,
                 va="top", transform=axes[1].get_xaxis_transform())
    axes[2].xaxis.set_major_locator(mdates.DayLocator())
    axes[2].xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    axes[2].set_xlabel("當地時間（UTC+8）")
    axes[2].set_xlim(x.min().floor("D"), x.max().ceil("D"))
    fig.suptitle("NDH5 垂直 TEC 與太空天氣，2024 花蓮地震前後（單一個案）")
    fig.tight_layout()
    fig.savefig(REP_DIR / "ndh5_vtec_timeseries.png", dpi=150)
    plt.close(fig)
    return {"timeseries": "ndh5_vtec_timeseries.png"}


def write_report(meta, daily_bias, daily, runs, s, figs, days, sens=None):
    eq = meta["eq"]
    L = []
    L.append("# 個案分析：NDH5 GNSS 站 TEC 與 2024 花蓮地震")
    L.append("")
    L.append(f"> 由 `pipeline/src/case_ndh5_tec.py` 自動產生於 {dt.datetime.now():%Y-%m-%d %H:%M}。")
    L.append("")
    L.append("> ⚠ **這是單一測站、單一事件的展示案例，不能當作 TEC 前兆存在的證據。**")
    L.append("> 即使看到「異常」，也無法排除巧合、太陽與地磁活動、或電離層本身的日變異。")
    L.append("> 要回答前兆有沒有用，必須用多個事件，並在相同條件下和只用地震目錄的基準模型比較（B3）。")
    L.append("")
    L.append("## 1. 資料")
    L.append("")
    L.append("| 項目 | 內容 |")
    L.append("|---|---|")
    L.append(f"| 測站 | {meta['station']}（{meta['receiver']}，取樣 {meta['interval_s']:.0f} 秒），"
             f"{meta['lat']:.3f}°N、{meta['lon']:.3f}°E（RINEX 標頭概略座標） |")
    if eq:
        L.append(f"| 地震 | {eq['time'][:19]} UTC（當地 {pd.Timestamp(eq['time']) + pd.Timedelta(hours=8):%m-%d %H:%M}），"
                 f"ML {eq['ml']:.2f}，深度 {eq['depth']:.1f} km，{eq['lat']:.3f}°N {eq['lon']:.3f}°E（GDMS 目錄） |")
        L.append(f"| 測站與震央距離 | {meta['eq_station_distance_km']:.0f} km |")
    L.append(f"| 觀測檔 | {len(days)} 天：" + "、".join(f"{d['date']:%m-%d}" for d in days) + " |")
    L.append("| 輔助資料 | GPS 廣播星曆、CAS 衛星 DCB（BKG 鏡像站）；Kp（GFZ）、Dst（NASA OMNI2，源自京都 WDC） |")
    L.append("")
    L.append("**各觀測檔的時間範圍**")
    L.append("")
    L.append("| 檔案 | 第一筆（GPS 時） | 最後一筆 | epoch 數 |")
    L.append("|---|---|---|---|")
    for _, r in daily_bias.iterrows():
        L.append(f"| {r['file']} | {r['first_obs']:%m-%d %H:%M} | {r['last_obs']:%m-%d %H:%M} | {r['n_obs_epochs']} |")
    L.append("")
    eq_t = pd.Timestamp(eq["time"]) if eq else None
    short = daily_bias[daily_bias["n_obs_epochs"] < 2800]
    for _, r in short.iterrows():
        msg = (f"⚠ `{r['file']}` 只到 {r['last_obs']:%H:%M} GPS 時（GPS 時比 UTC 快 18 秒），"
               "之後當天的觀測都缺失。")
        if eq_t is not None and r["last_obs"] < eq_t < r["date"] + pd.Timedelta(days=1, hours=1):
            msg += f"**地震前 {(eq_t - r['last_obs']) / pd.Timedelta(hours=1):.1f} 小時沒有資料。**"
        L.append(msg + "原始檔就是如此，缺失原因不明，這裡不做推測。")
        L.append("")
    L.append("## 2. 方法")
    L.append("")
    L.append("1. 雙頻 GPS 的 P1、P2 虛擬距離與 L1、L2 載波相位計算斜向 TEC，以相位平滑碼雜訊（每個連續弧段）。")
    L.append("2. 衛星硬體偏差用 CAS 每日 C1W−C2W DCB 修正。DCB 的正負號不憑記憶，"
             "兩種都試，選衛星間殘差較小者（見下表）。")
    L.append("3. 接收機偏差用最小標準差法（Ma & Maruyama, 2003）估計，只用當地黎明前 04–07 時、仰角 ≥ 30°；"
             "每個時刻允許 VTEC 隨穿刺點緯度、經度線性變化，因為台灣位在赤道異常北峰附近，水平梯度大。")
    L.append("4. 垂直 TEC：單層模型（350 km）、仰角 ≥ 30°，取各衛星的中位數，再取 15 分鐘中位數。")
    L.append(f"5. 背景值：前 {REF_DAYS} 天同一時刻的中位數 ± {IQR_K} × IQR（常見的四分位距法，"
             f"例如 Liu et al., 2004, Annales Geophysicae）。前期資料不足 {MIN_REF_DAYS} 天時，改用其他所有可用天，"
             "並標示為「暫定」。")
    L.append("")
    L.append("**DCB 符號與接收機偏差的逐日檢查**")
    L.append("")
    L.append("| 日期 | 選用符號 | 全天殘差（選用） | 全天殘差（另一符號） | 接收機偏差 (TECU) | 黎明前殘差 | 使用 epoch |")
    L.append("|---|---|---|---|---|---|---|")
    for d in days:
        mine, other = d["sign_check"][d["sign"]], d["sign_check"][-d["sign"]]
        bias = "—（該時段無資料）" if np.isnan(d["bias"]) else f"{d['bias']:.2f}"
        res = "—" if np.isnan(d["resid"]) else f"{d['resid']:.2f}"
        L.append(f"| {d['date']:%m-%d} | {d['sign']:+d} | {mine['resid']:.2f} | {other['resid']:.2f} | "
                 f"{bias} | {res} | {d['bias_epochs']} |")
    L.append("")
    L.append("**接收機偏差估計時段的比較**（各日估計值的範圍；範圍越小代表越穩定）")
    L.append("")
    L.append("| 估計時段 | 各日偏差 (TECU) | 全距 | 殘差中位數 |")
    L.append("|---|---|---|---|")
    for name in BIAS_WINDOWS:
        vals = [d["window_check"][name][0] for d in days]
        res = [d["window_check"][name][1] for d in days]
        v = np.array(vals, float)
        L.append(f"| {name} | " + ", ".join("—" if np.isnan(x) else f"{x:.1f}" for x in v)
                 + f" | {np.nanmax(v) - np.nanmin(v):.1f} | {np.nanmedian(np.array(res, float)):.2f} |")
    L.append("")
    L.append("當地午夜前後（太陽極大期常見電漿不規則結構）估得的偏差逐日跳動大，所以改用黎明前電離層最平滑的時段。"
             "缺少該時段資料的日子（—）不納入中位數。")
    L.append("")
    b = daily_bias["bias"]
    L.append(f"採用各日中位數 **{meta['rx_bias_tecu']:.2f} TECU**；各日範圍 {b.min():.2f} ～ {b.max():.2f}"
             f"（全距 {b.max() - b.min():.2f} TECU）。偏差的逐日變動會直接變成 VTEC 的逐日誤差，"
             "所以比這個全距小的 ΔVTEC 不宜解讀。")
    L.append("")
    L.append("## 3. 結果")
    L.append("")
    L.append(f"![VTEC 時間序列]({figs['timeseries']})")
    L.append("")
    L.append("**每日摘要**（超界比例 = 該日 15 分鐘格中，超出背景界線的比例）")
    L.append("")
    L.append("| 日期 (UTC) | 有效格數 | 高於上界 | 低於下界 | 最大 ΔVTEC | 最小 ΔVTEC | 背景 | Kp 最大 | Dst 最小 | F10.7 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for d, r in daily.iterrows():
        L.append(f"| {d:%m-%d} | {r['bins']} | {r['frac_above']:.0%} | {r['frac_below']:.0%} | "
                 f"{r['max_dvtec']:+.1f} | {r['min_dvtec']:+.1f} | {r['ref_mode']}（{r['n_ref']} 天） | "
                 f"{r.get('kp_max', np.nan):.2f} | {r.get('dst_min', np.nan):.0f} | {r.get('f107', np.nan):.0f} |")
    L.append("")
    L.append("**連續 1 小時以上超出界線的時段**")
    L.append("")
    if runs.empty:
        L.append("沒有。")
    else:
        L.append("| 開始（當地） | 結束（當地） | 方向 | 時數 | 最大 \\|ΔVTEC\\| |")
        L.append("|---|---|---|---|---|")
        for _, r in runs.iterrows():
            L.append(f"| {r['start_utc'] + pd.Timedelta(hours=8):%m-%d %H:%M} | "
                     f"{r['end_utc'] + pd.Timedelta(hours=8):%m-%d %H:%M} | {r['sign']} | {r['hours']:.2f} | "
                     f"{r['max_abs_dvtec']:.1f} |")
    L.append("")
    L.append("**觀察摘要**（只描述數據，不是結論）")
    L.append("")
    if eq_t is not None:
        pre_days = [d for d in daily.index if d < eq_t]
        post_days = [d for d in daily.index if d >= eq_t.floor("D") + pd.Timedelta(days=1)]
        top = daily["frac_above"].idxmax()
        rel = "地震前" if top < eq_t else f"地震後第 {(top - eq_t.floor('D')).days} 天（UTC 日期）"
        L.append(f"- 高於上界比例最高的一天是 {top:%m-%d}（{rel}），{daily.loc[top, 'frac_above']:.0%} 的時間高於上界，"
                 f"最大 ΔVTEC {daily.loc[top, 'max_dvtec']:+.1f} TECU；當天 Kp 最大 {daily.loc[top, 'kp_max']:.2f}、"
                 f"Dst 最小 {daily.loc[top, 'dst_min']:.0f} nT。")
        if pre_days and post_days:
            pre_rate = daily.loc[pre_days, "frac_above"].mean() + daily.loc[pre_days, "frac_below"].mean()
            post_rate = daily.loc[post_days, "frac_above"].mean() + daily.loc[post_days, "frac_below"].mean()
            L.append(f"- 震前 {len(pre_days)} 天平均超界比例（高＋低）{pre_rate:.0%}；"
                     f"震後完整的 {len(post_days)} 天平均 {post_rate:.0%}。")
        dist = daily[(daily["kp_max"] >= 4) | (daily["dst_min"] <= -30)]
        if dist.empty:
            L.append("- 這段期間沒有任何一天達到地磁擾動門檻（Kp ≥ 4 或 Dst ≤ −30 nT）。")
        else:
            L.append("- 達到地磁擾動門檻（Kp ≥ 4 或 Dst ≤ −30 nT）的日子：" + "、".join(
                f"{d:%m-%d}（Kp {r['kp_max']:.2f}、Dst {r['dst_min']:.0f}）" for d, r in dist.iterrows())
                + "。這些日子的 TEC 變化可能有太空天氣的成分。")
        L.append(f"- 在這 {len(daily)} 天內，震前日子的超界情形並沒有比其他日子突出；"
                 "整週最大的正異常出現在非震前、地磁寧靜的日子，說明單日的 TEC 變異本來就可以這麼大。"
                 if top >= eq_t else
                 "- 超界最多的日子在震前。但只有一個事件，這不足以支持任何因果解讀（見下一節）。")
    L.append("")
    if sens is not None and eq_t is not None:
        L.append("")
        L.append("### 3.1 敏感度：擾動日不當背景")
        L.append("")
        L.append("上表的背景包含地磁擾動日（例如 2024-03-24 的磁暴，Kp 8.3、Dst −128 nT）。"
                 "磁暴期間 TEC 常大幅偏高，會把背景墊高、界線變寬。下表改成只用寧靜日當背景"
                 "（寧靜日不足 10 天，所以最少 5 天即可；這是額外的不確定性）。")
        L.append("")
        L.append("| 日期 (UTC) | 擾動日 | 背景天數 | 高於上界 | 低於下界 | 最大 ΔVTEC | 最小 ΔVTEC |")
        L.append("|---|---|---|---|---|---|---|")
        for d, r in sens.iterrows():
            if pd.isna(r["n_ref"]):
                continue
            L.append(f"| {d:%m-%d}{' ⭐' if d == eq_t.floor('D') else ''} | {'是' if r['disturbed'] else ''} | {int(r['n_ref'])}"
                     f"{'（暫定）' if '暫定' in str(r['ref_mode']) else ''} | {r['frac_above']:.0%} | {r['frac_below']:.0%} "
                     f"| {r['max_dvtec']:.1f} | {r['min_dvtec']:.1f} |")
        pre_q = sens[(sens.index < eq_t) & (sens.index >= eq_t - pd.Timedelta(days=2))]
        if not pre_q.empty:
            L.append("")
            L.append(f"- 震前兩天（{', '.join(f'{d:%m-%d}' for d in pre_q.index)}）高於上界的比例："
                     + "、".join(f"{v:.0%}" for v in pre_q["frac_above"]) + "。")
    L.append("")
    L.append("## 4. 解讀時要注意")
    L.append("")
    prov = daily[daily["ref_mode"].str.contains("暫定")]
    if not prov.empty and eq_t is not None:
        pre = [d for d in daily.index if d < eq_t]
        post = [d for d in daily.index if d > eq_t]
        L.append(f"- **背景值是暫定的**：目前有 {len(daily)} 天觀測（{daily.index.min():%m-%d} ～ {daily.index.max():%m-%d}），"
                 f"地震前只有 {len(pre)} 天（UTC），其中有些天並不完整（見第 1 節）。"
                 f"背景是用「其他所有可用天」算的，其中 {len(post)} 天在地震之後。"
                 "只要震後幾天電離層本身偏高或偏低，震前的日子就會被判成「異常」，反之亦然。")
    L.append("- **隨機也會超界**：中位數 ± 1.5 IQR 在常態分布下約等於 ±2σ，每天本來就約有 4% 的時間超界；"
             f"背景只用 {int(daily['n_ref'].min())}–{int(daily['n_ref'].max())} 天，界線本身也有抽樣誤差，實際超界比例會更高。"
             "所以要看的是「震前是否比其他日子更常超界」，"
             "而不是「有沒有超界」。")
    L.append("- **太陽活動**：2024 年接近太陽極大期，電離層日變異本來就大。上表的 Kp、Dst 可以判斷有沒有磁暴；"
             "沒有磁暴也不代表沒有其他來源（例如太陽 EUV 變化、熱氣層風、行進式電離層擾動）。")
    L.append("- **接收機偏差**：用最小標準差法估計，逐日差異見第 2 節；小於該差異的 ΔVTEC 不應解讀。")
    L.append("- **單一測站**：無法區分局部與大尺度的變化。若要主張和震央有關，至少需要比較遠近不同測站的空間分布。")
    L.append("")
    L.append("## 5. 下一步（取得更多資料後）")
    L.append("")
    all_dates = pd.date_range(daily.index.min(), daily.index.max(), freq="D")
    missing = [d for d in all_dates if d not in daily.index]
    if missing:
        L.append("- 觀測期間內缺少的日子：" + "、".join(f"{d:%m-%d}" for d in missing)
                 + "（2026-11 向 GDMS 查詢 GNSS_ETEC 時，這些日子顯示「無資料」，原始資料就沒有）。")
    if not prov.empty:
        L.append(f"- 還有 {len(prov)} 天的背景是暫定的：補齊更早的 RINEX 後重新執行即可，背景會自動改用前 {REF_DAYS} 天。")
    L.append("- GDMS 的 GNSS_ETEC 測網資料到 2024-04-07 為止，所以無法延伸到 4 月底檢查震後是否回到背景。")
    L.append("- 加入幾個遠離震央的測站（例如台灣西部或離島）作為對照。")
    L.append("- 系統性的檢驗：把 TEC 轉成「網格 × 日」特徵，接到 B3 框架，和基準模型比較。")
    L.append("")
    (REP_DIR / "README.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
