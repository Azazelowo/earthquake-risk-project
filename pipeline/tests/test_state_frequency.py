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
        # 2024 花蓮地震（台灣時間 07:58）後 2 分鐘的 08:00 發布：只升高一點（alpha＝0，餘震還沒進來）；
        # 隔天 08:00：餘震陸續算進來，明顯高於平常
        self.assertGreater(hualien.iloc[0], 1.0)
        self.assertGreater(hualien.iloc[1], 3)
        self.assertGreater(hualien.iloc[1], hualien.iloc[0])


if __name__ == "__main__":
    unittest.main()
