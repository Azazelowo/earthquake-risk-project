"""APP 的計算核心：讀快照、任意規模 m／任意時間 T／任意區域的機率。不依賴 Streamlit，方便測試。

計算鏈（和 pipeline/src/eqforecast/hazard.py 相同，只是改成在 APP 端即時換算）：

    N_ref(格, T)  快照裡有 10 個時間長度（1 小時～30 天）的期望地震數（M≥m_ref，已校準）；
                  中間的 T 用 log T – log N 線性內插（N 隨 T 單調遞增，內插不會破壞單調性）。
    N(格, T, ≥m) = N_ref × 10^(−b (m − m_ref))           古騰堡–芮克特規模分布
    N(區域)      = Σ 格 N(格) × 該格落在區域內的面積比例     區域可以是「點＋半徑」或「線＋寬度」
    P(區域)      = 1 − exp(−N(區域))                       卜瓦松：至少發生一次

「平常時期」對照：快照的 n_bg 欄只有長期背景項、沒有近期活動加成，再換算成「只有背景」模型
自己擬合的水準（見 background_rescale），代表「這個地方平常的機率」。APP 會把兩者並排，讓使用者看出「現在比平常高幾倍」，而不是只看到一個沒有參考點的數字。

限制：
- 區域只算網格範圍（21–26°N、119–123°E）內的部分；超出的部分不計，APP 會提示比例。
- m < m_ref 是往下外推（目錄在小規模不完整）；m 很大時樣本極少，也是外推。
"""
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

KM_PER_DEG = 111.195
SUBSAMPLE = 10  # 每格切成 10×10 個點估算面積比例

HORIZON_CHOICES = [  # (天, 標籤)：APP 滑桿的刻度；中間值也能算，只是滑桿上不列
    (1 / 24, "1 小時"), (2 / 24, "2 小時"), (3 / 24, "3 小時"), (6 / 24, "6 小時"), (12 / 24, "12 小時"),
    (1, "1 天"), (2, "2 天"), (3, "3 天"), (5, "5 天"), (7, "7 天"), (10, "10 天"), (14, "14 天"),
    (21, "21 天"), (30, "30 天"),
]


def horizon_label(days):
    for d, label in HORIZON_CHOICES:
        if abs(d - days) < 1e-9:
            return label
    return f"{days * 24:g} 小時" if days < 1 else f"{days:g} 天"


@dataclass
class Snapshot:
    params: dict
    cells: pd.DataFrame        # lat_bin, lon_bin（格的西南角）
    horizons: np.ndarray       # (H,) 天，遞增
    n_ref: np.ndarray          # (格, H) 完整模型
    n_bg: np.ndarray           # (格, H) 只有長期背景
    backtest: pd.DataFrame
    events: pd.DataFrame

    @property
    def deg(self):
        return float(self.params["grid"]["deg"])

    @property
    def m_ref(self):
        return float(self.params["params"]["m_ref"])

    @property
    def b(self):
        return float(self.params["params"]["b"])

    @property
    def issue_time(self):
        return pd.Timestamp(self.params["issue_time"])

    @property
    def centers(self):
        h = self.deg / 2
        return self.cells["lat_bin"].to_numpy() + h, self.cells["lon_bin"].to_numpy() + h


def background_rescale(params):
    """把匯出的 n_bg 換成「只有背景」模型自己的水準。

    匯出的 n_bg 是完整模型裡的背景項，倍率 mu 是和近期加成一起擬合的；近期加成分走了一部分
    地震，所以 mu 偏小（約 0.54），直接拿來當「平常」會讓每個地方看起來都比平常高約 2 倍。
    只有背景時單獨擬合的倍率是 fit_info.mu_bg_only（約 1.21），用它的比例換算回來。
    """
    mu = params.get("params", {}).get("mu")
    mu_bg = params.get("fit_info", {}).get("mu_bg_only")
    if mu and mu_bg:
        return float(mu_bg) / float(mu)
    return 1.0


