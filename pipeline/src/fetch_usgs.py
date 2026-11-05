"""fetch_usgs.py：從 USGS FDSN event 服務抓取台灣周邊地震目錄，整理成統一欄位的 Parquet。

為什麼需要：
    GDMS 目錄落後約 1 個月且要登入下載；CWA 開放資料 API 只保留最新 16 筆報告。
    USGS 是唯一能完全自動化、隨時可重抓的來源，用來補外海（123°E 以東）事件、
    提供 Mw 與跨機構校對。比較見 docs/phase2/data_source_comparison.md。

資料來源：
    https://earthquake.usgs.gov/fdsnws/event/1/query （FDSN 標準，免金鑰）

三層資料（對應比較文件第 4 節）：
    第 1 層 原始回應  -> <EQ_DATA_DIR>/earthquake_data/raw/usgs_fdsn/*.csv  （只增不改，不進 git）
    第 2 層 標準化    -> pipeline/data/catalog/usgs.parquet                    （以 USGS id 去重）
    狀態檔            -> pipeline/data/catalog/usgs_state.json                 （上次成功時間與查詢條件）

用法：
    python pipeline/src/fetch_usgs.py backfill                       # 1990-01-01 至今
    python pipeline/src/fetch_usgs.py backfill --start 2020-01-01 --end 2021-01-01
    python pipeline/src/fetch_usgs.py update                         # 只抓上次之後新增或修訂的事件
    python pipeline/src/fetch_usgs.py rebuild                        # 不連網，用第 1 層原始檔重建 Parquet

已查證的 USGS 行為（2026-11-04）：
    - 單次查詢上限 20,000 筆，超過回 HTTP 400；先呼叫 /count 再決定要不要切段。
    - 只帶 updatedafter 時，starttime 預設為「30 天前」，舊事件的修訂會被漏掉，
      所以 update 一定要同時帶 starttime。
    - includedeleted=true 只能搭配 csv／geojson 的 query（/count 不接受）；
      被刪除的事件 status = "deleted"，其他欄位大多是空的。
    - 查無資料時回 HTTP 200，內容只有表頭。
    - 部分舊事件的 mag、magType 是空值（台灣框 1990–2009 有 772 筆沒有 magType，其中 427 筆也沒有 mag）。
    - 被刪除的事件包含「併入主事件後撤銷」的重複紀錄，例如 921 的 ISC-GEM 版本
      iscgem1656082。下游一定要排除 status == "deleted"，否則同一地震會被算兩次。
    - CSV 格式沒有 ids（同一事件的其他編號）欄位，只有 GeoJSON 有；
      所以 source_event_aliases 目前一律為 NA。

欄位規格：docs/phase2/catalog_event_matching_spec.md（PR #4）第 3.1 節。
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("EQ_DATA_DIR", BASE_DIR))
RAW_DIR = DATA_DIR / "earthquake_data" / "raw" / "usgs_fdsn"
OUT_DIR = BASE_DIR / "pipeline" / "data" / "catalog"

BASE_URL = "https://earthquake.usgs.gov/fdsnws/event/1"
USER_AGENT = "eq-risk-student-project/1.0 (NKUST)"

# ========== 參數 ==========
# 範圍取「每日日報台灣框」與「跨機構比對回測框」的聯集：
# 北界 26.5° 涵蓋與那國島以北，東界 124° 涵蓋 USGS 補強最多的東部外海。
DEFAULT_BBOX = (21.0, 26.5, 119.0, 124.0)   # (minlat, maxlat, minlon, maxlon)
DEFAULT_START = "1990-01-01"                # 與 events.parquet 的起點一致
DEFAULT_MIN_MAG = None                      # 不設下限：台灣框實質只收錄 M4 上下，不會太多
QUERY_LIMIT = 20000                         # USGS 單次查詢上限（/count 回傳的 maxAllowed）
SAFETY_LIMIT = 18000                        # 切段門檻：留餘裕，避免 count 與 query 之間有新事件
UPDATE_OVERLAP = dt.timedelta(hours=2)      # update 往前重疊的時間，避免時鐘誤差漏事件
REQUEST_PAUSE_S = 1.0                       # 每次請求之間的間隔，對公開服務保持禮貌
MAX_RETRIES = 4

# USGS CSV 欄位 -> 統一欄位。統一欄位的定義見比較文件 §4。
COLUMN_MAP = {
    "time": "time",
    "latitude": "lat",
    "longitude": "lon",
    "depth": "depth",
    "mag": "mag",
    "magType": "mag_type",
    "gap": "gap",
    "horizontalError": "horiz_err_km",
    "depthError": "depth_err_km",
    "id": "source_event_id",
    "updated": "source_updated",
    # 以下為 USGS 特有、保留備查
    "status": "status",
    "type": "event_type",
    "place": "place",
    "nst": "nst",
    "dmin": "dmin_deg",
    "rms": "rms",
    "magError": "mag_error",
    "net": "net",
}
FLOAT_COLS = ["lat", "lon", "depth", "mag", "gap", "horiz_err_km", "depth_err_km",
              "nst", "dmin_deg", "rms", "mag_error"]
STR_COLS = ["mag_type", "source_event_id", "status", "event_type", "place", "net"]
OUTPUT_COLUMNS = ["time", "lat", "lon", "depth", "mag", "mag_type", "mag_type_norm", "gap",
                  "horiz_err_km", "depth_err_km", "depth_fixed", "agency", "source",
                  "source_event_id", "source_event_aliases", "source_updated", "fetched_at",
                  "status", "event_type", "place", "nst", "dmin_deg", "rms", "mag_error", "net"]

AGENCY = "USGS"   # ComCat 由 USGS 彙整；實際定位網路記在 net 欄

# 規模類型標準化（規格 3.1 節）。USGS 的 magType 是小寫自由字串；
# 沒列在這裡的（例如 "m"：未註明類型）一律為 NA，避免把不同尺度混在一起。
MAG_TYPE_NORM = {
    "ml": "ML", "mb": "mb", "mb_lg": "mb", "ms": "Ms", "ms_20": "Ms", "md": "Md",
    "mw": "Mw", "mww": "Mw", "mwr": "Mw", "mwc": "Mw", "mwb": "Mw", "mwp": "Mw",
}

# 原始檔名：<抓取時間>_<模式>_<查詢起>_<查詢迄>.csv，rebuild 依抓取時間排序重放
RAW_NAME_RE = re.compile(r"^(\d{8}T\d{6}Z)_")


# ---------------------------------------------------------------- 時間工具
def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def parse_utc(text):
    """把 '2020-01-01' 或 ISO 時間字串轉成帶 UTC 時區的 datetime。"""
    ts = pd.Timestamp(text)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.to_pydatetime()


def iso(t):
    """FDSN 接受的時間格式（UTC，不帶時區字尾）。"""
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------- HTTP
class UsgsQueryError(RuntimeError):
    """USGS 回 4xx：查詢本身有問題，重試也沒用。"""


def http_get(url, opener=urllib.request.urlopen, sleep=time.sleep, timeout=120):
    """GET 並回傳文字。5xx、429、連線錯誤會指數退避重試；4xx 直接丟出錯誤訊息。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(MAX_RETRIES + 1):
        try:
            with opener(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code != 429 and 400 <= e.code < 500:
                body = e.read().decode("utf-8", errors="replace")
                raise UsgsQueryError(f"HTTP {e.code}：{body.strip()[:500]}") from e
            err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            err = e
        if attempt == MAX_RETRIES:
            raise RuntimeError(f"重試 {MAX_RETRIES} 次仍失敗：{url}") from err
        wait = 2 ** attempt * 5
        print(f"  請求失敗（{err}），{wait} 秒後重試…", file=sys.stderr)
        sleep(wait)


def build_params(start, end, bbox, min_mag=None, updated_after=None, fmt="csv"):
    """組 FDSN 查詢參數。starttime 一律明確帶上（見模組說明：預設只查 30 天）。"""
    minlat, maxlat, minlon, maxlon = bbox
    params = {
        "starttime": iso(start),
        "endtime": iso(end),
        "minlatitude": minlat,
        "maxlatitude": maxlat,
        "minlongitude": minlon,
        "maxlongitude": maxlon,
        "orderby": "time-asc",
    }
    if min_mag is not None:
        params["minmagnitude"] = min_mag
    if updated_after is not None:
        params["updatedafter"] = iso(updated_after)
    if fmt is not None:
        params["format"] = fmt
        # 要拿到「被刪除」的事件才能把它從第 2 層標記掉；只有 query 接受這個參數
        params["includedeleted"] = "true"
    return params


def count_events(params, get=http_get):
    """呼叫 /count。注意 /count 不接受 includedeleted，所以拿掉 format 相關參數。"""
    p = {k: v for k, v in params.items() if k not in ("format", "includedeleted", "orderby")}
    p["format"] = "geojson"
    text = get(f"{BASE_URL}/count?{urllib.parse.urlencode(p)}")
    return int(json.loads(text)["count"])


# ---------------------------------------------------------------- 切段
def plan_chunks(start, end, count_fn, limit=SAFETY_LIMIT, min_span=dt.timedelta(hours=1)):
    """把 [start, end] 切成每段筆數 <= limit 的時間段（筆數超過就對半切）。

    count_fn(start, end) -> int。回傳 [(start, end, n), ...]，依時間排序。
    """
    n = count_fn(start, end)
    if n <= limit:
        return [(start, end, n)]
    if end - start <= min_span:
        raise RuntimeError(f"{iso(start)}～{iso(end)} 有 {n} 筆，切到 {min_span} 仍超過上限，"
                           f"請縮小範圍或提高 --min-mag")
    mid = start + (end - start) / 2
    mid = mid.replace(microsecond=0)
    return plan_chunks(start, mid, count_fn, limit, min_span) + \
        plan_chunks(mid, end, count_fn, limit, min_span)


# ---------------------------------------------------------------- 解析與合併
def normalize_mag_type(s):
    """USGS magType -> 規格的標準化類型（ML、mb、Ms、Md、Mw），無法判定時為 NA。"""
    return s.astype("string").str.strip().str.lower().map(MAG_TYPE_NORM).astype("string")


def ensure_columns(df):
    """補上規格新增的欄位。

    為什麼需要：PR #4 規格定案前寫出的 usgs.parquet 沒有這些欄位，
    直接 upsert 會因為缺欄位而失敗。可以從既有欄位推得的就推得，其他填 NA。
    """
    df = df.copy()
    if "agency" not in df.columns:
        df["agency"] = AGENCY
    if "mag_type_norm" not in df.columns:
        df["mag_type_norm"] = normalize_mag_type(df["mag_type"])
    if "depth_fixed" not in df.columns:
        df["depth_fixed"] = pd.array([pd.NA] * len(df), dtype="boolean")
    if "source_event_aliases" not in df.columns:
        df["source_event_aliases"] = pd.array([pd.NA] * len(df), dtype="string")
    return df[OUTPUT_COLUMNS]


def parse_csv(text, fetched_at):
    """USGS CSV 文字 -> 統一欄位 DataFrame。時間一律為不帶時區的 UTC（與 events.parquet 一致）。"""
    raw = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    df = pd.DataFrame(index=raw.index)
    for src, dst in COLUMN_MAP.items():
        df[dst] = raw[src] if src in raw.columns else ""
    df = df.replace("", pd.NA)
    for c in FLOAT_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    for c in STR_COLS:
        df[c] = df[c].astype("string")
    for c in ("time", "source_updated"):
        df[c] = pd.to_datetime(df[c], utc=True, format="ISO8601").dt.tz_localize(None)
    df["source"] = "USGS"
    df["agency"] = AGENCY
    df["mag_type_norm"] = normalize_mag_type(df["mag_type"])
    # USGS CSV 沒有「深度固定」旗標；不用「深度 = 10／33 km」推測（規格 3.1 節）
    df["depth_fixed"] = pd.array([pd.NA] * len(df), dtype="boolean")
    df["source_event_aliases"] = pd.array([pd.NA] * len(df), dtype="string")
    df["fetched_at"] = pd.Timestamp(fetched_at).tz_convert("UTC").tz_localize(None)
    return df[OUTPUT_COLUMNS]


def upsert(existing, new):
    """以 source_event_id 合併：同一事件保留 source_updated 最新的版本（同時間取較晚抓到的）。

    被刪除的事件（status = deleted）也照樣保留一列，下游用 status 過濾，
    這樣才看得出「這個事件曾經存在、後來被 USGS 撤銷」。
    """
    frames = [ensure_columns(f) for f in (existing, new) if f is not None and len(f)]
    if not frames:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values(["source_event_id", "source_updated", "fetched_at"], na_position="first")
    df = df.drop_duplicates("source_event_id", keep="last")
    return df.sort_values("time", na_position="last").reset_index(drop=True)[OUTPUT_COLUMNS]


# ---------------------------------------------------------------- 讀寫
def save_raw(text, fetched_at, mode, start, end, raw_dir):
    raw_dir.mkdir(parents=True, exist_ok=True)
    name = (f"{fetched_at.strftime('%Y%m%dT%H%M%SZ')}_{mode}_"
            f"{start.strftime('%Y%m%dT%H%M%S')}_{end.strftime('%Y%m%dT%H%M%S')}.csv")
    path = raw_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def load_parquet(path):
    return pd.read_parquet(path) if path.exists() else None


def write_parquet(df, path):
    """先寫暫存檔再取代，避免中途失敗留下壞檔。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def load_state(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def write_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def query_signature(bbox, min_mag, catalog_start):
    """查詢條件；條件改變時 update 會拒絕執行，要求先 backfill。"""
    return {"bbox": list(bbox), "min_mag": min_mag, "catalog_start": iso(catalog_start)}


# ---------------------------------------------------------------- 主流程
def fetch_window(start, end, bbox, min_mag, mode, raw_dir, updated_after=None,
                 get=http_get, sleep=time.sleep, now=utc_now):
    """抓一個時間窗（必要時自動切段），回傳合併後的 DataFrame。"""
    def count_fn(s, e):
        n = count_events(build_params(s, e, bbox, min_mag, updated_after), get=get)
        sleep(REQUEST_PAUSE_S)
        return n

    chunks = plan_chunks(start, end, count_fn)
    print(f"  {len(chunks)} 段，預估 {sum(n for _, _, n in chunks)} 筆（未含已刪除事件）")
    frames = []
    for s, e, n in chunks:
        fetched_at = now()
        url = f"{BASE_URL}/query?{urllib.parse.urlencode(build_params(s, e, bbox, min_mag, updated_after))}"
        text = get(url)
        path = save_raw(text, fetched_at, mode, s, e, raw_dir)
        df = parse_csv(text, fetched_at)
        print(f"  {iso(s)} ～ {iso(e)}：{len(df)} 筆 -> {path.name}")
        frames.append(df)
        sleep(REQUEST_PAUSE_S)
    return upsert(None, pd.concat(frames, ignore_index=True)) if frames else upsert(None, None)


def run_backfill(args, out_dir, raw_dir, **kw):
    run_started = kw.get("now", utc_now)()
    start = parse_utc(args.start)
    end = parse_utc(args.end) if args.end else run_started
    bbox = tuple(args.bbox)

    state_path = out_dir / "usgs_state.json"
    state = load_state(state_path) or {}
    if state and (state.get("bbox") != list(bbox) or state.get("min_mag") != args.min_mag):
        print(f"警告：查詢條件與既有資料不同（既有 bbox={state.get('bbox')}、min_mag={state.get('min_mag')}），"
              f"兩種範圍會混在同一個 Parquet；若要換範圍，請改用新的 --out-dir", file=sys.stderr)

    print(f"回補 {iso(start)} ～ {iso(end)}，範圍 {bbox}，最小規模 {args.min_mag}")
    new = fetch_window(start, end, bbox, args.min_mag, "backfill", raw_dir, **kw)
    out = out_dir / "usgs.parquet"
    merged = upsert(load_parquet(out), new)
    write_parquet(merged, out)

    sig = query_signature(bbox, args.min_mag, start)
    # 只有「回補到現在」才更新 update 的起點；回補一段歷史不影響增量進度
    if not args.end:
        state.update(sig)
        state["last_success_utc"] = iso(run_started)
        write_state(state_path, state)
    report(merged, out)


def run_update(args, out_dir, raw_dir, **kw):
    state_path = out_dir / "usgs_state.json"
    state = load_state(state_path)
    if not state or "last_success_utc" not in state:
        raise SystemExit("找不到上次成功紀錄，請先執行：python pipeline/src/fetch_usgs.py backfill")
    bbox = tuple(state["bbox"])
    min_mag = state["min_mag"]
    catalog_start = parse_utc(state["catalog_start"])
    run_started = kw.get("now", utc_now)()
    updated_after = parse_utc(state["last_success_utc"]) - UPDATE_OVERLAP
    print(f"增量更新：{iso(updated_after)} 之後新增或修訂的事件（範圍 {bbox}）")
    new = fetch_window(catalog_start, run_started, bbox, min_mag, "update", raw_dir,
                       updated_after=updated_after, **kw)
    out = out_dir / "usgs.parquet"
    merged = upsert(load_parquet(out), new)
    write_parquet(merged, out)
    state["last_success_utc"] = iso(run_started)
    write_state(state_path, state)
    report(merged, out, n_new=len(new))


def run_rebuild(out_dir, raw_dir):
    """不連網：依抓取時間重放所有原始檔，重建第 2 層。"""
    files = sorted(p for p in raw_dir.glob("*.csv") if RAW_NAME_RE.match(p.name))
    if not files:
        raise SystemExit(f"{raw_dir} 沒有原始檔")
    merged = None
    for p in files:
        fetched_at = dt.datetime.strptime(RAW_NAME_RE.match(p.name).group(1), "%Y%m%dT%H%M%SZ") \
            .replace(tzinfo=dt.timezone.utc)
        merged = upsert(merged, parse_csv(p.read_text(encoding="utf-8"), fetched_at))
    out = out_dir / "usgs.parquet"
    write_parquet(merged, out)
    print(f"已用 {len(files)} 個原始檔重建")
    report(merged, out)


def report(df, out, n_new=None):
    active = df[df["status"].fillna("") != "deleted"]
    print(f"完成：{out}")
    if n_new is not None:
        print(f"  本次取得 {n_new} 筆（新增或修訂）")
    print(f"  總計 {len(df)} 筆，其中有效 {len(active)} 筆、已刪除 {len(df) - len(active)} 筆")
    if len(active):
        print(f"  時間 {active['time'].min()} ～ {active['time'].max()}（UTC）")
        print(f"  規模類型：{active['mag_type'].value_counts(dropna=False).head(5).to_dict()}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="抓取 USGS FDSN 地震目錄（台灣周邊）")
    ap.add_argument("mode", choices=["backfill", "update", "rebuild"])
    ap.add_argument("--start", default=DEFAULT_START, help="backfill 起始日（UTC），預設 1990-01-01")
    ap.add_argument("--end", default=None, help="backfill 結束日（UTC），預設現在")
    ap.add_argument("--bbox", nargs=4, type=float, default=list(DEFAULT_BBOX),
                    metavar=("MINLAT", "MAXLAT", "MINLON", "MAXLON"))
    ap.add_argument("--min-mag", type=float, default=DEFAULT_MIN_MAG)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR, help="第 2 層輸出資料夾")
    ap.add_argument("--raw-dir", type=Path, default=RAW_DIR, help="第 1 層原始檔資料夾")
    args = ap.parse_args(argv)

    try:
        if args.mode == "backfill":
            run_backfill(args, args.out_dir, args.raw_dir)
        elif args.mode == "update":
            run_update(args, args.out_dir, args.raw_dir)
        else:
            run_rebuild(args.out_dir, args.raw_dir)
    except UsgsQueryError as e:
        raise SystemExit(f"USGS 拒絕查詢：{e}")


if __name__ == "__main__":
    main()
