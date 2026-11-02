"""gnss_tec 的單元測試（合成資料，不依賴本機 RINEX）。

執行：python -m unittest discover -s pipeline/tests -v
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gnss_tec import orbit, tec  # noqa: E402


class TestConstants(unittest.TestCase):
    def test_tec_factor(self):
        # 文獻常見值：1 m 的 P2−P1 ≈ 9.52 TECU；1 ns ≈ 2.85 TECU
        self.assertAlmostEqual(tec.K_TECU_PER_M, 9.52, delta=0.01)
        self.assertAlmostEqual(tec.NS_TO_TECU, 2.854, delta=0.01)

    def test_mapping_function(self):
        self.assertAlmostEqual(float(orbit.mapping_function(np.radians(90.0))), 1.0, places=9)
        self.assertGreater(float(orbit.mapping_function(np.radians(30.0))), 1.5)


class TestGeometry(unittest.TestCase):
    def test_geodetic_roundtrip(self):
        # NDH5 標頭座標：約 24.01°N、121.62°E
        lat, lon, h = orbit.ecef_to_geodetic(-3056169.7212, 4964214.0177, 2579323.6439)
        self.assertAlmostEqual(lat, 24.010, delta=0.01)
        self.assertAlmostEqual(lon, 121.618, delta=0.01)
        self.assertTrue(-100 < h < 500)

    def test_zenith_satellite(self):
        rx = np.array([orbit.WGS84_A, 0.0, 0.0])
        sat = np.array([[orbit.WGS84_A + 20_000e3, 0.0, 0.0]])
        el, _ = orbit.elevation_azimuth(rx, sat)
        self.assertAlmostEqual(float(np.degrees(el[0])), 90.0, places=6)
        lat, lon = orbit.ipp(0.0, 0.0, el, np.array([0.0]))
        self.assertAlmostEqual(float(lat[0]), 0.0, places=6)

    def test_circular_orbit_radius(self):
        a = 26_560e3
        eph = pd.DataFrame([{
            "sqrt_a": np.sqrt(a), "delta_n": 0.0, "m0": 0.3, "e": 0.0, "omega": 0.0,
            "cus": 0.0, "cuc": 0.0, "crs": 0.0, "crc": 0.0, "cis": 0.0, "cic": 0.0,
            "i0": np.radians(55), "idot": 0.0, "omega0": 1.0, "omega_dot": 0.0, "toe": 0.0,
        }] * 3)
        t = pd.to_datetime(["1980-01-06 00:00", "1980-01-06 01:00", "1980-01-06 06:00"])
        xyz = orbit.sat_position(eph, t)
        np.testing.assert_allclose(np.linalg.norm(xyz, axis=1), a, rtol=1e-9)


class TestReceiverBias(unittest.TestCase):
    def test_recovers_known_bias_with_gradient(self):
        rng = np.random.default_rng(0)
        rows = []
        true_bias = -6.0
        for k in range(200):
            t = pd.Timestamp("2024-04-01 20:00") + pd.Timedelta(seconds=30 * k)
            for sv in range(8):
                el = np.radians(rng.uniform(30, 85))
                dlat, dlon = rng.uniform(-4, 4), rng.uniform(-4, 4)
                vtec = 20 + 1.5 * dlat - 0.8 * dlon          # 有水平梯度的電離層
                mf = orbit.mapping_function(el)
                rows.append({"time": t, "sv": f"G{sv:02d}", "el": np.degrees(el), "mf": mf,
                             "ipp_lat": 24 + dlat, "ipp_lon": 121 + dlon,
                             "stec_c": vtec * mf + true_bias + rng.normal(0, 0.2)})
        st = pd.DataFrame(rows)
        b_grad, resid, _ = tec.estimate_receiver_bias(st, gradient=True)
        b_flat, _, _ = tec.estimate_receiver_bias(st, gradient=False)
        self.assertAlmostEqual(b_grad, true_bias, delta=0.3)
        self.assertLess(resid, 0.3)
        # 不考慮梯度時，估計會偏掉（這正是改用平面模型的原因）
        self.assertGreater(abs(b_flat - true_bias), abs(b_grad - true_bias))


if __name__ == "__main__":
    unittest.main()
