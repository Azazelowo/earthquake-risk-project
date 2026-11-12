"""特徵。

1. 地震目錄特徵（catalog features）—— B3 基準模型只用這一類
   全部只用發布時間 t0 之前的事件（time < t0），而且只用 ML >= 門檻。門檻預設全期間 3.0；
   B3 設定檔從 2026-11-10 起用 feature_min_ml_by_year（04_estimate_mc.py 的分段 Mc，FR-B2-20），
   每個事件用它那一年的門檻（不低於 3.0），Mc 較高的年份（1990–91、2009–12）門檻會往上調。

   為什麼用「占全區比例」而不是絕對次數：
       通過 Gap<=110 的 ML>=3 事件數，從 1990 年代每年約 300 筆增加到 2020 年代每年 1,000 筆以上，
       主要是測站變密（Gap 變小），不是地殼變活躍。用絕對次數訓練，
       模型在測試期會看到從沒見過的數值範圍。改成同一時間點「這格占全區的比例」後，
       偵測能力的整體變化會被抵銷。

2. 外部特徵（external features）—— 之後 B2 的前兆資料從這裡接上
   輸入是「網格 × 日」的表，每個發布時間點只彙整 [t0 - lookback, t0) 的資料。
   沒有資料的網格保持 NaN（未知），交給模型的缺值處理，不補 0。
"""
import numpy as np
import pandas as pd

FEATURE_PREFIX = "f_"
NO_EVENT_DAYS = 3650.0   # 鄰域內從未發生過 ML>=5 時使用的「距今天數」


