"""IAGA-2002 讀取器（GDMS MAGNET 日檔，例如 xcg20261004dsec.sec）。

格式（IAGA-2002 規格）：
    表頭：每列以空白開頭、第 70 欄為 '|'，例如 ' Station Name   Xincheng   |'
    欄名列：'DATE       TIME         DOY     XCGX      XCGY      XCGZ      XCGF   |'
    資料列：日期、時間（UTC）、年積日、4 個分量（nT；D 分量為角分）
    缺值：99999 表示缺測，88888 表示「該分量沒有記錄」。
GDMS 的 F 欄整份都是 88888（2026-11 實測 30 + 7 個日檔），所以 F 一律由 X、Y、Z 算：F = √(X²+Y²+Z²)。
"""
import io
from pathlib import Path

import numpy as np
import pandas as pd

MISSING_MIN = 88887.0   # ≥ 此值視為缺值（88888、99999）


def _text(src):
    if isinstance(src, (bytes, bytearray)):
        raw = bytes(src)
    else:
        raw = Path(src).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        import gzip
        raw = gzip.decompress(raw)
    return raw.decode("ascii", errors="replace")


def read_iaga(src, name=None):
    """回傳 (header dict, DataFrame)。DataFrame：index 為 UTC 時間，欄位 X、Y、Z、F（nT，缺值為 NaN）。

    header 至少有 station（3 碼大寫）、lat、lon、elev、reported（例如 XYZF）、interval_s、
    f_from_xyz（F 是否由 XYZ 計算）。
    """
    text = _text(src)
    lines = text.splitlines()
    head, k = {}, 0
    for k, line in enumerate(lines):
        if line.startswith("DATE"):
            break
        if "|" in line:
            body = line.split("|")[0]
            key, val = body[:24].strip(), body[24:].strip()
            if key:
                head[key] = val
    else:
        raise ValueError("找不到 IAGA-2002 的欄名列（DATE TIME DOY ...）")
    cols = lines[k].replace("|", "").split()
    comp_cols = cols[3:7]
    df = pd.read_csv(io.StringIO("\n".join(lines[k + 1:])), sep=r"\s+", header=None,
                     names=["date", "time", "doy"] + comp_cols, usecols=range(7),
                     dtype={"date": str, "time": str})
    t = pd.to_datetime(df["date"] + " " + df["time"], format="%Y-%m-%d %H:%M:%S.%f", errors="coerce")
    vals = df[comp_cols].to_numpy(float)
    vals[vals >= MISSING_MIN] = np.nan
    station = (head.get("IAGA Code") or "").strip().upper()
    if not station:
        station = comp_cols[0][:-1].upper() if len(comp_cols[0]) > 1 else ""
    if not station and name:
        station = Path(name).name[:3].upper()
    reported = head.get("Reported", "".join(c[-1] for c in comp_cols)).upper()
    comps = [c[-1].upper() for c in comp_cols]
    data = dict(zip(comps, vals.T))
    if {"X", "Y", "Z"} <= data.keys():
        x, y, z = data["X"], data["Y"], data["Z"]
    elif {"H", "D", "Z"} <= data.keys():
        d_rad = np.radians(data["D"] / 60.0)          # IAGA-2002：D 以角分記錄
        x, y, z = data["H"] * np.cos(d_rad), data["H"] * np.sin(d_rad), data["Z"]
    else:
        raise ValueError(f"不支援的分量組合：{comps}")
    f_rec = data.get("F", np.full(len(x), np.nan))
    f_calc = np.sqrt(x ** 2 + y ** 2 + z ** 2)
    use_calc = np.isnan(f_rec).mean() > 0.5
    out = pd.DataFrame({"X": x, "Y": y, "Z": z, "F": f_calc if use_calc else f_rec}, index=t)
    out = out[out.index.notna()]
    out.index.name = "time"

    def num(key):
        try:
            return float(head.get(key, "nan"))
        except ValueError:
            return np.nan

    interval = head.get("Digital Sampling", "")
    header = {
        "station": station, "lat": num("Geodetic Latitude"), "lon": num("Geodetic Longitude"),
        "elev": num("Elevation"), "reported": reported, "source": head.get("Source of Data", ""),
        "data_type": head.get("Data Type", ""), "interval_s": 1.0 if "1 second" in interval else np.nan,
        "f_from_xyz": bool(use_calc),
    }
    return header, out
