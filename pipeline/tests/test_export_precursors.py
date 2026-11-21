"""app/export_precursors.py：地磁前兆的「展示用」輸出。合成資料，不需要 E 槽的真實資料。"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
import export_precursors as ep  # noqa: E402

END = pd.Timestamp("2026-09-30")


def station(code, start, end, seed=0, last_week_shift=0.0, nan_frac=0.1):
    """合成一站每日的三個指標：常態分布，最後 7 天可加一個位移；部分日子缺值（磁暴日）。"""
    rng = np.random.default_rng(seed)
    days = pd.date_range(start, end, freq="D")
    df = pd.DataFrame({"date": days, "station": code, "lat": 24.0, "lon": 121.0})
    for c in ep.INDICES:
        v = rng.normal(0, 1, len(days))
        v[-7:] += last_week_shift
        v[rng.random(len(days)) < nan_frac] = np.nan
        df[c] = v
    return df


def anomalies():
    return pd.concat([
        station("AAA", "2020-01-01", END, seed=1),
        station("BBB", "2024-06-01", END, seed=2, last_week_shift=6.0),   # 最近一週明顯偏高
        station("OLD", "2019-01-01", "2020-05-01", seed=3),               # 早已中斷
        station("NEW", "2026-08-01", END, seed=4),                        # 歷史太短，不給百分位
    ], ignore_index=True)


BACKTEST = {"report": "x.md", "primary_test": "t", "test_gain_nat": -0.02, "p_value": 0.46, "n_test_events": 100}


class BuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = ep.build_precursors(anomalies(), pd.Timestamp("2026-11-21 08:00"), BACKTEST)
        cls.by = {s["code"]: s for s in cls.out["stations"]}

    def test_header_marks_display_only_and_reports_staleness(self):
        o = self.out
        self.assertEqual((o["kind"], o["in_probability"]), ("display_only", False))
        self.assertEqual(o["data_end"], "2026-09-30")
        self.assertEqual(o["lag_days"], 52)
        self.assertTrue(o["stale"])
        self.assertIn("未計入任何機率", o["disclaimer"])
        self.assertEqual(o["backtest"]["p_value"], 0.46)
        self.assertEqual(o["generated_at"], "2026-11-21T08:00:00Z")

    def test_fresh_data_is_not_marked_stale(self):
        o = ep.build_precursors(anomalies(), pd.Timestamp("2026-10-05"), BACKTEST)
        self.assertEqual((o["lag_days"], o["stale"]), (5, False))

    def test_active_and_inactive_stations(self):
        self.assertTrue(self.by["AAA"]["active"] and self.by["BBB"]["active"] and self.by["NEW"]["active"])
        self.assertFalse(self.by["OLD"]["active"])
        self.assertNotIn("indices", self.by["OLD"])                     # 中斷的站不給「最近 30 天」
        self.assertEqual(self.by["OLD"]["last_date"], "2020-05-01")
        self.assertEqual([s["active"] for s in self.out["stations"]], [True, True, True, False])   # 運作中的排前面

    def test_series_has_thirty_days_with_missing_as_null(self):
        idx = self.by["AAA"]["indices"]["mag_sznet_z"]
        self.assertEqual(len(idx["series_30d"]), ep.SERIES_DAYS)
        self.assertEqual(idx["n_valid_30d"], sum(v is not None for v in idx["series_30d"]))
        self.assertTrue(any(v is None for v in idx["series_30d"]))      # 磁暴日缺值要保留成 null，不補 0
        self.assertEqual(self.by["AAA"]["series_start"], "2026-09-01")

    def test_rank_is_a_position_not_a_probability(self):
        for s in self.out["stations"]:
            for idx in (s.get("indices") or {}).values():
                if idx["rank_pct"] is not None:
                    self.assertTrue(0.0 <= idx["rank_pct"] <= 100.0)
        high = self.by["BBB"]["indices"]["mag_sznet_z"]
        self.assertGreater(high["rank_pct"], 95.0)                       # 最近一週偏高 → 位置在歷史上段
        mid = self.by["AAA"]["indices"]["mag_sznet_z"]["rank_pct"]
        self.assertTrue(5.0 < mid < 95.0)                                # 沒有位移的站不會落在極端

    def test_short_history_gets_no_percentile(self):
        idx = self.by["NEW"]["indices"]["mag_sznet_z"]
        self.assertIsNone(idx["rank_pct"])
        self.assertLess(idx["n_hist"], ep.MIN_HISTORY)

    def test_storm_days_stay_missing_and_do_not_count_as_zero(self):
        df = anomalies()
        df.loc[(df["station"] == "AAA") & (df["date"] > END - pd.Timedelta(days=7)), "mag_sznet_z"] = np.nan
        o = ep.build_precursors(df, pd.Timestamp("2026-11-21"), BACKTEST)
        a = {s["code"]: s for s in o["stations"]}["AAA"]["indices"]["mag_sznet_z"]
        self.assertIsNone(a["mean7"])                                    # 最近 7 天全缺 → 沒有 7 天平均
        self.assertIsNone(a["rank_pct"])


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.out = ep.build_precursors(anomalies(), pd.Timestamp("2026-11-21"), BACKTEST)

    def test_valid_output_passes_and_is_small(self):
        size = ep.check(copy.deepcopy(self.out))
        self.assertLess(size, ep.MAX_BYTES)

    def test_refuses_to_claim_probability_or_break_shape(self):
        bad = copy.deepcopy(self.out)
        bad["in_probability"] = True
        with self.assertRaisesRegex(ValueError, "display_only"):
            ep.check(bad)
        bad = copy.deepcopy(self.out)
        bad["stations"][0]["indices"]["mag_pol_z"]["series_30d"].pop()
        with self.assertRaisesRegex(ValueError, "序列長度"):
            ep.check(bad)
        bad = copy.deepcopy(self.out)
        bad["stations"][0]["indices"]["mag_pol_z"]["rank_pct"] = 120.0
        with self.assertRaisesRegex(ValueError, "百分位"):
            ep.check(bad)

    def test_json_roundtrip_keeps_nulls(self):
        s = json.dumps(self.out, ensure_ascii=False, allow_nan=False)    # 不得有 NaN：手機端的 JSON.parse 讀不了
        self.assertEqual(json.loads(s)["data_end"], "2026-09-30")


class BacktestReaderTest(unittest.TestCase):
    ROW = ("| M≥3.5 | 全部 6 個特徵 | 1833 | 2461 | -0.0201 | +0.0364 | -0.0244 | +0.0687 | 0.458 |\n"
           "| M≥4 | 全部 6 個特徵 | 710 | 1110 | -0.2326 | +0.0800 | -0.0396 | +0.0911 | 0.935 |\n")

    def read(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.md"
            p.write_text(text, encoding="utf-8")
            return ep.read_backtest(p)

    def test_reads_the_primary_row_only(self):
        r = self.read(self.ROW)
        self.assertEqual((r["test_gain_nat"], r["p_value"], r["n_test_events"]), (-0.0201, 0.458, 2461))

    def test_reads_unicode_minus_and_positive_gain(self):
        r = self.read("| M≥3.5 | 全部 6 個特徵 | 1833 | 2461 | −0.0201 | +0.0364 | -0.0244 | +0.0687 | 0.458 |\n")
        self.assertEqual(r["test_gain_nat"], -0.0201)
        r = self.read("| M≥3.5 | 全部 6 個特徵 | 10 | 20 | +0.0500 | +0.0364 | -0.0244 | +0.0687 | 0.040 |\n")
        self.assertEqual((r["test_gain_nat"], r["p_value"]), (0.05, 0.04))

    def test_unreadable_report_gives_nulls_and_a_note(self):
        self.assertIsNone(self.read("沒有表格")["test_gain_nat"])
        self.assertIsNone(ep.read_backtest(Path("不存在.md"))["p_value"])
        o = ep.build_precursors(anomalies(), pd.Timestamp("2026-11-21"), self.read("沒有表格"))
        self.assertTrue(any("無法從回測報告" in n for n in o["notes"]))

    def test_real_report_is_parseable(self):
        r = ep.read_backtest()
        self.assertIsNotNone(r["test_gain_nat"], "報告格式變了：export_precursors.read_backtest 的 regex 要跟著改")
        self.assertTrue(0.0 <= r["p_value"] <= 1.0)


if __name__ == "__main__":
    unittest.main()
