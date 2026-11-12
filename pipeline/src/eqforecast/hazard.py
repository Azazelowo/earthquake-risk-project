"""連續式風險函數：任意規模門檻 m、任意時間長度 T、每個網格的地震發生機率。

為什麼需要：B3 基準模型只回答「30 天內、ML≥5.5 主震」這一個固定問題。使用者要的是
規模、時間、地點都連續的風險評估（APP 上用滑桿選規模與時間）。做法是先估每格的
「發生率」，再用兩條統計規律換算：

    N(格, T, ≥m_ref) = mu × 背景發生率(格) × T  +  K × 近期活動加成(格, T)
    N(格, T, ≥m)     = N(格, T, ≥m_ref) × 10^(−b × (m − m_ref))        古騰堡–芮克特規模分布
    P(格, T, ≥m)     = 1 − exp(−N)                                     卜瓦松：至少發生一次的機率

- 背景發生率：發布時間之前所有 M≥m_ref 地震的每日次數，做高斯空間平滑（以前常震的地方）。
- 近期活動加成：過去 lookback 天內每個地震，依大森定律 (t + c)^(−p) 隨時間衰減，
  規模越大貢獻越多（10^(alpha × (M − m_ref))），在預報窗內積分後做空間平滑（剛震過的地方）。
- mu、K 用訓練期資料以卜瓦松最大概似估計；形狀參數（alpha、p、平滑寬度）用格點搜尋挑選。

重要限制：
1. 這個模型預報的是「全部地震」，包含餘震。它和 B3（只算去群聚後的主震）回答不同的問題，
   分數不能互相比較。近期加成的技巧大部分來自「大地震後會有餘震」。
2. m < m_ref 時目錄不完整，m 很大時樣本極少；這兩段都是用規模分布外推，不是直接驗證過的。
3. 所有量都只用發布時間之前的資料。
"""
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from scipy.ndimage import gaussian_filter
from scipy.optimize import minimize

DAY = np.timedelta64(1, "D")


@dataclass
class Grid:
    lat_min: float = 21.0
    lat_max: float = 26.0
    lon_min: float = 119.0
    lon_max: float = 123.0
    deg: float = 0.2

    @property
    def shape(self):
        return (int(round((self.lat_max - self.lat_min) / self.deg)),
                int(round((self.lon_max - self.lon_min) / self.deg)))

    def cell_index(self, lat, lon):
        """回傳 (i, j, 是否在區內)。i 為緯度方向、j 為經度方向。"""
        lat, lon = np.asarray(lat, float), np.asarray(lon, float)
        i = np.floor((lat - self.lat_min) / self.deg + 1e-9).astype(int)
        j = np.floor((lon - self.lon_min) / self.deg + 1e-9).astype(int)
        ni, nj = self.shape
        return i, j, (i >= 0) & (i < ni) & (j >= 0) & (j < nj)

    def frame(self):
        ni, nj = self.shape
        ii, jj = np.meshgrid(np.arange(ni), np.arange(nj), indexing="ij")
        return pd.DataFrame({"lat_bin": (self.lat_min + ii.ravel() * self.deg).round(4),
                             "lon_bin": (self.lon_min + jj.ravel() * self.deg).round(4)})


@dataclass
class HazardParams:
    m_ref: float = 3.5           # 發生率以 M≥m_ref 計；需高於目錄的完備規模
    b: float = 1.0               # 古騰堡–芮克特 b 值
    mu: float = 1.0              # 背景項倍率
    K: float = 0.0               # 近期加成的強度
    alpha: float = 0.8           # 規模每大 1 級，觸發後續地震的能力乘上 10^alpha
    c: float = 0.01              # 大森定律的時間偏移（天）
    p: float = 1.1               # 大森定律的衰減指數
    sigma_bg: float = 1.0        # 背景平滑寬度（格）
    sigma_st: float = 0.5        # 近期加成平滑寬度（格）
    floor_frac: float = 0.05     # 背景項混入多少比例的全區平均，避免從沒震過的格子機率為 0
    lookback_days: float = 365.0

    def to_dict(self):
        return asdict(self)


