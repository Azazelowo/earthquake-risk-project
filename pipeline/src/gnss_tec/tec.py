"""由雙頻 GPS 觀測量計算 TEC。

步驟：
1. 斜向 TEC（STEC）
      碼：   STEC_P = K × (P2 − P1)                雜訊大（約數 TECU）、但沒有整周未定值
      相位： STEC_L = K × (L1·λ1 − L2·λ2)          雜訊小（約 0.01 TECU）、但有未知常數
      K = f1² f2² / (40.3 (f1² − f2²)) / 1e16 ≈ 9.52 TECU/m
2. 弧段（arc）：同一顆衛星連續追蹤、沒有周跳的一段。資料中斷 > max_gap_s、LLI 周跳旗標、
   或相位 TEC 單一 epoch 跳動過大時切開。
3. 相位平滑（leveling）：每個弧段用「碼 − 相位」的加權平均把相位 TEC 拉到碼的絕對水準。
4. 衛星 DCB 修正：用 CAS 的 C1W−C2W 衛星偏差（ns）。符號由資料決定（見 choose_dcb_sign）。
5. 接收機偏差：NDH5 不是 IGS 站，沒有公開的接收機 DCB，用最小標準差法估計
   （Ma & Maruyama, 2003, Annales Geophysicae 21:2083）：同一時刻、同一測站上空，
   不同衛星換算成的垂直 TEC 應該接近，選讓各衛星 VTEC 離散度最小的偏差值。
   只用當地夜間（電離層水平梯度較小）估計。
6. 垂直 TEC：VTEC = STEC / M(E)，單層模型高度 350 km，只用仰角 ≥ 30° 的衛星。
"""
import numpy as np
import pandas as pd

from . import orbit

C = 299792458.0
F1, F2 = 1575.42e6, 1227.60e6
LAM1, LAM2 = C / F1, C / F2
K_TECU_PER_M = F1 ** 2 * F2 ** 2 / (40.3 * (F1 ** 2 - F2 ** 2)) / 1e16
NS_TO_TECU = C * 1e-9 * K_TECU_PER_M     # 1 ns 的碼偏差 ≈ 2.85 TECU


def slant_tec(obs, nav, rx_xyz, el_mask_deg=10.0, shell_km=350.0, max_gap_s=90.0,
              slip_tecu=1.5, min_arc_epochs=20):
    """回傳每顆衛星每個 epoch 的幾何與 STEC（尚未修正 DCB）。"""
    rx_lat, rx_lon, _ = orbit.ecef_to_geodetic(*rx_xyz)
    out = []
    for sv, g in obs.groupby("sv"):
        g = g.dropna(subset=["P1", "P2", "L1", "L2"]).sort_values("time")
        if g.empty:
            continue
        idx = orbit.select_ephemeris(nav, sv, g["time"])
        ok = idx >= 0
        if not ok.any():
            continue
        g = g[ok]
        eph = nav.loc[idx[ok]].reset_index(drop=True)
        xyz = orbit.sat_position(eph, g["time"])
        el, az = orbit.elevation_azimuth(rx_xyz, xyz)
        keep = np.degrees(el) >= el_mask_deg
        if not keep.any():
            continue
        g = g[keep].copy()
        el, az = el[keep], az[keep]
        g["el"] = np.degrees(el)
        g["az"] = np.degrees(az)
        g["ipp_lat"], g["ipp_lon"] = orbit.ipp(rx_lat, rx_lon, el, az, shell_km)
        g["mf"] = orbit.mapping_function(el, shell_km)
        g["stec_p"] = K_TECU_PER_M * (g["P2"] - g["P1"])
        g["stec_l"] = K_TECU_PER_M * (g["L1"] * LAM1 - g["L2"] * LAM2)

        # 弧段切割
        t = g["time"].to_numpy("datetime64[ns]")
        dt = np.r_[np.inf, np.diff(t) / np.timedelta64(1, "s")]
        dl = np.r_[np.inf, np.abs(np.diff(g["stec_l"].to_numpy()))]
        lli = ((g["L1_lli"].to_numpy() & 1) | (g["L2_lli"].to_numpy() & 1)) > 0
        new_arc = (dt > max_gap_s) | (dl > slip_tecu) | lli
        g["arc"] = np.cumsum(new_arc)
        # 相位平滑：以 sin²(E) 加權，降低低仰角多路徑效應的影響
        w = np.sin(el) ** 2
        g["w"] = w
        lev = (g.assign(d=(g["stec_p"] - g["stec_l"]) * w)
               .groupby("arc").agg(d=("d", "sum"), w=("w", "sum"), n=("w", "size")))
        lev["offset"] = lev["d"] / lev["w"]
        g = g.join(lev[["offset", "n"]], on="arc")
        g = g[g["n"] >= min_arc_epochs]
        g["stec"] = g["stec_l"] + g["offset"]
        g["sv"] = sv
        out.append(g[["time", "sv", "arc", "el", "az", "ipp_lat", "ipp_lon", "mf", "stec_p", "stec"]])
    return pd.concat(out, ignore_index=True)


