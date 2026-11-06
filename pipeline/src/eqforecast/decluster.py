"""視窗法去群聚（window-based declustering）。

為什麼要去群聚：
    大地震之後的餘震在時間與空間上高度集中。若把餘震也當成預報目標，
    任何「最近有地震的地方就發警報」的模型都會得到很高的命中率，
    但這只是在預報餘震，不是預報新的主震。前兆研究關心的是主震，
    所以目標事件先去群聚。

方法：
    依規模由大到小處理。尚未歸類的事件成為主震，並把它時空視窗內、
    尚未歸類的較小事件歸為同一群（餘震；視窗前段為前震）。
    只有主震會定義視窗（non-linked），所以 ML>=5.5 目標事件的判定只取決於更大的事件。

視窗公式：
    Gardner & Knopoff (1974), BSSA 64(5)，常用的擬合式（van Stiphout et al., 2012, CORSSA）：
        L = 10^(0.1238 M + 0.983) km
        T = 10^(0.032 M + 2.7389) 天（M >= 6.5），否則 10^(0.5409 M - 0.547) 天
    Uhrhammer (1986)：
        L = exp(-1.024 + 0.804 M) km，T = exp(-2.87 + 1.235 M) 天

限制：
    公式原本是用加州資料與矩規模推導的，這裡直接套用 ML，屬於近似。
    前震視窗沒有標準值：設成 1.0 時，M7.2 會把前 2.5 年內 75 km 的事件都算成前震，
    所以預設用 0.1，並在報告中做敏感度比較。
"""
import numpy as np
import pandas as pd

EARTH_RADIUS_KM = 6371.0


def windows(mag, method):
    mag = np.asarray(mag, float)
    if method == "gardner_knopoff":
        dist_km = 10 ** (0.1238 * mag + 0.983)
        time_d = np.where(mag >= 6.5, 10 ** (0.032 * mag + 2.7389), 10 ** (0.5409 * mag - 0.547))
    elif method == "uhrhammer":
        dist_km = np.exp(-1.024 + 0.804 * mag)
        time_d = np.exp(-2.87 + 1.235 * mag)
    else:
        raise ValueError(method)
    return dist_km, time_d


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(np.asarray(lon2) - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def decluster(events, method="gardner_knopoff", fs_time_prop=0.1):
    """回傳與 events 同索引的 DataFrame：cluster_id（主震的列位置）、is_mainshock。

    events 需有 time（datetime64）、lat、lon、ml。
    """
    n = len(events)
    if method == "none" or n == 0:
        return pd.DataFrame({"cluster_id": np.arange(n), "is_mainshock": np.ones(n, bool)},
                            index=events.index)
    t = (events["time"].to_numpy("datetime64[ns]") - np.datetime64("1970-01-01")) / np.timedelta64(1, "D")
    lat = events["lat"].to_numpy(float)
    lon = events["lon"].to_numpy(float)
    mag = events["ml"].to_numpy(float)

    time_order = np.argsort(t, kind="stable")
    t_sorted = t[time_order]
    # 規模由大到小；同規模時較早的事件優先成為主震
    proc_order = np.lexsort((t, -mag))
    dist_w, time_w = windows(mag, method)

    cluster = np.full(n, -1)
    for idx in proc_order:
        if cluster[idx] != -1:
            continue
        cluster[idx] = idx
        lo = np.searchsorted(t_sorted, t[idx] - fs_time_prop * time_w[idx], side="left")
        hi = np.searchsorted(t_sorted, t[idx] + time_w[idx], side="right")
        cand = time_order[lo:hi]
        cand = cand[cluster[cand] == -1]
        if cand.size == 0:
            continue
        d = haversine_km(lat[idx], lon[idx], lat[cand], lon[cand])
        cluster[cand[d <= dist_w[idx]]] = idx
    return pd.DataFrame({"cluster_id": cluster, "is_mainshock": cluster == np.arange(n)},
                        index=events.index)


def interevent_stats(times):
    """事件間隔的群聚程度：Weibull 形狀參數 k 與變異係數 CV。

    k ≈ 1、CV ≈ 1：接近 Poisson（隨機）；k < 1、CV > 1：群聚。
    """
    from scipy import stats

    t = np.sort(pd.to_datetime(pd.Series(times)).to_numpy("datetime64[ns]"))
    dt_days = np.diff(t) / np.timedelta64(1, "D")
    dt_days = dt_days[dt_days > 0]
    if dt_days.size < 10:
        return {"n_intervals": int(dt_days.size), "weibull_k": np.nan, "cv": np.nan}
    k, _, _ = stats.weibull_min.fit(dt_days, floc=0)
    return {
        "n_intervals": int(dt_days.size),
        "weibull_k": float(k),
        "cv": float(dt_days.std(ddof=1) / dt_days.mean()),
    }
