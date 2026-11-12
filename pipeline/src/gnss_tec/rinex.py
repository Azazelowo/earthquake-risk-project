"""RINEX 2.11 讀取器（觀測檔 .YYo 與 GPS 廣播星曆 .YYn）。

格式依 RINEX 2.11 規格（IGS/RTCM, 2012 修訂版）：
    觀測檔：每筆觀測 16 字元 = F14.3 數值 + 1 位 LLI（周跳旗標）+ 1 位訊號強度；每列最多 5 筆。
    epoch 列：yy mm dd hh mm ss.sssssss  flag  衛星數  衛星清單（每列最多 12 顆，超過時續行）。
    星曆檔：每顆衛星 8 列，數值以 D19.12 表示（指數用 D）。
"""
import gzip
from pathlib import Path

import numpy as np
import pandas as pd


def _open(path):
    path = Path(path)
    return gzip.open(path, "rt", encoding="ascii", errors="replace") if path.suffix == ".gz" \
        else open(path, "r", encoding="ascii", errors="replace")


def read_obs_header(f):
    header = {"obs_types": []}
    for line in f:
        label = line[60:].strip()
        if label == "# / TYPES OF OBSERV":
            if not header["obs_types"]:
                header["n_obs"] = int(line[:6])
            header["obs_types"] += line[6:60].split()
        elif label == "APPROX POSITION XYZ":
            header["approx_xyz"] = tuple(float(x) for x in line[:60].split()[:3])
        elif label == "MARKER NAME":
            header["marker"] = line[:60].strip()
        elif label == "INTERVAL":
            header["interval"] = float(line[:10])
        elif label == "REC # / TYPE / VERS":
            header["receiver"] = line[20:40].strip()
            header["receiver_sn"] = line[0:20].strip()
            header["receiver_fw"] = line[40:60].strip()
        elif label == "ANT # / TYPE":
            header["antenna_sn"] = line[0:20].strip()
            header["antenna"] = line[20:40].strip()
        elif label == "TIME OF FIRST OBS":
            header["time_system"] = line[48:51].strip() or "GPS"
        elif label == "END OF HEADER":
            break
    return header


def read_obs(path, systems=("G",)):
    """回傳 (header, DataFrame)。DataFrame 欄位：time（GPS 時）、sv、各觀測量、各觀測量的 LLI。"""
    rows = []
    with _open(path) as f:
        h = read_obs_header(f)
        types = h["obs_types"]
        n_obs = len(types)
        n_lines = (n_obs + 4) // 5
        line = f.readline()
        while line:
            if len(line.rstrip("\n")) < 29 or not line[28].isdigit():
                line = f.readline()
                continue
            flag = int(line[28])
            nsat = int(line[29:32])
            if flag > 1:
                # 2–5：特殊事件，後面接 nsat 列表頭資料；6：周跳紀錄，後面接 nsat 顆衛星的觀測
                skip = nsat if flag < 6 else nsat * n_lines
                for _ in range(skip):
                    f.readline()
                line = f.readline()
                continue
            yy = int(line[1:3])
            year = yy + (2000 if yy < 80 else 1900)
            t = pd.Timestamp(year=year, month=int(line[4:6]), day=int(line[7:9]),
                             hour=int(line[10:12]), minute=int(line[13:15])) \
                + pd.Timedelta(seconds=float(line[15:26]))
            sats = []
            sat_line = line
            while len(sats) < nsat:
                chunk = sat_line[32:68]
                for k in range(0, len(chunk.rstrip("\n")), 3):
                    s = chunk[k:k + 3]
                    if s.strip():
                        sats.append(s[0].replace(" ", "G") + s[1:].replace(" ", "0"))
                if len(sats) < nsat:
                    sat_line = f.readline()
            for sv in sats[:nsat]:
                text = ""
                for _ in range(n_lines):
                    text += f.readline().rstrip("\n").ljust(80)
                if sv[0] not in systems:
                    continue
                rec = {"time": t, "sv": sv}
                for i, name in enumerate(types):
                    field = text[16 * i:16 * i + 16]
                    v = field[:14].strip()
                    rec[name] = float(v) if v else np.nan
                    lli = field[14:15].strip()
                    rec[f"{name}_lli"] = int(lli) if lli.isdigit() else 0
                rows.append(rec)
            line = f.readline()
    df = pd.DataFrame(rows)
    return h, df


