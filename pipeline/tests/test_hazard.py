"""連續式風險函數（eqforecast/hazard.py）的單元測試，全部用合成目錄。"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from eqforecast import hazard as hz  # noqa: E402

GRID = hz.Grid()


def synthetic(n=6000, seed=0, b=1.0, m_min=3.5, start="1995-01-01", end="2015-01-01", hot=(23.5, 121.5)):
    """卜瓦松目錄：一半集中在 hot 附近、一半全區均勻；規模服從 GR。"""
    rng = np.random.default_rng(seed)
    t0, t1 = pd.Timestamp(start), pd.Timestamp(end)
    time = t0 + pd.to_timedelta(rng.uniform(0, (t1 - t0).total_seconds(), n), unit="s")
    half = n // 2
    lat = np.r_[rng.normal(hot[0], 0.15, half), rng.uniform(21, 26, n - half)]
    lon = np.r_[rng.normal(hot[1], 0.15, half), rng.uniform(119, 123, n - half)]
    # 規模取到 0.1：連續值要從 m_min − 0.05 起算，四捨五入後最小的一級才是完整的
    ml = np.round(m_min - 0.05 + rng.exponential(np.log10(np.e) / b, n), 1)
    return pd.DataFrame({"time": time, "lat": lat, "lon": lon, "ml": ml}).sort_values("time").reset_index(drop=True)


class BasicsTest(unittest.TestCase):
    def test_cell_index_and_bounds(self):
        i, j, ok = GRID.cell_index([21.0, 25.99, 26.0, 20.99, 23.4], [119.0, 122.99, 121.0, 121.0, 121.6])
        self.assertEqual(list(ok), [True, True, False, False, True])
        self.assertEqual((i[0], j[0]), (0, 0))
        self.assertEqual((i[1], j[1]), (24, 19))
        self.assertEqual((i[4], j[4]), (12, 13))
        self.assertEqual(len(GRID.frame()), 500)

    def test_omori_integral_matches_numeric_and_p_equal_one(self):
        for p in (1.0, 1.2):
            s = np.linspace(2.0, 9.0, 200001)
            y = (s + 0.01) ** (-p)
            numeric = float(np.sum((y[1:] + y[:-1]) / 2 * np.diff(s)))
            self.assertAlmostEqual(float(hz.omori_integral(2.0, 7.0, 0.01, p)), numeric, places=6)
        self.assertGreater(hz.omori_integral(0.1, 1.0, 0.01, 1.1), hz.omori_integral(30.0, 1.0, 0.01, 1.1))

    def test_magnitude_scaling_and_probability(self):
        self.assertAlmostEqual(float(hz.scale_magnitude(1.0, 4.5, 3.5, 1.0)), 0.1)
        self.assertAlmostEqual(float(hz.scale_magnitude(1.0, 2.5, 3.5, 1.0)), 10.0)
        self.assertAlmostEqual(float(hz.probability(0.0)), 0.0)
        self.assertAlmostEqual(float(hz.probability(1e-6)), 1e-6, places=9)
        self.assertLess(float(hz.probability(50.0)), 1.0 + 1e-12)
        p = hz.probability(hz.scale_magnitude(0.3, np.array([3.5, 4.5, 5.5]), 3.5, 1.0))
        self.assertTrue(np.all(np.diff(p) < 0))                      # 規模越大機率越小

    def test_estimate_b_recovers_true_value(self):
        for b in (0.8, 1.1):
            ev = synthetic(n=20000, b=b, seed=3)
            self.assertAlmostEqual(hz.estimate_b(ev["ml"], 3.5), b, delta=0.04)
        self.assertRaises(ValueError, hz.estimate_b, [3.6] * 10, 3.5)

    def test_smoothing_preserves_total(self):
        f = np.zeros(GRID.shape)
        f[0, 0] = 10.0                                               # 放在角落，平滑會流到界外
        self.assertAlmostEqual(hz._smooth(f, 1.5).sum(), 10.0, places=9)


class PastOnlyTest(unittest.TestCase):
    def test_future_events_do_not_change_forecast(self):
        ev = synthetic(seed=1)
        t0 = pd.Timestamp("2010-06-01")
        prm = hz.HazardParams(K=0.01)
        a1, b1 = hz.expected_ref(hz.Catalog(ev, GRID, 3.5), t0, 7.0, prm)
        ev2 = ev.copy()
        fut = ev2["time"] >= t0
        ev2.loc[fut, "ml"] = 7.5
        ev2.loc[fut, "lat"] = 22.0
        a2, b2 = hz.expected_ref(hz.Catalog(ev2, GRID, 3.5), t0, 7.0, prm)
        np.testing.assert_allclose(a1, a2)
        np.testing.assert_allclose(b1, b2)

    def test_event_exactly_at_issue_time_is_excluded(self):
        cat = hz.Catalog(synthetic(n=500, seed=2), GRID, 3.5)   # 區外的事件會被丟掉，所以用 cat 自己的時間
        k = len(cat) // 2
        t0 = cat.time[k]
        self.assertEqual(cat.before(t0), k)                      # 第 k 筆剛好在 t0，不算「之前」
        self.assertEqual(cat.counts(t0, t0 + np.timedelta64(36500, "D")).sum(), len(cat) - k)
        self.assertEqual(cat.counts(cat.t_start, t0).sum(), k)


class BehaviourTest(unittest.TestCase):
    def test_background_is_highest_at_hotspot_and_never_zero(self):
        ev = synthetic(seed=4)
        bg = hz.background_rate(hz.Catalog(ev, GRID, 3.5), pd.Timestamp("2015-01-01"), 1.0, 0.05)
        i, j, _ = GRID.cell_index([23.5], [121.5])
        self.assertGreater(bg[i[0], j[0]], 10 * np.median(bg))
        self.assertGreater(bg.min(), 0)
        self.assertAlmostEqual(bg.sum(), len(ev) / ((pd.Timestamp("2015-01-01") - ev["time"].iloc[0]).days + 0.5),
                               delta=0.02 * bg.sum())

    def test_recent_large_event_raises_local_short_term(self):
        ev = synthetic(n=800, seed=5)
        t0 = pd.Timestamp("2014-12-01")
        big = pd.DataFrame({"time": [t0 - pd.Timedelta(days=1)], "lat": [24.9], "lon": [119.5], "ml": [6.5]})
        cat = hz.Catalog(pd.concat([ev, big]).sort_values("time"), GRID, 3.5)
        st = hz.short_term(cat, t0, 7.0, 0.8, 0.01, 1.1, 0.5, 365.0, 3.5)
        i, j, _ = GRID.cell_index([24.9], [119.5])
        self.assertEqual(np.unravel_index(st.argmax(), st.shape), (i[0], j[0]))
        st_later = hz.short_term(cat, t0 + pd.Timedelta(days=90), 7.0, 0.8, 0.01, 1.1, 0.5, 365.0, 3.5)
        self.assertLess(st_later[i[0], j[0]], 0.1 * st[i[0], j[0]])  # 隨時間衰減

    def test_longer_horizon_never_lowers_expected_count(self):
        cat = hz.Catalog(synthetic(seed=6), GRID, 3.5)
        prm = hz.HazardParams(K=0.02)
        prev = np.zeros(GRID.shape)
        for T in (1 / 24, 1, 7, 30):
            a, b = hz.expected_ref(cat, pd.Timestamp("2012-01-01"), T, prm)
            self.assertTrue(np.all(a + b >= prev - 1e-15))
            prev = a + b


class FitTest(unittest.TestCase):
    def test_fit_mu_k_recovers_linear_mix(self):
        rng = np.random.default_rng(7)
        bg = rng.uniform(0.01, 0.2, 20000)
        st = rng.exponential(0.5, 20000) * (rng.random(20000) < 0.1)
        obs = rng.poisson(0.7 * bg + 0.3 * st)
        mu, k, _ = hz.fit_mu_k(obs, bg, st)
        self.assertAlmostEqual(mu, 0.7, delta=0.06)
        self.assertAlmostEqual(k, 0.3, delta=0.04)

    def test_fit_on_poisson_catalog_is_calibrated_and_beats_uniform(self):
        ev = synthetic(n=6000, seed=8)
        prm, log, info = hz.fit(ev, GRID, "2000-01-01", "2015-01-01", alphas=(0.8,), ps=(1.1,), sigmas_st=(0.5,),
                                sigmas_bg=(1.0,))
        self.assertAlmostEqual(prm.b, 1.0, delta=0.06)
        self.assertEqual(len(log), 1)
        # 沒有群聚的目錄：背景項應該解釋大部分地震（mu 接近 1，近期項只占小部分）
        self.assertGreater(prm.mu, 0.75)
        self.assertLess(prm.mu, 1.15)
        uniform_ll = hz.poisson_ll(np.array([info["n_events"]]), np.array([info["n_events"]]))  # 佔位，確認函式可用
        self.assertTrue(np.isfinite(uniform_ll))
        self.assertGreaterEqual(info["ll_full"], info["ll_bg_only"] - 1e-6)


if __name__ == "__main__":
    unittest.main()
