"""B2 第四步：GDMS GNSS（RINEX 2.11 觀測檔）→ 每站 15 分鐘垂直 TEC，串流處理。

和 case_ndh5_tec.py 用同一套 gnss_tec 模組，差別是：
- 批次：收件匣裡任何站、任何日的檔案都處理；多核心平行（--workers）。
- 輔助資料改用 2018–2026 每天都有的來源（2026-11-08 實測 BKG）：
    星曆：BRDC00WRD_R_*_MN.rnx.gz（RINEX 3），只留 GPS 存成約 60 kB 的 parquet，原檔刪除
    衛星 DCB：CODE 每月 P1−P2；接收機只有 C1 時，用 CODE 每月 P1−C1 換算 P1
- 原始 RINEX 處理完即可刪除（--delete-raw）；事件窗口內的保留（--keep-events）。

每個站日的步驟：
    RINEX → (C1→P1 換算) → 斜向 TEC、相位平滑 → 衛星 DCB（正負號由資料決定，記錄兩者殘差）
    → 接收機偏差（最小標準差法 + 平面梯度，UTC 20–22 時 = 台灣黎明前 04–07 時）
    → 垂直 TEC（仰角 ≥ 30°）→ 15 分鐘中位數
輸出（pipeline/data/tec/，不進版控）：
    15min/<STA>/<YYYY>/<STA>_<YYYY-MM-DD>.parquet
    daily/<STA>/<YYYY-MM-DD>.json      偏差、殘差、DCB 來源與距今天數、P1 是否由 C1 換算、資料涵蓋時數
    tec_15min.parquet、tec_daily.parquet（每次執行後重建）
    manifest.jsonl

用法：
    python pipeline/src/11_process_gnss_tec.py --inbox <資料夾> --workers 8 --delete-raw \\
        --keep-events earthquake_data/processed/event_list_M55_mainshocks_2018_2026.csv
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gnss_tec import aux_data, orbit, rinex, tec  # noqa: E402
from stream_io import Manifest, RawDisposer, Stream, iter_inbox, load_keep_windows  # noqa: E402

BASE_DIR = Path(__file__).resolve().parents[2]
OUT_DIR = BASE_DIR / "pipeline" / "data" / "tec"
NAME_RE = re.compile(r"^([a-z0-9]{4})(\d{3})0\.(\d{2})o(\.gz)?$", re.IGNORECASE)
BIAS_UTC_HOURS = range(20, 23)     # 台灣 04–07 時；理由見 case_ndh5_tec.py
EL_MIN = 30.0
FREQ = "15min"
VERSION = "tec-1"


def parse_name(name):
    m = NAME_RE.match(name)
    if not m:
        return None, None
    yy, doy = int(m.group(3)), int(m.group(2))
    date = pd.Timestamp(year=2000 + yy if yy < 80 else 1900 + yy, month=1, day=1) + pd.Timedelta(days=doy - 1)
    return m.group(1).upper(), date


def process_file(path, name, nav_path, p1p2, p1c1):
    """在子程序中執行。回傳 (15 分鐘 DataFrame, 每日 dict)。"""
    sta, date = parse_name(name)
    h, obs = rinex.read_obs(path)
    if obs.empty:
        raise ValueError("RINEX 沒有 GPS 觀測")
    obs, p1_from_c1 = tec.fill_p1_from_c1(obs, p1c1 or {})
    nav = pd.read_parquet(nav_path)
    st = tec.slant_tec(obs, nav, h["approx_xyz"])
    sign_resid = {}
    for sign in (+1, -1):
        s = tec.apply_sat_dcb(st, p1p2, sign)
        sign_resid[sign] = tec.estimate_receiver_bias(s, el_min=EL_MIN, hours_utc=None)[1]
    sign = min(sign_resid, key=lambda k: np.inf if np.isnan(sign_resid[k]) else sign_resid[k])
    s = tec.apply_sat_dcb(st, p1p2, sign)
    bias, resid, n_ep = tec.estimate_receiver_bias(s, el_min=EL_MIN, hours_utc=BIAS_UTC_HOURS)
    if np.isnan(bias):
        raise ValueError("黎明前時段沒有足夠衛星，無法估接收機偏差")
    v = tec.station_series(tec.vertical_tec(s, bias, el_min=EL_MIN), freq=FREQ)
    lat, lon, _ = orbit.ecef_to_geodetic(*h["approx_xyz"])
    day = {
        "station": sta, "date": str(date.date()), "lat": float(np.degrees(lat)) if abs(lat) < 2 else float(lat),
        "lon": float(np.degrees(lon)) if abs(lon) < 4 else float(lon),
        "receiver": h.get("receiver", ""), "interval_s": h.get("interval", np.nan),
        # 儀器識別：型號相同、只換序號或韌體也會讓偏差跳動（NDHU 2026-10-20 實例）
        "instrument_id": "|".join(h.get(k, "") for k in ("receiver_sn", "receiver", "receiver_fw",
                                                          "antenna_sn", "antenna")),
        "first_obs": str(obs["time"].min()), "last_obs": str(obs["time"].max()),
        "n_obs_epochs": int(obs["time"].nunique()),
        "hours_covered": float(v["vtec"].notna().sum() / 4.0),
        "p1_from_c1": bool(p1_from_c1), "dcb_sign": int(sign),
        "resid_sign_pos": float(sign_resid[1]), "resid_sign_neg": float(sign_resid[-1]),
        "rx_bias_tecu": float(bias), "rx_bias_resid_tecu": float(resid), "rx_bias_epochs": int(n_ep),
        "vtec_mean": float(v["vtec"].mean()), "vtec_max": float(v["vtec"].max()),
    }
    return v.astype("float32"), day


def rebuild(out_dir):
    days = [json.loads(p.read_text(encoding="utf-8")) for p in (out_dir / "daily").rglob("*.json")]
    if not days:
        return None, None
    daily = pd.DataFrame(days)
    daily["date"] = pd.to_datetime(daily["date"])
    daily = daily.sort_values(["station", "date"]).reset_index(drop=True)
    daily.to_parquet(out_dir / "tec_daily.parquet", index=False)
    parts = []
    for p in (out_dir / "15min").rglob("*.parquet"):
        s = pd.read_parquet(p)
        s["station"] = p.parent.parent.name
        parts.append(s.reset_index())
    series = pd.concat(parts, ignore_index=True).sort_values(["station", "time"])
    series.to_parquet(out_dir / "tec_15min.parquet", index=False)
    return daily, series


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inbox", required=True)
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--aux", default=None, help="星曆與 DCB 快取（預設 <out>/aux）")
    ap.add_argument("--delete-raw", action="store_true")
    ap.add_argument("--keep-events", default=None)
    ap.add_argument("--keep-days", type=int, default=7)
    ap.add_argument("--keep-dir", default=None)
    ap.add_argument("--stations", default=None, help="只處理這些測站（逗號分隔），其餘不動")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    args = ap.parse_args()

    out_dir = Path(args.out)
    aux_dir = Path(args.aux) if args.aux else out_dir / "aux"
    manifest = Manifest(out_dir / "manifest.jsonl")
    windows = load_keep_windows(args.keep_events, args.keep_days)
    keep_dir = Path(args.keep_dir) if args.keep_dir else (out_dir / "raw_keep" if windows else None)
    disposer = RawDisposer(args.delete_raw, keep_dir, windows)
    only = {s.strip().upper() for s in args.stations.split(",")} if args.stations else None

    def accept(n):
        sta, _ = parse_name(n)
        return sta is not None and (only is None or sta in only)

    items = iter_inbox(args.inbox, accept)
    print(f"收件匣：{args.inbox}，找到 {len(items)} 個 RINEX 觀測檔")
    date_of = lambda n: parse_name(n)[1]  # noqa: E731
    # 分批串流：同時最多 workers × 2 個檔案在處理中；zip 成員只在處理時才解到暫存資料夾
    stream = Stream(manifest, disposer, date_of, max_inflight=args.workers * 2)

    aux = {}

    def get_aux(d):
        """輔助資料在主程序依日期下載（避免多個子程序同時下載同一個檔案），每天只下載一次。"""
        if d not in aux:
            nav, msg = aux_data.get_nav(d, aux_dir)
            p1p2, f1, age, l1 = aux_data.get_dcb(d, aux_dir)
            p1c1, f2, _, l2 = aux_data.get_dcb(d, aux_dir, kind="p1c1")
            cache = aux_dir / "nav_gps" / f"{d.year}" / f"{d.dayofyear:03d}.parquet"
            aux[d] = {"nav": cache if nav is not None else None, "p1p2": p1p2, "p1c1": p1c1,
                      "dcb_file": f1, "dcb_age_days": age, "p1c1_file": f2, "log": [msg] + l1 + l2}
        return aux[d]

    tmp = Path(tempfile.mkdtemp(prefix="tec_"))
    tmp_paths = {}
    n_ok = n_fail = 0

    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        def submit(it, data, digest):
            nonlocal n_fail
            d = date_of(it.name)
            a = get_aux(d)
            if a["nav"] is None or a["p1p2"] is None:
                manifest.add(sha256=digest, name=it.name, size=len(data), status="error",
                             error="缺少星曆或 DCB：" + " | ".join(a["log"]), version=VERSION)
                disposer.done(it, d, False)
                n_fail += 1
                return None
            path = it.path
            if it.member is not None:
                path = tmp / f"{digest[:12]}_{it.name}"
                path.write_bytes(data)
                tmp_paths[digest] = path
            return ex.submit(process_file, path, it.name, a["nav"], a["p1p2"], a["p1c1"])

        for k, job in enumerate(stream.run(items, submit), 1):
            it, digest, size = job.item, job.digest, job.size
            d = date_of(it.name)
            try:
                v, day = job.future.result()
                day.update(dcb_file=aux[d]["dcb_file"], dcb_age_days=aux[d]["dcb_age_days"],
                           p1c1_file=aux[d]["p1c1_file"] if day["p1_from_c1"] else None)
                sta = day["station"]
                sp = out_dir / "15min" / sta / f"{d.year}" / f"{sta}_{d.date()}.parquet"
                sp.parent.mkdir(parents=True, exist_ok=True)
                v.to_parquet(sp)
                jp = out_dir / "daily" / sta / f"{d.date()}.json"
                jp.parent.mkdir(parents=True, exist_ok=True)
                jp.write_text(json.dumps(day, ensure_ascii=False, default=float), encoding="utf-8")
                manifest.add(sha256=digest, name=it.name, size=size, status="ok", station=sta,
                             date=day["date"], hours=day["hours_covered"], version=VERSION)
                n_ok += 1
                ok = True
            except Exception as e:  # noqa: BLE001
                manifest.add(sha256=digest, name=it.name, size=size, status="error", error=repr(e),
                             version=VERSION)
                print(f"  失敗 {it}: {e}")
                n_fail += 1
                ok = False
            p = tmp_paths.pop(digest, None)
            if p is not None:
                p.unlink(missing_ok=True)
            stream.done(job, ok)
            if k % 50 == 0:
                print(f"  已處理 {k} 個（收件匣共 {len(items)} 個；輔助資料 {len(aux)} 天）")
    disposer.finish()
    shutil.rmtree(tmp, ignore_errors=True)
    if stream.n_dup:
        print(f"已處理過或重複（相同 SHA-256）：{stream.n_dup} 個，略過")
    daily, _ = rebuild(out_dir)
    print(f"完成：成功 {n_ok}、失敗 {n_fail}；刪除原始檔 {disposer.deleted}、保留 {disposer.kept}")
    if daily is not None:
        s = daily.groupby("station").agg(days=("date", "size"), first=("date", "min"), last=("date", "max"),
                                         hours=("hours_covered", "mean"), bias_resid=("rx_bias_resid_tecu", "median"),
                                         p1_from_c1=("p1_from_c1", "mean"), vtec_mean=("vtec_mean", "mean"))
        print(s.round(2).to_string())


if __name__ == "__main__":
    main()
