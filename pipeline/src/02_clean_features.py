"""B2 第六步：把測站的地磁與 TEC 資料轉成「0.2° 網格 × 日」的前兆特徵，接到 B3。

輸入（由 10_、11_ 兩支程式產生）：
    pipeline/data/magnet/magnet_daily.parquet    每站每日：夜間 ULF Sz、Sh、Sz/Sh、資料涵蓋率
    pipeline/data/tec/tec_15min.parquet          每站 15 分鐘垂直 TEC
    pipeline/data/space_weather_daily.parquet    （選用）00_download_space_weather.py 的擾動日旗標

步驟：
1. 測站層級的異常指標，背景值只用「當天之前」的資料（不偷看未來）：
   背景 = 前 30 天內「最近的 15 個寧靜日」（至少 10 天）。2018–2026 跨過太陽極大期，
   擾動日占 39%，若硬取「前 15 天」常湊不滿 10 天，所以把搜尋範圍放寬到 30 天。
   地磁（每站每日）
       mag_pol_z    log10(Sz/Sh) 相對背景的穩健 z 值（中位數、1.4826 × MAD）
       mag_sz_z     log10(Sz) 的穩健 z 值
       mag_szref_z  log10(Sz / 參考站 Sz) 的穩健 z 值。參考站預設金門 KMN（距台灣本島 > 200 km），
                    用來扣掉磁層、太陽風造成的全區共同變化（梯度法）
   TEC（每站每 15 分鐘 → 每日）
       背景：背景日同一時刻的中位數 ± 1.5 IQR（和 case_ndh5_tec.py 相同）
       tec_pos_frac / tec_neg_frac   當天高於上界／低於下界的時間比例
       tec_dmax / tec_dmin           當天 ΔVTEC（觀測 − 背景中位數）的最大值、最小值，TECU
   背景至少要 10 天；不足時該日特徵為缺值。
2. 太空天氣（有 space_weather_daily.parquet 時）：
   擾動日（Kp ≥ 4 或 Dst ≤ −30 nT）不當背景；磁暴日（Kp ≥ 5 或 Dst ≤ −50 nT）當天特徵設為缺值。
   一般擾動日仍出特徵：全區共同的擾動另由 mag_szref_z（相對參考站）扣除。
   --keep-disturbed 可全部關掉。
3. 對應到網格：每個網格中心取「當天有資料、距離最近」的測站。
   地磁 ULF 訊號衰減快，限 150 km 內；TEC 是電離層 350 km 高的量，空間尺度大，限 300 km 內。
   超過距離的網格不給值（缺值），不補 0。
輸出：
    pipeline/data/features_magnet.parquet   lat_bin、lon_bin、time_bin、mag_*、mag_station、mag_dist_km
    pipeline/data/features_tec.parquet      lat_bin、lon_bin、time_bin、tec_*、tec_station、tec_dist_km
    pipeline/data/station_anomalies.parquet 測站層級的指標（報告與檢查用）
地磁與 TEC 分開兩個檔：B3 以「某網格有幾天有資料」計算覆蓋率，放同一檔會互相灌水。
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eqforecast.decluster import haversine_km  # noqa: E402

BASE_DIR = Path(__file__).resolve().parents[2]
DATA = BASE_DIR / "pipeline" / "data"
CONFIG = BASE_DIR / "pipeline" / "configs" / "b3_baseline.json"

GRID_DEG = 0.2
BG_SEARCH_DAYS = 30          # 往回找背景日的範圍
BG_DAYS = 15                 # 取最近的幾個寧靜日
MIN_BG_DAYS = 10
IQR_K = 1.5
MIN_MAG_COVERAGE = 0.9        # 當天 1 秒資料涵蓋率低於此值，不當背景、也不出特徵
MIN_TEC_HOURS = 20.0          # 當天 TEC 少於 20 小時，同上
MAG_RADIUS_KM = 150.0
TEC_RADIUS_KM = 300.0
MAG_REF = "KMN"
MAG_COLS = ["mag_pol_z", "mag_sz_z", "mag_szref_z"]
TEC_COLS = ["tec_pos_frac", "tec_neg_frac", "tec_dmax", "tec_dmin"]


def background_days(dates, bg_ok, i, search=BG_SEARCH_DAYS, n=BG_DAYS):
    """第 i 天的背景日：之前 search 天內、bg_ok 為真的日子中最近的 n 天（布林遮罩）。"""
    t = dates
    m = (t >= t[i] - pd.Timedelta(days=search)) & (t < t[i]) & bg_ok
    idx = np.flatnonzero(m)[-n:]
    out = np.zeros(len(t), bool)
    out[idx] = True
    return out


def robust_z(values, dates, bg_ok, out_ok, min_bg=MIN_BG_DAYS):
    """穩健 z 值。背景只用過去、bg_ok 的日子；out_ok 為假的日子不出值。"""
    order = np.argsort(pd.DatetimeIndex(dates))
    t = pd.DatetimeIndex(dates)[order]
    v = np.asarray(values, float)[order]
    bg_ok = np.asarray(bg_ok, bool)[order] & ~np.isnan(v)
    out_ok = np.asarray(out_ok, bool)[order]
    out = np.full(len(t), np.nan)
    for i in range(len(t)):
        if not out_ok[i] or np.isnan(v[i]):
            continue
        bg = v[background_days(t, bg_ok, i)]
        if len(bg) < min_bg:
            continue
        med = np.median(bg)
        mad = 1.4826 * np.median(np.abs(bg - med))
        if mad > 0:
            out[i] = (v[i] - med) / mad
    return pd.Series(out, index=t)


def magnet_station_features(mag, disturbed, storms, ref=MAG_REF):
    mag = mag.copy()
    mag["date"] = pd.to_datetime(mag["date"]).dt.normalize()
    mag["log_pol"] = np.log10(mag["ulf_pol"])
    mag["log_sz"] = np.log10(mag["ulf_sz"])
    ref_sz = mag[mag["station"] == ref].set_index("date")["log_sz"]
    mag["log_szref"] = mag["log_sz"] - mag["date"].map(ref_sz)
    good = mag["coverage"] >= MIN_MAG_COVERAGE
    mag["bg_ok"] = good & ~mag["date"].isin(disturbed)
    mag["out_ok"] = good & ~mag["date"].isin(storms)
    rows = []
    for sta, g in mag.sort_values("date").groupby("station"):
        g = g.drop_duplicates("date").set_index("date")
        out = pd.DataFrame(index=g.index)
        out["station"] = sta
        out["lat"], out["lon"] = g["lat"], g["lon"]
        for src, col in (("log_pol", "mag_pol_z"), ("log_sz", "mag_sz_z"), ("log_szref", "mag_szref_z")):
            z = robust_z(g[src].to_numpy(float), g.index, g["bg_ok"].to_numpy(), g["out_ok"].to_numpy())
            out[col] = z.reindex(g.index).to_numpy()
        if sta == ref:
            out["mag_szref_z"] = np.nan          # 參考站對自己的比值沒有意義
        rows.append(out.reset_index())
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


BIAS_JUMP_TECU = 8.0          # 接收機偏差和前 5 天中位數相差超過此值，視為換了儀器


def instrument_segments(ids, biases, jump=BIAS_JUMP_TECU):
    """把一個測站的連續日子切成「同一套儀器」的段落，回傳段落編號。

    換段的條件：(1) 儀器識別（序號、型號、韌體、天線）改變；(2) 接收機偏差突然跳動。
    實例：NDHU 2026-10-20 型號同為 TRIMBLE NETR9、只換了序號與韌體，估計偏差 33 → −3 TECU。
    背景值若跨過換機日，會把偏差差異誤判成 TEC 異常。
    """
    seg = np.zeros(len(ids), int)
    k = 0
    for i in range(1, len(ids)):
        prev = [b for b, s in zip(biases[max(0, i - 5):i], seg[max(0, i - 5):i]) if s == k and not np.isnan(b)]
        jumped = bool(prev) and not np.isnan(biases[i]) and abs(biases[i] - np.median(prev)) > jump
        if ids[i] != ids[i - 1] or jumped:
            k += 1
        seg[i] = k
    return seg


def tec_station_features(series, daily, disturbed, storms):
    s = series.copy()
    s["time"] = pd.to_datetime(s["time"])
    s["date"] = s["time"].dt.normalize()
    s["slot"] = s["time"] - s["date"]
    daily = daily.assign(date=pd.to_datetime(daily["date"]).dt.normalize())
    hours = daily.set_index(["station", "date"])["hours_covered"]
    idcol = "instrument_id" if "instrument_id" in daily else "receiver"
    instrument = daily.set_index(["station", "date"])[idcol]
    bias = daily.set_index(["station", "date"])["rx_bias_tecu"]
    pos = daily.groupby("station")[["lat", "lon"]].median()
    rows = []
    for sta, g in s.groupby("station"):
        tab = g.pivot_table(index="date", columns="slot", values="vtec")
        days = tab.index
        full = np.array([hours.get((sta, d), 0) >= MIN_TEC_HOURS for d in days])
        bg_ok = full & ~np.isin(days, list(disturbed))
        out_ok = full & ~np.isin(days, list(storms))
        segment = instrument_segments([instrument.get((sta, d), "") for d in days],
                                      [bias.get((sta, d), np.nan) for d in days])
        for i, d in enumerate(days):
            if not out_ok[i]:
                continue
            m = background_days(days, bg_ok & (segment == segment[i]), i)
            if m.sum() < MIN_BG_DAYS:
                continue
            ref = tab[m]
            q1, med, q3 = ref.quantile(0.25), ref.median(), ref.quantile(0.75)
            iqr = q3 - q1
            v = tab.loc[d]
            ok = v.notna() & med.notna()
            if ok.sum() < 48:                    # 至少 12 小時有背景可比
                continue
            dv = (v - med)[ok]
            rows.append({
                "station": sta, "date": d, "lat": pos.loc[sta, "lat"], "lon": pos.loc[sta, "lon"],
                "tec_pos_frac": float((v[ok] > (med + IQR_K * iqr)[ok]).mean()),
                "tec_neg_frac": float((v[ok] < (med - IQR_K * iqr)[ok]).mean()),
                "tec_dmax": float(dv.max()), "tec_dmin": float(dv.min()), "tec_n_bg_days": int(m.sum()),
            })
    return pd.DataFrame(rows)


def grid_cells(region, deg=GRID_DEG):
    lat_b = np.arange(region["lat_min"], region["lat_max"] - 1e-9, deg)
    lon_b = np.arange(region["lon_min"], region["lon_max"] - 1e-9, deg)
    la, lo = np.meshgrid(lat_b, lon_b, indexing="ij")
    return pd.DataFrame({"lat_bin": la.ravel().round(4), "lon_bin": lo.ravel().round(4)})


def to_grid(st_feat, cols, cells, radius_km, prefix):
    """每天、每個網格取最近且有值的測站。回傳只含「有值」的列。"""
    if st_feat.empty:
        return pd.DataFrame(columns=["lat_bin", "lon_bin", "time_bin"] + cols)
    clat = cells["lat_bin"].to_numpy() + GRID_DEG / 2
    clon = cells["lon_bin"].to_numpy() + GRID_DEG / 2
    sta_pos = st_feat.groupby("station")[["lat", "lon"]].median()
    names = sta_pos.index.to_numpy()
    dist = np.stack([haversine_km(r.lat, r.lon, clat, clon) for r in sta_pos.itertuples()], axis=1)
    parts = []
    for d, g in st_feat.groupby("date"):
        g = g.set_index("station")
        have = g[cols].notna().any(axis=1)
        avail = np.isin(names, g.index[have])
        dd = np.where(avail[None, :], dist, np.inf)
        j = dd.argmin(axis=1)
        dmin = dd[np.arange(len(cells)), j]
        ok = dmin <= radius_km
        if not ok.any():
            continue
        pick = names[j[ok]]
        vals = g.loc[pick, cols].to_numpy(float)
        part = pd.DataFrame(vals, columns=cols)
        part.insert(0, "lat_bin", cells["lat_bin"].to_numpy()[ok])
        part.insert(1, "lon_bin", cells["lon_bin"].to_numpy()[ok])
        part.insert(2, "time_bin", d)
        part[f"{prefix}_station"] = pick
        part[f"{prefix}_dist_km"] = dmin[ok].round(1)
        parts.append(part)
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if not out.empty:
        out["is_demo"] = False
    return out


def load_disturbed(path, keep_disturbed):
    """回傳 (擾動日集合, 磁暴日集合, 訊息)。"""
    if keep_disturbed:
        return set(), set(), "未排除擾動日（--keep-disturbed）"
    if not Path(path).exists():
        return set(), set(), f"⚠ 找不到 {path}，沒有排除擾動日；請先執行 00_download_space_weather.py"
    sw = pd.read_parquet(path)
    dates = pd.to_datetime(sw["date"]).dt.normalize()
    dist = set(dates[sw["disturbed"].astype(bool)])
    storm = set(dates[(sw["storm_kp"].astype(bool)) | (sw["storm_dst"].astype(bool))])
    return dist, storm, (f"擾動日（不當背景）{len(dist)} 天；磁暴日（特徵設缺值）{len(storm)} 天"
                         f"，資料期間 {dates.min().date()} ~ {dates.max().date()}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--magnet", default=str(DATA / "magnet" / "magnet_daily.parquet"))
    ap.add_argument("--tec", default=str(DATA / "tec" / "tec_15min.parquet"))
    ap.add_argument("--tec-daily", default=str(DATA / "tec" / "tec_daily.parquet"))
    ap.add_argument("--space-weather", default=str(DATA / "space_weather_daily.parquet"))
    ap.add_argument("--keep-disturbed", action="store_true")
    ap.add_argument("--mag-ref", default=MAG_REF)
    ap.add_argument("--out-dir", default=str(DATA))
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    region = json.loads(CONFIG.read_text(encoding="utf-8"))["region"]
    cells = grid_cells(region)
    disturbed, storms, sw_msg = load_disturbed(args.space_weather, args.keep_disturbed)
    print(sw_msg)

    have_mag, have_tec = Path(args.magnet).exists(), Path(args.tec).exists()
    if not (have_mag or have_tec):
        sys.exit("沒有前兆資料：請先執行 10_process_magnet.py 或 11_process_gnss_tec.py")
    st_parts = []
    if have_mag:
        mag = pd.read_parquet(args.magnet)
        mf = magnet_station_features(mag, disturbed, storms, args.mag_ref)
        if args.mag_ref not in set(mag["station"]):
            print(f"⚠ 沒有參考站 {args.mag_ref} 的資料，mag_szref_z 全為缺值")
        gm = to_grid(mf, MAG_COLS, cells, MAG_RADIUS_KM, "mag")
        gm.to_parquet(out_dir / "features_magnet.parquet", index=False)
        print(f"地磁：{mf['station'].nunique()} 站、{len(mf)} 站日；有 z 值的站日 "
              f"{int(mf['mag_pol_z'].notna().sum())}；網格日 {len(gm)} → features_magnet.parquet")
        st_parts.append(mf.assign(source="magnet"))
    if have_tec:
        tf = tec_station_features(pd.read_parquet(args.tec), pd.read_parquet(args.tec_daily), disturbed, storms)
        gt = to_grid(tf, TEC_COLS, cells, TEC_RADIUS_KM, "tec")
        gt.to_parquet(out_dir / "features_tec.parquet", index=False)
        n_sta = tf["station"].nunique() if not tf.empty else 0
        print(f"TEC：{n_sta} 站、有異常指標的站日 {len(tf)}；網格日 {len(gt)} → features_tec.parquet")
        st_parts.append(tf.assign(source="tec"))
    st = pd.concat(st_parts, ignore_index=True)
    st.to_parquet(out_dir / "station_anomalies.parquet", index=False)
    print(f"測站層級指標：{out_dir / 'station_anomalies.parquet'}")


if __name__ == "__main__":
    main()
