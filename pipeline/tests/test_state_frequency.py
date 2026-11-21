"""狀態門檻回測統計（app/state_frequency.py）的測試：切法和 APP 相同、統計表的意義、和 APP 首頁同一套計算。"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
import live_update as lu  # noqa: E402
import state_frequency as sf  # noqa: E402

BASE = ROOT / "app" / "data"


class TestStateFrequency(unittest.TestCase):
    def test_levels_match_app(self):
        """和 APP format.ts 的 levelOf 相同：< 1.5、1.5–3、3–10、≥ 10"""
        self.assertEqual([sf.level_of(r) for r in (0.4, 1.49, 1.5, 2.99, 3.0, 9.99, 10.0, 50)], [0, 0, 1, 1, 2, 2, 3, 3])

    def test_summary(self):
        t = pd.Timestamp("2020-01-01")
        df = pd.DataFrame({"issue_time": [t, t, t + pd.Timedelta(days=1), t + pd.Timedelta(days=1)],
                           "county": ["甲", "乙", "甲", "乙"], "p": [0.1] * 4, "p_bg": [0.1] * 4,
                           "ratio": [1.0, 4.0, 0.8, 1.2]})
        s = sf.summarize(df)
        self.assertEqual(s["overall"], [0.75, 0.0, 0.25, 0.0])
        self.assertEqual(s["any_county"], [0.5, 0.0, 0.5, 0.0])   # 第一天最高 4 倍、第二天 1.2 倍
        self.assertEqual(s["per_county_high"], {"乙": 0.5, "甲": 0.0})
        self.assertEqual(s["n_days"], 2)

    def test_outcomes_use_radius_window_and_magnitude(self):
        """之後 3 天內、代表點 50 km 內、M≥5 才算「真的發生」；發布前的地震、太遠、太小、太晚都不算。"""
        t0 = pd.Timestamp("2020-01-01")
        df = pd.DataFrame({"issue_time": [t0] * 4, "county": ["花蓮縣", "臺北市", "高雄市", "臺南市"],
                           "p": [0.1] * 4, "p_bg": [0.1] * 4, "ratio": [1.0] * 4})
        ev = pd.DataFrame({"time": pd.to_datetime(["2020-01-02", "2020-01-02", "2019-12-31", "2020-01-05"]),
                           "lat": [23.99, 25.04 + 0.6, 22.63, 22.99], "lon": [121.6, 121.56, 120.3, 120.21],
                           "ml": [5.2, 6.0, 5.5, 5.1]})
        ev.loc[len(ev)] = [pd.Timestamp("2020-01-02"), 22.99, 120.21, 4.9]
        hit = sf.outcomes(df, ev)
        # 花蓮：1 天後、震央在代表點 → 算；臺北：約 67 km 外 → 不算；高雄：發布前 → 不算；臺南：4 天後、或 M4.9 → 不算
        self.assertEqual(hit.tolist(), [True, False, False, False])

    def test_calibration_realized_ratio(self):
        """實際倍數＝發生比例 ÷ 平常的平均機率；模型倍數 3 而實際也是 3 倍時，兩者一致"""
        days = pd.date_range("2020-01-01", periods=400, freq="D")
        df = pd.DataFrame({"issue_time": np.repeat(days, 2), "county": ["甲", "乙"] * 400,
                           "p": np.tile([0.3, 0.1], 400), "p_bg": [0.1] * 800, "ratio": np.tile([3.0, 1.0], 400)})
        rng = np.random.default_rng(1)
        hit = np.where(df["ratio"] > 2, rng.random(800) < 0.3, rng.random(800) < 0.1)
        rows, overall = sf.calibration(df, hit, n_boot=200)
        high, usual = rows[2], rows[0]
        self.assertEqual((high["n"], usual["n"]), (400, 400))
        self.assertAlmostEqual(high["predicted_ratio"], 3.0)
        self.assertLess(abs(high["realized_ratio"] - 3.0), 0.6)
        self.assertLess(high["lo"], high["realized_ratio"])
        self.assertGreater(high["hi"], high["realized_ratio"])
        self.assertLess(abs(usual["realized_ratio"] - 1.0), 0.35)
        self.assertEqual(rows[1]["n"], 0)

    def test_weights_cover_each_county(self):
        w = sf.county_weights(lu.load_params(BASE))
        self.assertEqual(len(w), 20)
        for name, v in w.items():
            self.assertGreater(v.sum(), 5, name)   # 50 km 的圓約 8 格面積

    def test_one_day_runs_and_ratio_is_now_over_usual(self):
        df = sf.ratios("2024-04-03", "2024-04-04", log=lambda *_: None)
        self.assertEqual(len(df), 40)
        np.testing.assert_allclose(df["ratio"], df["p"] / df["p_bg"])
        hualien = df[df["county"] == "花蓮縣"].set_index("issue_time")["ratio"]
        # 2024 花蓮地震（台灣時間 07:58）後 2 分鐘的 08:00 發布：v1.2 依規模先估計餘震，已經遠高於平常
        # （v1.1 的 alpha＝0 時只有約 1.8 倍）；隔天 08:00：規模加權已衰減，改由實際的餘震撐住，仍明顯高於平常
        self.assertGreater(hualien.iloc[0], 5)
        self.assertGreater(hualien.iloc[1], 3)


if __name__ == "__main__":
    unittest.main()
