"""分段 Mc（04_estimate_mc.py）與隨年份變動的特徵門檻（eqforecast.features.event_min_ml）的單元測試。

全部用合成的 Gutenberg–Richter 目錄。執行：python -m unittest discover -s pipeline/tests -v
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from eqforecast.features import event_min_ml  # noqa: E402

spec = importlib.util.spec_from_file_location("estimate_mc", SRC / "04_estimate_mc.py")
MC = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MC)


def synthetic(n, mc_true, b=1.0, seed=0, sigma=0.15):
    """GR 分布（b 值）＋偵測機率在 mc_true 附近從 0 升到 1 的目錄，規模取到 0.1，剛好 n 筆。"""
    rng = np.random.default_rng(seed)
    m_min = mc_true - 1.0
    m = m_min + rng.exponential(np.log10(np.e) / b, size=int(n * 10 ** (b * 1.0) * 3))
    p = 1.0 / (1.0 + np.exp(-(m - (mc_true - 0.2)) / (sigma / 2)))
    m = m[rng.random(len(m)) < p][:n]
    return np.round(m, 1)


class TestMcEstimators(unittest.TestCase):
    def test_maxc_and_gft_find_completeness(self):
        for true in (1.5, 2.5, 3.3):
            m = synthetic(5000, true, seed=int(true * 10))
            self.assertLessEqual(abs(MC.mc_maxc(m) - true), 0.3, true)
            gft = MC.mc_gft(m)
            self.assertTrue(np.isfinite(gft))
            self.assertLessEqual(abs(gft - true), 0.4, true)

    def test_b_value_on_complete_part(self):
        m = synthetic(20000, 2.0, b=1.0, seed=3)
        self.assertAlmostEqual(MC.b_value(m, 2.5), 1.0, delta=0.08)

    def test_too_few_events_gives_nan(self):
        out = MC.estimate(synthetic(50, 2.0))
        self.assertEqual(out["n"], 50)
        self.assertTrue(np.isnan(out["mc"]))

    def test_zones(self):
        z = MC.zone_of([24.0, 24.0, 24.0, 21.5], [122.0, 119.5, 121.0, 121.0])
        self.assertEqual(list(z), ["東部外海", "西部外海", "本島", "南部外海"])


def catalog(spec):
    """spec: [(年, lat, lon, 事件數, Mc)] → events DataFrame。"""
    parts = []
    for k, (y, lat, lon, n, mc) in enumerate(spec):
        m = synthetic(n, mc, seed=k)
        t = pd.Timestamp(f"{y}-01-01") + pd.to_timedelta(np.linspace(0, 360, len(m)), unit="D")
        parts.append(pd.DataFrame({"time": t, "lat": lat, "lon": lon, "ml": m}))
    return pd.concat(parts, ignore_index=True)


class TestThresholds(unittest.TestCase):
    def test_pooling_and_previous_year_rule(self):
        ev = catalog([(y, 24.0, 121.0, 2000, 2.0) for y in range(2000, 2004)] +
                     [(y, 24.0, 122.5, 120, 3.5) for y in range(2000, 2004)])      # 外海每年 120 筆
        tab = MC.mc_table(ev)
        east = tab[tab.zone == "東部外海"].set_index("year")
        self.assertTrue(np.isnan(east.loc[2000, "mc"]))          # 只有 120 筆，往前沒有可併
        self.assertEqual(int(east.loc[2001, "pool_years"]), 2)   # 2000–2001 併成 240 筆
        self.assertGreater(east.loc[2001, "mc"], 3.0)
        thr = MC.thresholds(tab).set_index("year")
        self.assertEqual(thr.loc[2000, "threshold"], 3.0)        # 第一年用當年：外海估不出，本島 Mc 2.0 → 下限 3.0
        self.assertIn("第一年", thr.loc[2000, "basis_years"])
        self.assertEqual(thr.loc[2001, "threshold"], 3.0)        # 依 2000 年（外海還估不出來）
        self.assertGreater(thr.loc[2002, "threshold"], 3.0)      # 依 2001 年的外海 Mc
        self.assertEqual(thr.loc[2002, "zone"], "東部外海")

    def test_threshold_does_not_use_same_year(self):
        base = [(y, 24.0, 121.0, 1500, 2.0) for y in range(2010, 2014)]
        a = MC.thresholds(MC.mc_table(catalog(base))).set_index("year")
        changed = base[:-1] + [(2013, 24.0, 121.0, 1500, 3.6)]       # 只改最後一年的 Mc
        b = MC.thresholds(MC.mc_table(catalog(changed))).set_index("year")
        self.assertEqual(a.loc[2013, "threshold"], b.loc[2013, "threshold"])


class TestEventMinMl(unittest.TestCase):
    def test_lookup_clip_and_floor(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "thr.csv"
            pd.DataFrame({"year": [2000, 2001, 2002], "threshold": [3.4, 2.8, 3.1]}).to_csv(p, index=False)
            times = pd.to_datetime(["1999-05-01", "2000-12-31", "2001-06-01", "2002-01-01", "2030-01-01"])
            got = event_min_ml(times, 3.0, "thr.csv", d)
            np.testing.assert_allclose(got, [3.4, 3.4, 3.0, 3.1, 3.1])   # 表前用第一年、表後用最後一年、不低於 3.0
            np.testing.assert_allclose(event_min_ml(times, 3.0), 3.0)


class TestCompareRuns(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("compare_runs", SRC / "05_compare_runs.py")
        self.CR = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.CR)
        rng = np.random.default_rng(1)
        t0 = np.repeat(pd.date_range("2020-01-01", periods=20, freq="30D"), 50)
        self.base = pd.DataFrame({"t0": t0, "cell_id": np.tile(np.arange(50), 20),
                                  "score": rng.random(1000), "y": rng.random(1000) < 0.02})

    def test_identical_runs_have_zero_delta(self):
        r = self.CR.compare(self.base, self.base.copy(), 50)
        self.assertEqual(r["delta_ASS"]["delta"], 0.0)
        self.assertEqual(r["tau"][0.1]["only_a"] + r["tau"][0.1]["only_b"], 0)

    def test_better_scores_detected_and_mismatch_rejected(self):
        better = self.base.assign(score=self.base["score"] + self.base["y"] * 10)    # 目標格永遠排第一
        r = self.CR.compare(self.base, better, 50)
        self.assertGreater(r["delta_ASS"]["ci_low"], 0)
        self.assertEqual(r["tau"][0.1]["hits"], r["n_targets"])     # τ=10%：每次 5 格，足以涵蓋每次的目標
        with self.assertRaises(ValueError):
            self.CR.compare(self.base, self.base.assign(y=~self.base["y"]), 50)


if __name__ == "__main__":
    unittest.main()
