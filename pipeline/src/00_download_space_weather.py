"""00_download_space_weather.py：下載並整理太空天氣指數（Kp、ap、Dst、AE、F10.7、SN、IMF Bz）。

為什麼需要：
    TEC 與地磁的「異常」大多來自太陽與地磁活動，不是地震。分析前兆之前，
    必須先知道哪些日子處於磁暴或太陽活動高峰，才能排除或校正這些干擾。
    這些指數是「控制變數」，不是前兆。

資料來源（皆為官方、可公開下載）：
    1. GFZ Potsdam：Kp、ap、Ap、國際太陽黑子數 SN、F10.7（1932 至今，單一檔案）
       https://kp.gfz-potsdam.de/app/files/Kp_ap_Ap_SN_F107_since_1932.txt
       授權 CC BY 4.0（SN 為 CC BY-NC 4.0）。引用：Matzka et al. (2021), Space Weather,
       https://doi.org/10.1029/2020SW002641
    2. NASA SPDF OMNI2 每小時資料：Dst（來自京都 WDC）、AE、IMF Bz、太陽風速度等
       https://spdf.gsfc.nasa.gov/pub/data/omni/low_res_omni/omni2_YYYY.dat
       格式說明：https://spdf.gsfc.nasa.gov/pub/data/omni/low_res_omni/omni2.text

輸出：
    原始檔  -> <EQ_DATA_DIR>/地震預測/太空天氣指數/raw/      （不進 git，OMNI 以 gzip 壓縮保存）
    整理後  -> pipeline/data/space_weather_hourly.parquet   （UTC 每小時）
               pipeline/data/space_weather_daily.parquet    （UTC 每日摘要與磁暴旗標）

用法：
    python pipeline/src/00_download_space_weather.py                  # 1990 年至今
    python pipeline/src/00_download_space_weather.py --start-year 2020
    python pipeline/src/00_download_space_weather.py --offline        # 只用已下載的原始檔重新整理
"""
import argparse
import datetime as dt
import gzip
import os
import shutil
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("EQ_DATA_DIR", BASE_DIR))
RAW_DIR = DATA_DIR / "地震預測" / "太空天氣指數" / "raw"
OUT_DIR = BASE_DIR / "pipeline" / "data"

GFZ_URL = "https://kp.gfz-potsdam.de/app/files/Kp_ap_Ap_SN_F107_since_1932.txt"
OMNI_URL = "https://spdf.gsfc.nasa.gov/pub/data/omni/low_res_omni/omni2_{year}.dat"

# OMNI2 欄位（1-based word 編號，依 omni2.text）與填補值。只取需要的欄位。
OMNI_WORDS = {
    "bz_gsm_nT": (17, 999.9),
    "sw_speed_kms": (25, 9999.0),
    "sw_density_cc": (24, 999.9),
    "kp_x10": (39, 99),
    "dst_nT": (41, 99999),
    "ae_nT": (42, 9999),
    "ap_omni": (50, 999),
    "f107_omni": (51, 999.9),
}
OMNI_N_WORDS = 55

# 磁暴與擾動門檻（可依研究需要調整，結果會寫進 daily 表的旗標欄位）
KP_STORM = 5.0      # NOAA G-scale：Kp = 5 為 G1（minor storm）
DST_STORM = -50.0   # Dst <= -50 nT：中等磁暴（Gonzalez et al., 1994, JGR）
KP_DISTURBED = 4.0  # 常見的「非寧靜日」判斷，TEC 前兆研究多半排除這類日子
DST_DISTURBED = -30.0


