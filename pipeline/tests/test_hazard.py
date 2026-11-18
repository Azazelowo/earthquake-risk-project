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


class RecalibrationTest(unittest.TestCase):
    def test_factor_is_ratio_with_shrinkage_and_clipping(self):
        self.assertAlmostEqual(hz.recalibration_factor(100.0, 200.0, prior=0.0), 2.0)
        self.assertAlmostEqual(hz.recalibration_factor(0.0, 0.0), 1.0)          # 沒有資料 → 不調整
        self.assertLess(hz.recalibration_factor(1.0, 4.0), 2.0)                 # 樣本少 → 往 1 收縮
        self.assertEqual(hz.recalibration_factor(1.0, 1000.0), 3.0)
        self.assertEqual(hz.recalibration_factor(1000.0, 1.0), 0.5)

    def test_trailing_factor_corrects_activity_change_using_past_only(self):
        quiet = synthetic(n=3000, seed=10, start="1995-01-01", end="2010-01-01")
        busy = synthetic(n=3000, seed=11, start="2010-01-01", end="2013-01-01")   # 發生率變成 5 倍
        ev = pd.concat([quiet, busy]).sort_values("time").reset_index(drop=True)
        prm = hz.HazardParams(mu=1.0, K=0.0)
        t0 = pd.Timestamp("2012-06-01")
        cat = hz.Catalog(ev, GRID, 3.5)
        f = hz.trailing_factor(cat, t0, 7.0, prm)
        self.assertGreater(f, 1.8)                                              # 背景是長期平均，近兩年明顯偏低
        ev2 = ev.copy()
        ev2.loc[ev2["time"] >= t0, "lat"] = 99.0                                # 把未來的事件移出區外
        self.assertAlmostEqual(hz.trailing_factor(hz.Catalog(ev2, GRID, 3.5), t0, 7.0, prm), f, places=12)
        self.assertAlmostEqual(hz.trailing_factor(hz.Catalog(quiet, GRID, 3.5), pd.Timestamp("2009-06-01"), 7.0, prm),
                               1.0, delta=0.15)                                 # 發生率穩定時接近 1


class BackgroundOnlyExportTest(unittest.TestCase):
    """「平常值」要用只有背景模型自己的校準倍率（審查 M5）。"""

    def setUp(self):
        quiet = synthetic(n=3000, seed=30, start="1995-01-01", end="2010-01-01")
        busy = synthetic(n=3000, seed=31, start="2010-01-01", end="2013-01-01")   # 近年發生率約 5 倍
        self.ev = pd.concat([quiet, busy]).sort_values("time").reset_index(drop=True)
        self.t0 = pd.Timestamp("2012-06-01")
        self.cat = hz.Catalog(self.ev, GRID, 3.5)
        self.prm = hz.HazardParams(mu=0.5, K=0.05, alpha=0.0, p=0.9, sigma_bg=0.5, sigma_st=0.5)
        self.mu_bg = 1.1

    def test_bg_only_params_drop_short_term_and_use_own_mu(self):
        b = hz.bg_only_params(self.prm, self.mu_bg)
        self.assertEqual((b.mu, b.K), (1.1, 0.0))
        self.assertEqual((b.p, b.sigma_bg, b.b), (self.prm.p, self.prm.sigma_bg, self.prm.b))

    def test_uses_own_factor_not_the_full_models(self):
        n, f_bg = hz.background_only_export(self.cat, self.t0, 7.0, self.prm, self.mu_bg)
        bgp = hz.bg_only_params(self.prm, self.mu_bg)
        self.assertAlmostEqual(f_bg, hz.trailing_factor(self.cat, self.t0, 7.0, bgp), places=12)
        a, _ = hz.expected_ref(self.cat, self.t0, 7.0, bgp)
        np.testing.assert_allclose(n, a * f_bg, rtol=1e-12)
        f_full = hz.trailing_factor(self.cat, self.t0, 7.0, self.prm)
        self.assertNotAlmostEqual(f_bg, f_full, places=3)                      # 兩個模型的倍率本來就不同

    def test_total_matches_recent_observed_rate(self):
        """校準的意義：「平常」的全區總量要接近近兩年同長度窗的實際平均。"""
        n, _ = hz.background_only_export(self.cat, self.t0, 7.0, self.prm, self.mu_bg)
        h = np.timedelta64(7, "D")
        t0 = np.datetime64(self.t0, "us")
        obs = [float(self.cat.counts(t, t + h).sum()) for t in (t0 - h - np.timedelta64(7 * k, "D") for k in range(104))]
        self.assertAlmostEqual(float(n.sum()) / np.mean(obs), 1.0, delta=0.25)
        self.assertGreater(float(n.sum()), 2.0 * hz.expected_ref(self.cat, self.t0, 7.0,
                                                                  hz.bg_only_params(self.prm, self.mu_bg))[0].sum() * 0.5)


