"""eqforecast 的單元測試（不依賴本機資料，全部使用合成資料）。

執行：python -m unittest discover -s pipeline/tests -v
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eqforecast import metrics as M  # noqa: E402
from eqforecast.config import Region  # noqa: E402
from eqforecast.decluster import decluster, haversine_km, windows  # noqa: E402
from eqforecast.features import catalog_features  # noqa: E402
from eqforecast.grid import Grid  # noqa: E402
from eqforecast.splits import assign_splits, check_no_leakage  # noqa: E402
from eqforecast.targets import build_targets, issue_times  # noqa: E402


class TestMetrics(unittest.TestCase):
    def test_contingency_and_skill(self):
        alarm = np.array([1, 1, 0, 0, 1, 0], bool)
        y = np.array([1, 0, 1, 0, 0, 0], bool)
        a, b, c, d = M.contingency(alarm, y)
        self.assertEqual((a, b, c, d), (1, 2, 1, 2))
        s = M.skill(a, b, c, d)
        self.assertAlmostEqual(s["POD"], 0.5)
        self.assertAlmostEqual(s["FAR"], 2 / 3)
        self.assertAlmostEqual(s["CSI"], 0.25)
        self.assertAlmostEqual(s["tau"], 0.5)

    def test_ranks_within_window(self):
        t0 = np.array([0, 0, 0, 1, 1, 1])
        score = np.array([0.1, 0.9, 0.5, 3.0, 1.0, 2.0])
        r = M.window_ranks(t0, score)
        np.testing.assert_array_equal(r, [3, 1, 2, 1, 3, 2])

    def test_ass_random_and_perfect(self):
        rng = np.random.default_rng(0)
        n_cells, n_win = 100, 400
        t0 = np.repeat(np.arange(n_win), n_cells)
        y = rng.random(n_win * n_cells) < 0.01
        r_rand = M.window_ranks(t0, rng.random(y.size))
        self.assertAlmostEqual(M.area_skill_score(r_rand, y, n_cells), 0.5, delta=0.03)
        r_perfect = M.window_ranks(t0, y.astype(float))
        self.assertGreater(M.area_skill_score(r_perfect, y, n_cells), 0.98)

    def test_bootstrap_delta_matches_ass(self):
        rng = np.random.default_rng(1)
        n_cells, n_win = 50, 200
        t0 = np.repeat(np.arange(n_win), n_cells)
        y = rng.random(t0.size) < 0.02
        ra = M.window_ranks(t0, rng.random(t0.size))
        rb = M.window_ranks(t0, y + rng.normal(size=t0.size))
        exact = M.area_skill_score(rb, y, n_cells) - M.area_skill_score(ra, y, n_cells)
        boot = M.bootstrap_delta_ass(ra, rb, y, n_cells, n_boot=200)
        self.assertAlmostEqual(boot["delta"], exact, places=9)
        self.assertLess(boot["ci_low"], boot["delta"])

    def test_mcnemar(self):
        a = np.array([1, 1, 1, 0, 0, 1], bool)
        b = np.array([1, 0, 0, 0, 0, 1], bool)
        m = M.mcnemar_exact(a, b)
        self.assertEqual((m["only_a"], m["only_b"]), (2, 0))
        self.assertAlmostEqual(m["p_two_sided"], 0.5)


class TestDecluster(unittest.TestCase):
    def test_gk_windows_reference_values(self):
        L, T = windows([5.0, 7.0], "gardner_knopoff")
        self.assertAlmostEqual(L[0], 10 ** (0.1238 * 5 + 0.983))
        self.assertAlmostEqual(T[0], 10 ** (0.5409 * 5 - 0.547))
        self.assertAlmostEqual(T[1], 10 ** (0.032 * 7 + 2.7389))

    def test_haversine(self):
        self.assertAlmostEqual(haversine_km(0, 0, 0, 1), 111.19, delta=0.1)

    def test_mainshock_aftershock_foreshock(self):
        base = pd.Timestamp("2000-01-01")
        ev = pd.DataFrame({
            "time": [base, base + pd.Timedelta(days=1), base + pd.Timedelta(days=10),
                     base - pd.Timedelta(days=2), base + pd.Timedelta(days=5),
                     base - pd.Timedelta(days=200)],
            "lat": [23.0, 23.05, 23.1, 23.0, 25.5, 23.0],
            "lon": [121.0, 121.0, 121.05, 121.02, 121.0, 121.0],
            "ml": [6.5, 5.6, 4.0, 5.0, 5.6, 5.0],
        })
        # 0 主震；1、2 餘震；3 前震（2 天前，在 0.1 × T 之內）；4 距離太遠；5 時間太早
        out = decluster(ev, "gardner_knopoff", fs_time_prop=0.1)
        np.testing.assert_array_equal(out["is_mainshock"].to_numpy(), [True, False, False, False, True, True])
        np.testing.assert_array_equal(out["cluster_id"].to_numpy()[:4], [0, 0, 0, 0])
        none = decluster(ev, "none")
        self.assertTrue(none["is_mainshock"].all())


class TestTargetsSplitsFeatures(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(Region(21.0, 22.0, 120.0, 121.0), 0.5)   # 2×2 = 4 格

    def test_cell_of_boundaries(self):
        g = self.grid
        np.testing.assert_array_equal(g.cell_of([21.0, 21.5, 21.99, 22.0], [120.0, 120.5, 120.99, 120.0]),
                                      [0, 3, 3, -1])

    def test_targets_window_assignment(self):
        t0s = issue_times("2000-01-01", "2000-04-01", 30)
        self.assertEqual(len(t0s), 3)
        ev = pd.DataFrame({"time": pd.to_datetime(["2000-01-01", "2000-01-30 23:59", "2000-01-31", "2000-06-01"], format="ISO8601"),
                           "ml": [6.0, 5.6, 5.5, 7.0], "lat": [21.1] * 4, "lon": [120.1] * 4})
        cells = self.grid.cell_of(ev["lat"], ev["lon"])
        tg = build_targets(ev, cells, t0s, self.grid, 30, np.ones(4, bool))
        self.assertEqual(int(tg.loc[(tg.t0 == t0s[0]) & (tg.cell_id == 0), "n_events"].iloc[0]), 2)
        self.assertEqual(int(tg.loc[(tg.t0 == t0s[1]) & (tg.cell_id == 0), "n_events"].iloc[0]), 1)
        self.assertEqual(int(tg["n_events"].sum()), 3)   # 2000-06-01 不在任何窗內

    def test_splits_purge_and_leakage(self):
        t0s = issue_times("2000-01-01", "2003-01-01", 30)
        splits = {"train": ["2000-01-01", "2001-01-01"], "val": ["2001-01-01", "2002-01-01"],
                  "test": ["2002-01-01", None]}
        names = assign_splits(t0s, 30, splits, 0, pd.Timestamp("2003-01-01"))
        check_no_leakage(t0s, names, 30)
        self.assertIn("purged", set(names))
        h = pd.Timedelta(days=30)
        for t, n in zip(t0s, names):
            if n == "train":
                self.assertLessEqual(t + h, pd.Timestamp("2001-01-01"))

    def test_features_use_only_past(self):
        t0s = issue_times("2000-01-01", "2000-12-31", 30)
        ev = pd.DataFrame({"time": pd.to_datetime(["1999-12-15", "2000-03-15"]),
                           "ml": [4.5, 5.2], "lat": [21.1, 21.1], "lon": [120.1, 120.1]})
        cells = self.grid.cell_of(ev["lat"], ev["lon"])
        f = catalog_features(ev, cells, t0s, self.grid, 3.0)
        cell0 = f[f.cell_id == 0].set_index("t0")
        # 2000-03-15 的 ML5.2 只能影響之後的發布時間點
        before = cell0.loc[cell0.index <= pd.Timestamp("2000-03-15"), "f_log_days_since_m5_nbr"]
        after = cell0.loc[cell0.index > pd.Timestamp("2000-03-15"), "f_log_days_since_m5_nbr"]
        self.assertTrue(np.allclose(before, np.log10(3650)))
        self.assertTrue((after < np.log10(3650)).all())


class TestComparisonPower(unittest.TestCase):
    """比較方法本身的檢查：雜訊特徵不該顯著，有資訊的特徵應該顯著。"""

    def test_noise_vs_signal(self):
        rng = np.random.default_rng(7)
        n_cells, n_win = 200, 300
        t0 = np.repeat(np.arange(n_win), n_cells)
        base = rng.normal(size=t0.size)
        y = rng.random(t0.size) < 1 / (1 + np.exp(-(base * 1.5 - 7)))
        noise = base + 0.0 * rng.normal(size=t0.size)
        r_base = M.window_ranks(t0, base)
        r_noise = M.window_ranks(t0, base + 0.01 * rng.normal(size=t0.size))
        r_signal = M.window_ranks(t0, base + 2.0 * y)
        k = int(0.05 * n_cells)
        p_noise = M.mcnemar_exact(r_base[y] <= k, r_noise[y] <= k)["p_two_sided"]
        p_signal = M.mcnemar_exact(r_base[y] <= k, r_signal[y] <= k)["p_two_sided"]
        self.assertGreater(p_noise, 0.05)
        self.assertLess(p_signal, 0.001)
        del noise


if __name__ == "__main__":
    unittest.main()
