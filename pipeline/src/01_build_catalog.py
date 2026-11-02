"""01_build_catalog.py：整併 CWA XML 與 GDMS CSV 地震目錄，輸出 events.parquet。

處理順序：
1. 讀取 CWA XML（台灣時間 +08:00）與 GDMS CSV（UTC），統一轉成 UTC。
2. 完備規模篩選：ML >= MC。
3. 來源內去重：時間取到分鐘＋經緯度取到小數 4 位＋規模取到 0.1。
4. 跨來源去重：CWA 與 GDMS 在 MATCH_SECONDS 秒、MATCH_DEG 度內視為同一事件，保留 GDMS
   （GDMS 有方位角空隙 gap 等定位品質欄位）。
5. 定位品質標記：gap <= GAP_MAX（或 gap 未知）的事件標記 use_for_features = True，
   只有這些事件可以拿來計算地震活動特徵。
6. 目標事件：ML >= TARGET_ML 的事件標記 is_target = True，不論 gap 都保留。
   外海大地震的 gap 通常偏大（測站只在陸地一側），若用 gap 刪除，
   模型會系統性地看不到東部外海的大地震。
7. 兩者皆非的事件（gap > GAP_MAX 的小地震）不輸出。

所有時間一律為 UTC（不帶時區的 datetime）。

路徑：
- 原始資料從 EQ_DATA_DIR 讀取（預設為專案根目錄），方便資料放在 git 之外。
- 輸出寫到專案根目錄的 pipeline/data/。
"""
import os
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd

# ========== 參數（對應期初提案） ==========
MC = 2.0             # 完備規模
GAP_MAX = 110        # 方位角空隙上限（度），只用於特徵事件
TARGET_ML = 5.5      # 回測目標事件的規模門檻
MATCH_SECONDS = 60   # 跨來源視為同一事件的時間容許差（秒）
MATCH_DEG = 0.3      # 跨來源視為同一事件的經緯度容許差（度）

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("EQ_DATA_DIR", BASE_DIR))


def parse_cwa_xml(xml_path):
    """解析 CWA XML 目錄，回傳 DataFrame（time 為 UTC）。"""
    root = ET.parse(xml_path).getroot()
    rows = []
    for eq in root.findall(".//{*}EarthquakeInfo"):
        lat = eq.findtext("{*}EpicenterLatitude")
        lon = eq.findtext("{*}EpicenterLongitude")
        depth = eq.findtext("{*}FocalDepth")
        ml = eq.findtext("{*}LocalMagnitude")
        gap_text = eq.findtext("{*}AzimuthalGap")
        try:
            gap = float(gap_text) if gap_text and gap_text.strip() else np.nan
        except ValueError:
            gap = np.nan
        rows.append({
            # 保留原始時區字串（例如 1999-09-21T01:47:16+08:00），稍後統一轉 UTC
            "time": eq.findtext("{*}OriginTime"),
            "lat": float(lat) if lat is not None else None,
            "lon": float(lon) if lon is not None else None,
            "depth": float(depth) if depth is not None else None,
            "ml": float(ml) if ml is not None else None,
            "gap": gap,
            "source": "CWA_XML",
            "event_id": eq.findtext("{*}EarthquakeNo"),
        })
    df = pd.DataFrame(rows)
    # 原本的程式直接切掉 +08:00，造成 CWA 比 GDMS 快 8 小時、同一事件無法去重。
    # 這裡先轉成 UTC 再去掉時區資訊，才能和 GDMS（UTC）對齊。
    df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce").dt.tz_convert(None)
    return df


def parse_gdms_csv(csv_path):
    """解析 GDMS CSV（時間為 UTC），欄位對應到統一格式。"""
    df = pd.read_csv(csv_path)
    # 欄位：date,time,lat,lon,depth,ML,nstn,dmin,gap,trms,ERH,ERZ,fixed,nph,quality
    df["time"] = pd.to_datetime(df["date"] + " " + df["time"], errors="coerce")
    df = df.rename(columns={"ML": "ml"})
    df["gap"] = pd.to_numeric(df["gap"], errors="coerce") if "gap" in df.columns else np.nan
    df["source"] = "GDMS"
    df["event_id"] = None
    return df[["time", "lat", "lon", "depth", "ml", "gap", "source", "event_id"]]


