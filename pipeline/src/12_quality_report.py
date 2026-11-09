"""B2 資料品質報告（FR-B2-23）：下載、處理到哪裡了？哪些站、哪些年還有缺口？

讀 10_process_magnet.py、11_process_gnss_tec.py 的紀錄，不需要原始檔，也不用網路：
    pipeline/data/magnet/manifest.jsonl、magnet/daily/<STA>/<日期>.json
    pipeline/data/tec/manifest.jsonl、tec/daily/<STA>/<日期>.json

報告內容：
1. 每站每年的涵蓋率：應有天數、已處理天數、可用天數（地磁 1 秒涵蓋率 ≥ 90%；TEC ≥ 20 小時，
   門檻與 02_clean_features.py 相同）。
2. 需要補抓的站年：可用率 < 80%；GNSS 附上下載清單中的備選站。
3. 資料缺口：連續 ≥ 7 天沒有資料的區間。
4. 處理失敗的檔案與原因（後來重試成功、或同一站日有別的檔案成功的不算）。
5. TEC 的 DCB 沿用：當月 CODE DCB 還沒公布、改用前幾個月的天數。
6. 儀器更換日：TEC 用 02_clean_features.py 的 instrument_segments（和背景分段完全一致）；
   地磁沒有公開的儀器紀錄，只列「可能換儀器」的候選日（標頭座標或資料類型改變、日平均 H／Z 跳動），需人工確認。

輸出（預設 docs/reports/b2_data_quality/）：
    README.md                       給人看的報告
    coverage_by_station_year.csv    每站每年的涵蓋率
    gaps.csv、failures.csv、instrument_changes.csv、dcb_fallback.csv

用法：
    python pipeline/src/12_quality_report.py
    python pipeline/src/12_quality_report.py --start 2026-10-01 --end 2026-10-31 --out <資料夾>
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path(__file__).resolve().parent
BASE_DIR = SRC.parents[1]
DATA = BASE_DIR / "pipeline" / "data"
OUT_DIR = BASE_DIR / "docs" / "reports" / "b2_data_quality"

PERIOD = ("2018-01-01", "2026-09-30")
USABLE_TARGET = 0.8          # 可用率低於此值的站年列入「需要補抓」
GAP_MIN_DAYS = 7             # 連續幾天沒資料才列為缺口
MAG_JUMP_NT = 100.0          # 地磁日平均 H 或 Z 與前 5 天中位數相差超過此值 → 可能換儀器
MAG_POS_DEG = 0.01           # 標頭座標改變超過此值（約 1 km）→ 可能搬站
DCB_SAME_MONTH_DAYS = 16     # 和所用 DCB 月份中點相差超過 16 天 = 不是當月的 DCB

# 下載清單（docs/b2_download_plan/README.md 第 2 節）
MAGNET_STATIONS = ["XCG", "HLN", "YHG", "NCG", "CNU", "TTN", "LNU", "KMN"]
GNSS_STATIONS = {
    "PLIN": ["TSHI", "FIVE"], "HSIN": ["YAME", "WIPN"], "ILAN": ["LTUN", "SLNP"],
    "TUNM": ["BLOW", "DNFU"], "CHIU": ["DAHU", "YUSN"], "GAIS": ["ZEND", "NEMN"],
    "DCHU": ["CHUL", "TTCS"], "LIKN": ["CTOU", "SGAN"], "DANL": ["HENC"], "PANG": [],
}


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), SRC / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B2 = _load_script("02_clean_features.py")


def _name_parsers():
    """MAGNET、GNSS 的檔名解析（回傳 (測站, 日期)）；延後載入，避免測試時連帶載入多核心處理程式。"""
    return {"magnet": _load_script("10_process_magnet.py").parse_name,
            "tec": _load_script("11_process_gnss_tec.py").parse_name}


# ---------------------------------------------------------------- 讀取

def load_manifest(path):
    """manifest.jsonl → DataFrame，每個 SHA-256 只留最後一筆（失敗後重試成功的，以成功為準）。"""
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=["sha256", "name", "status", "error", "processed_at"])
    rows = [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines() if s.strip()]
    if not rows:
        return pd.DataFrame(columns=["sha256", "name", "status", "error", "processed_at"])
    df = pd.DataFrame(rows)
    return df.drop_duplicates("sha256", keep="last").reset_index(drop=True)


def load_daily(daily_dir):
    """daily/<STA>/<日期>.json → DataFrame（station、date 已排序）。"""
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(daily_dir).rglob("*.json"))]
    if not rows:
        return pd.DataFrame(columns=["station", "date"])
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    return df.sort_values(["station", "date"]).drop_duplicates(["station", "date"], keep="last") \
             .reset_index(drop=True)


def usable_mask(daily, kind):
    """可用日：門檻與 02_clean_features.py 相同。"""
    if daily.empty:
        return pd.Series(dtype=bool)
    if kind == "magnet":
        return daily["coverage"].astype(float) >= B2.MIN_MAG_COVERAGE
    return daily["hours_covered"].astype(float) >= B2.MIN_TEC_HOURS


# ---------------------------------------------------------------- 涵蓋率與缺口

def coverage_by_year(daily, kind, stations, start, end):
    """每站每年：應有天數、已處理天數、可用天數、可用率。stations 中沒有任何資料的站也會列出（全 0）。"""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    days = pd.date_range(start, end, freq="D")
    expected = pd.Series(days.year).value_counts().sort_index()
    d = daily[(daily["date"] >= start) & (daily["date"] <= end)].copy() if not daily.empty else daily
    if not d.empty:
        d["usable"] = usable_mask(d, kind).to_numpy()
        d["year"] = d["date"].dt.year
    names = list(dict.fromkeys(list(stations) + (sorted(set(d["station"])) if not d.empty else [])))
    rows = []
    for sta in names:
        g = d[d["station"] == sta] if not d.empty else d
        for y, n_exp in expected.items():
            gy = g[g["year"] == y] if not g.empty else g
            n_proc = int(len(gy))
            n_use = int(gy["usable"].sum()) if n_proc else 0
            rows.append({"source": kind, "station": sta, "year": int(y), "expected_days": int(n_exp),
                         "processed_days": n_proc, "usable_days": n_use,
                         "usable_pct": round(100.0 * n_use / n_exp, 1)})
    return pd.DataFrame(rows)


def find_gaps(daily, kind, stations, start, end, min_days=GAP_MIN_DAYS):
    """每站連續 ≥ min_days 天沒有「已處理」資料的區間。"""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    all_days = pd.date_range(start, end, freq="D")
    rows = []
    names = list(dict.fromkeys(list(stations) + (sorted(set(daily["station"])) if not daily.empty else [])))
    for sta in names:
        have = set(daily.loc[daily["station"] == sta, "date"]) if not daily.empty else set()
        missing = np.array([d not in have for d in all_days])
        i = 0
        while i < len(all_days):
            if not missing[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(all_days) and missing[j + 1]:
                j += 1
            n = j - i + 1
            if n >= min_days:
                rows.append({"source": kind, "station": sta, "first_missing": all_days[i].date(),
                             "last_missing": all_days[j].date(), "days": n})
            i = j + 1
    return pd.DataFrame(rows, columns=["source", "station", "first_missing", "last_missing", "days"])


def needs_redownload(cov, stations, alternates=None, target=USABLE_TARGET):
    """清單測站中可用率低於 target 的站年；GNSS 附上備選站，以及備選站當年已有的可用天數。"""
    alternates = alternates or {}
    low = cov[cov["station"].isin(stations) & (cov["usable_pct"] < 100 * target)].copy()
    if low.empty:
        return low.assign(alternates="")
    have = cov.set_index(["station", "year"])["usable_days"]

    def alt_text(r):
        alts = alternates.get(r["station"], [])
        return "、".join(f"{a}（{int(have.get((a, r['year']), 0))} 天）" for a in alts)

    low["alternates"] = low.apply(alt_text, axis=1)
    return low


# ---------------------------------------------------------------- 失敗檔案

def classify_error(msg):
    """把錯誤訊息歸成幾類，方便統計。"""
    s = str(msg)
    for key, label in (("缺少星曆或 DCB", "缺少星曆或 DCB"), ("黎明前", "黎明前衛星不足，無法估接收機偏差"),
                       ("沒有 GPS 觀測", "RINEX 沒有 GPS 觀測"), ("BadZipFile", "壓縮檔損壞"),
                       ("EOFError", "檔案不完整（下載中斷？）"), ("MemoryError", "記憶體不足")):
        if key in s:
            return label
    head = s.split("(", 1)[0] if "(" in s else s
    return head[:60] or "未知"


def failures(manifest, daily, parse_name, kind):
    """最後狀態仍是失敗、且同一站日沒有別的檔案成功的檔案。"""
    if manifest.empty or "status" not in manifest:
        return pd.DataFrame(columns=["source", "station", "date", "name", "reason", "error", "processed_at"])
    bad = manifest[manifest["status"] != "ok"]
    ok_days = set(zip(daily["station"], daily["date"])) if not daily.empty else set()
    rows = []
    for r in bad.itertuples():
        sta, date = parse_name(r.name)
        date = pd.Timestamp(date).normalize() if date is not None else None
        if sta is not None and (sta, date) in ok_days:
            continue
        err = getattr(r, "error", "")
        rows.append({"source": kind, "station": sta, "date": date.date() if date is not None else None,
                     "name": r.name, "reason": classify_error(err), "error": err,
                     "processed_at": getattr(r, "processed_at", None)})
    return pd.DataFrame(rows, columns=["source", "station", "date", "name", "reason", "error", "processed_at"])


# ---------------------------------------------------------------- DCB 與儀器

def dcb_fallback(tec_daily):
    """每年：TEC 處理過的日子中，DCB 不是當月的天數與最大距今天數（DCB 是全球產品，同一天各站相同）。"""
    if tec_daily.empty or "dcb_age_days" not in tec_daily:
        return pd.DataFrame(columns=["year", "dates", "fallback_dates", "max_age_days", "fallback_files"])
    d = tec_daily.drop_duplicates("date")[["date", "dcb_age_days", "dcb_file"]].copy()
    d["age"] = pd.to_numeric(d["dcb_age_days"], errors="coerce")
    d["fallback"] = d["age"] > DCB_SAME_MONTH_DAYS
    d["year"] = d["date"].dt.year
    rows = []
    for y, g in d.groupby("year"):
        fb = g[g["fallback"]]
        rows.append({"year": int(y), "dates": len(g), "fallback_dates": int(len(fb)),
                     "max_age_days": int(g["age"].max()) if g["age"].notna().any() else None,
                     "fallback_files": "、".join(sorted(set(fb["dcb_file"].dropna().astype(str))))})
    return pd.DataFrame(rows)


def tec_instrument_changes(tec_daily):
    """用 02_clean_features.instrument_segments 找出換段日，並說明原因。"""
    rows = []
    if tec_daily.empty:
        return pd.DataFrame(rows, columns=["source", "station", "date", "reason", "detail"])
    idcol = "instrument_id" if "instrument_id" in tec_daily else "receiver"
    for sta, g in tec_daily.sort_values("date").groupby("station"):
        ids = g[idcol].fillna("").astype(str).tolist()
        bias = g["rx_bias_tecu"].astype(float).to_numpy()
        seg = B2.instrument_segments(ids, bias)
        dates = g["date"].tolist()
        for i in range(1, len(seg)):
            if seg[i] == seg[i - 1]:
                continue
            if ids[i] != ids[i - 1]:
                old, new = ids[i - 1].split("|"), ids[i].split("|")
                fields = ["接收機序號", "接收機型號", "韌體", "天線序號", "天線型號"]
                diff = [f"{f}：{a or '—'} → {b or '—'}" for f, a, b in zip(fields, old, new) if a != b]
                rows.append({"source": "tec", "station": sta, "date": dates[i].date(),
                             "reason": "儀器識別改變", "detail": "；".join(diff) or f"{ids[i-1]} → {ids[i]}"})
            else:
                prev = bias[max(0, i - 5):i]
                rows.append({"source": "tec", "station": sta, "date": dates[i].date(),
                             "reason": "接收機偏差跳動",
                             "detail": f"前 5 天中位數 {np.nanmedian(prev):.1f} → {bias[i]:.1f} TECU"})
    return pd.DataFrame(rows, columns=["source", "station", "date", "reason", "detail"])


def magnet_instrument_candidates(mag_daily, jump_nt=MAG_JUMP_NT, pos_deg=MAG_POS_DEG):
    """地磁「可能換儀器」的候選日：標頭座標或資料類型改變，或日平均 H／Z 跳動（只用涵蓋率夠的日子）。"""
    rows = []
    if mag_daily.empty:
        return pd.DataFrame(rows, columns=["source", "station", "date", "reason", "detail"])
    for sta, g in mag_daily.sort_values("date").groupby("station"):
        g = g.reset_index(drop=True)
        good = usable_mask(g, "magnet").to_numpy()
        for i in range(1, len(g)):
            r, p = g.loc[i], g.loc[i - 1]
            reasons = []
            if abs(float(r["lat"]) - float(p["lat"])) > pos_deg or abs(float(r["lon"]) - float(p["lon"])) > pos_deg:
                reasons.append(("標頭座標改變", f"({p['lat']:.3f}, {p['lon']:.3f}) → ({r['lat']:.3f}, {r['lon']:.3f})"))
            if "data_type" in g and str(r["data_type"]) != str(p["data_type"]):
                reasons.append(("資料類型改變", f"{p['data_type']} → {r['data_type']}"))
            if good[i]:
                prev = g.loc[max(0, i - 5):i - 1]
                prev = prev[good[max(0, i - 5):i]]
                for col, label in (("h_mean", "H"), ("z_mean", "Z")):
                    if len(prev) and col in g and not np.isnan(r[col]):
                        med = float(np.nanmedian(prev[col]))
                        if abs(r[col] - med) > jump_nt:
                            reasons.append((f"日平均 {label} 跳動",
                                            f"前 5 天中位數 {med:.0f} → {r[col]:.0f} nT"))
            for reason, detail in reasons:
                rows.append({"source": "magnet", "station": sta, "date": r["date"].date(),
                             "reason": reason, "detail": detail})
    return pd.DataFrame(rows, columns=["source", "station", "date", "reason", "detail"])


# ---------------------------------------------------------------- 報告

def _md_table(df, cols=None, max_rows=None):
    if df.empty:
        return "（無）\n"
    df = df[cols] if cols else df
    more = ""
    if max_rows and len(df) > max_rows:
        more = f"\n…另有 {len(df) - max_rows} 列，見 CSV。\n"
        df = df.head(max_rows)
    head = "| " + " | ".join(map(str, df.columns)) + " |\n|" + "---|" * len(df.columns) + "\n"
    body = "".join("| " + " | ".join("" if pd.isna(v) else str(v) for v in row) + " |\n"
                   for row in df.itertuples(index=False))
    return head + body + more


def coverage_matrix(cov):
    """站 × 年的可用率表（%）。低於目標的加粗，完全沒有資料的寫「—」。"""
    if cov.empty:
        return "（無）\n"
    pv = cov.pivot(index="station", columns="year", values="usable_pct")
    pv = pv.reindex(list(dict.fromkeys(cov["station"])))

    def fmt(v):
        if v == 0:
            return "—"
        s = f"{v:.0f}"
        return f"**{s}**" if v < 100 * USABLE_TARGET else s

    tab = pv.apply(lambda col: col.map(fmt))
    tab.insert(0, "測站", tab.index)
    return _md_table(tab.reset_index(drop=True).rename(columns=str))


def render(start, end, cov, todo, gaps, fails, dcb, inst, summary):
    L = []
    L.append("# B2 資料品質報告\n")
    L.append(f"> 由 `pipeline/src/12_quality_report.py` 產生（{pd.Timestamp.now():%Y-%m-%d %H:%M}）。"
             f"期間 {start} ～ {end}。對應需求 FR-B2-23。\n")
    L.append("## 1. 摘要\n")
    L.append(_md_table(summary))
    L.append(f"\n- **可用日**：地磁當天 1 秒資料涵蓋率 ≥ {B2.MIN_MAG_COVERAGE:.0%}；TEC 當天 ≥ {B2.MIN_TEC_HOURS:.0f} 小時。"
             "門檻與 `02_clean_features.py` 相同，不可用的日子不當背景、也不出特徵。\n")
    for kind, title in (("magnet", "地磁（MAGNET）"), ("tec", "GNSS TEC")):
        L.append(f"\n## 2{'a' if kind == 'magnet' else 'b'}. {title}：每站每年可用率（%）\n")
        L.append(f"粗體 = 低於 {USABLE_TARGET:.0%}；— = 沒有資料（多半是還沒下載）。完整數字見 `coverage_by_station_year.csv`。\n\n")
        L.append(coverage_matrix(cov[cov["source"] == kind]))
    L.append(f"\n## 3. 需要補抓或改用備選站的站年（可用率 < {USABLE_TARGET:.0%}）\n")
    L.append("括號內是備選站當年已有的可用天數。地磁沒有備選站。\n\n")
    L.append(_md_table(todo.rename(columns={"source": "測網", "station": "測站", "year": "年", "expected_days": "應有",
                                            "usable_days": "可用", "usable_pct": "可用率%", "alternates": "備選站"}),
                       ["測網", "測站", "年", "應有", "可用", "可用率%", "備選站"], max_rows=80))
    L.append(f"\n## 4. 資料缺口（連續 ≥ {GAP_MIN_DAYS} 天沒有資料）\n")
    L.append("整年都沒下載的站也會出現在這裡；開始下載後這張表會變短。\n\n")
    L.append(_md_table(gaps.rename(columns={"source": "測網", "station": "測站", "first_missing": "開始",
                                            "last_missing": "結束", "days": "天數"}), max_rows=60))
    L.append("\n## 5. 處理失敗的檔案\n")
    if fails.empty:
        L.append("（無）\n")
    else:
        L.append("依原因統計：\n\n")
        L.append(_md_table(fails.groupby(["source", "reason"]).size().rename("檔案數").reset_index()
                           .rename(columns={"source": "測網", "reason": "原因"})))
        L.append("\n明細（失敗的原始檔不會被刪，修好後重跑同一個指令即可重試）：\n\n")
        L.append(_md_table(fails.rename(columns={"source": "測網", "station": "測站", "date": "日期", "name": "檔名",
                                                 "reason": "原因"}),
                           ["測網", "測站", "日期", "檔名", "原因"], max_rows=60))
    L.append("\n## 6. TEC 的 DCB 沿用\n")
    L.append(f"CODE 每月 DCB 當月還沒公布時，程式往前找最多 3 個月。和所用月份中點相差超過 {DCB_SAME_MONTH_DAYS} 天，"
             "就表示用的不是當月 DCB。月與月之間衛星 DCB 通常只差約 0.1 ns，影響小，但要揭露。\n\n")
    L.append(_md_table(dcb.rename(columns={"year": "年", "dates": "處理天數", "fallback_dates": "沿用天數",
                                           "max_age_days": "最大距今天數", "fallback_files": "沿用的檔案"})))
    L.append("\n## 7. 儀器更換日\n")
    L.append("- **TEC**：和 `02_clean_features.py` 背景分段用的是同一個函式，這裡列出的每一天，背景都會重新起算。\n"
             "- **地磁**：MAGNET 沒有公開的儀器紀錄，以下只是**候選日**，需人工對照 1 分鐘資料確認。"
             f"日平均跳動門檻 {MAG_JUMP_NT:.0f} nT（大磁暴時 H 也可能變化這麼多，請對照 Kp）。"
             "目前地磁特徵還**沒有**依換儀器日分段（FR-B2-17 地磁部分）。\n\n")
    L.append(_md_table(inst.rename(columns={"source": "測網", "station": "測站", "date": "日期", "reason": "原因",
                                            "detail": "說明"}), max_rows=80))
    return "".join(L)


def build(data_dir, start, end):
    """讀取所有紀錄並算出各張表。回傳 dict，方便測試。"""
    data_dir = Path(data_dir)
    parsers = _name_parsers()
    out = {"cov": [], "gaps": [], "fails": [], "todo": [], "summary": []}
    daily = {}
    for kind, stations, alts in (("magnet", MAGNET_STATIONS, {}), ("tec", list(GNSS_STATIONS), GNSS_STATIONS)):
        d = load_daily(data_dir / kind / "daily")
        man = load_manifest(data_dir / kind / "manifest.jsonl")
        daily[kind] = d
        cov = coverage_by_year(d, kind, stations, start, end)
        fl = failures(man, d, parsers[kind], kind)
        out["cov"].append(cov)
        out["todo"].append(needs_redownload(cov, stations, alts))
        out["gaps"].append(find_gaps(d, kind, stations, start, end))
        out["fails"].append(fl)
        in_period = d[(d["date"] >= pd.Timestamp(start)) & (d["date"] <= pd.Timestamp(end))] if not d.empty else d
        n_exp = int(cov[cov["station"].isin(stations)]["expected_days"].sum())
        n_use = int(cov[cov["station"].isin(stations)]["usable_days"].sum())
        out["summary"].append({
            "測網": kind, "清單測站": len(stations),
            "已有資料的測站": int(in_period["station"].nunique()) if not in_period.empty else 0,
            "已處理站日": int(len(in_period)), "可用站日": n_use, "應有站日": n_exp,
            "整體可用率%": round(100.0 * n_use / n_exp, 1) if n_exp else 0.0,
            "期間外站日": int(len(d) - len(in_period)), "失敗檔案": int(len(fl)),
            "manifest 筆數": int(len(man)),
        })
    out = {k: (pd.concat(v, ignore_index=True) if k != "summary" else pd.DataFrame(v)) for k, v in out.items()}
    out["dcb"] = dcb_fallback(daily["tec"])
    out["inst"] = pd.concat([tec_instrument_changes(daily["tec"]), magnet_instrument_candidates(daily["magnet"])],
                            ignore_index=True)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(DATA), help="pipeline/data 資料夾（底下有 magnet/、tec/）")
    ap.add_argument("--start", default=PERIOD[0])
    ap.add_argument("--end", default=PERIOD[1])
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args()

    r = build(args.data, args.start, args.end)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    r["cov"].to_csv(out / "coverage_by_station_year.csv", index=False, encoding="utf-8-sig")
    r["gaps"].to_csv(out / "gaps.csv", index=False, encoding="utf-8-sig")
    r["fails"].to_csv(out / "failures.csv", index=False, encoding="utf-8-sig")
    r["inst"].to_csv(out / "instrument_changes.csv", index=False, encoding="utf-8-sig")
    r["dcb"].to_csv(out / "dcb_fallback.csv", index=False, encoding="utf-8-sig")
    md = render(args.start, args.end, r["cov"], r["todo"], r["gaps"], r["fails"], r["dcb"], r["inst"], r["summary"])
    (out / "README.md").write_text(md, encoding="utf-8")
    print(r["summary"].to_string(index=False))
    print(f"需要補抓的站年：{len(r['todo'])}；缺口：{len(r['gaps'])}；失敗檔案：{len(r['fails'])}；"
          f"儀器更換（含地磁候選）：{len(r['inst'])}")
    print(f"報告：{out / 'README.md'}")


if __name__ == "__main__":
    main()
