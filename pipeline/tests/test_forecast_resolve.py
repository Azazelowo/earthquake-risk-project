"""app/forecast_resolve.py：裁決（速判／定判）、計分、模型狀態。合成目錄與合成裁決結果，不連網。"""
import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
import forecast_card as fc  # noqa: E402
import forecast_resolve as fr  # noqa: E402
import hazard_core as hc  # noqa: E402

BASE = ROOT / "app" / "data"
KHH = (22.63, 120.30)
KM_PER_DEG = 111.195


def events(rows):
    return pd.DataFrame(rows, columns=["time", "lat", "lon", "ml"]).assign(time=lambda d: pd.to_datetime(d["time"]))


def shifted(snap, days):
    t = (snap.issue_time + np.timedelta64(days, "D")).strftime("%Y-%m-%d %H:%M:%S")
    return dataclasses.replace(snap, params={**snap.params, "issue_time": t})


class ResolveCardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        snap = hc.load_snapshot(BASE)
        uniform, _ = fc.uniform_per_cell(BASE, snap, (7,))
        cls.header, cards = fc.build_batch(shifted(snap, -10), uniform, regions=fc.STANDARD_REGIONS[:2],
                                           mags=(4.0, 5.0), windows=(7,))
        cls.by = {(c["r"], c["m"]): c for c in cards}
        cls.t0 = pd.Timestamp(cls.header["issued_at"]).tz_localize(None)          # 2026-09-20 00:00
        cls.card = cls.by[("KHH", 5.0)]

    def r(self, rows, card=None):
        return fr.resolve_card(card or self.card, self.header, events(rows))

    def test_window_is_left_closed_right_open(self):
        end = self.t0 + pd.Timedelta(days=7)
        self.assertEqual(self.r([(self.t0, *KHH, 5.5)])["n"], 1)                  # 剛好在發布時間：算
        self.assertEqual(self.r([(end, *KHH, 5.5)])["n"], 0)                      # 剛好在結束時間：不算
        self.assertEqual(self.r([(self.t0 - pd.Timedelta(seconds=1), *KHH, 5.5)])["n"], 0)

    def test_magnitude_threshold_is_inclusive(self):
        t = self.t0 + pd.Timedelta(days=1)
        self.assertEqual(self.r([(t, *KHH, 5.0)])["y"], 1)
        self.assertEqual(self.r([(t, *KHH, 4.99)])["y"], 0)

    def test_radius_boundary(self):
        t = self.t0 + pd.Timedelta(days=1)
        inside = (t, KHH[0] + 48.9 / KM_PER_DEG, KHH[1], 5.5)
        outside = (t, KHH[0] + 51.2 / KM_PER_DEG, KHH[1], 5.5)
        self.assertEqual(self.r([inside])["n"], 1)
        self.assertEqual(self.r([outside])["n"], 0)

    def test_whole_grid_region_covers_everywhere_inside_the_grid_only(self):
        t = self.t0 + pd.Timedelta(days=1)
        tw = self.by[("TW", 5.0)]
        self.assertEqual(self.r([(t, 24.8, 121.9, 5.5)], tw)["n"], 1)             # 圓外、網格內
        self.assertEqual(self.r([(t, 27.0, 121.9, 5.5)], tw)["n"], 0)             # 網格外
        self.assertEqual(self.r([(t, 24.8, 121.9, 5.5)], self.card)["n"], 0)      # 高雄的圓不含它

    def test_reports_count_and_largest_events(self):
        t = self.t0 + pd.Timedelta(days=2)
        out = self.r([(t, *KHH, 5.2), (t + pd.Timedelta(hours=1), *KHH, 6.1), (t + pd.Timedelta(hours=2), *KHH, 5.0)])
        self.assertEqual((out["y"], out["n"], out["max_ml"]), (1, 3, 6.1))
        self.assertEqual(out["events"][0]["ml"], 6.1)


class ResolvePendingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = hc.load_snapshot(BASE)
        cls.uniform, _ = fc.uniform_per_cell(BASE, cls.snap, (1, 7))
        cls.small = dict(regions=fc.STANDARD_REGIONS[1:2], mags=(5.0,), windows=(1, 7))

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.reg, self.res = Path(self._tmp.name) / "reg", Path(self._tmp.name) / "res"
        h, c = fc.build_batch(shifted(self.snap, -10), self.uniform, **self.small)     # 2026-09-20
        fc.append_batch(self.reg, h, c)
        self.t0 = pd.Timestamp(h["issued_at"]).tz_localize(None)
        self.ev = events([(self.t0 + pd.Timedelta(hours=5), *KHH, 5.5)])

    def tearDown(self):
        self._tmp.cleanup()

    def run_stage(self, stage, now, covered, **kw):
        return fr.resolve_pending(self.reg, self.res, stage, self.ev, covered, now, **kw)

    def test_final_needs_catalog_to_cover_window_end(self):
        n = self.run_stage(fr.STAGE_FINAL, self.t0 + pd.Timedelta(days=60), self.t0 + pd.Timedelta(days=3))
        self.assertEqual(n, 1)                                                      # 只有 1 天窗涵蓋到
        n = self.run_stage(fr.STAGE_FINAL, self.t0 + pd.Timedelta(days=61), self.t0 + pd.Timedelta(days=8))
        self.assertEqual(n, 1)                                                      # 7 天窗補上
        self.assertEqual(self.run_stage(fr.STAGE_FINAL, self.t0 + pd.Timedelta(days=62), self.t0 + pd.Timedelta(days=9)), 0)

    def test_speed_waits_one_day_after_window_end(self):
        covered = self.t0 + pd.Timedelta(days=30)
        fetched = pd.Series(pd.date_range(self.t0, self.t0 + pd.Timedelta(days=30), freq="1h"))
        n = self.run_stage(fr.STAGE_SPEED, self.t0 + pd.Timedelta(days=1, hours=23), covered, fetched_at=fetched)
        self.assertEqual(n, 0)                                                      # 1 天窗要到 T+1 天（第 2 天）
        n = self.run_stage(fr.STAGE_SPEED, self.t0 + pd.Timedelta(days=2), covered, fetched_at=fetched)
        self.assertEqual(n, 1)

    def test_each_card_and_stage_is_resolved_only_once_and_results_chain_verifies(self):
        now = self.t0 + pd.Timedelta(days=60)
        covered = self.t0 + pd.Timedelta(days=40)
        self.assertEqual(self.run_stage(fr.STAGE_FINAL, now, covered), 2)
        self.assertEqual(self.run_stage(fr.STAGE_FINAL, now + pd.Timedelta(days=1), covered), 0)
        stats = fc.verify_registry(self.res)
        self.assertEqual((stats["batches"], stats["cards"]), (1, 2))
        # 同一張卡的速判與定判是兩筆不同的紀錄
        fetched = pd.Series(pd.date_range(self.t0, self.t0 + pd.Timedelta(days=30), freq="1h"))
        self.assertEqual(self.run_stage(fr.STAGE_SPEED, now + pd.Timedelta(days=2), covered, fetched_at=fetched), 2)
        self.assertEqual(fc.verify_registry(self.res)["cards"], 4)

    def test_speed_flags_long_fetch_gaps_as_possibly_missing(self):
        covered = self.t0 + pd.Timedelta(days=30)
        hours = pd.date_range(self.t0, self.t0 + pd.Timedelta(days=30), freq="1h")
        gap = hours[(hours < self.t0 + pd.Timedelta(hours=6)) | (hours > self.t0 + pd.Timedelta(hours=12))]   # 留下 5 點與 13 點，中間 8 小時沒抓
        self.run_stage(fr.STAGE_SPEED, self.t0 + pd.Timedelta(days=9), covered, fetched_at=pd.Series(gap))
        out = fr.load_resolved(self.reg, self.res, fr.STAGE_SPEED)
        self.assertTrue(out["maybe_missing"].all())                                 # 兩個窗都包含那段空白
        self.assertAlmostEqual(fr.fetch_gap_hours(pd.Series(gap), self.t0, self.t0 + pd.Timedelta(days=1)), 8.0, places=6)
        self.assertLess(fr.fetch_gap_hours(pd.Series(hours), self.t0, self.t0 + pd.Timedelta(days=1)), 1.01)

    def test_results_do_not_modify_the_registry(self):
        before = fc.verify_registry(self.reg)
        self.run_stage(fr.STAGE_FINAL, self.t0 + pd.Timedelta(days=60), self.t0 + pd.Timedelta(days=40))
        self.assertEqual(fc.verify_registry(self.reg), before)

    def test_load_resolved_joins_cards_and_outcomes(self):
        self.run_stage(fr.STAGE_FINAL, self.t0 + pd.Timedelta(days=60), self.t0 + pd.Timedelta(days=40))
        d = fr.load_resolved(self.reg, self.res, fr.STAGE_FINAL)
        self.assertEqual(len(d), 2)
        one = d[d["T"] == 1].iloc[0]
        self.assertEqual((one["y"], one["n_events"]), (1, 1))                       # 5 小時後的 M5.5 在 1 天窗內
        self.assertEqual(one["set"], "standard")
        self.assertEqual(len(fr.load_resolved(self.reg, self.res, fr.STAGE_SPEED)), 0)