class Catalog:
    """區內、M≥m_ref 的地震，依時間排序，並預先算好所在網格。"""

    def __init__(self, events, grid, m_ref):
        ev = events[np.isfinite(events["ml"]) & (events["ml"] >= m_ref)]
        i, j, ok = grid.cell_index(ev["lat"].to_numpy(), ev["lon"].to_numpy())
        ev = ev[ok].assign(_i=i[ok], _j=j[ok]).sort_values("time")
        self.grid = grid
        self.time = ev["time"].to_numpy("datetime64[us]")
        self.ml = ev["ml"].to_numpy(float)
        self.flat = (ev["_i"].to_numpy() * grid.shape[1] + ev["_j"].to_numpy()).astype(int)
        self.t_start = self.time[0] if len(self.time) else None

    def __len__(self):
        return len(self.time)

    def before(self, t0):
        """time < t0 的事件數（事件已排序，所以就是前幾筆）。"""
        return int(np.searchsorted(self.time, np.datetime64(t0, "us"), side="left"))

    def counts(self, lo, hi, min_ml=None):
        """[lo, hi) 內每格的事件數（可另加規模門檻）。"""
        a, b = self.before(lo), self.before(hi)
        flat = self.flat[a:b]
        if min_ml is not None:
            flat = flat[self.ml[a:b] >= min_ml]
        n = self.grid.shape[0] * self.grid.shape[1]
        return np.bincount(flat, minlength=n).reshape(self.grid.shape).astype(float)


def _smooth(field, sigma):
    """高斯平滑，並把總量調回平滑前（邊界外流失的部分補回來）。"""
    if sigma <= 0:
        return field
    total = field.sum()
    out = gaussian_filter(field, sigma=sigma, mode="constant")
    s = out.sum()
    return out * (total / s) if s > 0 else out


def omori_integral(age_days, horizon_days, c, p):
    """∫ (s + c)^(−p) ds，s 從 age 到 age + horizon。age 是事件距發布時間的天數（> 0）。"""
    a = np.asarray(age_days, float) + c
    b = a + horizon_days
    if abs(p - 1.0) < 1e-9:
        return np.log(b / a)
    return (b ** (1 - p) - a ** (1 - p)) / (1 - p)


def background_rate(cat, t0, sigma_bg, floor_frac):
    """t0 之前的每格每日發生率（M≥m_ref）。"""
    t0 = np.datetime64(t0, "us")
    years_days = (t0 - cat.t_start) / DAY
    if years_days <= 0:
        return np.zeros(cat.grid.shape)
    rate = _smooth(cat.counts(cat.t_start, t0), sigma_bg) / years_days
    return (1 - floor_frac) * rate + floor_frac * rate.mean()


def short_term(cat, t0, horizon_days, alpha, c, p, sigma_st, lookback_days, m_ref):
    """t0 起 horizon_days 內，近期地震依大森定律貢獻的量（未乘 K）。"""
    t0 = np.datetime64(t0, "us")
    a = cat.before(t0 - np.timedelta64(int(lookback_days * 86400), "s"))
    b = cat.before(t0)
    n = cat.grid.shape[0] * cat.grid.shape[1]
    if b <= a:
        return np.zeros(cat.grid.shape)
    age = (t0 - cat.time[a:b]) / DAY
    w = 10 ** (alpha * (cat.ml[a:b] - m_ref)) * omori_integral(age, horizon_days, c, p)
    return _smooth(np.bincount(cat.flat[a:b], weights=w, minlength=n).reshape(cat.grid.shape), sigma_st)


def expected_ref(cat, t0, horizon_days, prm):
    """N(格, T, ≥m_ref)：背景項與近期項分開回傳 (mu × bg × T, K × st)。"""
    bg = background_rate(cat, t0, prm.sigma_bg, prm.floor_frac) * horizon_days
    st = short_term(cat, t0, horizon_days, prm.alpha, prm.c, prm.p, prm.sigma_st, prm.lookback_days, prm.m_ref)
    return prm.mu * bg, prm.K * st


def scale_magnitude(n_ref, m, m_ref, b):
    """把 M≥m_ref 的期望次數換算成 M≥m。"""
    return np.asarray(n_ref, float) * 10 ** (-b * (m - m_ref))


def probability(n):
    """期望次數 → 至少發生一次的機率。"""
    return -np.expm1(-np.asarray(n, float))


def estimate_b(ml, m_ref, bin_width=None):
    """Aki–Utsu 最大概似 b 值。bin_width 未給時依規模的小數位數判斷（0.1 或 0.01）。"""
    m = np.asarray(ml, float)
    m = m[np.isfinite(m) & (m >= m_ref)]
    if len(m) < 50:
        raise ValueError(f"M≥{m_ref} 只有 {len(m)} 筆，不足以估 b 值")
    if bin_width is None:
        one_decimal = np.mean(np.abs(m * 10 - np.round(m * 10)) < 1e-6)
        bin_width = 0.1 if one_decimal > 0.5 else 0.01
    return float(np.log10(np.e) / (m.mean() - (m_ref - bin_width / 2)))


