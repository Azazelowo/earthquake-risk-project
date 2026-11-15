"""前兆調整項：檢驗「前兆特徵能不能讓風險函數的發生率更準」。

為什麼這樣做：B3 的前兆比較只有十幾個 ML≥5.5 主震，沒有檢定力。這裡改用風險函數的
全部地震（M≥3.5，數千個）當樣本，把前兆當成發生率的倍率：

    N(格, 窗) = N0(格, 窗) × exp(b0 + b1·x1 + b2·x2 + …)

N0 是風險函數（背景＋近期加成＋滾動校準）的期望次數，當成固定的「偏移」；x 是該格在發布
時間之前的前兆特徵。若前兆沒有資訊，b1… 應該接近 0，測試期的對數概似不會比「只有 b0」好。

判斷是不是碰巧：把前兆特徵沿時間軸「整段循環平移」後重做同樣的擬合與評估，重複很多次，
看真實結果在這些平移結果中排第幾。整段平移保留了特徵自己的時間連續性與空間結構，
只打斷它和地震的時間對應；逐點打亂會破壞連續性，讓檢定過於樂觀。
"""
import numpy as np


def fit_poisson_offset(X, y, offset, ridge=1e-3, max_iter=100, tol=1e-9):
    """卜瓦松迴歸（對數連結、固定偏移），牛頓法。回傳係數（第 0 個是截距）。

    X 不含截距欄；ridge 只加在非截距係數上，避免特徵共線時發散。
    """
    X = np.asarray(X, float)
    y = np.asarray(y, float)
    off = np.asarray(offset, float)
    A = np.column_stack([np.ones(len(y)), X])
    pen = np.r_[0.0, np.full(A.shape[1] - 1, ridge)]
    beta = np.zeros(A.shape[1])
    beta[0] = np.log(max(y.sum(), 1e-12) / max(np.exp(off).sum(), 1e-300))

    def objective(b):
        eta = np.clip(off + A @ b, -700, 50)
        return float(np.sum(y * eta - np.exp(eta)) - 0.5 * np.sum(pen * b * b))

    cur = objective(beta)
    for _ in range(max_iter):
        mu = np.exp(np.clip(off + A @ beta, -700, 50))
        grad = A.T @ (y - mu) - pen * beta
        hess = (A * mu[:, None]).T @ A + np.diag(pen) + 1e-12 * np.eye(len(beta))
        step = np.linalg.solve(hess, grad)
        t = 1.0
        while t > 1e-8:
            new = objective(beta + t * step)
            if new >= cur - 1e-12:
                break
            t /= 2
        beta = beta + t * step
        if abs(new - cur) < tol * (1 + abs(cur)):
            cur = new
            break
        cur = new
    return beta


def poisson_ll_offset(beta, X, y, offset):
    """對數概似（略去 log(y!)）。"""
    eta = np.clip(np.asarray(offset, float) + np.column_stack([np.ones(len(y)), X]) @ beta, -700, 50)
    return float(np.sum(np.asarray(y, float) * eta - np.exp(eta)))


def rolling_before(daily, lookback, min_frac=0.5):
    """每一天「之前 lookback 天」（不含當天）的平均。

    daily：形狀 (天, …) 的陣列，缺值為 NaN。有效天數不到 lookback × min_frac 時回傳 NaN。
    第 d 列的結果只用到第 d−lookback … d−1 列。
    """
    a = np.asarray(daily, float)
    ok = np.isfinite(a)
    cs = np.concatenate([np.zeros((1,) + a.shape[1:]), np.cumsum(np.where(ok, a, 0.0), axis=0)])
    cn = np.concatenate([np.zeros((1,) + a.shape[1:]), np.cumsum(ok, axis=0)])
    idx = np.arange(a.shape[0])
    lo = np.clip(idx - lookback, 0, None)
    s = cs[idx] - cs[lo]
    n = cn[idx] - cn[lo]
    out = np.full(a.shape, np.nan)
    good = n >= max(1, int(np.ceil(lookback * min_frac)))
    out[good] = s[good] / n[good]
    return out


def standardize(train, *others):
    """用訓練資料的平均與標準差標準化（標準差為 0 的欄位不縮放）。"""
    mean = np.nanmean(train, axis=0)
    std = np.nanstd(train, axis=0)
    std = np.where(std > 0, std, 1.0)
    return [(x - mean) / std for x in (train,) + others]


def evaluate(feat, y, offset, is_train, is_test):
    """擬合「只有截距」與「截距＋前兆」，回傳測試期每個地震的資訊增益與係數。

    feat：(樣本, 特徵)，含 NaN 的樣本會被排除（兩個模型用同一批樣本）。
    """
    valid = np.isfinite(feat).all(axis=1) & np.isfinite(offset)
    tr, te = valid & is_train, valid & is_test
    n_te = float(y[te].sum())
    if tr.sum() < 50 or y[tr].sum() < 10 or n_te < 1:
        return {"gain": np.nan, "n_train_rows": int(tr.sum()), "n_test_rows": int(te.sum()),
                "n_train_events": int(y[tr].sum()), "n_test_events": int(n_te), "beta": None}
    xtr, xte = standardize(feat[tr], feat[te])
    b_base = fit_poisson_offset(np.empty((int(tr.sum()), 0)), y[tr], offset[tr])
    b_full = fit_poisson_offset(xtr, y[tr], offset[tr])
    ll_base = poisson_ll_offset(b_base, np.empty((int(te.sum()), 0)), y[te], offset[te])
    ll_full = poisson_ll_offset(b_full, xte, y[te], offset[te])
    return {"gain": (ll_full - ll_base) / n_te, "n_train_rows": int(tr.sum()), "n_test_rows": int(te.sum()),
            "n_train_events": int(y[tr].sum()), "n_test_events": int(n_te), "beta": b_full,
            "train_gain": (poisson_ll_offset(b_full, xtr, y[tr], offset[tr])
                           - poisson_ll_offset(b_base, np.empty((int(tr.sum()), 0)), y[tr], offset[tr]))
                          / float(y[tr].sum())}


def circular_shift_test(feat3, y2, off2, is_train_t, is_test_t, n_shifts=200, min_shift=8, seed=0):
    """循環時間平移檢定。

    feat3：(發布時間, 格, 特徵)；y2、off2：(發布時間, 格)；is_train_t、is_test_t：(發布時間,)。
    回傳 (真實結果 dict, 平移後的增益陣列, p 值)。p = (1 + 平移增益 ≥ 真實增益的次數) / (1 + 有效平移次數)。
    """
    T, C, K = feat3.shape
    tr = np.repeat(is_train_t, C)
    te = np.repeat(is_test_t, C)
    y, off = y2.ravel(), off2.ravel()
    real = evaluate(feat3.reshape(T * C, K), y, off, tr, te)
    rng = np.random.default_rng(seed)
    candidates = np.arange(min_shift, T - min_shift + 1)
    if len(candidates) == 0 or not np.isfinite(real["gain"]):
        return real, np.array([]), np.nan
    shifts = rng.choice(candidates, size=min(n_shifts, len(candidates)), replace=False)
    null = np.array([evaluate(np.roll(feat3, int(s), axis=0).reshape(T * C, K), y, off, tr, te)["gain"]
                     for s in shifts])
    null = null[np.isfinite(null)]
    p = (1 + int(np.sum(null >= real["gain"]))) / (1 + len(null)) if len(null) else np.nan
    return real, null, p