def dedup_within_source(df):
    """來源內去重：時間取到分鐘、經緯度取到小數 4 位、規模取到 0.1 後完全相同者只留一筆。"""
    key = pd.DataFrame({
        "source": df["source"],
        "t": df["time"].dt.round("1min"),
        "lat": df["lat"].round(4),
        "lon": df["lon"].round(4),
        "ml": df["ml"].round(1),
    })
    return df[~key.duplicated(keep="first")]


def dedup_cross_source(df):
    """跨來源去重：CWA 事件若在 GDMS 中找得到同一事件，就移除 CWA 那筆。

    兩個機構的定位結果不同（例如 921：23.853 vs 23.8525），不能用四捨五入後完全相等來比對，
    改用時間與經緯度的容許差。
    """
    cwa = df[df["source"] == "CWA_XML"].sort_values("time").reset_index()
    gdms = df[df["source"] == "GDMS"].sort_values("time")
    if cwa.empty or gdms.empty:
        return df, 0
    g = gdms[["time", "lat", "lon"]].rename(columns={"lat": "lat_g", "lon": "lon_g"})
    g["time_g"] = g["time"]
    m = pd.merge_asof(cwa, g, on="time", direction="nearest",
                      tolerance=pd.Timedelta(seconds=MATCH_SECONDS))
    same = (
        m["time_g"].notna()
        & ((m["lat"] - m["lat_g"]).abs() <= MATCH_DEG)
        & ((m["lon"] - m["lon_g"]).abs() <= MATCH_DEG)
    )
    return df.drop(index=m.loc[same, "index"]), int(same.sum())


def gap_quality(g):
    if pd.isna(g):
        return "未知"
    if g <= 90:
        return "優"
    if g <= GAP_MAX:
        return "可"
    return "差"


def main():
    print("開始建構地震目錄...")
    print(f"資料來源目錄：{DATA_DIR}")
    xml_files = sorted((DATA_DIR / "E-A0073-002").glob("CWA-EQ-Catalog-*.xml"))
    gdms_files = sorted((DATA_DIR / "earthquake_data" / "raw").glob("GDMS地震目錄_*.csv"))

    all_dfs = []
    for f in xml_files:
        all_dfs.append(parse_cwa_xml(f))
        print(f"已讀取 {f.name}")
    for f in gdms_files:
        all_dfs.append(parse_gdms_csv(f))
        print(f"已讀取 {f.name}")
    if not all_dfs:
        raise SystemExit("錯誤：未讀取到任何資料檔案，請確認 EQ_DATA_DIR")

    df = pd.concat(all_dfs, ignore_index=True).dropna(subset=["time", "lat", "lon", "ml"])
    print(f"原始筆數：{len(df)}")

    df = df[df["ml"] >= MC]
    print(f"ML >= {MC}：{len(df)}")

    df = dedup_within_source(df)
    print(f"來源內去重後：{len(df)}")

    df, n_cross = dedup_cross_source(df)
    print(f"跨來源去重：移除 {n_cross} 筆 CWA 重複事件，剩 {len(df)}")

    df["use_for_features"] = df["gap"].isna() | (df["gap"] <= GAP_MAX)
    df["is_target"] = df["ml"] >= TARGET_ML
    n_dropped = int((~df["use_for_features"] & ~df["is_target"]).sum())
    df = df[df["use_for_features"] | df["is_target"]].copy()
    print(f"移除 gap > {GAP_MAX} 且 ML < {TARGET_ML} 的事件 {n_dropped} 筆，剩 {len(df)}")
    print(f"  特徵事件（gap <= {GAP_MAX} 或未知）：{int(df['use_for_features'].sum())}")
    print(f"  目標事件（ML >= {TARGET_ML}，不論 gap）：{int(df['is_target'].sum())}，"
          f"其中 gap > {GAP_MAX}：{int((df['is_target'] & ~df['use_for_features']).sum())}")

    df["gap_quality"] = df["gap"].apply(gap_quality)
    df = df.sort_values("time").reset_index(drop=True)

    output_path = BASE_DIR / "pipeline" / "data" / "events.parquet"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, index=False)

    print(f"完成，共 {len(df)} 筆事件，輸出至 {output_path}")
    print("來源分布：")
    print(df["source"].value_counts())
    print(f"定位品質分布（優 <=90°、可 90–{GAP_MAX}°、差 >{GAP_MAX}°、未知 = 無 gap 欄位）：")
    print(df["gap_quality"].value_counts())


if __name__ == "__main__":
    main()
