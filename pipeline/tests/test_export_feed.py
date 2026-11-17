"""app/export_feed.py：快照 → 手機 PWA 的 feed.json（格式見 earthquake-risk-app/src/data/schema.ts）。不連網。"""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
import export_feed as ef  # noqa: E402
import hazard_core as hc  # noqa: E402
import live_update as lu  # noqa: E402

BASE = ROOT / "app" / "data"


class ExportFeedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = hc.load_snapshot(BASE)
        cls.feed = ef.build_feed(cls.snap)

    def test_shape_matches_schema_and_passes_check(self):
        f = ef.check_feed(copy.deepcopy(self.feed))
        n, h = len(f["cells"]["lat"]), len(f["horizons_days"])
        self.assertEqual(n, len(self.snap.cells))
        self.assertEqual(len(f["n_full"]), n)
        self.assertTrue(all(len(r) == h for r in f["n_bg"]))
        self.assertFalse(f["mock"])
        self.assertTrue(f["issued_at"].endswith("Z"))
        self.assertTrue(set(e["source"] for e in f["recent"]) <= {"GDMS", "CWA", "USGS"})

    def test_normal_uses_background_rescale_and_matches_core(self):
        """feed 的「平常」要和計算核心（hazard_core）用的同一份數字，手機端才算得出一樣的倍數。"""
        self.assertAlmostEqual(self.feed["n_bg"][123][4], float(f"{self.snap.n_bg[123][4]:.6g}"))
        self.assertAlmostEqual(self.feed["n_full"][77][9], float(f"{self.snap.n_ref[77][9]:.6g}"))

    def test_check_rejects_bad_feeds(self):
        bad = copy.deepcopy(self.feed)
        bad["recent"].append({"time": "2026-11-17T00:00:00Z", "lat": 23.0, "lon": 121.0, "depth_km": 10.0, "ml": 4.0,
                              "source": "<script>"})
        self.assertRaises(ValueError, ef.check_feed, bad)
        bad = copy.deepcopy(self.feed)
        bad["n_full"][0][0] = float("nan")
        self.assertRaises(ValueError, ef.check_feed, bad)
        bad = copy.deepcopy(self.feed)
        bad["n_bg"].pop()
        self.assertRaises(ValueError, ef.check_feed, bad)

    def test_live_update_output_converts_gap_ratio_and_sources(self):
        prov = pd.DataFrame({"time": ["2026-11-10 01:00:00"], "lat": [24.0], "lon": [121.6], "depth": [12.0],
                             "ml": [4.6], "source": ["USGS"], "source_id": ["us1"],
                             "fetched_at": ["2026-11-10 02:00:00"]})
        with tempfile.TemporaryDirectory() as d:
            live = Path(d)
            prov.to_csv(live / "provisional_events.csv", index=False)
            status = lu.update(now="2026-11-12 03:30:00", base_dir=BASE, live_dir=live, network=False, log=lambda *_: None)
            f = ef.check_feed(ef.build_feed(hc.load_snapshot(live)))
        self.assertEqual(f["issued_at"], "2026-11-12T03:00:00Z")
        self.assertEqual(f["data_status"]["n_provisional"], 1)
        self.assertIn(ef.SOURCE_NAMES["USGS"], f["data_status"]["sources"])
        self.assertEqual([e["source"] for e in f["recent"][:1]], ["USGS"])   # 最新的一筆是暫時資料
        k7 = f"{7.0:g}"
        self.assertAlmostEqual(f["data_status"]["undercount_by_horizon"][k7], 1 - status["gap_ratio"]["7.00000"], places=4)


if __name__ == "__main__":
    unittest.main()
