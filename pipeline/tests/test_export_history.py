"""歷史回放（app/export_history.py）的測試：找主震、不偷看未來、輸出通過 feed 檢查。

用真的 app/data 快照，但校準只回看 30 天（正式輸出是 730 天），讓測試在幾秒內跑完。
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
import export_history as eh  # noqa: E402
import live_update as lu  # noqa: E402

BASE = ROOT / "app" / "data"
T0 = pd.Timestamp("2024-04-03 00:58:00")   # 2024 花蓮地震（2024-04-02 23:58 UTC）後 1 小時


class TestHistory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.params = lu.load_params(BASE)
        cls.base = lu.read_base_catalog(BASE)
        cls.shown = cls.base.assign(depth=10.0)

    def test_find_mainshock(self):
        ms = eh.find_mainshock(self.base, "2024-04-03")
        self.assertEqual(ms["time"], pd.Timestamp("2024-04-02 23:58:09"))
        self.assertGreaterEqual(ms["ml"], 7.0)
        with self.assertRaises(ValueError):
            eh.find_mainshock(self.base.iloc[:0], "2024-04-03")

    def test_no_look_ahead(self):
        """t0 之後的地震（再大）都不影響 t0 的數字與校準倍率"""
        a, fa = eh.hazard_at(self.base, self.params, T0, trailing_days=30)
        future = pd.DataFrame({"time": [T0 + pd.Timedelta(minutes=1), T0 + pd.Timedelta(days=3)],
                               "lat": [23.9, 24.0], "lon": [121.6, 121.7], "ml": [7.5, 6.0]})
        b, fb = eh.hazard_at(pd.concat([self.base, future], ignore_index=True), self.params, T0, trailing_days=30)
        self.assertEqual(fa, fb)
        np.testing.assert_array_equal(a.to_numpy(), b.to_numpy())

    def test_after_mainshock_is_elevated_near_epicenter(self):
        """主震後 1 小時，震央那一格的近期加成遠大於主震前 1 小時"""
        after, _ = eh.hazard_at(self.base, self.params, T0, trailing_days=30)
        before, _ = eh.hazard_at(self.base, self.params, T0 - pd.Timedelta(hours=2), trailing_days=30)
        cell = (after["lat_bin"] <= 23.8757) & (after["lat_bin"] + 0.2 > 23.8757) & \
               (after["lon_bin"] <= 121.5735) & (after["lon_bin"] + 0.2 > 121.5735)
        col_ref, col_bg = "n_ref_1.00000d", "n_bg_1.00000d"
        ratio_after = after.loc[cell, col_ref].item() / after.loc[cell, col_bg].item()
        ratio_before = before.loc[cell, col_ref].item() / before.loc[cell, col_bg].item()
        self.assertGreater(ratio_after, 3 * ratio_before)

    def test_feed_passes_check_and_lists_only_past_events(self):
        feed = eh.feed_at(self.base, self.shown, self.params, T0, BASE / "backtest.csv", trailing_days=30)
        self.assertEqual(feed["issued_at"], "2024-04-03T00:58:00Z")
        self.assertFalse(feed["mock"])
        self.assertEqual(feed["data_status"]["gap_days"], 0.0)
        times = [pd.Timestamp(e["time"]).tz_localize(None) for e in feed["recent"]]
        self.assertTrue(times and max(times) < T0)
        self.assertTrue(min(times) >= T0 - pd.Timedelta(days=30))
        self.assertTrue(any(e["ml"] >= 7.0 for e in feed["recent"]))   # 主震在列表上
        self.assertEqual({e["source"] for e in feed["recent"]}, {"GDMS"})


if __name__ == "__main__":
    unittest.main()
