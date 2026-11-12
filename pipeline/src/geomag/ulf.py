"""地磁 ULF（超低頻）特徵。

依據：地震前兆研究最常用的地磁指標是 ULF 頻段的垂直分量功率 Sz 與「極化比」Sz/Sh
（Hayakawa et al., 1996, GRL 23:241；Hattori, 2004, Annals of Geophysics 47:205）。
想法：來自磁層的 ULF 波動以水平分量為主；若有來自地殼的訊號，Z 分量相對增加，Sz/Sh 上升。

實作的選擇（都寫成常數，方便報告列出、改動）：
- 只用當地夜間 00–04 時（UTC 16–20 時）：白天的人為雜訊（交通、電力）與電離層電流大很多。
  注意：檔案日期 D（UTC）的 UTC 16–20 時，是台灣時間 D+1 日的 00–04 時；特徵以「檔案的 UTC 日期」標記。
- 頻段 0.005–0.05 Hz（週期 20–200 秒，約 Pc3–Pc4）；常見研究多取 0.01 Hz 附近。
- 每 30 分鐘一段：去線性趨勢、Welch 功率譜（每段 512 秒、Hann 窗），對頻段積分得到功率（nT²）。
  夜間共 8 段，取中位數，降低單段雜訊的影響。
- 去尖峰：與 61 秒移動中位數相差 > 5 nT 的點視為尖峰設為缺值；短缺口（≤ 10 秒）線性內插；
  一段內缺值 > 10% 時整段不用。
"""
import numpy as np
import pandas as pd
from scipy.signal import welch

NIGHT_UTC_HOURS = (16, 20)        # 台灣時間 00–04 時
BAND_HZ = (0.005, 0.05)
SEG_S = 1800
NPERSEG = 512
DESPIKE_WIN = 61
DESPIKE_NT = 5.0
MAX_GAP_FILL_S = 10
MAX_NAN_FRAC = 0.10
MIN_SEGMENTS = 4                  # 夜間 8 段中至少要 4 段可用，否則當天 ULF 特徵為缺值


def despike(x, win=DESPIKE_WIN, thr=DESPIKE_NT):
    s = pd.Series(x)
    med = s.rolling(win, center=True, min_periods=win // 2).median()
    bad = (s - med).abs() > thr
    return s.mask(bad).to_numpy(), int(bad.sum())


def band_power(seg, fs=1.0, band=BAND_HZ, nperseg=NPERSEG):
    """一段資料在頻段內的功率（nT²）。輸入不能有 NaN。"""
    t = np.arange(len(seg))
    seg = seg - np.polyval(np.polyfit(t, seg, 1), t)
    f, p = welch(seg, fs=fs, window="hann", nperseg=min(nperseg, len(seg)), detrend=False)
    m = (f >= band[0]) & (f <= band[1])
    return float(np.trapezoid(p[m], f[m]))


def minute_means(df, min_valid=30):
    """1 秒 → 1 分鐘平均；一分鐘內有效秒數少於 min_valid 時為缺值。"""
    g = df[["X", "Y", "Z", "F"]].resample("1min")
    m = g.mean()
    n = df["Z"].resample("1min").count()
    m[n < min_valid] = np.nan
    m["n_sec"] = n
    return m


def night_ulf(df, date):
    """某一 UTC 日的夜間 ULF 特徵。df 為 1 秒資料（index 為 UTC）。"""
    d = pd.Timestamp(date).normalize()
    t0 = d + pd.Timedelta(hours=NIGHT_UTC_HOURS[0])
    t1 = d + pd.Timedelta(hours=NIGHT_UTC_HOURS[1])
    full = pd.date_range(t0, t1, freq="1s", inclusive="left")
    w = df.reindex(full)
    res = {"night_coverage": float(w["Z"].notna().mean())}
    clean, spikes = {}, 0
    for c in ("X", "Y", "Z"):
        x, n_sp = despike(w[c].to_numpy(float))
        spikes += n_sp
        clean[c] = pd.Series(x).interpolate(limit=MAX_GAP_FILL_S, limit_area="inside").to_numpy()
    res["night_spikes"] = spikes
    sz, sh, ratio = [], [], []
    for k in range(0, len(full) - SEG_S + 1, SEG_S):
        segs = {c: clean[c][k:k + SEG_S] for c in clean}
        if any(np.isnan(s).mean() > MAX_NAN_FRAC for s in segs.values()):
            continue
        p = {}
        for c, s in segs.items():
            s = pd.Series(s).interpolate(limit_direction="both").to_numpy()
            p[c] = band_power(s)
        h = p["X"] + p["Y"]
        if h <= 0:
            continue
        sz.append(p["Z"])
        sh.append(h)
        ratio.append(p["Z"] / h)
    res["n_segments"] = len(sz)
    ok = len(sz) >= MIN_SEGMENTS
    res["ulf_sz"] = float(np.median(sz)) if ok else np.nan
    res["ulf_sh"] = float(np.median(sh)) if ok else np.nan
    res["ulf_pol"] = float(np.median(ratio)) if ok else np.nan
    return res


def daily_summary(df, date):
    """整天的品質與平均值，加上夜間 ULF。"""
    d = pd.Timestamp(date).normalize()
    day = df[(df.index >= d) & (df.index < d + pd.Timedelta(days=1))]
    h = np.sqrt(day["X"] ** 2 + day["Y"] ** 2)
    out = {
        # coverage 以 Z 分量計（ULF 特徵需要向量分量）；coverage_f 以總磁場 F 計。
        # 只記錄 F 的純量磁力儀測站（2026-11 實測：HLN、TTN）會是 coverage = 0、coverage_f ≈ 1。
        "date": d, "coverage": float(day["Z"].notna().sum() / 86400.0),
        "coverage_f": float(day["F"].notna().sum() / 86400.0),
        "h_mean": float(h.mean()), "z_mean": float(day["Z"].mean()), "f_mean": float(day["F"].mean()),
        "h_std": float(h.std()), "z_std": float(day["Z"].std()),
    }
    out.update(night_ulf(df, d))
    return out