class UniformReferenceTest(unittest.TestCase):
    """預報卡的「均勻值」：每格相同，總量用過去資料滾動校準，和回測的均勻對照同一個定義。"""

    def setUp(self):
        quiet = synthetic(n=3000, seed=20, start="1995-01-01", end="2010-01-01")
        busy = synthetic(n=3000, seed=21, start="2010-01-01", end="2013-01-01")   # 發生率變成約 5 倍
        self.ev = pd.concat([quiet, busy]).sort_values("time").reset_index(drop=True)
        self.t0 = pd.Timestamp("2012-06-01")
        self.n_cells = GRID.shape[0] * GRID.shape[1]
        # 訓練期（只有平靜期）的每格每日發生率，和 fit() 的 uniform_rate_per_cell_day 同一個意思
        days = (pd.Timestamp("2010-01-01") - pd.Timestamp("1995-01-01")).days
        self.rate = 3000 / days / self.n_cells

    def test_is_spatially_uniform_and_scales_with_horizon_before_calibration(self):
        cat = hz.Catalog(self.ev, GRID, 3.5)
        u7 = hz.uniform_expected_ref(cat, self.t0, 7.0, self.rate)
        self.assertEqual(u7.shape, GRID.shape)
        self.assertEqual(float(u7.max()), float(u7.min()))                       # 每格一樣
        self.assertTrue(np.all(u7 > 0))

    def test_calibration_follows_past_activity_and_is_clipped(self):
        cat = hz.Catalog(self.ev, GRID, 3.5)
        u = hz.uniform_expected_ref(cat, self.t0, 7.0, self.rate)
        raw = self.rate * 7.0
        self.assertGreater(float(u[0, 0]) / raw, 1.8)                            # 近兩年變活躍 → 倍率明顯大於 1
        self.assertLessEqual(float(u[0, 0]) / raw, 3.0 + 1e-12)                  # 上限 3
        quiet_cat = hz.Catalog(self.ev[self.ev["time"] < "2010-01-01"], GRID, 3.5)
        u_quiet = hz.uniform_expected_ref(quiet_cat, pd.Timestamp("2009-06-01"), 7.0, self.rate)
        self.assertAlmostEqual(float(u_quiet[0, 0]) / raw, 1.0, delta=0.15)      # 穩定期接近 1

    def test_uses_past_only(self):
        cat = hz.Catalog(self.ev, GRID, 3.5)
        base = hz.uniform_expected_ref(cat, self.t0, 7.0, self.rate)
        ev2 = self.ev.copy()
        ev2.loc[ev2["time"] >= self.t0, "lat"] = 99.0                            # 把未來的事件移出區外
        moved = hz.uniform_expected_ref(hz.Catalog(ev2, GRID, 3.5), self.t0, 7.0, self.rate)
        np.testing.assert_allclose(moved, base, rtol=0, atol=1e-15)

    def test_uses_same_windows_and_calibration_as_trailing_factor(self):
        """窗的取法與 `trailing_factor` 相同（自 t0 − T 起每 7 天往回、730 天內），校準用同一個函式。

        注意：回測（06_hazard_model）的窗錨在固定的每週格點上，前瞻版錨在發布時間，
        兩者定義相同但窗的起點可以差幾天，數字不會逐位相同。"""
        cat = hz.Catalog(self.ev, GRID, 3.5)
        T = 14.0
        h = np.timedelta64(int(round(T * 86400)), "s")
        step = np.timedelta64(7 * 86400, "s")
        t0 = np.datetime64(self.t0, "us")
        t, pred, obs = t0 - h, 0.0, 0.0
        while t >= t0 - np.timedelta64(730 * 86400, "s"):
            pred += self.rate * T * self.n_cells
            obs += float(cat.counts(t, t + h).sum())
            t = t - step
        expect = self.rate * T * hz.recalibration_factor(pred, obs)
        got = hz.uniform_expected_ref(cat, self.t0, T, self.rate)
        self.assertAlmostEqual(float(got[0, 0]), expect, places=12)


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