def download(url, dest, compress=False, timeout=120):
    """下載到 dest；compress=True 時以 gzip 存檔。已存在則跳過。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    final = dest.with_name(dest.name + ".gz") if compress else dest
    if final.exists() and final.stat().st_size > 0:
        return final, False
    tmp = final.with_name(final.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "eq-risk-student-project/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if compress:
            with gzip.open(tmp, "wb") as out:
                shutil.copyfileobj(resp, out)
        else:
            with open(tmp, "wb") as out:
                shutil.copyfileobj(resp, out)
    tmp.replace(final)
    return final, True


def parse_gfz(path):
    """解析 GFZ Kp 檔：回傳 3 小時 Kp/ap 表與每日表。缺值（-1）轉 NaN。"""
    cols = (["year", "month", "day", "days", "days_m", "bsr", "db"]
            + [f"kp{i}" for i in range(1, 9)] + [f"ap{i}" for i in range(1, 9)]
            + ["Ap", "SN", "f107_obs", "f107_adj", "definitive"])
    df = pd.read_csv(path, comment="#", sep=r"\s+", header=None, names=cols)
    df["date"] = pd.to_datetime(df[["year", "month", "day"]])
    kp = df[[f"kp{i}" for i in range(1, 9)]].to_numpy(float)
    ap = df[[f"ap{i}" for i in range(1, 9)]].to_numpy(float)
    kp[kp < 0] = np.nan
    ap[ap < 0] = np.nan
    three_hour = pd.DataFrame({
        "time": (df["date"].to_numpy()[:, None]
                 + (np.arange(8) * np.timedelta64(3, "h"))[None, :]).ravel(),
        "kp": kp.ravel(),
        "ap": ap.ravel(),
    })
    daily = pd.DataFrame({
        "date": df["date"],
        "kp_max": np.nanmax(np.where(np.isnan(kp), -np.inf, kp), axis=1),
        "kp_sum": np.nansum(kp, axis=1),
        "Ap": df["Ap"].where(df["Ap"] >= 0),
        "sn": df["SN"].where(df["SN"] >= 0),
        "f107_obs": df["f107_obs"].where(df["f107_obs"] > 0),
        "f107_adj": df["f107_adj"].where(df["f107_adj"] > 0),
        "kp_definitive": df["definitive"] >= 1,
    })
    daily.loc[np.isinf(daily["kp_max"]), "kp_max"] = np.nan
    return three_hour, daily


def parse_omni_year(path):
    """解析單一年度 OMNI2 檔（以空白分隔；每列應有 55 個欄位）。"""
    opener = gzip.open if str(path).endswith(".gz") else open
    rows = []
    with opener(path, "rt", encoding="ascii") as f:
        for line in f:
            parts = line.split()
            if len(parts) != OMNI_N_WORDS:
                raise ValueError(f"{path}: 欄位數 {len(parts)} != {OMNI_N_WORDS}，格式可能已變更")
            rows.append(parts)
    arr = np.array(rows)
    year = arr[:, 0].astype(int)
    doy = arr[:, 1].astype(int)
    hour = arr[:, 2].astype(int)
    time = (pd.to_datetime(year.astype(str), format="%Y")
            + pd.to_timedelta(doy - 1, unit="D") + pd.to_timedelta(hour, unit="h"))
    out = pd.DataFrame({"time": time})
    for name, (word, fill) in OMNI_WORDS.items():
        v = arr[:, word - 1].astype(float)
        v[np.isclose(v, fill)] = np.nan
        out[name] = v
    out["kp_omni"] = out.pop("kp_x10") / 10.0
    return out


def build_daily(hourly, gfz_daily):
    h = hourly.copy()
    h["date"] = h["time"].dt.floor("D")
    agg = h.groupby("date").agg(
        dst_min=("dst_nT", "min"),
        dst_mean=("dst_nT", "mean"),
        ae_max=("ae_nT", "max"),
        bz_min=("bz_gsm_nT", "min"),
        sw_speed_max=("sw_speed_kms", "max"),
        n_hours_dst=("dst_nT", "count"),
    ).reset_index()
    daily = gfz_daily.merge(agg, on="date", how="outer").sort_values("date")
    daily["storm_kp"] = daily["kp_max"] >= KP_STORM
    daily["storm_dst"] = daily["dst_min"] <= DST_STORM
    daily["disturbed"] = (daily["kp_max"] >= KP_DISTURBED) | (daily["dst_min"] <= DST_DISTURBED)
    # 只要其中一個指數缺值，就不能判定為寧靜日
    daily["quiet"] = (daily["kp_max"] < KP_DISTURBED) & (daily["dst_min"] > DST_DISTURBED)
    return daily.reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start-year", type=int, default=1990)
    ap.add_argument("--end-year", type=int, default=dt.date.today().year)
    ap.add_argument("--offline", action="store_true", help="不下載，只整理已存在的原始檔")
    args = ap.parse_args()

    print(f"原始檔目錄：{RAW_DIR}")
    gfz_path = RAW_DIR / "gfz" / "Kp_ap_Ap_SN_F107_since_1932.txt"
    log = []
    if not args.offline:
        # GFZ 檔每天更新，所以每次都重新下載
        if gfz_path.exists():
            gfz_path.unlink()
        p, _ = download(GFZ_URL, gfz_path)
        log.append(f"GFZ Kp: {p.name} ({p.stat().st_size / 1e6:.1f} MB)")
        this_year = dt.date.today().year
        for y in range(args.start_year, args.end_year + 1):
            dest = RAW_DIR / "omni2" / f"omni2_{y}.dat"
            gz = dest.with_name(dest.name + ".gz")
            if y >= this_year - 1 and gz.exists():
                gz.unlink()  # 今年與去年的 OMNI 仍會更新（Dst 由 quicklook 改為 provisional/final）
            try:
                p, new = download(OMNI_URL.format(year=y), dest, compress=True)
                log.append(f"OMNI2 {y}: {'下載' if new else '已存在'} ({p.stat().st_size / 1e6:.1f} MB gz)")
            except Exception as e:  # noqa: BLE001 - 記錄後繼續處理其他年份
                log.append(f"OMNI2 {y}: 失敗 {e}")
        print("\n".join(log))

    if not gfz_path.exists():
        sys.exit(f"找不到 {gfz_path}，請先在有網路時執行一次（不加 --offline）")
    kp3h, gfz_daily = parse_gfz(gfz_path)

    omni_files = sorted((RAW_DIR / "omni2").glob("omni2_*.dat.gz"))
    omni_files = [f for f in omni_files
                  if args.start_year <= int(f.name[6:10]) <= args.end_year]
    hourly = pd.concat([parse_omni_year(f) for f in omni_files], ignore_index=True)
    # 以 GFZ 的 Kp 為準（定義來源），併入每小時表（3 小時值向前填入該 3 小時）
    kp3h = kp3h[(kp3h["time"].dt.year >= args.start_year) & (kp3h["time"].dt.year <= args.end_year)]
    hourly = pd.merge_asof(hourly.sort_values("time"), kp3h.rename(columns={"kp": "kp_gfz", "ap": "ap_gfz"}),
                           on="time", direction="backward", tolerance=pd.Timedelta(hours=2, minutes=59))

    gfz_daily = gfz_daily[(gfz_daily["date"].dt.year >= args.start_year)
                          & (gfz_daily["date"].dt.year <= args.end_year)]
    daily = build_daily(hourly, gfz_daily)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    hourly.to_parquet(OUT_DIR / "space_weather_hourly.parquet", index=False)
    daily.to_parquet(OUT_DIR / "space_weather_daily.parquet", index=False)

    # 一致性檢查：OMNI 的 Kp 與 GFZ 的 Kp 應幾乎相同（兩者同源）
    both = hourly.dropna(subset=["kp_omni", "kp_gfz"])
    diff = (both["kp_omni"] - both["kp_gfz"]).abs()
    print(f"每小時：{len(hourly)} 列，{hourly['time'].min()} ~ {hourly['time'].max()}")
    print(f"每日：{len(daily)} 列；Kp 有值 {daily['kp_max'].notna().sum()} 天，Dst 有值 {daily['dst_min'].notna().sum()} 天")
    print(f"Kp 交叉檢查（OMNI vs GFZ）：{len(both)} 小時，|差| > 0.34 的比例 = {(diff > 0.34).mean():.4f}")
    print(f"磁暴日（Kp>={KP_STORM}）：{int(daily['storm_kp'].sum())}；（Dst<={DST_STORM}）：{int(daily['storm_dst'].sum())}")
    print(f"輸出：{OUT_DIR / 'space_weather_daily.parquet'}")


if __name__ == "__main__":
    main()
