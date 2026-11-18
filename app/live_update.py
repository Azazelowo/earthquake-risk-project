"""即時更新：把風險函數推進到「現在」。

為什麼需要：正式目錄（GDMS）要登入下載、而且約落後一個半月，APP 的機率會停在目錄最後一天。
這個模組用兩個能自動取得的來源補上目錄結束之後的地震，再用**同一組參數**重算發布時間＝現在的機率。

資料來源與已知缺口（見 docs/phase2/data_source_comparison.md）：
- CWA 有感地震報告（E-A0015-001 顯著、E-A0016-001 小區域）：同一個規模尺度（ML），但只有「有感」地震，
  而且 API 只留最新 16 筆 → 每次執行都把抓到的報告累積進 provisional_events.csv，漏抓的補不回來。
  需要環境變數 CWA_API_KEY；沒有就跳過。
- USGS FDSN：可任意回補，但台灣附近實質只收錄 M4 以上，規模尺度也和 ML 不同。
- 兩者重複的事件（|Δt| ≤ 10 秒且距離 ≤ 75 km，對應跨目錄比對規格的 medium 門檻）以 CWA 為準。

因為小地震不完整，近期活動加成會偏低。`assess_gap` 在正式目錄的期間模擬「最後 N 天只剩 M≥4」，
量化總數會被低估多少，寫進 data_status 讓 APP 顯示；不拿來修正數字（CWA 有感報告會補到一部分 M<4，
用 M≥4 模擬是偏保守的上限，直接修正會過度補償）。

校準倍率沿用正式目錄期間算好的值：滾動校準需要「已結束的窗」的完整地震數，暫時資料不夠完整。

用法：
    python app/live_update.py                 # 更新到現在，輸出到 app/live/
    python app/live_update.py --no-network    # 只用已累積的暫時資料重算（測試用）
"""
import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR.parent / "pipeline" / "src"))
from eqforecast import hazard as hz  # noqa: E402

BASE_DIR = APP_DIR / "data"
LIVE_DIR = APP_DIR / "live"
USER_AGENT = "eq-hazard-app/1.0"
USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
CWA_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/{}?format=JSON"
CWA_DATASETS = ("E-A0015-001", "E-A0016-001")
MATCH_S, MATCH_KM = 10.0, 75.0
GAP_PROXY_ML = 4.0
ISSUE_STEP = "15min"  # 發布時間取到 15 分鐘（排程每 15 分鐘跑一次）；模型的時間是連續的，不依賴整點
PROV_COLS = ["time", "lat", "lon", "depth", "ml", "source", "source_id", "fetched_at"]


# ---------------- 取得資料 ----------------

