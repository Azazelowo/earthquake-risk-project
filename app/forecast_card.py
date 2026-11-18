"""預報卡產生器與登記簿（規格：docs/phase2/forecast_card_spec.md）。

做什麼：讀一份風險快照，依標準表（6 區域 × 7 規模 × 9 時間窗）算出每張卡的機率、平常值、均勻值，
寫進只能新增的登記簿（JSONL），並用雜湊鏈串起來，之後任何一張舊卡被改動都會被偵測到。

為什麼這樣設計（對應規格 §1、§2、§7）：
    - 標準卡由程式依固定表產生，不手動挑選，才能排除「只貼猜中的」。
    - 機率旁邊永遠並列「平常值」（只有背景）與「均勻值」，沒有基準就看不出模型有沒有比常識好。
    - 每次發布寫成一個「批次」：模型版本、資料狀態、區域定義、裁決規則只在批次標頭寫一次，
      卡片只存數字（體積：每年約 13.8 萬張卡，完整紀錄每張約 700 位元組不可行）。
    - 雜湊以批次為單位：hash = sha256(prev_hash + 正規化 JSON(標頭, 全部卡片))。
    - 這個程式**只負責產生與驗證**。第一張標準卡何時開始發布，要等規格 §13 的檢查清單全部通過；
      所以必須明確指定 --registry，沒有預設位置，避免不小心寫進正式登記簿。

卡片欄位（精簡鍵名，完整語意見規格 §4.2）：
    id 卡片編號；r 區域代碼；m 規模門檻（ML）；T 時間窗（天）；
    p／pb／pu 模型／平常／均勻 的機率；n／nb／nu 對應的期望地震數（卜瓦松的 λ）；
    x=1 外推且樣本極少（ML≥6.5）；i=1 時間窗是內插出來的（不在 feed 的匯出刻度上）。

用法：
    python app/forecast_card.py --registry <資料夾>            # 用最新快照發布一批標準卡
    python app/forecast_card.py --registry <資料夾> --verify    # 只驗證雜湊鏈
    python app/forecast_card.py --registry <資料夾> --dry-run   # 只算、不寫，印出體積
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(ROOT / "pipeline" / "src"))
import safe_console  # noqa: E402,F401
import hazard_core as hc  # noqa: E402
from eqforecast import hazard as hz  # noqa: E402

SPEC_VERSION = "1"
SET_STANDARD = "standard"
ISSUE_HOUR_UTC = 0                 # 標準卡每日一次：UTC 00:00（台北 08:00）的快照
EXTRAPOLATED_MAG = 6.5             # 與 hazard_core.reliability 相同：M6.5 以上回測樣本極少
RECENT_DAYS = 30
RECENT_TOP = 3

# 標準卡集合（規格 §5，使用者 2026-11-18 確認）。變更必須升級 SPEC_VERSION，舊卡不改。
STANDARD_REGIONS = (
    {"id": "TW", "name": "全台", "kind": "all"},
    {"id": "KHH", "name": "高雄", "kind": "circle", "lat": 22.63, "lon": 120.30, "radius_km": 50.0},
    {"id": "TPE", "name": "台北", "kind": "circle", "lat": 25.04, "lon": 121.56, "radius_km": 50.0},
    {"id": "ILA", "name": "宜蘭", "kind": "circle", "lat": 24.75, "lon": 121.75, "radius_km": 50.0},
    {"id": "HUA", "name": "花蓮", "kind": "circle", "lat": 23.98, "lon": 121.60, "radius_km": 50.0},
    {"id": "TTT", "name": "台東", "kind": "circle", "lat": 22.76, "lon": 121.14, "radius_km": 50.0},
)
STANDARD_MAGS = (4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0)
STANDARD_WINDOWS = (1, 2, 3, 5, 7, 10, 14, 20, 30)

RESOLUTION = {
    "event": "發布後的時間窗 [issued_at, issued_at+T) 內，區域內（震央距圓心 ≤ 半徑；all 為整個網格）"
             "至少一個 ML ≥ m 的定位良好地震（gap ≤ 110° 或未知）；深度不限；餘震計入",
    "stages": {"速判": "暫時目錄（CWA 有感報告累積檔＋USGS），標「暫時」、分開統計",
               "定判": "GDMS 正式目錄；模型狀態與對外成績以定判為準"},
    "mag_type": "ML",
}


class RegistryError(ValueError):
    """登記簿違反規則（重複、時間倒退、雜湊對不上……）。"""


# ---------- 正規化與雜湊 ----------

def sig(x, digits=6):
    """有效位數 6 位：數值本身是估計值；也讓同一個數字永遠寫成同一串文字，雜湊才穩定。"""
    return float(f"{float(x):.{digits}g}")


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def batch_hash(prev_hash, header, cards):
    """header 不含 hash 欄位。"""
    body = canonical({"header": header, "cards": cards})
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


GENESIS = "0" * 64


def iso(ts):
    return pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 算卡 ----------

def uniform_per_cell(base_dir, snap, windows):
    """每個時間窗的「均勻值」：每格相同的 M≥m_ref 期望數。

    定義與回測的均勻對照相同（eqforecast.hazard.uniform_expected_ref）；校準取在基準快照的發布時間，
    和完整模型的校準倍率（params.calibration_factors）是同一個時間點，兩者才公平。
    """
    base_dir = Path(base_dir)
    base_params = json.loads((base_dir / "params.json").read_text(encoding="utf-8"))
    rate = float(base_params["fit_info"]["uniform_rate_per_cell_day"])
    grid = hz.Grid(**base_params["grid"])
    ev = pd.read_csv(base_dir / "catalog_base.csv", parse_dates=["time"])
    cat = hz.Catalog(ev, grid, float(base_params["params"]["m_ref"]))
    t0 = pd.Timestamp(base_params["issue_time"])
    return {T: float(hz.uniform_expected_ref(cat, t0, float(T), rate)[0, 0]) for T in windows}, rate


def region_weights(snap, region):
    if region["kind"] == "all":
        return hc.all_weights(snap)
    return hc.point_weights(snap, region["lat"], region["lon"], region["radius_km"])


def recent_events(snap, region, issued_at):
    """依據欄：發布前 30 天、區域內規模最大的幾筆（只列出來，說明為什麼近期加成不是 1）。"""
    ev = snap.events
    if len(ev) == 0:
        return []
    ev = ev[(ev["time"] < issued_at) & (ev["time"] >= issued_at - pd.Timedelta(days=RECENT_DAYS))].copy()
    if region["kind"] != "all":
        km = hc.haversine_km(region["lat"], region["lon"], ev["lat"].to_numpy(float), ev["lon"].to_numpy(float))
        ev = ev[km <= region["radius_km"]]
    ev = ev.sort_values("ml", ascending=False).head(RECENT_TOP)
    return [{"time": iso(r.time), "lat": round(float(r.lat), 3), "lon": round(float(r.lon), 3),
             "ml": round(float(r.ml), 1)} for r in ev.itertuples()]


def git_sha():
    """目前提交的 sha；工作區有未提交的變更時加上 `+dirty`。

    為什麼要標：預先登記的意思是「當時用的程式可以事後重現」。如果發布時工作區有沒提交的改動，
    只記 sha 會讓人以為用的是那個提交，實際上不是。
    """
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10)
        if sha.returncode != 0 or not sha.stdout.strip():
            return "unknown"
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                               capture_output=True, text=True, timeout=30)
        return sha.stdout.strip() + ("+dirty" if dirty.returncode == 0 and dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def model_info(snap):
    p = snap.params
    core = {"params": p["params"], "calibration_factors": p["calibration_factors"],
            "uniform_rate_per_cell_day": p["fit_info"]["uniform_rate_per_cell_day"]}
    prm = p["params"]
    return {"version": p.get("model_version", "unknown"), "git_sha": git_sha(),
            "params_hash": hashlib.sha256(canonical(core).encode("utf-8")).hexdigest(),
            "b": sig(prm["b"]), "m_ref": prm["m_ref"], "mu": sig(prm["mu"]), "K": sig(prm["K"]),
            "alpha": prm["alpha"], "p": prm["p"]}


def card_id(issued_at, set_name, region_id, m, T):
    return f"F-{pd.Timestamp(issued_at):%Y%m%dT%H%M}-{set_name}-{region_id}-M{m:g}-{T:g}d"


def build_batch(snap, uniform, regions=STANDARD_REGIONS, mags=STANDARD_MAGS, windows=STANDARD_WINDOWS,
                set_name=SET_STANDARD):
    """快照 → (標頭, 卡片清單)。標頭還沒有 prev_hash／hash。"""
    issued_at = snap.issue_time
    cards, region_info = [], {}
    on_grid = lambda T: bool(np.isclose(float(T), snap.horizons, rtol=1e-9, atol=0).any())  # noqa: E731
    for rg in regions:
        w = region_weights(snap, rg)
        area = hc.region_area_km2(snap, w)
        region_info[rg["id"]] = {**{k: v for k, v in rg.items() if k != "id"}, "area_km2": round(area, 1),
                                 "recent": recent_events(snap, rg, issued_at)}
        for m in mags:
            for T in windows:
                n = hc.region_expected(snap, w, m, T, "full")
                nb = hc.region_expected(snap, w, m, T, "bg")
                nu = float(w.sum()) * float(hz.scale_magnitude(uniform[T], m, snap.m_ref, snap.b))
                card = {"id": card_id(issued_at, set_name, rg["id"], m, T), "r": rg["id"], "m": m, "T": T,
                        "p": sig(hc.probability(n)), "pb": sig(hc.probability(nb)), "pu": sig(hc.probability(nu)),
                        "n": sig(n), "nb": sig(nb), "nu": sig(nu)}
                if m >= EXTRAPOLATED_MAG:
                    card["x"] = 1
                if not on_grid(T):
                    card["i"] = 1
                cards.append(card)
    status = snap.params.get("data_status") or {}
    header = {"type": "batch", "spec": SPEC_VERSION, "set": set_name, "registered": True,
              "issued_at": iso(issued_at), "model": model_info(snap),
              "data_status": {"catalog_end": status.get("catalog_end") or snap.params.get("catalog_end"),
                              "gap_days": status.get("gap_days"), "n_provisional": status.get("n_provisional"),
                              "n_provisional_by_source": status.get("n_provisional_by_source")},
              "resolution": RESOLUTION, "regions": region_info, "mags": list(mags), "windows": list(windows),
              "uniform_rate_per_cell_day": sig(snap.params["fit_info"]["uniform_rate_per_cell_day"]),
              "n_cards": len(cards)}
    return header, cards


# ---------- 登記簿 ----------

def _month_file(registry_dir, issued_at):
    return Path(registry_dir) / f"{pd.Timestamp(issued_at):%Y-%m}.jsonl"


def read_batches(path):
    """讀一個 JSONL 檔，回傳 [(標頭, 卡片清單)]。格式不對就丟 RegistryError。"""
    batches, header, cards = [], None, []
    for ln, line in enumerate(Path(path).read_text(encoding="utf-8").split("\n"), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise RegistryError(f"{path} 第 {ln} 行不是合法 JSON：{e}") from e
        if row.get("type") == "batch":
            if header is not None:
                batches.append((header, cards))
            header, cards = row, []
        elif header is None:
            raise RegistryError(f"{path} 第 {ln} 行：卡片前面沒有批次標頭")
        else:
            cards.append(row)
    if header is not None:
        batches.append((header, cards))
    return batches


def _all_batches(registry_dir):
    out = []
    for f in sorted(Path(registry_dir).glob("*.jsonl")):
        out.extend(read_batches(f))
    return out


def verify_registry(registry_dir):
    """重算整條雜湊鏈。通過回傳統計；任何不符丟出 RegistryError（訊息說明是哪一批、哪裡不對）。"""
    registry_dir = Path(registry_dir)
    prev, last_issued, n_cards = GENESIS, None, 0
    batches = _all_batches(registry_dir)
    for k, (header, cards) in enumerate(batches):
        where = f"第 {k + 1} 批（{header.get('issued_at')}）"
        h = dict(header)
        stored = h.pop("hash", None)
        if h.get("prev_hash") != prev:
            raise RegistryError(f"{where}：prev_hash 對不上前一批，鏈被中斷或前面的批次被改過")
        if stored != batch_hash(prev, h, cards):
            raise RegistryError(f"{where}：雜湊不符，這一批的內容被改過")
        if header.get("n_cards") != len(cards):
            raise RegistryError(f"{where}：標頭說 {header.get('n_cards')} 張，實際 {len(cards)} 張")
        if last_issued is not None and header["issued_at"] <= last_issued:
            raise RegistryError(f"{where}：發布時間沒有嚴格遞增（前一批 {last_issued}）")
        ids = [c["id"] for c in cards]
        if len(set(ids)) != len(ids):
            raise RegistryError(f"{where}：卡片編號重複")
        prev, last_issued, n_cards = stored, header["issued_at"], n_cards + len(cards)
    head_path = registry_dir / "head.json"
    if batches:
        if not head_path.exists():
            raise RegistryError("有批次但沒有 head.json")
        head = json.loads(head_path.read_text(encoding="utf-8"))
        if head.get("hash") != prev or head.get("batch_count") != len(batches):
            raise RegistryError("head.json 和實際最後一批不符（寫入中斷，或被改過）")
    return {"batches": len(batches), "cards": n_cards, "head": prev, "last_issued_at": last_issued}


def append_batch(registry_dir, header, cards):
    """把一批寫進登記簿。重複或時間倒退直接拒絕；舊批次永遠不會被改寫。回傳新的 hash。"""
    registry_dir = Path(registry_dir)
    registry_dir.mkdir(parents=True, exist_ok=True)
    existing = _all_batches(registry_dir)
    if existing:
        last_hdr = existing[-1][0]
        if header["issued_at"] <= last_hdr["issued_at"]:
            raise RegistryError(f"發布時間 {header['issued_at']} 不晚於最後一批 {last_hdr['issued_at']}"
                                "（已發布過，或快照太舊）；不允許補發或改寫")
        prev = last_hdr["hash"]
    else:
        prev = GENESIS
    h = dict(header, prev_hash=prev)
    h["hash"] = batch_hash(prev, {k: v for k, v in h.items() if k != "hash"}, cards)
    lines = [canonical(h)] + [canonical(c) for c in cards]
    with open(_month_file(registry_dir, header["issued_at"]), "a", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    head = {"hash": h["hash"], "batch_count": len(existing) + 1,
            "card_count": sum(len(c) for _, c in existing) + len(cards), "last_issued_at": header["issued_at"],
            "spec": SPEC_VERSION}
    tmp = registry_dir / "head.json.tmp"
    tmp.write_text(json.dumps(head, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    tmp.replace(registry_dir / "head.json")
    return h["hash"]


# ---------- 入口 ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--registry", required=True, help="登記簿資料夾（沒有預設，避免誤寫進正式登記簿）")
    ap.add_argument("--data", default=None, help="快照資料夾；預設自動選 app/live/（較新時）或 app/data/")
    ap.add_argument("--base", default=str(APP_DIR / "data"), help="基準快照（取均勻對照用的目錄與參數）")
    ap.add_argument("--verify", action="store_true", help="只驗證雜湊鏈")
    ap.add_argument("--dry-run", action="store_true", help="只算、不寫")
    ap.add_argument("--any-hour", action="store_true", help="允許非 UTC 00:00 的快照（測試用；標準卡每日只發一次）")
    args = ap.parse_args()

    if args.verify:
        print(json.dumps(verify_registry(args.registry), ensure_ascii=False))
        return
    data_dir = Path(args.data) if args.data else hc.pick_data_dir(APP_DIR / "data", APP_DIR / "live")
    snap = hc.load_snapshot(data_dir)
    if snap.issue_time.hour != ISSUE_HOUR_UTC and not args.any_hour:
        sys.exit(f"快照發布時間 {iso(snap.issue_time)} 不是 UTC {ISSUE_HOUR_UTC:02d}:00；"
                 "標準卡每日只在這個整點的快照發布。")
    uniform, _ = uniform_per_cell(args.base, snap, STANDARD_WINDOWS)
    header, cards = build_batch(snap, uniform)
    size = sum(len(canonical(c)) + 1 for c in cards) + len(canonical(header)) + 1
    if args.dry_run:
        print(f"試算：{len(cards)} 張卡、約 {size / 1024:.0f} KB（發布時間 {header['issued_at']}），未寫入")
        return
    h = append_batch(args.registry, header, cards)
    stats = verify_registry(args.registry)
    print(f"已發布 {len(cards)} 張卡（約 {size / 1024:.0f} KB），hash {h[:12]}…；登記簿共 {stats['batches']} 批、{stats['cards']} 張")


if __name__ == "__main__":
    main()
