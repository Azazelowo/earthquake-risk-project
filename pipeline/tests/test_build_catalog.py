"""01_build_catalog 跨來源比對的單元測試（合成資料，不依賴本機目錄檔）。

執行：python -m unittest discover -s pipeline/tests -v
"""
import importlib.util
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

_SRC = Path(__file__).resolve().parents[1] / "src" / "01_build_catalog.py"
_spec = importlib.util.spec_from_file_location("build_catalog", _SRC)
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)


def ev(source, t, lat, lon, depth=10.0, ml=3.0, gap=50.0):
    return {"time": pd.Timestamp(t), "lat": lat, "lon": lon, "depth": depth, "ml": ml,
            "gap": gap, "source": source, "event_id": None}


class TestMatchCrossSource(unittest.TestCase):
    def setUp(self):
        rows = [
            # 1. CWA XML 是 GDMS 四捨五入到整秒、座標取到小數三位 → high
            ev("GDMS", "2020-01-01 00:00:00.40", 24.12345, 121.54321),
            ev("CWA_XML", "2020-01-01 00:00:00", 24.123, 121.543),
            # 2. 時間差 1.5 秒、距離約 5.6 km → medium
            ev("GDMS", "2020-01-02 00:00:00.00", 23.0, 121.0),
            ev("CWA_XML", "2020-01-02 00:00:01.50", 23.05, 121.0),
            # 3. 距離約 33 km（舊版 0.3° 方框會配對）→ 不配對
            ev("GDMS", "2020-01-03 00:00:00.00", 22.0, 120.5),
            ev("CWA_XML", "2020-01-03 00:00:00.00", 22.3, 120.5),
            # 4. 兩筆 CWA 搶同一筆 GDMS → 只有較近的那筆配到（一對一）
            ev("GDMS", "2020-01-04 00:00:00.00", 24.0, 121.0),
            ev("CWA_XML", "2020-01-04 00:00:00.00", 24.0, 121.0),
            ev("CWA_XML", "2020-01-04 00:00:00.50", 24.003, 121.0),
            # 5. 只有 GDMS
            ev("GDMS", "2020-01-05 00:00:00.00", 21.5, 120.0),
        ]
        self.df = pd.DataFrame(rows)
        self.events, self.matches, self.pairs = bc.build_event_matches(
            self.df.copy(), pd.Timestamp("2026-11-05 00:00:00"))

    def test_confidence_levels(self):
        self.assertEqual(sorted(self.pairs["confidence"]), ["high", "high", "medium"])

    def test_far_pair_not_matched(self):
        far = self.matches[self.matches["source_event_id"].str.contains("22.3000")]
        self.assertTrue(far["confidence"].isna().all())

    def test_one_to_one(self):
        # 第 4 組：兩筆 CWA 中只有一筆配到
        jan4 = self.matches[self.matches["source_event_id"].str.startswith("20200104")]
        self.assertEqual(int(jan4["confidence"].notna().sum()), 2)   # GDMS 1 筆 + CWA 1 筆
        self.assertEqual(self.pairs["ri"].nunique(), len(self.pairs))
        self.assertEqual(self.pairs["qi"].nunique(), len(self.pairs))

    def test_events_keep_gdms_and_unmatched(self):
        # 10 筆原始紀錄，3 組配對 → events 剩 7 筆；配對成功的都保留 GDMS
        self.assertEqual(len(self.events), 7)
        self.assertEqual(len(self.matches), 10)
        cwa_left = self.events[self.events["source"] == "CWA_XML"]
        self.assertEqual(len(cwa_left), 2)   # 太遠的那筆＋搶輸的那筆
        self.assertEqual(self.events["match_group_id"].nunique(), len(self.events))

    def test_group_and_pair_direction(self):
        m = self.matches.set_index("source_event_id")
        g = m[m["source"] == "GDMS"].iloc[0]
        c = m.loc[g["pair_with"].split(":", 1)[1]]
        self.assertEqual(g["match_group_id"], c["match_group_id"])
        self.assertTrue(g["match_group_id"].startswith("EMG_20200101000000_"))
        # dt_s 是「對方 − 本筆」，兩邊正負相反
        self.assertAlmostEqual(g["dt_s"], -c["dt_s"])
        self.assertAlmostEqual(c["dt_s"], 0.4, places=6)
        self.assertEqual((m["matcher_version"] == bc.MATCHER_VERSION).sum(), len(m))

    def test_event_ids_unique_and_stable(self):
        # 鍵是 (source, source_event_id)：不同來源可能剛好組出相同 ID（第 4 組就是）
        self.assertFalse(self.matches.duplicated(["source", "source_event_id"]).any())
        # 24.12345 以浮點數格式化到小數四位是 24.1234（二進位表示略小於 .5）
        self.assertIn("20200101T000000.40_24.1234_121.5432", set(self.matches["source_event_id"]))

    def test_time_unit_agnostic(self):
        """pandas 3 可能用 [s]／[ms]／[us]；不同單位不可影響時間差。"""
        t = pd.Series(pd.to_datetime(["2020-01-01 00:00:01"])).astype("datetime64[s]")
        u = pd.Series(pd.to_datetime(["2020-01-01 00:00:00.5"])).astype("datetime64[us]")
        self.assertAlmostEqual(float(bc.epoch_s(t)[0] - bc.epoch_s(u)[0]), 0.5)

    def test_no_candidates(self):
        q = pd.DataFrame([ev("CWA_XML", "2020-01-01", 24.0, 121.0)])
        r = pd.DataFrame([ev("GDMS", "2021-01-01", 24.0, 121.0)])
        self.assertEqual(len(bc.match_cross_source(q, r)), 0)


if __name__ == "__main__":
    unittest.main()