def apply_sat_dcb(st, dsb_ns, sign):
    """STEC_corr = STEC + sign × DSB(ns) × 2.85。dsb_ns：{PRN: ns}，缺少的衛星會被移除。"""
    d = st["sv"].map(dsb_ns)
    out = st[d.notna()].copy()
    out["stec_c"] = out["stec"] + sign * d[d.notna()] * NS_TO_TECU
    return out


def _residualize(s, cols, design):
    """每個 epoch 內，把 cols 對 design（含常數）做最小平方，回傳殘差。"""
    out = {c: np.empty(len(s)) for c in cols}
    pos = 0
    for _, g in s.groupby("time", sort=False):
        X = np.column_stack([np.ones(len(g))] + [g[d].to_numpy() for d in design])
        for c in cols:
            y = g[c].to_numpy()
            beta, *_ = np.linalg.lstsq(X, y, rcond=None)
            out[c][pos:pos + len(g)] = y - X @ beta
        pos += len(g)
    return out


def estimate_receiver_bias(st, el_min=30.0, hours_utc=None, gradient=True):
    """最小標準差法。回傳 (偏差 TECU, 殘差標準差 TECU, 使用的 epoch 數)。

    VTEC_i = a_i − B w_i，a_i = STEC_i / M_i，w_i = 1 / M_i。
    gradient=False：每個 epoch 假設 VTEC 均勻（原始方法），B = Σ ã w̃ / Σ w̃²，ã、w̃ 為去平均後的值。
    gradient=True： 每個 epoch 允許 VTEC 隨穿刺點緯度、經度線性變化（平面），
                    ã、w̃ 改為對 [1, Δlat, Δlon] 迴歸後的殘差（Frisch–Waugh），B 的公式不變。
                    台灣位在赤道異常北峰附近，水平梯度大，平面模型較合理。
    """
    s = st[st["el"] >= el_min].sort_values("time")
    if hours_utc is not None:
        s = s[s["time"].dt.hour.isin(list(hours_utc))]
    s = s.assign(a=s["stec_c"] / s["mf"], w=1.0 / s["mf"])
    min_sats = 5 if gradient else 4
    cnt = s.groupby("time")["a"].transform("size")
    s = s[cnt >= min_sats]
    if s.empty:
        return np.nan, np.nan, 0
    design = ["ipp_lat", "ipp_lon"] if gradient else []
    r = _residualize(s, ["a", "w"], design)
    am, wm = r["a"], r["w"]
    b = float((am * wm).sum() / (wm ** 2).sum())
    resid = am - b * wm
    return b, float(np.sqrt((resid ** 2).mean())), int(s["time"].nunique())


def vertical_tec(st, rx_bias, el_min=30.0):
    s = st[st["el"] >= el_min].copy()
    s["vtec"] = (s["stec_c"] - rx_bias) / s["mf"]
    return s


def station_series(v, freq="15min", min_sats=3):
    """每個時間格的測站上空 VTEC（各衛星 VTEC 的中位數）。"""
    per_epoch = v.groupby("time").agg(vtec=("vtec", "median"), n_sats=("vtec", "size"))
    per_epoch = per_epoch[per_epoch["n_sats"] >= min_sats]
    s = per_epoch.resample(freq).agg({"vtec": "median", "n_sats": "mean"})
    s["n_epochs"] = per_epoch["vtec"].resample(freq).count()
    return s