class ScoringPrimitivesTest(unittest.TestCase):
    def test_n_test_flags_counts_outside_the_poisson_band(self):
        self.assertFalse(fr.n_test(10, 10.0)["exceed"])
        self.assertTrue(fr.n_test(25, 10.0)["exceed"])
        self.assertTrue(fr.n_test(0, 10.0)["exceed"])
        self.assertIsNone(fr.n_test(0, 0.0)["exceed"])

    def test_information_gain(self):
        obs = np.array([3.0, 0.0, 1.0, 0.0])
        flat = np.full(4, 1.0)
        self.assertAlmostEqual(fr.information_gain(obs, flat, flat), 0.0)
        better = np.array([3.0, 0.2, 1.0, 0.2])
        self.assertGreater(fr.information_gain(obs, better, flat), 0.0)
        self.assertLess(fr.information_gain(obs, flat, better), 0.0)
        self.assertIsNone(fr.information_gain(np.zeros(4), better, flat))

    def test_brier_and_reliability_sample_rule(self):
        self.assertAlmostEqual(fr.brier([0.0, 1.0], [0, 1]), 0.0)
        self.assertAlmostEqual(fr.brier([0.5, 0.5], [0, 1]), 0.25)
        rng = np.random.default_rng(1)
        p = np.r_[np.full(500, 0.05), np.full(30, 0.4)]
        y = np.r_[rng.random(500) < 0.05, rng.random(30) < 0.4].astype(int)
        tab = fr.reliability_table(p, y)
        self.assertEqual(len(tab), 2)
        by = {round(r["mean_p"], 2): r for r in tab}
        self.assertTrue(by[0.05]["enough"] is (by[0.05]["n_hit"] >= fr.MIN_EVENTS))
        self.assertFalse(by[0.4]["enough"])                                         # 發生的卡不到 20 張

    def test_non_overlapping_picks_one_window_every_T_days(self):
        t0 = pd.Timestamp("2026-10-01")
        df = pd.DataFrame({"issued_at": [t0 + pd.Timedelta(days=k) for k in range(30)]})
        df["window_end"] = df["issued_at"] + pd.Timedelta(days=7)
        keep = fr.non_overlapping(df)
        self.assertEqual(list(keep["issued_at"].dt.day), [1, 8, 15, 22, 29])
        self.assertTrue((keep["issued_at"].iloc[1:].to_numpy() >= keep["window_end"].iloc[:-1].to_numpy()).all())