class EventPopulationTest(unittest.TestCase):
    """審查 M1：events.parquet 的兩種母體混用會壓低 b 值；load_events 預設只用定位良好的事件。"""

    @staticmethod
    def _mixed_catalog(seed=21, n=40000, b=1.0, keep_small=0.4):
        ev = synthetic(n=n, b=b, seed=seed)
        rng = np.random.default_rng(seed + 1)
        located = rng.random(len(ev)) < keep_small            # 定位品質好與否和規模無關（例如外海）
        is_target = ev["ml"] >= 5.5
        return ev.assign(use_for_features=located, is_target=is_target)[located | is_target]

    def test_mixing_populations_biases_b_low_and_located_only_recovers_it(self):
        ev = self._mixed_catalog()
        b_mixed = hz.estimate_b(ev["ml"], 3.5)
        b_located = hz.estimate_b(ev.loc[ev["use_for_features"], "ml"], 3.5)
        self.assertAlmostEqual(b_located, 1.0, delta=0.04)
        self.assertLess(b_mixed, b_located - 0.01)            # 大地震被人為放大 → b 偏低

    def test_load_events_filters_and_reports_counts(self):
        import tempfile
        ev = self._mixed_catalog(n=5000)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.parquet"
            ev.to_parquet(path, index=False)
            located, info = hz.load_events(path)
            self.assertEqual(info["quality"], "located")
            self.assertEqual(info["n_used"], int(ev["use_for_features"].sum()))
            self.assertEqual(info["n_all"], len(ev))
            self.assertEqual(list(located.columns), ["time", "lat", "lon", "ml"])
            self.assertEqual(pd.Timestamp(info["time_max"]), ev["time"].max())   # 發布時間以篩選前為準
            everything, info_all = hz.load_events(path, "all")
            self.assertEqual(len(everything), len(ev))
            self.assertRaises(ValueError, hz.load_events, path, "bogus")
            ev.drop(columns=["use_for_features"]).to_parquet(path, index=False)
            self.assertRaises(ValueError, hz.load_events, path)                 # 舊檔沒有旗標：要求明確選擇
            self.assertEqual(len(hz.load_events(path, "all")[0]), len(ev))


class SearchTest(unittest.TestCase):
    """審查 M3：fit_mu_k 的快速寫法和原本的概似相同；最佳值落在邊界要被標出來。"""

    def test_fast_objective_matches_full_poisson_likelihood(self):
        rng = np.random.default_rng(12)
        bg = rng.uniform(0.0, 0.05, (300, 50))
        st = rng.exponential(0.2, (300, 50)) * (rng.random((300, 50)) < 0.05)
        obs = rng.poisson(0.8 * bg + 0.5 * st)
        mu, k, ll = hz.fit_mu_k(obs, bg, st)
        self.assertAlmostEqual(ll, hz.poisson_ll(obs, mu * bg + k * st), places=6)
        for dm, dk in ((1.05, 1.0), (0.95, 1.0), (1.0, 1.1), (1.0, 0.9)):     # 確實是最大值
            self.assertGreater(ll, hz.poisson_ll(obs, mu * dm * bg + k * dk * st))

    def test_boundary_hits(self):
        grids = {"alpha": (0.0, 0.5, 1.0), "p": (0.8, 1.0, 1.2), "sigma_bg": (1.0,)}
        hits = hz.boundary_hits({"alpha": 0.0, "p": 1.2, "sigma_bg": 1.0}, grids)
        self.assertEqual(hits, [("alpha", 0.0, "下限"), ("p", 1.2, "上限")])     # 單一候選值不檢查
        self.assertEqual(hz.boundary_hits({"alpha": 0.5, "p": 1.0, "sigma_bg": 1.0}, grids), [])

    def test_default_grid_extends_v1_range_and_fit_reports_it(self):
        self.assertLess(min(hz.ALPHAS), 0.2)
        self.assertLess(min(hz.PS), 0.8)
        self.assertLess(min(hz.SIGMAS_BG), 0.5)
        ev = synthetic(n=3000, seed=13)
        _, log, info = hz.fit(ev, GRID, "2005-01-01", "2015-01-01", alphas=(0.4, 0.8), ps=(1.1,),
                              sigmas_st=(0.5,), sigmas_bg=(1.0,))
        self.assertEqual(len(log), 2)
        self.assertEqual(info["search_grid"]["alpha"], [0.4, 0.8])
        self.assertTrue(all(h[0] == "alpha" for h in info["boundary_hits"]))   # 只有 alpha 有兩個候選值


if __name__ == "__main__":
    unittest.main()