def to_taipei(ts):
    """快照裡的時間都是不帶時區的 UTC；畫面上顯示台灣時間。"""
    return pd.Timestamp(ts) + pd.Timedelta(hours=8)


def pick_data_dir(base_dir, live_dir):
    """有即時更新的輸出、且發布時間比正式快照新，就用即時版。"""
    base_dir, live_dir = Path(base_dir), Path(live_dir)
    try:
        live = json.loads((live_dir / "params.json").read_text(encoding="utf-8"))
        base = json.loads((base_dir / "params.json").read_text(encoding="utf-8"))
        if pd.Timestamp(live["issue_time"]) > pd.Timestamp(base["issue_time"]) \
                and (live_dir / "hazard_latest.csv").exists():
            return live_dir
    except (FileNotFoundError, KeyError, ValueError):
        pass
    return base_dir


def gap_ratio_at(params, T):
    """即時版的缺口評估（缺口版總數 ÷ 完整版），取最接近 T 的時間長度；沒有評估時回傳 None。"""
    ratios = (params.get("data_status") or {}).get("gap_ratio") or {}
    if not ratios:
        return None
    keys = np.array([float(k) for k in ratios])
    k = list(ratios)[int(np.argmin(np.abs(np.log(keys) - np.log(T))))]
    return float(ratios[k])


def load_snapshot(data_dir):
    d = Path(data_dir)
    params = json.loads((d / "params.json").read_text(encoding="utf-8"))
    latest = pd.read_csv(d / "hazard_latest.csv")
    horizons = np.array(sorted(float(h) for h in params["export_horizons_days"]))
    n_ref = np.column_stack([latest[f"n_ref_{h:.5f}d"].to_numpy(float) for h in horizons])
    n_bg = np.column_stack([latest[f"n_bg_{h:.5f}d"].to_numpy(float) for h in horizons])
    n_bg = n_bg * background_rescale(params)
    backtest = pd.read_csv(d / "backtest.csv")
    ev_path = d / "recent_events.csv"
    events = pd.read_csv(ev_path, parse_dates=["time"]) if ev_path.exists() else pd.DataFrame(
        columns=["time", "lat", "lon", "depth", "ml"])
    return Snapshot(params, latest[["lat_bin", "lon_bin"]].copy(), horizons, n_ref, n_bg, backtest, events)


def interp_horizon(mat, horizons, T):
    """每一格在時間長度 T（天）的期望數；在 log–log 空間線性內插。T 必須在匯出範圍內。"""
    T = float(T)
    lo, hi = horizons[0], horizons[-1]
    if not (lo * (1 - 1e-9) <= T <= hi * (1 + 1e-9)):
        raise ValueError(f"時間長度 {T} 天超出快照範圍 {lo:.4f}–{hi:g} 天")
    T = min(max(T, lo), hi)
    k = int(np.searchsorted(horizons, T))
    if k < len(horizons) and abs(horizons[k] - T) < 1e-12:
        return mat[:, k].copy()
    k = min(max(k, 1), len(horizons) - 1)
    t0, t1 = horizons[k - 1], horizons[k]
    w = (np.log(T) - np.log(t0)) / (np.log(t1) - np.log(t0))
    y0 = np.log(np.maximum(mat[:, k - 1], 1e-300))
    y1 = np.log(np.maximum(mat[:, k], 1e-300))
    return np.exp(y0 + w * (y1 - y0))


def scale_magnitude(n_ref, m, m_ref, b):
    return np.asarray(n_ref) * 10.0 ** (-b * (m - m_ref))


def probability(n):
    return -np.expm1(-np.asarray(n, float))


def expected(snap, m, T, which="full"):
    mat = snap.n_ref if which == "full" else snap.n_bg
    return scale_magnitude(interp_horizon(mat, snap.horizons, T), m, snap.m_ref, snap.b)


# ---------- 區域 ----------