def _day_index(times, day0):
    t = pd.to_datetime(pd.Series(times)).to_numpy("datetime64[ns]")
    return ((t - day0) // np.timedelta64(1, "D")).astype(np.int64)


def _cumulative_counts(day_idx, cell_ids, n_cells, n_days):
    """回傳 (n_cells, n_days + 1) 陣列：cs[c, d] = 第 d 天之前（不含）該格的事件數。"""
    counts = np.zeros((n_cells, n_days), np.int32)
    np.add.at(counts, (cell_ids, day_idx), 1)
    cs = np.zeros((n_cells, n_days + 1), np.int32)
    np.cumsum(counts, axis=1, out=cs[:, 1:])
    return cs


def _window_count(cs, d0, lookback):
    """[d0 - lookback, d0) 的事件數；cs 形狀 (n_cells, n_days+1)，d0 形狀 (n_windows,)。"""
    lo = np.maximum(d0 - lookback, 0)
    return (cs[:, d0] - cs[:, lo]).T   # (n_windows, n_cells)


def _log_share(count, n_cells):
    """同一發布時間點內，該格占全區的比例（取 log10，加小常數避免 log 0）。"""
    total = count.sum(axis=1, keepdims=True)
    return np.log10((count + 0.1) / (total + 0.1 * n_cells))


def event_min_ml(times, floor, table_path=None, base_dir=None):
    """每個事件適用的特徵門檻。table_path 為 None 時全部是 floor。

    表格（04_estimate_mc.py 產生）每年一個門檻；表格之前的年份用第一年、之後的年份用最後一年。
    門檻不會低於 floor。
    """
    years = pd.to_datetime(pd.Series(times)).dt.year.to_numpy()
    if not table_path:
        return np.full(len(years), float(floor))
    from pathlib import Path
    p = Path(table_path)
    if not p.is_absolute() and base_dir is not None:
        p = Path(base_dir) / p
    tab = pd.read_csv(p).sort_values("year")
    y = tab["year"].to_numpy(int)
    thr = tab["threshold"].to_numpy(float)
    idx = np.clip(np.searchsorted(y, years, side="right") - 1, 0, len(y) - 1)
    return np.maximum(thr[idx], float(floor))


def catalog_features(events, cell_ids, t0s, grid, min_ml=3.0):
    """回傳樣本表 (t0, cell_id, f_*)，列順序與 targets.build_targets 相同。"""
    t0s = pd.DatetimeIndex(t0s)
    day0 = np.datetime64(pd.Timestamp(events["time"].min()).floor("D"))
    d0 = ((t0s.to_numpy("datetime64[ns]") - day0) // np.timedelta64(1, "D")).astype(np.int64)
    n_days = int(d0.max()) + 1
    n_cells = grid.n_cells

    ml = events["ml"].to_numpy(float)
    day = _day_index(events["time"], day0)
    ok = (cell_ids >= 0) & (day >= 0) & (day < n_days)

    feats = {}
    cs = {}
    for m in (3, 4, 5):
        sel = ok & (ml >= max(m, min_ml))
        cs[m] = _cumulative_counts(day[sel], cell_ids[sel], n_cells, n_days)
    ncs = {m: grid.neighborhood_sum(cs[m]) for m in (3, 4, 5)}

    feats["f_n3_30d_cell"] = _log_share(_window_count(cs[3], d0, 30), n_cells)
    feats["f_n3_365d_cell"] = _log_share(_window_count(cs[3], d0, 365), n_cells)
    feats["f_n3_30d_nbr"] = _log_share(_window_count(ncs[3], d0, 30), n_cells)
    feats["f_n3_365d_nbr"] = _log_share(_window_count(ncs[3], d0, 365), n_cells)
    feats["f_n4_365d_nbr"] = _log_share(_window_count(ncs[4], d0, 365), n_cells)
    # 長期（從目錄起點累積到 t0）ML>=4 比例：相對強度（Relative Intensity）基準使用此欄
    feats["f_n4_all_nbr"] = _log_share(ncs[4][:, d0].T, n_cells)

    # 鄰域內距離上一次 ML>=5 的天數
    days_since = np.full((len(d0), n_cells), NO_EVENT_DAYS)
    for c in range(n_cells):
        row = ncs[5][c]
        k = row[d0]
        has = k > 0
        if has.any():
            last_day = np.searchsorted(row, k[has], side="left") - 1
            days_since[has, c] = np.minimum(d0[has] - last_day, NO_EVENT_DAYS)
    feats["f_log_days_since_m5_nbr"] = np.log10(days_since)

    # 鄰域內過去 365 天的最大規模（沒有事件時為 min_ml - 0.5，代表「低於門檻」）
    max_ml = np.full((len(d0), n_cells), min_ml - 0.5)
    sel = ok & (ml >= min_ml)
    order = np.argsort(day[sel], kind="stable")
    d_sel, c_sel, m_sel = day[sel][order], cell_ids[sel][order], ml[sel][order]
    for w, dd in enumerate(d0):
        lo, hi = np.searchsorted(d_sel, [dd - 365, dd], side="left")
        if hi > lo:
            cell_max = np.full(n_cells, -np.inf)
            np.maximum.at(cell_max, c_sel[lo:hi], m_sel[lo:hi])
            max_ml[w] = np.maximum(max_ml[w], grid.neighborhood_max(cell_max))
    feats["f_maxml_365d_nbr"] = max_ml

    out = pd.DataFrame({
        "t0": np.repeat(t0s.to_numpy(), n_cells),
        "cell_id": np.tile(np.arange(n_cells), len(t0s)),
    })
    for k, v in feats.items():
        out[k] = v.ravel()
    return out


def external_features(spec, t0s, grid, base_dir=None):
    """把 B2 的「網格 × 日」前兆表彙整成每個 (t0, cell_id) 的特徵。

    回傳欄位：f_<name>_<col>_<agg>，以及 f_<name>_coverage（回看期間有資料的天數比例）。
    沒有資料的 (t0, cell) 保持 NaN。
    """
    from pathlib import Path

    path = Path(spec.path)
    if not path.is_absolute() and base_dir is not None:
        path = Path(base_dir) / path
    raw = pd.read_parquet(path)
    if raw.get("is_demo", pd.Series(False)).any():
        raise ValueError(f"{path} 含有 is_demo=True 的示範資料，不能拿來評估")
    lat_c = raw[spec.lat_col].to_numpy(float) + spec.native_cell_deg / 2
    lon_c = raw[spec.lon_col].to_numpy(float) + spec.native_cell_deg / 2
    df = pd.DataFrame({
        "date": pd.to_datetime(raw[spec.time_col]).dt.floor("D"),
        "cell_id": grid.cell_of(lat_c, lon_c),
    })
    keys = {spec.lat_col, spec.lon_col, spec.time_col}
    cols = spec.value_cols or [c for c in raw.columns
                               if c not in keys and pd.api.types.is_numeric_dtype(raw[c])
                               and raw[c].dtype != bool]
    for c in cols:
        df[c] = raw[c].to_numpy(float)
    df = df[df["cell_id"] >= 0]
    # 原生網格比 B3 網格細時，同一天同一格取平均
    daily = df.groupby(["date", "cell_id"], as_index=False)[cols].mean()

    t0s = pd.DatetimeIndex(t0s)
    L = pd.Timedelta(days=spec.lookback_days)
    parts = []
    for t0 in t0s:
        win = daily[(daily["date"] >= t0 - L) & (daily["date"] < t0)]
        if win.empty:
            continue
        g = win.groupby("cell_id")
        agg = g[cols].agg(spec.aggs)
        agg.columns = [f"f_{spec.name}_{c}_{a}" for c, a in agg.columns]
        agg[f"f_{spec.name}_coverage"] = g.size() / spec.lookback_days
        agg["t0"] = t0
        parts.append(agg.reset_index())
    skeleton = pd.DataFrame({
        "t0": np.repeat(t0s.to_numpy(), grid.n_cells),
        "cell_id": np.tile(np.arange(grid.n_cells), len(t0s)),
    })
    cov = f"f_{spec.name}_coverage"
    if not parts:
        # 前兆資料和所有發布時間都不重疊：欄位照樣給（全缺值、覆蓋率 0），下游才不會因少欄位而失敗
        for c in cols:
            for a in spec.aggs:
                skeleton[f"f_{spec.name}_{c}_{a}"] = np.nan
        skeleton[cov] = 0.0
        return skeleton
    feats = pd.concat(parts, ignore_index=True)
    out = skeleton.merge(feats, on=["t0", "cell_id"], how="left")
    out[cov] = out[cov].fillna(0.0)   # 覆蓋率 0 是已知事實（沒有資料），不是缺值
    return out
