"""即時更新（app/live_update.py）的測試：解析、累積、去重、重算重現性；不連網（用假的抓取函式）。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
import hazard_core as hc  # noqa: E402
import live_update as lu  # noqa: E402

BASE = ROOT / "app" / "data"

CWA_JSON = json.dumps({"success": "true", "records": {"Earthquake": [
    {"EarthquakeNo": 115001, "EarthquakeInfo": {
        "OriginTime": "2026-11-13T09:55:55+08:00", "FocalDepth": 14.6,
        "Epicenter": {"EpicenterLatitude": 24.02, "EpicenterLongitude": 121.51},
        "EarthquakeMagnitude": {"MagnitudeValue": 3.8}}},
    {"EarthquakeNo": 115002, "EarthquakeInfo": {"OriginTime": "壞掉的時間"}},
]}})

USGS_CSV = ("time,latitude,longitude,depth,mag,magType,id\n"
            "2026-11-13T01:55:58.000Z,24.05,121.55,15,4.1,mb,us1\n"
            "2026-11-01T00:00:00.000Z,22.50,121.80,30,4.6,mb,us2\n")


class TestParsing(unittest.TestCase):
    def test_cwa_time_converted_to_utc_and_bad_rows_skipped(self):
        df = lu.parse_cwa_json(CWA_JSON, "E-A0016-001")
        self.assertEqual(len(df), 1)
        self.assertEqual(df.loc[0, "time"], pd.Timestamp("2026-11-13 01:55:55"))
        self.assertEqual(df.loc[0, "source_id"], "E-A0016-001:115001")
        self.assertAlmostEqual(df.loc[0, "ml"], 3.8)

    def test_usgs_csv(self):
        df = lu.parse_usgs_csv(USGS_CSV)
        self.assertEqual(list(df["source_id"]), ["us1", "us2"])
        self.assertIsNone(df["time"].dt.tz)
        self.assertTrue(lu.parse_usgs_csv("").empty)

    def test_accumulate_keeps_old_reports_and_latest_version(self):
        a = lu.parse_cwa_json(CWA_JSON, "X").assign(fetched_at=pd.Timestamp("2026-11-14"))
        b = a.assign(ml=3.9, fetched_at=pd.Timestamp("2026-11-15"))
        other = a.assign(source_id="X:999", time=pd.Timestamp("2026-10-01"))
        store = lu.accumulate(lu.accumulate(a, other), b)
        self.assertEqual(len(store), 2)                     # 舊報告保留（API 只留最新 16 筆）
        self.assertAlmostEqual(store.loc[store["source_id"] == "X:115001", "ml"].item(), 3.9)

    def test_merge_prefers_cwa_for_duplicates(self):
        cwa = lu.parse_cwa_json(CWA_JSON, "X")
        usgs = lu.parse_usgs_csv(USGS_CSV)
        merged = lu.merge_sources(pd.concat([cwa, usgs], ignore_index=True))
        self.assertEqual(sorted(merged["source_id"]), ["X:115001", "us2"])  # us1 與 CWA 相差 3 秒、約 5 km


class TestRecompute(unittest.TestCase):
    def test_base_snapshot_is_reproduced(self):
        p = lu.load_params(BASE)
        out = lu.compute_hazard(lu.read_base_catalog(BASE), p, pd.Timestamp(p["issue_time"]))
        ref = pd.read_csv(BASE / "hazard_latest.csv")
        cols = [c for c in ref.columns if c.startswith("n_")]
        np.testing.assert_allclose(out[cols].to_numpy(), ref[cols].to_numpy(), rtol=1e-4)

    def test_normal_is_background_only_with_its_own_factor(self):
        p = lu.load_params(BASE)
        self.assertIn("calibration_factors_bg", p, "app/data 要是新版快照（scripts/refresh_bg_calibration.py）")
        t0 = pd.Timestamp(p["issue_time"])
        out = lu.compute_hazard(lu.read_base_catalog(BASE), p, t0)
        prm, grid = lu.hazard_params(p), lu.grid_of(p)
        cat = lu.hz.Catalog(lu.read_base_catalog(BASE), grid, prm.m_ref)
        bgp = lu.hz.bg_only_params(prm, p["fit_info"]["mu_bg_only"])
        a_bg, _ = lu.hz.expected_ref(cat, t0, 7.0, bgp)
        want = a_bg.ravel() * p["calibration_factors_bg"]["7.00000"]
        np.testing.assert_allclose(out["n_bg_7.00000d"].to_numpy(), want, rtol=1e-12)

    def test_normal_total_is_close_to_recent_average_rate(self):
        """「平常」的意思是近兩年同長度窗的平均水準。舊算法（用完整模型的倍率）會系統性低估約 1.5–1.8 倍。"""
        p = lu.load_params(BASE)
        t0 = pd.Timestamp(p["issue_time"])
        prm, grid = lu.hazard_params(p), lu.grid_of(p)
        cat = lu.hz.Catalog(lu.read_base_catalog(BASE), grid, prm.m_ref)
        h = np.timedelta64(7, "D")
        t0n = np.datetime64(t0, "us")
        obs = np.mean([float(cat.counts(t, t + h).sum())
                       for t in (t0n - h - np.timedelta64(7 * k, "D") for k in range(104))])
        ref = pd.read_csv(BASE / "hazard_latest.csv")
        self.assertAlmostEqual(float(ref["n_bg_7.00000d"].sum()) / obs, 1.0, delta=0.2)
        legacy = float(ref["n_ref_7.00000d"].sum())          # 完整模型現在（偏安靜）比近兩年平均低，但不該像舊「平常」那樣低到 0.6
        self.assertGreater(float(ref["n_bg_7.00000d"].sum()), legacy)

    def test_update_offline_with_fake_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "live"
            calls = {}

            def fake_usgs(start, end, grid):
                calls["usgs"] = (start, end)
                return lu.parse_usgs_csv(USGS_CSV)

            def fake_cwa(key):
                calls["cwa"] = key
                return lu.parse_cwa_json(CWA_JSON, "X")

            status = lu.update(now="2026-11-15 04:20", base_dir=BASE, live_dir=live, cwa_key="k", log=lambda *_: None,
                               fetch_usgs_fn=fake_usgs, fetch_cwa_fn=fake_cwa)
            self.assertEqual(status["issue_time"], "2026-11-15 04:15:00")  # 取到 15 分鐘
            self.assertEqual(status["n_provisional_by_source"], {"CWA": 1, "USGS": 1})
            self.assertFalse(status["errors"])
            self.assertEqual(calls["cwa"], "k")
            g = status["gap_ratio"]
            self.assertTrue(all(0 < v <= 1 for v in g.values()))
            # APP 會選即時版，而且讀得進來
            self.assertEqual(hc.pick_data_dir(BASE, live), live)
            snap = hc.load_snapshot(live)
            self.assertEqual(snap.issue_time, pd.Timestamp("2026-11-15 04:15"))
            self.assertTrue((snap.events["source"] == "USGS（暫時）").any())
            self.assertIsNotNone(hc.gap_ratio_at(snap.params, 7))
            # 抓取紀錄：一次成功的抓取一列，CWA 單一資料集回了 1 筆
            log = lu.read_fetch_log(live / "fetch_log.csv")
            self.assertEqual(len(log), 1)
            self.assertTrue(bool(log["usgs_ok"].iloc[0]) and bool(log["cwa_ok"].iloc[0]))
            self.assertEqual(int(log["cwa_max_n"].iloc[0]), 1)
            self.assertEqual(log["fetched_at"].iloc[0], pd.Timestamp("2026-11-15 04:20:00"))
            # 再跑一次（離線）不會重複累積，也不會多記一次抓取
            lu.update(now="2026-11-15 04:20", base_dir=BASE, live_dir=live, network=False, log=lambda *_: None)
            self.assertEqual(len(lu.read_store(live / "provisional_events.csv")), 3)
            self.assertEqual(len(lu.read_fetch_log(live / "fetch_log.csv")), 1)

    def test_network_failure_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            def boom(*a, **k):
                raise OSError("離線")
            status = lu.update(now="2026-11-15", base_dir=BASE, live_dir=Path(tmp), cwa_key="", log=lambda *_: None,
                               fetch_usgs_fn=boom, fetch_cwa_fn=boom)
            self.assertTrue(any("USGS" in e for e in status["errors"]))
            self.assertTrue(any("CWA_API_KEY" in e for e in status["errors"]))
            log = lu.read_fetch_log(Path(tmp) / "fetch_log.csv")                 # 失敗的抓取也要記，才看得出缺口
            self.assertEqual(len(log), 1)
            self.assertFalse(bool(log["usgs_ok"].iloc[0]) or bool(log["cwa_ok"].iloc[0]))
            self.assertIsNone(lu.read_fetch_log(Path(tmp) / "nope.csv"))

    def test_base_is_used_when_no_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(hc.pick_data_dir(BASE, Path(tmp)), BASE)


if __name__ == "__main__":
    unittest.main()