def synth(n_days, true_scale=1.0, base_rate=0.5, seed=0, r="TW", m=4.0, T=1, set_name="standard", version="v1.1"):
    """每天一張 1 天窗的卡。模型的期望數隨日子起伏，「平常」是定值；實際地震數依 true_scale × 模型抽出。"""
    rng = np.random.default_rng(seed)
    t0 = pd.Timestamp("2026-06-01")
    lam = base_rate * np.exp(rng.normal(0, 0.8, n_days))
    lam *= base_rate / lam.mean()                                                     # 模型期望數平均 = 平常
    k = rng.poisson(lam * true_scale)
    issued = [t0 + pd.Timedelta(days=i) for i in range(n_days)]
    return pd.DataFrame({
        "r": r, "m": m, "T": T, "set": set_name, "model_version": version, "params_hash": "h", "stage": "final",
        "issued_at": issued, "window_end": [t + pd.Timedelta(days=T) for t in issued],
        "n": lam, "nb": np.full(n_days, base_rate), "nu": np.full(n_days, base_rate),
        "p": -np.expm1(-lam), "pb": -np.expm1(-np.full(n_days, base_rate)), "pu": -np.expm1(-np.full(n_days, base_rate)),
        "y": (k > 0).astype(int), "n_events": k, "maybe_missing": False})


class StatusTest(unittest.TestCase):
    def ev(self, n_events, exceed, gain=0.1):
        return {"n_events": n_events, "gain_vs_bg": gain, "n_test": {"exceed": exceed}}

    def test_classify_follows_the_spec_table(self):
        c = fr.classify
        self.assertEqual(c(self.ev(19, True), self.ev(30, True)), fr.STATUS_LOW)               # 事件不足 → 先講樣本不足
        self.assertEqual(c(self.ev(40, False), self.ev(40, False)), fr.STATUS_ENOUGH)
        self.assertEqual(c(self.ev(40, True), self.ev(40, False)), fr.STATUS_WATCH)            # 單次超出：觀察中
        self.assertEqual(c(self.ev(40, True), self.ev(40, True)), fr.STATUS_FAILED)            # 連續兩次超出：失效
        self.assertEqual(c(self.ev(40, False, gain=0.0), self.ev(40, False)), fr.STATUS_FAILED)   # 增益 ≤ 0：失效
        self.assertEqual(c(self.ev(40, False, gain=-0.2), self.ev(40, False)), fr.STATUS_FAILED)
        self.assertEqual(c(self.ev(40, True), self.ev(10, True)), fr.STATUS_WATCH)             # 上一次事件不足，不算連續

    def test_calibrated_informative_model_is_valid(self):
        d = synth(200, true_scale=1.0, seed=3)
        g = fr.score_groups(d, d["window_end"].max())
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["status"], fr.STATUS_ENOUGH)
        cur = g[0]["current"]
        self.assertGreater(cur["gain_vs_bg"], 0.0)
        self.assertGreater(cur["brier_skill_vs_bg"], 0.0)
        self.assertLessEqual(cur["n_cards"], 90)                                              # 只看近 90 天，且 1 天窗每天一張

    def test_underpredicting_model_fails_after_two_consecutive_exceedances(self):
        d = synth(200, true_scale=3.0, seed=4)
        g = fr.score_groups(d, d["window_end"].max())
        self.assertEqual(g[0]["status"], fr.STATUS_FAILED)
        self.assertTrue(g[0]["current"]["n_test"]["exceed"] and g[0]["previous"]["n_test"]["exceed"])

    def test_sparse_group_is_low_sample(self):
        d = synth(200, base_rate=0.04, seed=5)
        self.assertEqual(fr.score_groups(d, d["window_end"].max())[0]["status"], fr.STATUS_LOW)

    def test_custom_cards_and_other_model_versions_are_kept_apart(self):
        d = pd.concat([synth(120, seed=6), synth(120, seed=7, set_name="custom"), synth(120, seed=8, version="v2")],
                      ignore_index=True)
        g = fr.score_groups(d, d["window_end"].max())
        self.assertEqual(sorted(x["model_version"] for x in g), ["v1.1", "v2"])               # custom 不計分
        self.assertEqual(fr.score_groups(pd.DataFrame(), pd.Timestamp("2026-12-01")), [])

    def test_evaluation_only_uses_windows_that_have_ended_before_as_of(self):
        d = synth(100, seed=9)
        as_of = d["window_end"].iloc[49]
        ev = fr.evaluate_group(d, as_of)
        self.assertLessEqual(ev["n_cards"], 50)
        self.assertEqual(fr.evaluate_group(d, d["issued_at"].min())["n_cards"], 0)


if __name__ == "__main__":
    unittest.main()
