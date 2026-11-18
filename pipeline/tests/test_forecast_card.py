"""app/forecast_card.py：標準卡產生與登記簿（雜湊鏈）。用 app/data 的實際快照，不連網。"""
import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
import forecast_card as fc  # noqa: E402
import hazard_core as hc  # noqa: E402

BASE = ROOT / "app" / "data"


def later(snap, days):
    """同一份快照，發布時間往後 days 天（用來造第二批）。"""
    t = (snap.issue_time + np.timedelta64(days, "D")).strftime("%Y-%m-%d %H:%M:%S")
    return dataclasses.replace(snap, params={**snap.params, "issue_time": t})


class BuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = hc.load_snapshot(BASE)
        cls.uniform, cls.rate = fc.uniform_per_cell(BASE, cls.snap, fc.STANDARD_WINDOWS)
        cls.header, cls.cards = fc.build_batch(cls.snap, cls.uniform)
        cls.by = {(c["r"], c["m"], c["T"]): c for c in cls.cards}

    def test_standard_set_size_and_unique_ids(self):
        self.assertEqual(len(self.cards), 6 * 7 * 9)
        self.assertEqual(self.header["n_cards"], 378)
        ids = [c["id"] for c in self.cards]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(self.by[("KHH", 5.0, 7)]["id"], "F-20260930T0000-standard-KHH-M5-7d")

    def test_probabilities_are_valid_and_consistent_with_expected_counts(self):
        for c in self.cards:
            for p, n in (("p", "n"), ("pb", "nb"), ("pu", "nu")):
                self.assertTrue(0.0 <= c[p] <= 1.0, c["id"])
                self.assertGreaterEqual(c[n], 0.0, c["id"])
                self.assertAlmostEqual(c[p], float(f"{-np.expm1(-c[n]):.6g}"), delta=1e-5 + 1e-5 * c[p], msg=c["id"])

    def test_probability_falls_with_magnitude_and_rises_with_window(self):
        for r in ("TW", "KHH", "HUA"):
            for T in fc.STANDARD_WINDOWS:
                ps = [self.by[(r, m, T)]["n"] for m in fc.STANDARD_MAGS]
                self.assertTrue(all(a > b for a, b in zip(ps, ps[1:])), (r, T))
            for m in fc.STANDARD_MAGS:
                ns = [self.by[(r, m, T)]["n"] for T in fc.STANDARD_WINDOWS]
                self.assertTrue(all(a < b for a, b in zip(ns, ns[1:])), (r, m))

    def test_whole_island_covers_every_circle(self):
        for r in ("KHH", "TPE", "ILA", "HUA", "TTT"):
            for m, T in ((4.0, 7), (5.5, 30)):
                self.assertGreater(self.by[("TW", m, T)]["n"], self.by[(r, m, T)]["n"])
                self.assertGreater(self.by[("TW", m, T)]["nu"], self.by[(r, m, T)]["nu"])

    def test_uniform_is_proportional_to_area(self):
        """均勻值每格相同，所以區域的 nu 應該和面積成正比。"""
        a_tw = self.header["regions"]["TW"]["area_km2"]
        for r in ("KHH", "HUA"):
            ratio = self.by[(r, 5.0, 7)]["nu"] / self.by[("TW", 5.0, 7)]["nu"]
            self.assertAlmostEqual(ratio, self.header["regions"][r]["area_km2"] / a_tw, delta=0.02 * ratio)
        a_khh = self.header["regions"]["KHH"]["area_km2"]
        self.assertAlmostEqual(a_khh, np.pi * 50 ** 2, delta=0.03 * np.pi * 50 ** 2)   # 半徑 50 km 的圓

    def test_flags_mark_extrapolated_magnitudes_and_interpolated_windows_only(self):
        for c in self.cards:
            self.assertEqual(bool(c.get("x")), c["m"] >= 6.5, c["id"])
            self.assertEqual(bool(c.get("i")), c["T"] in (5, 10, 20), c["id"])

    def test_header_records_model_data_and_resolution(self):
        h = self.header
        self.assertEqual(h["model"]["version"], self.snap.params["model_version"])
        self.assertEqual(len(h["model"]["params_hash"]), 64)
        self.assertTrue(h["registered"])
        self.assertEqual(h["set"], "standard")
        self.assertIn("ML", h["resolution"]["mag_type"])
        self.assertEqual(set(h["regions"]), {"TW", "KHH", "TPE", "ILA", "HUA", "TTT"})

    def test_canonical_json_is_stable_under_key_order(self):
        self.assertEqual(fc.canonical({"b": 1, "a": [2, 3]}), fc.canonical({"a": [2, 3], "b": 1}))
        self.assertEqual(fc.sig(0.1 + 0.2), fc.sig(0.3))


