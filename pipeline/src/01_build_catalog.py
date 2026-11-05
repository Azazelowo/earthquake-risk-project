"""01_build_catalog.py：整併 CWA XML 與 GDMS CSV 地震目錄，輸出 events.parquet 與 event_matches.parquet。

處理順序：
1. 讀取 CWA XML（台灣時間 +08:00）與 GDMS CSV（UTC），統一轉成 UTC。
2. 完備規模篩選：ML >= MC。
3. 來源內去重：時間取到分鐘＋經緯度取到小數 4 位＋規模取到 0.1。
4. 跨來源比對（規格：docs/phase2/catalog_event_matching_spec.md，PR #4）：
   CWA XML 是 GDMS 四捨五入到整秒、座標取到小數三位的版本（回測：|dt| 最大 0.5 秒、
   距離最大 0.08 km），所以用一對一配對，|dt| <= 1 秒且距離 <= 1 km 為 high，
   其餘 |dt| <= 2 秒且距離 <= 10 km 為 medium（需人工確認）。
   配對結果寫進 event_matches.parquet（每筆原始紀錄一列）；events.parquet 每個群組只留一筆，
   保留 GDMS（GDMS 有方位角空隙 gap 等定位品質欄位）。medium 也合併，因為重複計數的傷害
   比誤合併少數幾筆大。舊版的 60 秒／0.3° 方框，巧合配對上限約 4,000 筆。
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
# 跨來源比對門檻（CWA XML × GDMS，依 2026-11-05 回測；見規格 4.1 節）
MATCH_HIGH_S, MATCH_HIGH_KM = 1.0, 1.0
MATCH_MEDIUM_S, MATCH_MEDIUM_KM = 2.0, 10.0
MATCHER_VERSION = "v1:cwa_xml_gdms=1s/1km(high),2s/10km(medium)"
EARTH_RADIUS_KM = 6371.0

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


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


def epoch_s(t):
    # 與單位無關：pandas 3 的 datetime 可能是 [s]、[ms]、[us]，不能用 astype(int64) 再除固定常數
    return ((t - pd.Timestamp("1970-01-01")) / pd.Timedelta(seconds=1)).to_numpy()


def assign_event_ids(df):
    """GDMS 與 CWA XML 都沒有穩定事件編號，用「時間（0.01 秒）＋座標」組成（規格 3.1 節）。"""
    base = (df["time"].dt.strftime("%Y%m%dT%H%M%S") + "."
            + (df["time"].dt.microsecond // 10000).astype(str).str.zfill(2) + "_"
            + df["lat"].map("{:.4f}".format) + "_" + df["lon"].map("{:.4f}".format))
    # 理論上來源內去重後不會重複；保險起見，重複時加序號
    dup = base.groupby([df["source"], base]).cumcount()
    return base.where(dup == 0, base + "_" + dup.astype(str))


def match_cross_source(query, ref):
    """一對一配對 query（CWA XML）與 ref（GDMS），回傳配對表。

    回傳欄位：qi、ri（輸入 DataFrame 的位置）、dt_s（ref − query）、dist_km、ddepth_km、confidence。
    深度與規模不參與判定（規格 4.3 節）。
    """
    qt, rt = epoch_s(query["time"]), epoch_s(ref["time"])
    order = np.argsort(rt, kind="stable")
    rt_sorted = rt[order]
    lo = np.searchsorted(rt_sorted, qt - MATCH_MEDIUM_S, "left")
    hi = np.searchsorted(rt_sorted, qt + MATCH_MEDIUM_S, "right")
    qi = np.repeat(np.arange(len(query)), hi - lo)
    if len(qi) == 0:
        return pd.DataFrame(columns=["qi", "ri", "dt_s", "dist_km", "ddepth_km", "confidence"])
    ri = order[np.concatenate([np.arange(a, b) for a, b in zip(lo, hi)])]
    p = pd.DataFrame({
        "qi": qi, "ri": ri, "dt_s": rt[ri] - qt[qi],
        "dist_km": haversine_km(query["lat"].to_numpy()[qi], query["lon"].to_numpy()[qi],
                                ref["lat"].to_numpy()[ri], ref["lon"].to_numpy()[ri]),
        "ddepth_km": np.abs(ref["depth"].to_numpy(dtype=float)[ri] - query["depth"].to_numpy(dtype=float)[qi]),
    })
    p = p[p["dist_km"] <= MATCH_MEDIUM_KM]
    # 依標準化分數由小到大，兩邊都還沒配到才配（規格 4.2 節）
    p = p.assign(score=p["dt_s"].abs() / MATCH_MEDIUM_S + p["dist_km"] / MATCH_MEDIUM_KM) \
         .sort_values(["score", "qi", "ri"], kind="stable")
    used_q, used_r, keep = set(), set(), []
    for idx, a, b in zip(p.index, p["qi"], p["ri"]):
        if a in used_q or b in used_r:
            continue
        used_q.add(a)
        used_r.add(b)
        keep.append(idx)
    p = p.loc[keep].drop(columns="score").reset_index(drop=True)
    high = (p["dt_s"].abs() <= MATCH_HIGH_S) & (p["dist_km"] <= MATCH_HIGH_KM)
    p["confidence"] = np.where(high, "high", "medium")
    return p


def build_event_matches(df, matched_at):
    """跨來源比對，回傳 (events, event_matches)。

    events：每個比對群組一列（優先保留 GDMS），多一欄 match_group_id。
    event_matches：每筆原始紀錄一列（規格 3.2 節），沒配到的也有一列、confidence 為空。
    """
    df = df.sort_values("time", kind="stable").reset_index(drop=True)
    df["source_event_id"] = assign_event_ids(df)
    cwa = df[df["source"] == "CWA_XML"]
    gdms = df[df["source"] == "GDMS"]
    pairs = match_cross_source(cwa.reset_index(drop=True), gdms.reset_index(drop=True))
    q_idx = cwa.index.to_numpy()[pairs["qi"].to_numpy(dtype=int)]
    r_idx = gdms.index.to_numpy()[pairs["ri"].to_numpy(dtype=int)]

    # 群組：配對成功的兩筆同一群，其餘各自一群；群組 ID 用群內最早時間
    group_of = np.arange(len(df))
    group_of[q_idx] = r_idx
    first_time = df.groupby(group_of)["time"].transform("min")
    stamp = first_time.dt.strftime("EMG_%Y%m%d%H%M%S")
    seq = pd.Series(group_of).groupby(stamp).transform(lambda s: pd.factorize(s)[0])
    df["match_group_id"] = stamp + "_" + seq.astype(str).str.zfill(2)

    rec = pd.DataFrame({
        "match_group_id": df["match_group_id"],
        "source": df["source"],
        "source_event_id": df["source_event_id"],
        "confidence": pd.array([pd.NA] * len(df), dtype="string"),
        "pair_with": pd.array([pd.NA] * len(df), dtype="string"),
        "dt_s": np.nan, "dist_km": np.nan, "ddepth_km": np.nan,
    })
    for mine, other, sign in ((q_idx, r_idx, 1.0), (r_idx, q_idx, -1.0)):
        rec.loc[mine, "confidence"] = pairs["confidence"].to_numpy()
        rec.loc[mine, "pair_with"] = (df.loc[other, "source"] + ":" + df.loc[other, "source_event_id"]).to_numpy()
        rec.loc[mine, "dt_s"] = sign * pairs["dt_s"].to_numpy()   # 對方 − 本筆
        rec.loc[mine, "dist_km"] = pairs["dist_km"].to_numpy()
        rec.loc[mine, "ddepth_km"] = pairs["ddepth_km"].to_numpy()
    rec["matcher_version"] = MATCHER_VERSION
    rec["matched_at"] = pd.Timestamp(matched_at)

    # 每群只留一筆：配對成功時刪掉 CWA XML 那筆
    events = df.drop(index=q_idx).drop(columns="source_event_id")
    return events, rec, pairs


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

    matched_at = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("s")
    df, matches, pairs = build_event_matches(df, matched_at)
    n_conf = pairs["confidence"].value_counts()
    print(f"跨來源比對：{len(pairs)} 組 CWA XML × GDMS（high {int(n_conf.get('high', 0))}、"
          f"medium {int(n_conf.get('medium', 0))}），合併後剩 {len(df)}")

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
    # event_matches 涵蓋所有原始紀錄（含被 gap 規則排除的），比對結果才完整可查
    matches_path = BASE_DIR / "pipeline" / "data" / "catalog" / "event_matches.parquet"
    matches_path.parent.mkdir(parents=True, exist_ok=True)
    matches.to_parquet(matches_path, index=False)

    print(f"完成，共 {len(df)} 筆事件，輸出至 {output_path}")
    print(f"比對紀錄 {len(matches)} 筆，輸出至 {matches_path}")
    print("來源分布：")
    print(df["source"].value_counts())
    print(f"定位品質分布（優 <=90°、可 90–{GAP_MAX}°、差 >{GAP_MAX}°、未知 = 無 gap 欄位）：")
    print(df["gap_quality"].value_counts())


if __name__ == "__main__":
    main()