def poisson_ll(n_obs, n_exp):
    """卜瓦松對數概似（略去與模型無關的 log(n!) 項）。"""
    n_exp = np.clip(n_exp, 1e-300, None)
    return float(np.sum(n_obs * np.log(n_exp) - n_exp))


def fit_mu_k(obs, bg, st, with_short_term=True):
    """給定形狀參數下，以最大概似估 mu、K。obs、bg、st 形狀相同（窗 × 格攤平）。"""
    obs, bg, st = (np.asarray(x, float).ravel() for x in (obs, bg, st))

    def nll(theta):
        mu = np.exp(theta[0])
        k = np.exp(theta[1]) if with_short_term else 0.0
        return -poisson_ll(obs, mu * bg + k * st)

    total = obs.sum()
    x0 = [np.log(max(total / max(bg.sum(), 1e-12), 1e-6) * (0.5 if with_short_term else 1.0)),
          np.log(max(0.5 * total / max(st.sum(), 1e-12), 1e-9))]
    res = minimize(nll, x0, method="Nelder-Mead", options={"xatol": 1e-4, "fatol": 1e-6, "maxiter": 2000})
    mu = float(np.exp(res.x[0]))
    return mu, (float(np.exp(res.x[1])) if with_short_term else 0.0), -float(res.fun)


def issue_times(start, end, step_days):
    """[start, end) 內每隔 step_days 一個發布時間。"""
    return pd.date_range(start, end, freq=f"{int(step_days)}D", inclusive="left").to_numpy("datetime64[us]")


def fit(events, grid, train_start, train_end, m_ref=3.5, horizon_days=7.0,
        alphas=(0.6, 0.8, 1.0), ps=(1.0, 1.2), sigmas_st=(0.5, 1.0), sigmas_bg=(1.0, 2.0), base=None):
    """用 [train_start, train_end) 的資料估參數。回傳 (HazardParams, 搜尋紀錄 DataFrame)。

    b 值只用 train_end 之前的地震；每個訓練窗的背景與近期項只用該窗發布時間之前的地震。
    """
    base = base or HazardParams(m_ref=m_ref)
    cat = Catalog(events, grid, m_ref)
    n_before = cat.before(train_end)
    b = estimate_b(cat.ml[:n_before], m_ref)
    t0s = issue_times(train_start, train_end, horizon_days)
    h = np.timedelta64(int(horizon_days * 86400), "s")
    t0s = t0s[t0s + h <= np.datetime64(train_end, "us")]
    obs = np.stack([cat.counts(t, t + h) for t in t0s])
    bgs = {s: np.stack([background_rate(cat, t, s, base.floor_frac) * horizon_days for t in t0s]) for s in sigmas_bg}
    rows, best = [], None
    for alpha in alphas:
        for p in ps:
            for s_st in sigmas_st:
                st = np.stack([short_term(cat, t, horizon_days, alpha, base.c, p, s_st, base.lookback_days, m_ref)
                               for t in t0s])
                for s_bg in sigmas_bg:
                    mu, k, ll = fit_mu_k(obs, bgs[s_bg], st)
                    rows.append({"alpha": alpha, "p": p, "sigma_st": s_st, "sigma_bg": s_bg, "mu": mu, "K": k, "ll": ll})
                    if best is None or ll > best["ll"]:
                        best = rows[-1]
    prm = HazardParams(m_ref=m_ref, b=b, mu=best["mu"], K=best["K"], alpha=best["alpha"], c=base.c, p=best["p"],
                       sigma_bg=best["sigma_bg"], sigma_st=best["sigma_st"], floor_frac=base.floor_frac,
                       lookback_days=base.lookback_days)
    mu_bg, _, ll_bg = fit_mu_k(obs, bgs[prm.sigma_bg], np.zeros_like(obs), with_short_term=False)
    info = {"n_windows": int(len(t0s)), "n_events": int(obs.sum()), "ll_full": best["ll"], "ll_bg_only": ll_bg,
            "mu_bg_only": mu_bg, "uniform_rate_per_cell_day": float(obs.sum() / obs.size / horizon_days)}
    return prm, pd.DataFrame(rows).sort_values("ll", ascending=False).reset_index(drop=True), info