def _get(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def fetch_usgs(start, end, grid, min_mag=3.5, get=_get):
    """USGS 在網格範圍內、[start, end) 的地震。時間為不帶時區的 UTC。"""
    q = {"starttime": pd.Timestamp(start).strftime("%Y-%m-%dT%H:%M:%S"),
         "endtime": pd.Timestamp(end).strftime("%Y-%m-%dT%H:%M:%S"),
         "minlatitude": grid["lat_min"], "maxlatitude": grid["lat_max"],
         "minlongitude": grid["lon_min"], "maxlongitude": grid["lon_max"],
         "minmagnitude": min_mag, "orderby": "time-asc", "format": "csv", "eventtype": "earthquake"}
    text = get(f"{USGS_URL}?{urllib.parse.urlencode(q)}")
    return parse_usgs_csv(text)


def parse_usgs_csv(text):
    if not text.strip():
        return pd.DataFrame(columns=PROV_COLS[:-1])
    raw = pd.read_csv(io.StringIO(text))
    return pd.DataFrame({
        "time": pd.to_datetime(raw["time"], utc=True, format="ISO8601").dt.tz_localize(None),
        "lat": raw["latitude"].astype(float), "lon": raw["longitude"].astype(float),
        "depth": raw["depth"].astype(float), "ml": raw["mag"].astype(float),
        "source": "USGS", "source_id": raw["id"].astype(str)})


def fetch_cwa(api_key, get=_get):
    """CWA 兩種有感地震報告（各只有最新 16 筆）。"""
    frames = []
    for ds in CWA_DATASETS:
        frames.append(parse_cwa_json(get(CWA_URL.format(ds), headers={"Authorization": api_key}), ds))
    return pd.concat(frames, ignore_index=True)


def parse_cwa_json(text, dataset=""):
    d = json.loads(text)
    rows = []
    for eq in d.get("records", {}).get("Earthquake", []):
        info = eq.get("EarthquakeInfo", {})
        epi, mag = info.get("Epicenter", {}), info.get("EarthquakeMagnitude", {})
        try:
            t = pd.Timestamp(info["OriginTime"])
            t = (t.tz_convert("UTC") if t.tzinfo else t.tz_localize("Asia/Taipei").tz_convert("UTC")).tz_localize(None)
            rows.append({"time": t, "lat": float(epi["EpicenterLatitude"]), "lon": float(epi["EpicenterLongitude"]),
                         "depth": float(info.get("FocalDepth", np.nan)), "ml": float(mag["MagnitudeValue"]),
                         "source": "CWA", "source_id": f"{dataset}:{eq.get('EarthquakeNo', t.isoformat())}"})
        except (KeyError, TypeError, ValueError):
            continue
    return pd.DataFrame(rows, columns=PROV_COLS[:-1])


# ---------------- 合併 ----------------

def _km(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = (np.sin((p2 - p1) / 2) ** 2
         + np.cos(p1) * np.cos(p2) * np.sin(np.radians(np.asarray(lon2) - np.asarray(lon1)) / 2) ** 2)
    return 2 * 6371.0 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def typed(df):
    """固定欄位型別。空表格和有資料的表格 concat 時，部分 pandas 版本會把數字欄變成 object，後面的數值運算會壞。"""
    df = df.reindex(columns=PROV_COLS).copy()
    for c in ("lat", "lon", "depth", "ml"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    for c in ("time", "fetched_at"):
        df[c] = pd.to_datetime(df[c], errors="coerce")
    for c in ("source", "source_id"):
        df[c] = df[c].astype("string")
    return df


def accumulate(store, new):
    """把新抓到的事件併進累積檔（同 source_id 以較新抓到的為準）。"""
    parts = [typed(x) for x in (store, new) if len(x)]
    if not parts:
        return typed(pd.DataFrame())
    both = pd.concat(parts, ignore_index=True)
    both = both.sort_values("fetched_at").drop_duplicates("source_id", keep="last")
    return both.sort_values("time").reset_index(drop=True)


def merge_sources(prov):
    """USGS 與 CWA 重複的事件只留 CWA（規模尺度和正式目錄一致）。"""
    cwa = prov[prov["source"] == "CWA"]
    usgs = prov[prov["source"] == "USGS"]
    if len(cwa) and len(usgs):
        tc = cwa["time"].to_numpy("datetime64[ms]").astype("int64") / 1000.0
        dup = []
        for r in usgs.itertuples():
            dt = np.abs(tc - np.datetime64(r.time, "ms").astype("int64") / 1000.0)
            near = dt <= MATCH_S
            dup.append(bool(near.any() and (_km(r.lat, r.lon, cwa["lat"].to_numpy()[near],
                                                 cwa["lon"].to_numpy()[near]) <= MATCH_KM).any()))
        usgs = usgs[~np.array(dup, bool)]
    return pd.concat([cwa, usgs], ignore_index=True).sort_values("time").reset_index(drop=True)


# ---------------- 重算 ----------------

def load_params(base_dir=BASE_DIR):
    return json.loads((Path(base_dir) / "params.json").read_text(encoding="utf-8"))


def hazard_params(params):
    return hz.HazardParams(**params["params"])


def grid_of(params):
    return hz.Grid(**params["grid"])


def compute_hazard(events, params, t0):
    """以 params 的參數與校準倍率，算發布時間 t0 的每格期望數（與 06_hazard_model.export_latest 相同的欄位）。"""
    prm, grid = hazard_params(params), grid_of(params)
    cat = hz.Catalog(events, grid, prm.m_ref)
    out = grid.frame()
    own_bg = "calibration_factors_bg" in params     # 新版快照：「平常」用自己的校準倍率（審查 M5）
    bgp = hz.bg_only_params(prm, params["fit_info"]["mu_bg_only"]) if own_bg else None
    for T in params["export_horizons_days"]:
        a, b = hz.expected_ref(cat, t0, T, prm)
        f = float(params["calibration_factors"][f"{T:.5f}"])
        out[f"n_ref_{T:.5f}d"] = (a + b).ravel() * f
        if own_bg:
            a_bg, _ = hz.expected_ref(cat, t0, T, bgp)
            out[f"n_bg_{T:.5f}d"] = a_bg.ravel() * float(params["calibration_factors_bg"][f"{T:.5f}"])
        else:                                        # 舊快照：維持舊算法，載入時再用 mu 的比例換算
            out[f"n_bg_{T:.5f}d"] = a.ravel() * f
    return out


def assess_gap(base_events, params, gap_days, n_points=24, step_days=30, proxy_ml=GAP_PROXY_ML):
    """在正式目錄期間模擬「發布前 gap_days 天只剩 M≥proxy_ml」，回傳各時間長度的總數比（缺口版 ÷ 完整版）。

    取正式目錄最後 n_points 個、每隔 step_days 的發布時間，比值取中位數。比值 < 1 代表會低估。
    """
    if gap_days <= 0:
        return {}
    prm, grid = hazard_params(params), grid_of(params)
    end = pd.Timestamp(params.get("catalog_end") or base_events["time"].max()).normalize()
    t0s = [end - pd.Timedelta(days=step_days * k) for k in range(n_points)]
    full_cat = hz.Catalog(base_events, grid, prm.m_ref)
    ratios = {f"{T:.5f}": [] for T in params["export_horizons_days"]}
    for t0 in t0s:
        hole = (base_events["time"] >= t0 - pd.Timedelta(days=gap_days)) & (base_events["time"] < t0) \
            & (base_events["ml"] < proxy_ml)
        gap_cat = hz.Catalog(base_events[~hole], grid, prm.m_ref)
        for T in params["export_horizons_days"]:
            a, b = hz.expected_ref(full_cat, t0, T, prm)
            a2, b2 = hz.expected_ref(gap_cat, t0, T, prm)
            ratios[f"{T:.5f}"].append(float((a2 + b2).sum() / (a + b).sum()))
    return {k: float(np.median(v)) for k, v in ratios.items()}


def read_base_catalog(base_dir=BASE_DIR):
    return pd.read_csv(Path(base_dir) / "catalog_base.csv", parse_dates=["time"])


FETCH_LOG_COLS = ["fetched_at", "usgs_ok", "cwa_ok", "cwa_max_n"]
CWA_CAP = 16        # CWA API 每個資料集只回最新 16 筆


def append_fetch_log(path, row):
    """每次抓取記一行：什麼時候、USGS 與 CWA 各自成不成功、CWA 單一資料集回了幾筆。

    為什麼要另存：暫時事件裡的 fetched_at 只是「這個事件最後一次被看到的時間」（同一事件重抓會被覆蓋），
    不是抓取紀錄。預報卡的速判要知道「視窗內有沒有連續抓取」，沒有這份紀錄就沒辦法判斷會不會漏事件。
    """
    path = Path(path)
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        if new:
            f.write(",".join(FETCH_LOG_COLS) + "\n")
        f.write(",".join(str(row[c]) for c in FETCH_LOG_COLS) + "\n")


def read_fetch_log(path):
    """讀抓取紀錄；沒有檔案回傳 None。"""
    if not Path(path).exists():
        return None
    d = pd.read_csv(path, parse_dates=["fetched_at"])
    for c in ("usgs_ok", "cwa_ok"):
        d[c] = d[c].astype(str).str.lower().eq("true")
    return d


def read_store(path):
    if Path(path).exists():
        return typed(pd.read_csv(path))
    return typed(pd.DataFrame())


def update(now=None, base_dir=BASE_DIR, live_dir=LIVE_DIR, network=True, cwa_key=None, log=print,
           fetch_usgs_fn=fetch_usgs, fetch_cwa_fn=fetch_cwa):
    """抓暫時資料、重算、寫出 live 快照。回傳 data_status。"""
    base_dir, live_dir = Path(base_dir), Path(live_dir)
    live_dir.mkdir(parents=True, exist_ok=True)
    params = load_params(base_dir)
    grid = params["grid"]
    now = pd.Timestamp.now("UTC").tz_localize(None) if now is None else pd.Timestamp(now)
    t0 = now.floor(ISSUE_STEP)
    catalog_end = pd.Timestamp(params["catalog_end"])
    base_events = read_base_catalog(base_dir)
    store_path = live_dir / "provisional_events.csv"
    store = read_store(store_path)
    status = {"mode": "live", "catalog_end": str(catalog_end), "issue_time": str(t0),
              "updated_at": str(now.floor("s")), "errors": []}

    if network:
        fetched_at = now.floor("s")
        fetch_row = {"fetched_at": str(fetched_at), "usgs_ok": False, "cwa_ok": False, "cwa_max_n": 0}
        try:
            u = fetch_usgs_fn(catalog_end, now, grid)
            store = accumulate(store, u.assign(fetched_at=fetched_at))
            fetch_row["usgs_ok"] = True
            log(f"USGS：{len(u)} 筆")
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            status["errors"].append(f"USGS 取得失敗：{e}")
        key = cwa_key if cwa_key is not None else os.environ.get("CWA_API_KEY")
        if key:
            try:
                c = fetch_cwa_fn(key)
                store = accumulate(store, c.assign(fetched_at=fetched_at))
                fetch_row["cwa_ok"] = True
                # 每個資料集各回傳多少筆：達到上限（16）就可能有更早的報告被擠掉，裁決時要標出來
                fetch_row["cwa_max_n"] = (int(c["source_id"].astype(str).str.split(":").str[0].value_counts().max())
                                          if len(c) else 0)
                log(f"CWA 有感報告：{len(c)} 筆")
            except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
                status["errors"].append(f"CWA 取得失敗：{e}")
        else:
            status["errors"].append("沒有 CWA_API_KEY，只用 USGS（M4 以上）")
        store.to_csv(store_path, index=False, date_format="%Y-%m-%d %H:%M:%S")
        append_fetch_log(live_dir / "fetch_log.csv", fetch_row)

    prov = store[(store["time"] > catalog_end) & (store["time"] < t0)
                 & store["lat"].between(grid["lat_min"], grid["lat_max"])
                 & store["lon"].between(grid["lon_min"], grid["lon_max"])]
    prov = typed(merge_sources(prov)) if len(prov) else prov
    cols = ["time", "lat", "lon", "ml"]
    events = pd.concat([base_events[cols]] + ([prov[cols]] if len(prov) else []), ignore_index=True)
    events["time"] = pd.to_datetime(events["time"])
    for c in ("lat", "lon", "ml"):
        events[c] = events[c].astype("float64")

    latest = compute_hazard(events, params, t0)
    latest.to_csv(live_dir / "hazard_latest.csv", index=False, float_format="%.6g")

    gap_days = max((t0 - catalog_end) / pd.Timedelta(days=1), 0.0)
    status.update({
        "gap_days": round(gap_days, 2),
        "n_provisional": int(len(prov)),
        "n_provisional_by_source": {k: int(v) for k, v in prov["source"].value_counts().items()},
        "provisional_first_cwa": str(store.loc[store["source"] == "CWA", "time"].min()) if len(store) else None,
        "gap_ratio_proxy_ml": GAP_PROXY_ML,
        "gap_ratio": assess_gap(base_events, params, gap_days),
    })

    recent_base = pd.read_csv(base_dir / "recent_events.csv", parse_dates=["time"])
    recent = pd.concat([recent_base] + ([] if prov.empty else [prov.assign(source=prov["source"].map({"CWA": "CWA 有感報告（暫時）",
                                                                           "USGS": "USGS（暫時）"}))
                        [["time", "lat", "lon", "depth", "ml", "source"]]]), ignore_index=True)
    recent = recent[recent["time"] >= t0 - pd.Timedelta(days=365)]
    recent.to_csv(live_dir / "recent_events.csv", index=False, float_format="%.4f", date_format="%Y-%m-%d %H:%M:%S")

    live_params = dict(params, issue_time=str(t0), data_status=status)
    (live_dir / "params.json").write_text(json.dumps(live_params, ensure_ascii=False, indent=1), encoding="utf-8")
    (live_dir / "backtest.csv").write_bytes((base_dir / "backtest.csv").read_bytes())
    log(f"已更新到 {t0}（UTC）：暫時資料 {len(prov)} 筆，輸出 {live_dir}")
    return status


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=str(BASE_DIR))
    ap.add_argument("--live", default=str(LIVE_DIR))
    ap.add_argument("--now", default=None, help="發布時間（UTC），預設為現在")
    ap.add_argument("--no-network", action="store_true")
    args = ap.parse_args()
    st = update(now=args.now, base_dir=args.base, live_dir=args.live, network=not args.no_network)
    print(json.dumps(st, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
