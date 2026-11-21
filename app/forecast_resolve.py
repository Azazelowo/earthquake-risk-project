"""預報卡的裁決（速判／定判）、計分與模型狀態（規格：docs/phase2/forecast_card_spec.md §6）。

兩件事，刻意分開：
    裁決 resolve：到期的卡，對照目錄，記下「發生了幾個、有沒有」。結果寫進另一本只能新增的紀錄
        （同樣的雜湊鏈），**不改登記簿**；同一張卡同一階段只裁決一次。
    計分 score：把裁決過的卡整批拿來算校準、資訊增益、Brier、命中數檢定（hit_test），並判定模型狀態。

為什麼單張卡不評對錯（規格 §6.3）：p = 3% 的卡，發生也不代表準、沒發生也不代表錯。
所以這裡只做整批統計；而且為了不讓重疊的時間窗與餘震成群灌水顯著性，
**檢定只用不重疊的子樣本**（T 天窗每 T 天取一張）。

兩個階段（規格 §6.2）：
    速判：發布後 T + 1 天，用暫時目錄（CWA 有感報告累積檔＋USGS）。母體沒有定位品質欄位，
          CWA API 只留最新 16 筆，所以會記下抓取間隔；間隔太長就標「可能漏事件」。分開統計、標「暫時」。
    定判：GDMS 正式目錄涵蓋視窗結束之後，且只用定位良好的事件（和模型同一個母體）。模型狀態以它為準。

用法：
    python app/forecast_resolve.py resolve --registry R --results RS --stage speed \
        --base app/data --provisional app/live/provisional_events.csv
    python app/forecast_resolve.py resolve --registry R --results RS --stage final --formal pipeline/data/events.parquet
    python app/forecast_resolve.py score --registry R --results RS --stage final
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import poisson

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR.parent / "pipeline" / "src"))
import safe_console  # noqa: E402,F401
import forecast_card as fc  # noqa: E402
import hazard_core as hc  # noqa: E402
from eqforecast import hazard as hz  # noqa: E402

STAGE_SPEED, STAGE_FINAL = "speed", "final"
STAGE_NAMES = {STAGE_SPEED: "速判", STAGE_FINAL: "定判"}
SPEED_DELAY_DAYS = 1.0             # 速判：視窗結束後多久
MAX_FETCH_GAP_H = 3.0              # 視窗內暫時目錄的抓取間隔超過這麼久，標「可能漏事件」
CWA_CAP = 16                       # CWA API 每個資料集只回最新 16 筆（與 live_update.CWA_CAP 相同）
GRID_BOUNDS = {"lat_min": 21.0, "lat_max": 26.0, "lon_min": 119.0, "lon_max": 123.0}   # 「all」區域 = 整個網格

# 模型狀態門檻（規格 §6.5，使用者 2026-11-18 接受；第一張標準卡發布後不得調整，要調整只能升級規格版本）
EVAL_DAYS = 90
MIN_EVENTS = 20
MONTH_DAYS = 30
NTEST_ALPHA = 0.025                # 雙尾，各 2.5%
STATUS_ENOUGH, STATUS_WATCH, STATUS_FAILED, STATUS_LOW = "有效", "觀察中", "失效", "樣本不足"
RELIABILITY_EDGES = (0.0, 1e-3, 1e-2, 5e-2, 0.1, 0.25, 0.5, 1.0 + 1e-12)


# ---------- 裁決 ----------

def in_region(ev, region, grid=GRID_BOUNDS):
    """ev 的震央是否在區域內（圓：距圓心 ≤ 半徑，邊界算內；all：整個網格）。"""
    if region["kind"] == "all":
        return ((ev["lat"] >= grid["lat_min"]) & (ev["lat"] < grid["lat_max"])
                & (ev["lon"] >= grid["lon_min"]) & (ev["lon"] < grid["lon_max"])).to_numpy()
    km = hc.haversine_km(region["lat"], region["lon"], ev["lat"].to_numpy(float), ev["lon"].to_numpy(float))
    return km <= float(region["radius_km"])


def resolve_card(card, header, events):
    """一張卡的結果：{"y", "n", "max_ml", "events"}。窗是 [issued_at, issued_at + T)，ML ≥ m。"""
    t0 = pd.Timestamp(header["issued_at"]).tz_localize(None)
    t1 = t0 + pd.Timedelta(days=float(card["T"]))
    ev = events[(events["time"] >= t0) & (events["time"] < t1) & (events["ml"] >= float(card["m"]) - 1e-9)]
    if len(ev):
        ev = ev[in_region(ev, header["regions"][card["r"]])]
    ev = ev.sort_values("time")
    top = ev.sort_values("ml", ascending=False).head(3)
    return {"y": int(len(ev) > 0), "n": int(len(ev)),
            "max_ml": None if not len(ev) else round(float(ev["ml"].max()), 2),
            "events": [{"time": fc.iso(r.time), "ml": round(float(r.ml), 2)} for r in top.itertuples()]}


def fetch_gap_hours(times, t0, t1):
    """[t0, t1] 內，相鄰兩次抓取的最大間隔（小時）。第一次抓取之前、最後一次之後也算間隔。"""
    ts = sorted(pd.to_datetime(pd.Series(list(times))).dropna().unique())
    pts = [t for t in ts if t0 <= pd.Timestamp(t) <= t1]
    marks = [t0] + [pd.Timestamp(t) for t in pts] + [t1]
    return max((b - a) / pd.Timedelta(hours=1) for a, b in zip(marks, marks[1:]))


def speed_coverage(fetch_log, t0, t1):
    """速判的資料完整度：視窗內「USGS 與 CWA 都成功」的抓取是否連續，以及有沒有抓到 CWA 的回傳上限。

    抓取紀錄是 live_update 每次抓取寫的（不是暫時事件裡的 fetched_at：那只是事件最後一次被看到的時間）。
    沒有紀錄、間隔超過 MAX_FETCH_GAP_H、或視窗內有一次回傳數達上限（更早的報告可能被擠掉），都標 maybe_missing。
    """
    if fetch_log is None or not len(fetch_log):
        return {"fetch_gap_h": None, "cap_hits": None, "maybe_missing": True}
    win = fetch_log[(fetch_log["fetched_at"] >= t0) & (fetch_log["fetched_at"] <= t1)]
    good = fetch_log[fetch_log["usgs_ok"] & fetch_log["cwa_ok"]]
    gap = fetch_gap_hours(good["fetched_at"], t0, t1)
    cap = int((win["cwa_max_n"] >= CWA_CAP).sum())
    return {"fetch_gap_h": round(float(gap), 2), "cap_hits": cap, "maybe_missing": bool(gap > MAX_FETCH_GAP_H or cap > 0)}


def pending_cards(registry_dir, results_dir, stage):
    """還沒裁決過這個階段的卡：[(標頭, 卡)]。"""
    done = set()
    for _, rows in fc.read_registry(results_dir) if Path(results_dir).exists() else []:
        done.update(r["id"] for r in rows)
    out = []
    for header, cards in fc.read_registry(registry_dir):
        for c in cards:
            if f"{c['id']}|{stage}" not in done:
                out.append((header, c))
    return out


def resolve_pending(registry_dir, results_dir, stage, events, covered_until, now, fetch_log=None, catalog_note=""):
    """把到期、尚未裁決的卡裁決掉並寫進紀錄。回傳寫入的筆數。

    covered_until：這份目錄涵蓋到哪一刻。定判要求視窗結束 ≤ covered_until；速判要求
        視窗結束 + SPEED_DELAY_DAYS ≤ now，且資料至少抓到視窗結束（covered_until ≥ 視窗結束）。
    """
    now = pd.Timestamp(now).tz_localize(None)
    covered_until = pd.Timestamp(covered_until)
    rows = []
    for header, card in pending_cards(registry_dir, results_dir, stage):
        t0 = pd.Timestamp(header["issued_at"]).tz_localize(None)
        t1 = t0 + pd.Timedelta(days=float(card["T"]))
        if t1 > covered_until:
            continue                                            # 目錄還沒涵蓋到視窗結束
        if stage == STAGE_SPEED and now < t1 + pd.Timedelta(days=SPEED_DELAY_DAYS):
            continue                                            # 速判要等 1 天
        r = resolve_card(card, header, events)
        row = {"id": f"{card['id']}|{stage}", "card": card["id"], "stage": stage, **r}
        if stage == STAGE_SPEED:
            row.update(speed_coverage(fetch_log, t0, t1))
        rows.append(row)
    if not rows:
        return 0
    header = {"type": "batch", "kind": "result", "spec": fc.SPEC_VERSION, "stage": stage,
              "stage_name": STAGE_NAMES[stage], "issued_at": fc.iso(now),
              "catalog": {"covered_until": fc.iso(covered_until), "note": catalog_note}, "n_cards": len(rows)}
    fc.append_batch(results_dir, header, rows)
    return len(rows)


# ---------- 計分 ----------

def load_resolved(registry_dir, results_dir, stage):
    """把裁決結果接回卡片：回傳 DataFrame，每列一張已裁決的卡。"""
    cards = {}
    for header, cs in fc.read_registry(registry_dir):
        for c in cs:
            cards[c["id"]] = (header, c)
    rows = []
    if Path(results_dir).exists():
        for _, rs in fc.read_registry(results_dir):
            for r in rs:
                if r["stage"] != stage or r["card"] not in cards:
                    continue
                h, c = cards[r["card"]]
                issued = pd.Timestamp(h["issued_at"]).tz_localize(None)
                rows.append({**c, "set": h["set"], "stage": stage, "model_version": h["model"]["version"],
                             "params_hash": h["model"]["params_hash"], "issued_at": issued,
                             "window_end": issued + pd.Timedelta(days=float(c["T"])),
                             "y": r["y"], "n_events": r["n"], "maybe_missing": bool(r.get("maybe_missing", False))})
    return pd.DataFrame(rows)


def non_overlapping(df):
    """同一組（區域、規模、時間窗）內，貪婪地取視窗互不重疊的卡：T 天窗每 T 天取一張。"""
    keep, last_end = [], None
    for idx, r in df.sort_values("issued_at").iterrows():
        if last_end is None or r["issued_at"] >= last_end:
            keep.append(idx)
            last_end = r["window_end"]
    return df.loc[keep]


def hit_test(y, p, alpha=NTEST_ALPHA):
    """命中數檢定：發生（y＝1）的卡片數，對「各卡機率 p 不同」的白努利和（Poisson-binomial，精確分佈）做雙尾檢定。

    為什麼不用事件數對卜瓦松（n_test）：地震成群發生，事件數的變異比卜瓦松大。模擬（複合卜瓦松，κ＝1.765，
    模型完全正確、近 90 天約 13 個不重疊的 7 天窗，2,000 次）：事件數卜瓦松檢定誤判 20%（名目 5%），
    命中數檢定誤判 2.8%。卡片的機率本來就是 P(至少一次)＝1−exp(−n/κ)，和「有沒有發生」同一個量，
    所以對命中數檢定與卡片的定義一致，而且不必假設每群有多大。代價：樣本是「窗數」不是「事件數」，檢定力較弱。
    """
    p = np.clip(np.asarray(p, float), 0.0, 1.0)
    y = np.asarray(y, int)
    if not len(p):
        return {"kind": "hits", "expected": 0.0, "observed": 0, "p_low": None, "p_high": None, "exceed": None}
    pmf = np.array([1.0])
    for q in p:
        pmf = np.convolve(pmf, [1.0 - q, q])
    obs = int(y.sum())
    p_low, p_high = float(pmf[: obs + 1].sum()), float(pmf[obs:].sum())
    return {"kind": "hits", "expected": float(p.sum()), "observed": obs, "p_low": p_low, "p_high": p_high,
            "exceed": bool(p_low < alpha or p_high < alpha)}


def n_test(observed, expected, alpha=NTEST_ALPHA):
    """觀測事件數對 Poisson(期望) 的雙尾檢定。exceed＝落在 2.5%／97.5% 之外。"""
    if not expected or expected <= 0:
        return {"expected": float(expected or 0), "observed": int(observed), "p_low": None, "p_high": None, "exceed": None}
    p_low = float(poisson.cdf(observed, expected))
    p_high = float(poisson.sf(observed - 1, expected))
    return {"expected": float(expected), "observed": int(observed), "p_low": p_low, "p_high": p_high,
            "exceed": bool(p_low < alpha or p_high < alpha)}


def information_gain(n_obs, n_model, n_ref):
    """每個地震平均多得到的資訊（nat）；和 06_hazard_model 回測同一個定義（hz.poisson_ll）。"""
    total = float(np.sum(n_obs))
    if total <= 0:
        return None
    return (hz.poisson_ll(np.asarray(n_obs, float), np.asarray(n_model, float))
            - hz.poisson_ll(np.asarray(n_obs, float), np.asarray(n_ref, float))) / total


def brier(p, y):
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((p - y) ** 2)) if len(p) else None


def reliability_table(p, y, edges=RELIABILITY_EDGES, min_events=MIN_EVENTS):
    """依 p 分箱：卡數、平均 p、實際發生率。發生的卡少於 min_events 的箱子標「樣本不足」。"""
    p, y = np.asarray(p, float), np.asarray(y, float)
    out = []
    for lo, hi in zip(edges, edges[1:]):
        m = (p >= lo) & (p < hi)
        if not m.any():
            continue
        out.append({"lo": lo, "hi": min(hi, 1.0), "n_cards": int(m.sum()), "mean_p": float(p[m].mean()),
                    "observed_rate": float(y[m].mean()), "n_hit": int(y[m].sum()),
                    "enough": bool(y[m].sum() >= min_events)})
    return out


def evaluate_group(df, as_of):
    """一組卡在 as_of 的評估：只用窗已結束、且結束時間在近 EVAL_DAYS 天內的卡，並取不重疊的子樣本。"""
    as_of = pd.Timestamp(as_of)
    d = df[(df["window_end"] <= as_of) & (df["window_end"] > as_of - pd.Timedelta(days=EVAL_DAYS))]
    d = non_overlapping(d) if len(d) else d
    n_events = int(d["n_events"].sum()) if len(d) else 0
    res = {"as_of": fc.iso(as_of), "n_cards": int(len(d)), "n_events": n_events,
           "maybe_missing_cards": int(d["maybe_missing"].sum()) if len(d) else 0}
    if not len(d):
        return {**res, "n_test": hit_test([], []), "gain_vs_bg": None, "gain_vs_uniform": None,
                "brier": None, "brier_skill_vs_bg": None, "brier_skill_vs_uniform": None, "reliability": []}
    # 檢定用「命中數」（hit_test），不用事件數的卜瓦松檢定：後者在地震成群時會誤判（見 hit_test）。
    # 事件數與期望事件數仍然記下來，只供描述。
    res["n_test"] = hit_test(d["y"], d["p"])
    res["events_expected"] = float(d["n"].sum())
    res["gain_vs_bg"] = information_gain(d["n_events"], d["n"], d["nb"])
    res["gain_vs_uniform"] = information_gain(d["n_events"], d["n"], d["nu"])
    bs = brier(d["p"], d["y"])
    res["brier"] = bs
    for key, col in (("bg", "pb"), ("uniform", "pu")):
        ref = brier(d[col], d["y"])
        res[f"brier_skill_vs_{key}"] = None if not ref else 1.0 - bs / ref
    res["reliability"] = reliability_table(d["p"], d["y"])
    return res


def classify(cur, prev):
    """模型狀態（規格 §6.5）。cur 是這次評估、prev 是 MONTH_DAYS 天前的評估。"""
    if cur["n_events"] < MIN_EVENTS:
        return STATUS_LOW
    g = cur["gain_vs_bg"]
    two_exceed = bool(cur["n_test"]["exceed"]) and bool(prev["n_test"]["exceed"]) and prev["n_events"] >= MIN_EVENTS
    if two_exceed or (g is not None and g <= 0):
        return STATUS_FAILED
    if cur["n_test"]["exceed"]:
        return STATUS_WATCH
    return STATUS_ENOUGH


def score_groups(resolved, as_of, keys=("r", "m", "T")):
    """對每個（模型版本、區域、規模、時間窗）算評估與狀態。回傳 list。"""
    if resolved is None or not len(resolved):
        return []
    as_of = pd.Timestamp(as_of)
    out = []
    for gkey, g in resolved[resolved["set"] == "standard"].groupby(["model_version", "params_hash", *keys]):
        cur = evaluate_group(g, as_of)
        prev = evaluate_group(g, as_of - pd.Timedelta(days=MONTH_DAYS))
        out.append({"model_version": gkey[0], "params_hash": gkey[1], **dict(zip(keys, gkey[2:])),
                    "status": classify(cur, prev), "current": cur, "previous": prev})
    return out


# ---------- 入口 ----------

def _json_default(o):
    """numpy 的整數、浮點數、布林轉成一般型別；其餘轉成字串。"""
    return o.item() if hasattr(o, "item") else str(o)


def _speed_events(base_dir, provisional_path, fetch_log_path):
    """速判用的事件：基準目錄（正式、定位良好）＋暫時目錄（CWA＋USGS，CWA 優先）。

    回傳 (events, 抓取紀錄, 涵蓋到哪一刻)。涵蓋時間＝最後一次「USGS 與 CWA 都成功」的抓取。
    """
    import live_update as lu                                    # 延後匯入：只有速判需要
    base = lu.read_base_catalog(base_dir)[["time", "lat", "lon", "ml"]]
    prov = lu.read_store(provisional_path)
    merged = lu.typed(lu.merge_sources(prov)) if len(prov) else prov
    cols = ["time", "lat", "lon", "ml"]
    ev = pd.concat([base] + ([merged[cols]] if len(merged) else []), ignore_index=True)
    ev["time"] = pd.to_datetime(ev["time"])
    log = lu.read_fetch_log(fetch_log_path)
    good = log[log["usgs_ok"] & log["cwa_ok"]] if log is not None else None
    covered = good["fetched_at"].max() if good is not None and len(good) else pd.NaT
    return ev, log, covered


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("resolve", "score"):
        p = sub.add_parser(name)
        p.add_argument("--registry", required=True)
        p.add_argument("--results", required=True)
        p.add_argument("--stage", choices=(STAGE_SPEED, STAGE_FINAL), required=True)
    r = sub.choices["resolve"]
    r.add_argument("--base", default=str(APP_DIR / "data"))
    r.add_argument("--provisional", default=str(APP_DIR / "live" / "provisional_events.csv"))
    r.add_argument("--fetch-log", default=str(APP_DIR / "live" / "fetch_log.csv"),
                   help="live_update 每次抓取寫的紀錄；速判靠它判斷視窗內有沒有連續抓取")
    r.add_argument("--formal", default=None, help="定判：GDMS 正式目錄 events.parquet")
    r.add_argument("--now", default=None)
    s = sub.choices["score"]
    s.add_argument("--as-of", default=None)
    s.add_argument("--out", default=None, help="把完整計分結果（每組的評估與狀態）寫成 JSON")
    args = ap.parse_args()

    if args.cmd == "resolve":
        now = pd.Timestamp.now("UTC").tz_localize(None) if args.now is None else pd.Timestamp(args.now)
        if args.stage == STAGE_FINAL:
            if not args.formal:
                sys.exit("定判需要 --formal（GDMS 正式目錄 events.parquet）")
            ev, _ = hz.load_events(args.formal, "located")
            ev["time"] = pd.to_datetime(ev["time"])
            covered = ev["time"].max()
            n = resolve_pending(args.registry, args.results, args.stage, ev, covered, now,
                                catalog_note=f"GDMS 正式目錄（定位良好）：{args.formal}")
        else:
            ev, log, covered = _speed_events(args.base, args.provisional, args.fetch_log)
            if pd.isna(covered):
                sys.exit("沒有成功的抓取紀錄（fetch_log.csv），無法速判")
            n = resolve_pending(args.registry, args.results, args.stage, ev, covered, now, fetch_log=log,
                                catalog_note="暫時目錄（CWA 有感報告累積檔＋USGS）；母體未篩定位品質，可能漏事件")
        print(f"{STAGE_NAMES[args.stage]}：裁決 {n} 張卡")
        return
    resolved = load_resolved(args.registry, args.results, args.stage)
    as_of = pd.Timestamp.now("UTC").tz_localize(None) if args.as_of is None else pd.Timestamp(args.as_of)
    groups = score_groups(resolved, as_of)
    counts = pd.Series([g["status"] for g in groups]).value_counts().to_dict() if groups else {}
    summary = {"stage": STAGE_NAMES[args.stage], "as_of": fc.iso(as_of), "resolved_cards": int(len(resolved)),
               "groups": len(groups), "status_counts": {k: int(v) for k, v in counts.items()}}
    if args.out:
        full = {**summary, "provisional": args.stage == STAGE_SPEED,
                "note": "速判用暫時目錄，母體未篩定位品質、可能漏事件；模型狀態以定判為準" if args.stage == STAGE_SPEED else "",
                "thresholds": {"eval_days": EVAL_DAYS, "min_events": MIN_EVENTS, "month_days": MONTH_DAYS,
                               "ntest_alpha": NTEST_ALPHA}, "results": groups}
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(full, ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