def _subpoints(snap):
    """每格 SUBSAMPLE×SUBSAMPLE 個子點的經緯度，形狀 (格, S²)。快取在 snap 上。"""
    cache = getattr(snap, "_subpts", None)
    if cache is not None:
        return cache
    off = (np.arange(SUBSAMPLE) + 0.5) / SUBSAMPLE * snap.deg
    oy, ox = np.meshgrid(off, off, indexing="ij")
    lat = snap.cells["lat_bin"].to_numpy()[:, None] + oy.ravel()[None, :]
    lon = snap.cells["lon_bin"].to_numpy()[:, None] + ox.ravel()[None, :]
    snap._subpts = (lat, lon)
    return snap._subpts


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dlat, dlon = p2 - p1, np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(dlat / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlon / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def point_weights(snap, lat, lon, radius_km):
    """點震源：以 (lat, lon) 為圓心、半徑 radius_km 的圓，每格落在圓內的面積比例。"""
    plat, plon = _subpoints(snap)
    inside = haversine_km(lat, lon, plat, plon) <= radius_km
    return inside.mean(axis=1)


def _dist_to_polyline_km(plat, plon, line):
    """點到折線的最短距離（km）。區域不大（台灣附近），用以折線中點為準的等距投影近似。"""
    line = np.asarray(line, float)
    lat0 = np.radians(line[:, 0].mean())
    kx = KM_PER_DEG * np.cos(lat0)
    px, py = plon * kx, plat * KM_PER_DEG
    best = np.full(plat.shape, np.inf)
    for (a_lat, a_lon), (b_lat, b_lon) in zip(line[:-1], line[1:]):
        ax_, ay_ = a_lon * kx, a_lat * KM_PER_DEG
        bx_, by_ = b_lon * kx, b_lat * KM_PER_DEG
        dx, dy = bx_ - ax_, by_ - ay_
        L2 = dx * dx + dy * dy
        t = np.zeros_like(px) if L2 == 0 else np.clip(((px - ax_) * dx + (py - ay_) * dy) / L2, 0, 1)
        best = np.minimum(best, np.hypot(px - (ax_ + t * dx), py - (ay_ + t * dy)))
    return best


def line_weights(snap, line, half_width_km):
    """線震源：折線 line=[(lat, lon), ...] 左右各 half_width_km 的帶狀區域，每格的面積比例。"""
    if len(line) < 2:
        raise ValueError("線震源至少要兩個點")
    plat, plon = _subpoints(snap)
    return (_dist_to_polyline_km(plat, plon, line) <= half_width_km).mean(axis=1)


def all_weights(snap):
    return np.ones(len(snap.cells))


def region_area_km2(snap, weights):
    lat_c, _ = snap.centers
    cell_area = (snap.deg * KM_PER_DEG) ** 2 * np.cos(np.radians(lat_c))
    return float((weights * cell_area).sum())


def circle_area_km2(radius_km):
    return float(np.pi * radius_km ** 2)


def line_area_km2(line, half_width_km):
    line = np.asarray(line, float)
    length = float(sum(haversine_km(a[0], a[1], b[0], b[1]) for a, b in zip(line[:-1], line[1:])))
    return 2 * half_width_km * length + np.pi * half_width_km ** 2


# ---------- 區域機率與曲線 ----------

def region_expected(snap, weights, m, T, which="full"):
    return float((weights * expected(snap, m, T, which)).sum())


def region_probability(snap, weights, m, T, which="full"):
    return float(probability(region_expected(snap, weights, m, T, which)))


def curve_vs_magnitude(snap, weights, T, mags):
    """固定地點與時間，機率隨規模門檻變化。回傳 DataFrame(m, p_full, p_bg)。"""
    n_full = (weights * interp_horizon(snap.n_ref, snap.horizons, T)).sum()
    n_bg = (weights * interp_horizon(snap.n_bg, snap.horizons, T)).sum()
    mags = np.asarray(mags, float)
    return pd.DataFrame({"m": mags,
                         "p_full": probability(scale_magnitude(n_full, mags, snap.m_ref, snap.b)),
                         "p_bg": probability(scale_magnitude(n_bg, mags, snap.m_ref, snap.b))})


def curve_vs_horizon(snap, weights, m, horizons_days):
    """固定地點與規模，機率隨時間長度變化。"""
    rows = [(T, region_probability(snap, weights, m, T, "full"), region_probability(snap, weights, m, T, "bg"))
            for T in horizons_days]
    return pd.DataFrame(rows, columns=["T", "p_full", "p_bg"])


def magnitude_at_probability(snap, weights, T, p_target, which="full"):
    """反查：在這個地點、這段時間內，機率達 p_target 的最大規模門檻（GR 可解析反解）。"""
    mat = snap.n_ref if which == "full" else snap.n_bg
    n_ref = float((weights * interp_horizon(mat, snap.horizons, T)).sum())
    n_target = -np.log1p(-p_target)
    if n_ref <= 0:
        return float("nan")
    return snap.m_ref + np.log10(n_ref / n_target) / snap.b


# ---------- 可信度提示 ----------

def reliability(snap, m, T):
    """回傳這組 (m, T) 的可信度提示：是否外推、回測中最接近那一列的「預測／實際」與地震數。"""
    notes = []
    if m < snap.m_ref:
        notes.append(f"規模門檻低於 M{snap.m_ref:g}：目錄在小規模不完整，這是用規模分布往下外推的。")
    if m >= 6.5:
        notes.append("M6.5 以上在回測期間樣本極少，數字是用規模分布外推的，只能當量級參考。")
    bt = snap.backtest.dropna(subset=["n_ratio_full"])
    row = None
    if len(bt):
        dm = (bt["m"] - m).abs()
        dT = (np.log(bt["horizon_days"]) - np.log(T)).abs()
        row = bt.loc[(dm + dT).idxmin()]
        if row["n_events"] < 20:
            notes.append(f"最接近的回測組（M≥{row['m']:g}、{horizon_label(row['horizon_days'])}）只有 "
                         f"{int(row['n_events'])} 個地震，校準數字不穩。")
        r = row["n_ratio_full"]
        if r > 1.25:
            notes.append(f"回測中這附近的組合系統性高估（預測÷實際 ≈ {r:.2f}），實際機率可能比顯示的低。")
        elif r < 0.8:
            notes.append(f"回測中這附近的組合系統性低估（預測÷實際 ≈ {r:.2f}），實際機率可能比顯示的高。")
    return {"notes": notes, "backtest_row": None if row is None else row.to_dict()}


def format_probability(p):
    if not np.isfinite(p):
        return "—"
    if p >= 0.995:
        return "> 99%"
    if p >= 0.1:
        return f"{p * 100:.0f}%"
    if p >= 0.01:
        return f"{p * 100:.1f}%"
    if p >= 1e-4:
        return f"{p * 100:.2f}%"
    return "< 0.01%"


def format_odds(p):
    """把機率換成「約 N 次裡有 1 次」，比百分比直覺。"""
    if not np.isfinite(p) or p <= 0:
        return "—"
    if p >= 0.5:
        return "比一半還高"
    n = 1 / p
    if n < 10:
        return f"約 {n:.1f} 次裡有 1 次"
    if n < 1e4:
        return f"約 {n:,.0f} 次裡有 1 次"
    return f"約 {n / 1e4:,.0f} 萬次裡有 1 次"


def color_scale(p, p_min=1e-5, p_max=0.3):
    """機率 → RGBA（對數色階，淡黃 → 橘 → 深紅）。低於 p_min 透明。"""
    p = np.asarray(p, float)
    x = np.clip((np.log10(np.maximum(p, 1e-12)) - np.log10(p_min)) / (np.log10(p_max) - np.log10(p_min)), 0, 1)
    stops = np.array([[255, 245, 200], [253, 174, 97], [227, 74, 51], [140, 10, 30]], float)
    pos = x * (len(stops) - 1)
    i = np.minimum(pos.astype(int), len(stops) - 2)
    f = (pos - i)[:, None] if pos.ndim else pos - i
    rgb = stops[i] * (1 - f) + stops[i + 1] * f
    alpha = np.where(p < p_min, 0, 60 + 150 * x)
    return np.column_stack([rgb, alpha]).astype(int) if p.ndim else np.append(rgb, alpha).astype(int)
