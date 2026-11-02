import pandas as pd
from pathlib import Path
import xml.etree.ElementTree as ET
import re
import numpy as np

def parse_cwa_xml(xml_path):
    """解析 CWA XML 目錄，回傳 DataFrame"""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    rows = []
    # Find all EarthquakeInfo elements (assuming default namespace)
    for eq in root.findall(".//{*}EarthquakeInfo"):
        origin = eq.findtext("{*}OriginTime")
        lat = eq.findtext("{*}EpicenterLatitude")
        lon = eq.findtext("{*}EpicenterLongitude")
        depth = eq.findtext("{*}FocalDepth")
        ml = eq.findtext("{*}LocalMagnitude")
        # AzimuthalGap not in XML, set as NaN
        gap_text = eq.findtext("{*}AzimuthalGap")
        if gap_text is not None and gap_text.strip() != '':
            try:
                gap = float(gap_text)
            except:
                gap = np.nan
        else:
            gap = np.nan
        event_id = eq.findtext("{*}EarthquakeNo")  # may not exist
        # Strip timezone offset if present (e.g., +08:00)
        if origin:
            origin = re.sub(r'\s*[+-]\d{2}:\d{2}$', '', origin)
        rows.append({
            "time": origin,
            "lat": float(lat) if lat is not None else None,
            "lon": float(lon) if lon is not None else None,
            "depth": float(depth) if depth is not None else None,
            "ml": float(ml) if ml is not None else None,
            "gap": gap,
            "source": "CWA_XML",
            "event_id": event_id,
        })
    return pd.DataFrame(rows)

def parse_gdms_csv(csv_path):
    """解析 GDMS CSV，欄位名稱對應到統一格式"""
    df = pd.read_csv(csv_path)
    # Expected columns: date,time,lat,lon,depth,ML,nstn,dmin,gap,trms,ERH,ERZ,fixed,nph,quality
    # Combine date and time into a single datetime
    df["time"] = pd.to_datetime(df["date"] + " " + df["time"])
    df = df.rename(columns={
        "lat": "lat",
        "lon": "lon",
        "depth": "depth",
        "ML": "ml",
        "gap": "gap",
    })
    # Ensure gap exists, otherwise set NaN
    if "gap" not in df.columns:
        df["gap"] = np.nan
    else:
        # Ensure numeric, coerce errors to NaN
        df["gap"] = pd.to_numeric(df["gap"], errors='coerce')
    df["source"] = "GDMS"
    # Keep only needed columns
    df = df[["time", "lat", "lon", "depth", "ml", "gap", "source"]]
    return df

def main():
    print("開始建構地震目錄...")
    base_path = Path(".")
    cwa_xml_dir = base_path / "E-A0073-002"
    gdms_csv_dir = base_path / "earthquake_data" / "raw"
    
    xml_files = sorted(cwa_xml_dir.glob("CWA-EQ-Catalog-*.xml"))
    gdms_files = sorted(gdms_csv_dir.glob("GDMS地震目錄_*.csv"))
    
    all_dfs = []
    for xml_file in xml_files:
        df_xml = parse_cwa_xml(xml_file)
        all_dfs.append(df_xml)
        print(f"已讀取 {xml_file.name}")
    for csv_file in gdms_files:
        df_csv = parse_gdms_csv(csv_file)
        all_dfs.append(df_csv)
        print(f"已讀取 {csv_file.name}")
    
    if not all_dfs:
        print("錯誤：未讀取到任何資料檔案")
        return
    
    df = pd.concat(all_dfs, ignore_index=True)
    # Ensure time is datetime
    df["time"] = pd.to_datetime(df["time"], errors='coerce')
    # Mc = 2.0 completeness
    df = df[df["ml"] >= 2.0]
    # Gap filtering: keep if gap is NaN (missing) or <= 160
    df = df[df["gap"].isna() | (df["gap"] <= 160)]
    # Deduplication: round time to minute, lat/lon to 4 decimal, ml to 0.1
    df["time_rounded"] = df["time"].dt.round("1min")
    df["lat_rounded"] = df["lat"].round(4)
    df["lon_rounded"] = df["lon"].round(4)
    df["ml_rounded"] = df["ml"].round(1)
    df = df.sort_values("ml_rounded", ascending=False).drop_duplicates(
        subset=["time_rounded", "lat_rounded", "lon_rounded", "ml_rounded"],
        keep="first"
    )
    
    output_dir = base_path / "pipeline" / "data"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "events.parquet"
    df.to_parquet(output_path, index=False)
    print(f"完成，共 {len(df)} 筆事件，輸出至 {output_path}")
    print("來源分布:")
    print(df["source"].value_counts())
    print("Gap品質分布 (≤110 優, 110-160 可, NaN 未知):")
    def gap_quality(g):
        if pd.isna(g):
            return "未知"
        elif g <= 110:
            return "優"
        elif g <= 160:
            return "可"
        else:
            return "剔除"
    df["gap_quality"] = df["gap"].apply(gap_quality)
    print(df["gap_quality"].value_counts())

if __name__ == "__main__":
    main()