class LagTest(unittest.TestCase):
    def test_late_or_future_snapshots_are_refused(self):
        issued = pd.Timestamp("2026-11-20 00:00")
        self.assertAlmostEqual(fc.check_lag(issued, pd.Timestamp("2026-11-20 00:20"), 2.0), 1 / 3)
        self.assertEqual(fc.check_lag(issued, pd.Timestamp("2026-11-20 02:00"), 2.0), 2.0)      # 剛好 2 小時：允許
        with self.assertRaisesRegex(fc.RegistryError, "今天不發"):
            fc.check_lag(issued, pd.Timestamp("2026-11-20 02:01"), 2.0)
        with self.assertRaisesRegex(fc.RegistryError, "晚於現在"):
            fc.check_lag(issued, pd.Timestamp("2026-11-19 23:00"), 2.0)

    def test_generated_at_is_recorded_only_when_given(self):
        snap = hc.load_snapshot(BASE)
        uniform, _ = fc.uniform_per_cell(BASE, snap, (7,))
        small = dict(regions=fc.STANDARD_REGIONS[:1], mags=(4.0,), windows=(7,))
        self.assertNotIn("generated_at", fc.build_batch(snap, uniform, **small)[0])
        h, _ = fc.build_batch(snap, uniform, generated_at=pd.Timestamp("2026-09-30 00:20"), **small)
        self.assertEqual(h["generated_at"], "2026-09-30T00:20:00Z")
        self.assertEqual(h["data_status"]["errors"], [])


class RegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = hc.load_snapshot(BASE)
        cls.uniform, _ = fc.uniform_per_cell(BASE, cls.snap, (7,))
        small = dict(regions=fc.STANDARD_REGIONS[:2], mags=(4.0, 5.0), windows=(7,))
        # 三批都落在 9 月（快照發布時間是 9/30，往前移 10 天），同一個月檔；跨月另有測試
        cls.b1 = fc.build_batch(later(cls.snap, -10), cls.uniform, **small)
        cls.b2 = fc.build_batch(later(cls.snap, -9), cls.uniform, **small)
        cls.b3 = fc.build_batch(later(cls.snap, -8), cls.uniform, **small)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def fill(self, *batches):
        for h, c in batches:
            fc.append_batch(self.dir, h, c)

    def month_file(self):
        return next(self.dir.glob("*.jsonl"))

    def rewrite(self, fn):
        path = self.month_file()
        lines = path.read_text(encoding="utf-8").split("\n")
        path.write_text("\n".join(fn(lines)), encoding="utf-8", newline="\n")

    def test_chain_verifies_and_head_tracks_last_batch(self):
        self.fill(self.b1, self.b2, self.b3)
        stats = fc.verify_registry(self.dir)
        self.assertEqual((stats["batches"], stats["cards"]), (3, 12))
        head = json.loads((self.dir / "head.json").read_text(encoding="utf-8"))
        self.assertEqual(head["hash"], stats["head"])
        self.assertEqual(head["card_count"], 12)

    def test_first_batch_links_to_genesis_and_later_batches_to_previous_hash(self):
        h1 = fc.append_batch(self.dir, *self.b1)
        fc.append_batch(self.dir, *self.b2)
        rows = [json.loads(x) for x in self.month_file().read_text(encoding="utf-8").splitlines()]
        headers = [r for r in rows if r.get("type") == "batch"]
        self.assertEqual(headers[0]["prev_hash"], fc.GENESIS)
        self.assertEqual(headers[1]["prev_hash"], h1)

    def test_editing_an_old_card_is_detected(self):
        self.fill(self.b1, self.b2)

        def edit(lines):
            i = next(k for k, ln in enumerate(lines) if '"type":"batch"' not in ln and ln.strip())
            row = json.loads(lines[i])
            row["p"] = row["p"] / 2                    # 事後把機率改小
            lines[i] = fc.canonical(row)
            return lines
        self.rewrite(edit)
        with self.assertRaisesRegex(fc.RegistryError, "雜湊不符"):
            fc.verify_registry(self.dir)

    def test_editing_a_header_is_detected(self):
        self.fill(self.b1)

        def edit(lines):
            row = json.loads(lines[0])
            row["model"]["b"] = 1.0                    # 事後改模型參數
            lines[0] = fc.canonical(row)
            return lines
        self.rewrite(edit)
        with self.assertRaisesRegex(fc.RegistryError, "雜湊不符"):
            fc.verify_registry(self.dir)

    def test_removing_a_middle_batch_breaks_the_chain(self):
        self.fill(self.b1, self.b2, self.b3)
        per = 1 + self.b1[0]["n_cards"]

        def edit(lines):
            return lines[:per] + lines[2 * per:]
        self.rewrite(edit)
        with self.assertRaisesRegex(fc.RegistryError, "prev_hash"):
            fc.verify_registry(self.dir)

    def test_dropping_a_card_is_detected(self):
        self.fill(self.b1)
        self.rewrite(lambda lines: lines[:3] + lines[4:])
        with self.assertRaises(fc.RegistryError):
            fc.verify_registry(self.dir)

    def test_duplicate_or_backdated_batch_is_refused_and_leaves_registry_intact(self):
        self.fill(self.b1, self.b2)
        before = self.month_file().read_bytes()
        with self.assertRaises(fc.AlreadyIssued):
            fc.append_batch(self.dir, *self.b2)        # 同一天重跑：不是錯誤，但也不會寫第二次
        with self.assertRaisesRegex(fc.RegistryError, "早於"):
            fc.append_batch(self.dir, *self.b1)        # 補發舊的
        self.assertEqual(self.month_file().read_bytes(), before)
        fc.verify_registry(self.dir)

    def test_stale_head_is_detected(self):
        self.fill(self.b1, self.b2)
        head_path = self.dir / "head.json"
        head = json.loads(head_path.read_text(encoding="utf-8"))
        head["hash"] = "f" * 64
        head_path.write_text(json.dumps(head), encoding="utf-8")
        with self.assertRaisesRegex(fc.RegistryError, "head.json"):
            fc.verify_registry(self.dir)

    def test_file_uses_lf_only_and_empty_registry_verifies(self):
        self.assertEqual(fc.verify_registry(self.dir)["batches"], 0)
        self.fill(self.b1)
        self.assertNotIn(b"\r", self.month_file().read_bytes())

    def test_batches_in_different_months_go_to_different_files(self):
        self.fill(self.b1, fc.build_batch(later(self.snap, 40), self.uniform, regions=fc.STANDARD_REGIONS[:1],
                                          mags=(4.0,), windows=(7,)))
        self.assertEqual(len(list(self.dir.glob("*.jsonl"))), 2)
        self.assertEqual(fc.verify_registry(self.dir)["batches"], 2)


if __name__ == "__main__":
    unittest.main()
