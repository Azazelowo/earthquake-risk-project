"""警報式預報的評估指標。

警報規則：每個發布時間點，分數最高的 k 個網格發警報（k = round(τ × 網格數)）。
    這樣做的好處：
    1. 只用當下的分數決定警報，不需要知道未來的分數分布（不偷看）。
    2. 不同模型可以在相同的警報成本 τ 下比較，避免「多發警報換命中率」的假進步。

列聯表（以「網格 × 預報窗」為單位）：
                     有目標事件     沒有目標事件
    發警報           a 命中          b 誤報
    沒發警報         c 漏報          d 正確否定

    POD  = a / (a + c)            偵測率：目標事件有多少被警報涵蓋
    FAR  = b / (a + b)            誤報比：警報中有多少是落空的（false alarm ratio）
    CSI  = a / (a + b + c)        臨界成功指數：同時懲罰漏報與誤報
    τ    = (a + b) / N            警報占用的時空比例
    增益 = POD / τ                 相對於隨機警報的機率增益（隨機 = 1）

Molchan 圖：橫軸 τ、縱軸漏報率 ν = 1 - POD。隨機警報落在對角線上。
ASS（area skill score）= POD 對 τ 曲線下的面積：隨機 = 0.5，完美 = 1。
"""
import numpy as np
from scipy import stats


def window_ranks(t0, score, seed=0):
    """每個發布時間點內的名次（1 = 分數最高）。同分時以固定亂數種子打散。"""
    t0 = np.asarray(t0)
    score = np.asarray(score, float)
    score = np.where(np.isnan(score), -np.inf, score)
    tie = np.random.default_rng(seed).random(len(score))
    _, grp = np.unique(t0, return_inverse=True)
    order = np.lexsort((tie, -score, grp))
    ranks = np.empty(len(score), np.int64)
    grp_sorted = grp[order]
    starts = np.r_[0, np.flatnonzero(np.diff(grp_sorted)) + 1]
    pos = np.arange(len(score)) - np.repeat(starts, np.diff(np.r_[starts, len(score)]))
    ranks[order] = pos + 1
    return ranks


def contingency(alarm, y):
    alarm = np.asarray(alarm, bool)
    y = np.asarray(y, bool)
    a = int((alarm & y).sum())
    b = int((alarm & ~y).sum())
    c = int((~alarm & y).sum())
    d = int((~alarm & ~y).sum())
    return a, b, c, d


def skill(a, b, c, d):
    n = a + b + c + d
    pod = a / (a + c) if a + c else np.nan
    far = b / (a + b) if a + b else np.nan
    csi = a / (a + b + c) if a + b + c else np.nan
    tau = (a + b) / n if n else np.nan
    pofd = b / (b + d) if b + d else np.nan
    return {
        "hits": a, "false_alarms": b, "misses": c, "correct_negatives": d,
        "POD": pod, "FAR": far, "CSI": csi, "tau": tau,
        "gain": pod / tau if tau and not np.isnan(pod) else np.nan,
        "PSS": pod - pofd if not (np.isnan(pod) or np.isnan(pofd)) else np.nan,
    }


def p_value_vs_random(hits, n_targets, tau):
    """隨機警報（每個目標以機率 τ 被涵蓋）命中數 >= hits 的機率。"""
    if n_targets == 0:
        return np.nan
    return float(stats.binom.sf(hits - 1, n_targets, tau))


def at_k(ranks, y, k, n_cells):
    alarm = ranks <= k
    s = skill(*contingency(alarm, y))
    s["k"] = int(k)
    s["p_vs_random"] = p_value_vs_random(s["hits"], s["hits"] + s["misses"], k / n_cells)
    return s


def molchan(ranks, y, n_cells):
    """回傳 (tau, pod)，k = 0..n_cells。"""
    r = np.asarray(ranks)[np.asarray(y, bool)]
    k = np.arange(n_cells + 1)
    if r.size == 0:
        return k / n_cells, np.full(k.size, np.nan)
    hist = np.bincount(r, minlength=n_cells + 1)
    pod = np.cumsum(hist)[: n_cells + 1] / r.size
    return k / n_cells, pod


def area_skill_score(ranks, y, n_cells):
    tau, pod = molchan(ranks, y, n_cells)
    return float(np.trapezoid(pod, tau))


def best_k_by_csi(ranks, y, n_cells, max_tau=0.5):
    """在 τ <= max_tau 範圍內找 CSI 最大的 k（用在驗證集，再套到測試集）。"""
    best_k, best_csi = 1, -1.0
    for k in range(1, int(max_tau * n_cells) + 1):
        csi = skill(*contingency(ranks <= k, y))["CSI"]
        if csi > best_csi:
            best_k, best_csi = k, csi
    return best_k, best_csi


def mcnemar_exact(hit_a, hit_b):
    """兩個模型在同一批目標上的配對比較（相同 τ 時警報總數相同，多一個命中就少一個誤報，所以只需比較命中）。

    n_ab：A 命中、B 沒命中；n_ba：B 命中、A 沒命中。雙尾精確二項檢定。
    """
    hit_a = np.asarray(hit_a, bool)
    hit_b = np.asarray(hit_b, bool)
    n_ab = int((hit_a & ~hit_b).sum())
    n_ba = int((~hit_a & hit_b).sum())
    n = n_ab + n_ba
    p = float(stats.binomtest(n_ab, n, 0.5).pvalue) if n else 1.0
    return {"only_a": n_ab, "only_b": n_ba, "p_two_sided": p}


def bootstrap_delta_ass(ranks_a, ranks_b, y, n_cells, n_boot=2000, seed=0):
    """以目標事件為單位重抽樣，估計 ASS(B) - ASS(A) 的 95% 信賴區間。

    ASS 只取決於目標樣本的名次分布，所以重抽目標即可。
    """
    y = np.asarray(y, bool)
    ra = np.asarray(ranks_a)[y]
    rb = np.asarray(ranks_b)[y]
    if ra.size == 0:
        return {"delta": np.nan, "ci_low": np.nan, "ci_high": np.nan}
    # 名次 r 對 ASS 的貢獻 = 1 - (r - 0.5) / n_cells（梯形面積的離散版本）
    ca = 1 - (ra - 0.5) / n_cells
    cb = 1 - (rb - 0.5) / n_cells
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, ra.size, size=(n_boot, ra.size))
    deltas = (cb[idx] - ca[idx]).mean(axis=1)
    return {
        "delta": float((cb - ca).mean()),
        "ci_low": float(np.quantile(deltas, 0.025)),
        "ci_high": float(np.quantile(deltas, 0.975)),
    }
