"""時間切分。

為什麼不能隨機切分：
    地震在時間上群聚，隨機切分會讓同一個地震序列同時出現在訓練與測試集，
    模型等於「看過答案」。所以一律依時間先後切成訓練、驗證、測試。

Purge：預報窗 [t0, t0+H) 只要跨過切分點，該樣本就丟掉，
       確保訓練標籤的時間全部早於驗證／測試期的開始。
Embargo：每個切分（第一個除外）開頭再多空出 embargo_days 天，作為額外緩衝。
"""
import numpy as np
import pandas as pd

PURGED = "purged"


def assign_splits(t0s, horizon_days, splits, embargo_days=0, data_end=None):
    """回傳與 t0s 等長的切分名稱陣列（purge 掉的為 'purged'）。"""
    h = pd.Timedelta(days=horizon_days)
    emb = pd.Timedelta(days=embargo_days)
    names = np.full(len(t0s), PURGED, dtype=object)
    ordered = sorted(splits.items(), key=lambda kv: pd.Timestamp(kv[1][0]))
    for k, (name, (s, e)) in enumerate(ordered):
        s = pd.Timestamp(s) + (emb if k > 0 else pd.Timedelta(0))
        e = pd.Timestamp(e) if e is not None else pd.Timestamp(data_end)
        inside = (t0s >= s) & (t0s + h <= e)
        names[np.asarray(inside)] = name
    return names


def check_no_leakage(t0s, split_names, horizon_days, order=("train", "val", "test")):
    """確認每個切分的最後一個標籤時間，都早於下一個切分的第一個發布時間。"""
    h = pd.Timedelta(days=horizon_days)
    t0s = pd.DatetimeIndex(t0s)
    report = {}
    for a, b in zip(order[:-1], order[1:]):
        ta, tb = t0s[split_names == a], t0s[split_names == b]
        if len(ta) == 0 or len(tb) == 0:
            raise AssertionError(f"切分 {a} 或 {b} 沒有樣本")
        last_label_end = ta.max() + h
        if last_label_end > tb.min():
            raise AssertionError(f"{a} 的標籤延伸到 {last_label_end}，晚於 {b} 的第一個發布時間 {tb.min()}")
        report[f"{a}->{b}"] = {"last_label_end": str(last_label_end), "next_first_t0": str(tb.min())}
    return report
