"""APP 計算核心（app/hazard_core.py）的單元測試：用合成快照，不需要真實資料或 Streamlit。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
import hazard_core as hc  # noqa: E402

HORIZONS = [1 / 24, 1, 7, 30]
GRID = {"lat_min": 21.0, "lat_max": 26.0, "lon_min": 119.0, "lon_max": 123.0, "deg": 0.2}


def make_snapshot(tmp, rate_per_day=1e-3, bg_share=0.5):
    """每格發生率相同、N 和 T 成正比的合成快照（方便驗算）。"""
    ni, nj = 25, 20
    ii, jj = np.meshgrid(np.arange(ni), np.arange(nj), indexing="ij")
    df = pd.DataFrame({"lat_bin": (21.0 + ii.ravel() * 0.2).round(4), "lon_bin": (119.0 + jj.ravel() * 0.2).round(4)})
    for T in HORIZONS:
        df[f"n_ref_{T:.5f}d"] = rate_per_day * T
        df[f"n_bg_{T:.5f}d"] = rate_per_day * T * bg_share
    d = Path(tmp)
    df.to_csv(d / "hazard_latest.csv", index=False)
    params = {"params": {"m_ref": 3.5, "b": 1.0}, "issue_time": "2026-09-30 00:00:00",
              "export_horizons_days": HORIZONS, "grid": GRID, "train": ["1995-01-01", "2015-01-01"],
              "test": ["2015-01-01", "2026-09-30"]}
    (d / "params.json").write_text(json.dumps(params), encoding="utf-8")
    pd.DataFrame({"m": [3.5, 6.0, 3.5], "horizon_days": [7.0, 1.0, 1 / 24], "n_events": [500, 7, 40],
                  "base_rate": [0.01, 1e-5, 1e-4], "auc_full": [0.9, 0.9, 0.9], "auc_bg": [0.88, 0.88, 0.88],
                  "n_ratio_full": [1.0, 1.8, 0.95]}).to_csv(d / "backtest.csv", index=False)
    return hc.load_snapshot(d)


class TestHazardCore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.snap = make_snapshot(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_interpolation_hits_exports_and_is_loglog_linear(self):
        s = self.snap
        np.testing.assert_allclose(hc.interp_horizon(s.n_ref, s.horizons, 7), 7e-3)
        # N ∝ T，log–log 內插應該精確還原中間值
        np.testing.assert_allclose(hc.interp_horizon(s.n_ref, s.horizons, 3), 3e-3, rtol=1e-9)
        np.testing.assert_allclose(hc.interp_horizon(s.n_ref, s.horizons, 0.5), 0.5e-3, rtol=1e-9)
        with self.assertRaises(ValueError):
            hc.interp_horizon(s.n_ref, s.horizons, 60)

    def test_magnitude_scaling_follows_gr(self):
        s = self.snap
        n35 = hc.expected(s, 3.5, 7).sum()
        n45 = hc.expected(s, 4.5, 7).sum()
        self.assertAlmostEqual(n45 / n35, 0.1, places=12)  # b = 1 → 每大 1 級少 10 倍

    def test_probability_monotone_in_m_and_T(self):
        s, w = self.snap, hc.all_weights(self.snap)
        ps_m = [hc.region_probability(s, w, m, 7) for m in (3, 4, 5, 6)]
        ps_t = [hc.region_probability(s, w, 5, T) for T in (1 / 24, 0.5, 1, 7, 30)]
        self.assertTrue(all(a > b for a, b in zip(ps_m, ps_m[1:])))
        self.assertTrue(all(a < b for a, b in zip(ps_t, ps_t[1:])))
        self.assertTrue(all(0 <= p <= 1 for p in ps_m + ps_t))

    def test_point_weights_match_circle_area(self):
        s = self.snap
        w = hc.point_weights(s, 23.5, 121.0, 60)
        self.assertAlmostEqual(hc.region_area_km2(s, w) / hc.circle_area_km2(60), 1.0, delta=0.03)
        # 半徑比一格還小也不會變 0（以面積比例計，而不是看格中心）
        self.assertGreater(hc.point_weights(s, 23.55, 121.05, 3).sum(), 0)

    def test_line_weights_match_band_area(self):
        s = self.snap
        line = [(23.0, 120.6), (24.0, 121.2)]
        w = hc.line_weights(s, line, 10)
        self.assertAlmostEqual(hc.region_area_km2(s, w) / hc.line_area_km2(line, 10), 1.0, delta=0.04)
        with self.assertRaises(ValueError):
            hc.line_weights(s, [(23.0, 120.0)], 10)

    def test_region_outside_grid_is_partial(self):
        s = self.snap
        w = hc.point_weights(s, 21.0, 119.0, 50)  # 圓心在網格角落：只有約四分之一在區內
        frac = hc.region_area_km2(s, w) / hc.circle_area_km2(50)
        self.assertGreater(frac, 0.2)
        self.assertLess(frac, 0.3)

    def test_background_curve_shapes(self):
        s, w = self.snap, hc.point_weights(self.snap, 24.0, 121.5, 40)
        cm = hc.curve_vs_magnitude(s, w, 7, np.arange(2, 8, 0.5))
        self.assertTrue((cm["p_bg"] <= cm["p_full"] + 1e-15).all())  # 合成快照沒有 fit_info，不換算
        self.assertTrue((np.diff(cm["p_bg"]) < 0).all())

    def test_background_rescaled_to_bg_only_fit(self):
        self.assertEqual(hc.background_rescale({}), 1.0)
        prm = {"params": {"mu": 0.5}, "fit_info": {"mu_bg_only": 1.25}}
        self.assertAlmostEqual(hc.background_rescale(prm), 2.5)
        d = Path(self._tmp.name)
        p = json.loads((d / "params.json").read_text(encoding="utf-8"))
        p.update(prm | {"params": {"m_ref": 3.5, "b": 1.0, "mu": 0.5}})
        (d / "params.json").write_text(json.dumps(p), encoding="utf-8")
        s2 = hc.load_snapshot(d)
        np.testing.assert_allclose(s2.n_bg, self.snap.n_bg * 2.5)

    def test_magnitude_at_probability_inverts(self):
        s, w = self.snap, hc.all_weights(self.snap)
        m = hc.magnitude_at_probability(s, w, 30, 0.1)
        self.assertAlmostEqual(hc.region_probability(s, w, m, 30), 0.1, places=9)

    def test_reliability_flags(self):
        s = self.snap
        notes = hc.reliability(s, 6.0, 1)["notes"]
        self.assertTrue(any("高估" in n for n in notes))
        self.assertTrue(any("只有" in n for n in notes))
        self.assertTrue(any("外推" in n for n in hc.reliability(s, 2.0, 7)["notes"]))
        self.assertFalse(hc.reliability(s, 4.0, 7)["notes"])

    def test_formatting(self):
        self.assertEqual(hc.format_probability(0.123), "12%")
        self.assertEqual(hc.format_probability(0.0123), "1.2%")
        self.assertEqual(hc.format_probability(1e-6), "< 0.01%")
        self.assertEqual(hc.format_odds(0.01), "約 100 次裡有 1 次")
        self.assertEqual(hc.horizon_label(7), "7 天")
        rgba = hc.color_scale(np.array([1e-7, 1e-3, 0.5]))
        self.assertEqual(rgba.shape, (3, 4))
        self.assertEqual(rgba[0, 3], 0)


if __name__ == "__main__":
    unittest.main()
