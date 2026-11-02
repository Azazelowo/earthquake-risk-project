"""發布時間點與目標標籤。

時間約定（全部為 UTC）：
    發布時間 t0 = issue_start + k × H
    預報窗   [t0, t0 + H)            目標：這段時間內是否發生目標事件
    特徵     只用 time < t0 的資料     所以特徵與標籤在時間上不重疊

一個「樣本」= 一個網格 × 一個預報窗。每個預報窗都包含研究區內全部網格，
所以每個發布時間點的樣本數相同，警報比例 τ 才有一致的意義。
"""
import numpy as np
import pandas as pd


def issue_times(start, end, horizon_days):
    """回傳所有預報窗完整落在 [start, end) 內的發布時間。"""
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    h = pd.Timedelta(days=horizon_days)
    n = int((end - start) // h)
    return pd.DatetimeIndex([start + k * h for k in range(n)])


def window_index(times, t0s, horizon_days):
    """事件所在的預報窗編號；不在任何窗內為 -1。"""
    t = pd.to_datetime(pd.Series(times)).to_numpy("datetime64[ns]")
    start = t0s[0].to_datetime64()
    h = np.timedelta64(horizon_days, "D")
    w = ((t - start) // h).astype(np.int64)
    w[(t < start) | (w >= len(t0s))] = -1
    return w


def build_targets(events, cell_ids, t0s, grid, horizon_days, target_mask):
    """建立樣本表：每列 = (t0, cell_id)，y = 該窗該格是否有目標事件。

    events:      事件表（需有 time、ml）
    cell_ids:    每個事件的網格編號（研究區外為 -1）
    target_mask: 哪些事件算目標（例如 ML>=5.5 且為主震）
    """
    w = window_index(events["time"], t0s, horizon_days)
    sel = target_mask & (w >= 0) & (cell_ids >= 0)
    n_w, n_c = len(t0s), grid.n_cells
    count = np.zeros((n_w, n_c), np.int32)
    max_ml = np.full((n_w, n_c), np.nan)
    np.add.at(count, (w[sel], cell_ids[sel]), 1)
    ml = events["ml"].to_numpy(float)[sel]
    for ww, cc, m in zip(w[sel], cell_ids[sel], ml):
        if np.isnan(max_ml[ww, cc]) or m > max_ml[ww, cc]:
            max_ml[ww, cc] = m
    return pd.DataFrame({
        "t0": np.repeat(t0s.to_numpy(), n_c),
        "cell_id": np.tile(np.arange(n_c), n_w),
        "n_events": count.ravel(),
        "y": count.ravel() > 0,
        "max_ml": max_ml.ravel(),
    })