NAV_FIELDS = [
    ["iode", "crs", "delta_n", "m0"],
    ["cuc", "e", "cus", "sqrt_a"],
    ["toe", "cic", "omega0", "cis"],
    ["i0", "crc", "omega", "omega_dot"],
    ["idot", "l2_codes", "gps_week", "l2p_flag"],
    ["sv_accuracy", "sv_health", "tgd", "iodc"],
    ["transmit_time", "fit_interval"],
]


def _d(s):
    s = s.strip().replace("D", "E").replace("d", "e")
    return float(s) if s else np.nan


def nav_version(path):
    with _open(path) as f:
        first = f.readline()
    try:
        return float(first[:9])
    except ValueError:
        return 2.0


def read_nav(path):
    """依版本讀 GPS 廣播星曆：RINEX 2（brdcDDD0.YYn）或 RINEX 3（BRDC00WRD_R_..._MN.rnx 混合檔）。"""
    return read_gps_nav3(path) if nav_version(path) >= 3 else read_gps_nav(path)


def read_gps_nav3(path):
    """讀 RINEX 3 混合星曆中的 GPS 紀錄。

    RINEX 3：第一列 'Gnn yyyy mm dd hh mm ss' + 3 個 D19.12；續行開頭 4 個空白、每列 4 個 19 字元欄位。
    各系統的續行數不同（GLONASS／SBAS 較少，3.05 版又多一列），所以用「開頭是否為字母」切分紀錄，
    只保留 GPS（G）。
    """
    recs = []
    with _open(path) as f:
        for line in f:
            if "END OF HEADER" in line:
                break
        block = []
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            if line[0] != " ":
                if block:
                    recs.append(block)
                block = [line.ljust(80)]
            elif block:
                block.append(line.ljust(80))
        if block:
            recs.append(block)
    out = []
    for b in recs:
        l0 = b[0]
        if l0[0] != "G" or len(b) < 8:
            continue
        rec = {
            "sv": f"G{int(l0[1:3]):02d}",
            "toc": pd.Timestamp(year=int(l0[4:8]), month=int(l0[9:11]), day=int(l0[12:14]),
                                hour=int(l0[15:17]), minute=int(l0[18:20]), second=int(l0[21:23])),
            "af0": _d(l0[23:42]), "af1": _d(l0[42:61]), "af2": _d(l0[61:80]),
        }
        for j, names in enumerate(NAV_FIELDS, start=1):
            l = b[j]
            for i, name in enumerate(names):
                rec[name] = _d(l[4 + 19 * i: 4 + 19 * (i + 1)])
        out.append(rec)
    return pd.DataFrame(out)


def read_gps_nav(path):
    """讀 RINEX 2 GPS 廣播星曆，回傳每筆星曆一列的 DataFrame。"""
    recs = []
    with _open(path) as f:
        for line in f:
            if "END OF HEADER" in line:
                break
        lines = [l.rstrip("\n").ljust(80) for l in f if l.strip()]
    for k in range(0, len(lines) - 7, 8):
        l0 = lines[k]
        yy = int(l0[3:5])
        rec = {
            "sv": f"G{int(l0[0:2]):02d}",
            "toc": pd.Timestamp(year=yy + (2000 if yy < 80 else 1900), month=int(l0[6:8]), day=int(l0[9:11]),
                                hour=int(l0[12:14]), minute=int(l0[15:17]))
            + pd.Timedelta(seconds=float(l0[17:22])),
            "af0": _d(l0[22:41]), "af1": _d(l0[41:60]), "af2": _d(l0[60:79]),
        }
        for j, names in enumerate(NAV_FIELDS, start=1):
            l = lines[k + j]
            for i, name in enumerate(names):
                rec[name] = _d(l[3 + 19 * i: 3 + 19 * (i + 1)])
        recs.append(rec)
    return pd.DataFrame(recs)
