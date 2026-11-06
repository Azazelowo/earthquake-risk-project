"""由 GPS 廣播星曆計算衛星位置，以及測站看衛星的仰角與電離層穿刺點（IPP）。

衛星位置演算法依 IS-GPS-200（Table 20-IV）。忽略訊號傳播時間（約 0.07 秒）造成的位置差，
對仰角的影響遠小於 0.01°，對 TEC 計算可忽略。
"""
import numpy as np
import pandas as pd

MU = 3.986005e14            # WGS-84 地球重力常數 (m^3/s^2)
OMEGA_E = 7.2921151467e-5   # 地球自轉角速度 (rad/s)
WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
R_EARTH = 6371.0e3
GPS_EPOCH = pd.Timestamp("1980-01-06")


def gps_seconds_of_week(t):
    dt = (pd.to_datetime(t) - GPS_EPOCH) / pd.Timedelta(seconds=1)
    return np.mod(np.asarray(dt, float), 604800.0)


def select_ephemeris(nav, sv, t):
    """對每個時間點選 toe 最接近、且相差不超過 2 小時的星曆。回傳星曆列索引（找不到為 -1）。"""
    eph = nav[(nav["sv"] == sv) & (nav["sv_health"] == 0)]
    if eph.empty:
        return np.full(len(t), -1)
    toc = eph["toc"].to_numpy("datetime64[ns]")
    tt = pd.to_datetime(t).to_numpy("datetime64[ns]")
    diff = np.abs((tt[:, None] - toc[None, :]) / np.timedelta64(1, "s"))
    best = diff.argmin(axis=1)
    idx = eph.index.to_numpy()[best]
    idx[diff[np.arange(len(tt)), best] > 7200] = -1
    return idx


def sat_position(eph, t):
    """eph：星曆 DataFrame（每列對應一個時間點），t：GPS 時間。回傳 ECEF (n, 3)，單位公尺。"""
    tk = gps_seconds_of_week(t) - eph["toe"].to_numpy(float)
    tk = np.where(tk > 302400, tk - 604800, np.where(tk < -302400, tk + 604800, tk))
    a = eph["sqrt_a"].to_numpy(float) ** 2
    n = np.sqrt(MU / a ** 3) + eph["delta_n"].to_numpy(float)
    m = eph["m0"].to_numpy(float) + n * tk
    e = eph["e"].to_numpy(float)
    E = m.copy()
    for _ in range(10):
        E = m + e * np.sin(E)
    v = np.arctan2(np.sqrt(1 - e ** 2) * np.sin(E), np.cos(E) - e)
    phi = v + eph["omega"].to_numpy(float)
    s2, c2 = np.sin(2 * phi), np.cos(2 * phi)
    u = phi + eph["cus"].to_numpy(float) * s2 + eph["cuc"].to_numpy(float) * c2
    r = a * (1 - e * np.cos(E)) + eph["crs"].to_numpy(float) * s2 + eph["crc"].to_numpy(float) * c2
    i = (eph["i0"].to_numpy(float) + eph["cis"].to_numpy(float) * s2 + eph["cic"].to_numpy(float) * c2
         + eph["idot"].to_numpy(float) * tk)
    xp, yp = r * np.cos(u), r * np.sin(u)
    omg = (eph["omega0"].to_numpy(float) + (eph["omega_dot"].to_numpy(float) - OMEGA_E) * tk
           - OMEGA_E * eph["toe"].to_numpy(float))
    x = xp * np.cos(omg) - yp * np.cos(i) * np.sin(omg)
    y = xp * np.sin(omg) + yp * np.cos(i) * np.cos(omg)
    z = yp * np.sin(i)
    return np.column_stack([x, y, z])


def ecef_to_geodetic(x, y, z):
    """WGS-84 ECEF → 緯度、經度（度）、橢球高（m）。"""
    e2 = WGS84_F * (2 - WGS84_F)
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - e2))
    for _ in range(10):
        n = WGS84_A / np.sqrt(1 - e2 * np.sin(lat) ** 2)
        h = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1 - e2 * n / (n + h)))
    return np.degrees(lat), np.degrees(lon), h


def elevation_azimuth(rx_xyz, sat_xyz):
    """測站 ECEF (3,) 與衛星 ECEF (n,3) → 仰角、方位角（弧度）。"""
    lat, lon, _ = ecef_to_geodetic(*rx_xyz)
    la, lo = np.radians(lat), np.radians(lon)
    d = sat_xyz - np.asarray(rx_xyz)[None, :]
    east = -np.sin(lo) * d[:, 0] + np.cos(lo) * d[:, 1]
    north = (-np.sin(la) * np.cos(lo) * d[:, 0] - np.sin(la) * np.sin(lo) * d[:, 1] + np.cos(la) * d[:, 2])
    up = (np.cos(la) * np.cos(lo) * d[:, 0] + np.cos(la) * np.sin(lo) * d[:, 1] + np.sin(la) * d[:, 2])
    el = np.arctan2(up, np.hypot(east, north))
    az = np.mod(np.arctan2(east, north), 2 * np.pi)
    return el, az


def ipp(rx_lat_deg, rx_lon_deg, el, az, shell_km=350.0):
    """單層電離層模型的穿刺點緯度、經度（度）。"""
    psi = np.pi / 2 - el - np.arcsin(R_EARTH / (R_EARTH + shell_km * 1e3) * np.cos(el))
    la0, lo0 = np.radians(rx_lat_deg), np.radians(rx_lon_deg)
    lat = np.arcsin(np.sin(la0) * np.cos(psi) + np.cos(la0) * np.sin(psi) * np.cos(az))
    lon = lo0 + np.arcsin(np.sin(psi) * np.sin(az) / np.cos(lat))
    return np.degrees(lat), np.degrees(lon)


def mapping_function(el, shell_km=350.0):
    """斜向 → 垂直的投影係數 M(E)：STEC = M × VTEC。"""
    return 1.0 / np.sqrt(1 - (R_EARTH / (R_EARTH + shell_km * 1e3) * np.cos(el)) ** 2)
