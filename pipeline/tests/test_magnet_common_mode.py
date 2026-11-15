"""扣除全區共同擾動的兩個地磁特徵（02_clean_features.py）的單元測試。

mag_sznet_z：相對「其他測站當天中位數」；mag_szres_z：扣掉由地磁指數預期的值。
全部用合成資料。執行：python -m unittest discover -s pipeline/tests -v
"""
import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

spec = importlib.util.spec_from_file_location("clean_features", SRC / "02_clean_features.py")
CF = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CF)

DAYS = pd.date_range("2022-01-01", periods=200, freq="D")


def synthetic(stations=("AAA", "BBB", "CCC", "DDD"), seed=0, noise=0.02):
    """各站 log_sz = 站別常數 + 全區共同項（由 kp_sum、dst_min 決定）+ 小雜訊。"""
    rng = np.random.default_rng(seed)
    kp_sum = rng.uniform(5, 30, len(DAYS))
    dst_min = -rng.uniform(0, 45, len(DAYS))
    common = 0.03 * kp_sum - 0.004 * dst_min
    rows = []
    for k, sta in enumerate(stations):
        log_sz = -2.0 + 0.3 * k + common + rng.normal(0, noise, len(DAYS))
        rows.append(pd.DataFrame({"station": sta, "date": DAYS, "lat": 23.0 + k, "lon": 121.0,
                                  "ulf_sz": 10 ** log_sz, "ulf_pol": 1.0 + 0.01 * rng.random(len(DAYS)),
                                  "coverage": 1.0}))
    idx = pd.DataFrame({"kp_sum": kp_sum, "dst_min": dst_min}, index=DAYS)
    return pd.concat(rows, ignore_index=True), idx


def prep(mag):
    mag = mag.copy()
    mag["date"] = pd.to_datetime(mag["date"]).dt.normalize()
    mag["log_sz"] = np.log10(mag["ulf_sz"])
    return mag


class NetworkReferenceTest(unittest.TestCase):
    def test_excludes_own_station(self):
        mag, _ = synthetic()
        mag.loc[(mag["station"] == "AAA") & (mag["date"] == DAYS[50]), "ulf_sz"] *= 1000
        m = prep(mag)
        ref = pd.Series(CF.network_reference(m), index=pd.MultiIndex.from_arrays([m["station"], m["date"]]))
        others = m[(m["date"] == DAYS[50]) & (m["station"] != "AAA")]["log_sz"]
        self.assertAlmostEqual(ref[("AAA", DAYS[50])], float(np.median(others)))

    def test_needs_two_other_stations(self):
        mag, _ = synthetic(stations=("AAA", "BBB"))
        self.assertTrue(np.isnan(CF.network_reference(prep(mag))).all())

    def test_low_coverage_station_not_used_as_reference(self):
        mag, _ = synthetic(stations=("AAA", "BBB", "CCC"))
        mag.loc[(mag["station"] == "CCC") & (mag["date"] == DAYS[10]), "coverage"] = 0.5
        m = prep(mag)
        ref = pd.Series(CF.network_reference(m), index=pd.MultiIndex.from_arrays([m["station"], m["date"]]))
        self.assertTrue(np.isnan(ref[("AAA", DAYS[10])]))      # 只剩 BBB 一站可當參考
        self.assertFalse(np.isnan(ref[("AAA", DAYS[11])]))


class IndexResidualTest(unittest.TestCase):
    def test_removes_index_driven_variation(self):
        mag, idx = synthetic(stations=("AAA",))
        y = np.log10(mag["ulf_sz"].to_numpy())
        res = CF.index_residual(y, DAYS, idx.to_numpy(), np.ones(len(y), bool))
        self.assertTrue(np.isnan(res[:CF.REG_MIN_DAYS]).all())  # 訓練日不足
        tail = res[CF.REG_MIN_DAYS + 20:]
        self.assertLess(np.nanstd(tail), 0.2 * np.std(y))

    def test_uses_past_only(self):
        mag, idx = synthetic(stations=("AAA",))
        y = np.log10(mag["ulf_sz"].to_numpy())
        base = CF.index_residual(y, DAYS, idx.to_numpy(), np.ones(len(y), bool))
        y2 = y.copy()
        y2[150:] += 5.0                                         # 改掉未來
        changed = CF.index_residual(y2, DAYS, idx.to_numpy(), np.ones(len(y), bool))
        np.testing.assert_allclose(base[:150], changed[:150], equal_nan=True)

    def test_missing_index_gives_nan(self):
        mag, idx = synthetic(stations=("AAA",))
        X = idx.to_numpy().copy()
        X[120, 0] = np.nan
        res = CF.index_residual(np.log10(mag["ulf_sz"].to_numpy()), DAYS, X, np.ones(len(DAYS), bool))
        self.assertTrue(np.isnan(res[120]))
        self.assertFalse(np.isnan(res[121]))


class StationFeatureTest(unittest.TestCase):
    def test_local_anomaly_stands_out_common_mode_does_not(self):
        mag, idx = synthetic()
        local, common_day = DAYS[150], DAYS[160]
        mag.loc[(mag["station"] == "AAA") & (mag["date"] == local), "ulf_sz"] *= 3
        # 四站同時變大、指數沒反映。合成資料裡指數造成的日變化本來就有 ±0.4 dex，
        # 所以要放大 100 倍才會在未扣共同項的 mag_sz_z 上明顯。
        mag.loc[mag["date"] == common_day, "ulf_sz"] *= 100
        out = CF.magnet_station_features(mag, set(), set(), indices=idx).set_index(["station", "date"])
        for col in ("mag_sznet_z", "mag_szres_z"):
            self.assertGreater(out.loc[("AAA", local), col], 5, col)
        self.assertLess(abs(out.loc[("AAA", common_day), "mag_sznet_z"]), 3)
        self.assertGreater(out.loc[("AAA", common_day), "mag_sz_z"], 3)
        self.assertLess(abs(out.loc[("BBB", local), "mag_sznet_z"]), 3)   # 鄰站不受單站異常拖累

    def test_without_indices_residual_is_nan_and_old_column_kept(self):
        mag, _ = synthetic()
        out = CF.magnet_station_features(mag, set(), set())
        self.assertTrue(out["mag_szres_z"].isna().all())
        self.assertTrue(out["mag_szref_z"].isna().all())        # 沒有 KMN
        self.assertTrue(out["mag_sznet_z"].notna().any())
        self.assertEqual(set(CF.MAG_COLS) - set(out.columns), set())


class MagnetSegmentTest(unittest.TestCase):
    def frame(self, h, z=None, coverage=None):
        days = pd.date_range("2022-01-01", periods=len(h), freq="D")
        return pd.DataFrame({"h_mean": h, "z_mean": np.full(len(h), 26000.0) if z is None else z,
                             "coverage": np.ones(len(h)) if coverage is None else coverage}, index=days)

    def test_level_shift_starts_new_segment_and_noise_does_not(self):
        rng = np.random.default_rng(0)
        h = 36000 + rng.normal(0, 10, 60)
        h[30:] += 400                                           # 第 30 天起基線永久改變
        seg = CF.magnet_segments(self.frame(h), set())
        self.assertEqual(list(np.unique(seg)), [0, 1])
        self.assertEqual(int(np.argmax(seg == 1)), 30)
        self.assertEqual(CF.magnet_segments(self.frame(36000 + rng.normal(0, 10, 60)), set()).max(), 0)

    def test_storm_day_and_low_coverage_day_do_not_split(self):
        h = np.full(40, 36000.0)
        h[20] -= 300                                            # 單日大偏移
        g = self.frame(h)
        self.assertGreater(CF.magnet_segments(g, set()).max(), 0)             # 不知道是磁暴 → 會切段
        self.assertEqual(CF.magnet_segments(g, {g.index[20]}).max(), 0)       # 標為磁暴日 → 不切
        cov = np.ones(40)
        cov[20] = 0.3
        self.assertEqual(CF.magnet_segments(self.frame(h, coverage=cov), set()).max(), 0)

    def test_jump_in_either_component_splits_and_past_only(self):
        h, z = np.full(50, 36000.0), np.full(50, 26000.0)
        z[25:] -= 250
        seg = CF.magnet_segments(self.frame(h, z), set())
        self.assertEqual(int(np.argmax(seg == 1)), 25)
        h2 = h.copy()
        h2[40:] += 900                                          # 改掉未來，不應影響第 40 天之前的段落
        np.testing.assert_array_equal(CF.magnet_segments(self.frame(h2, z), set())[:40], seg[:40])

    def test_background_does_not_cross_segment(self):
        mag, idx = synthetic(stations=("AAA", "BBB", "CCC"))
        mag["h_mean"], mag["z_mean"] = 36000.0, 26000.0
        aaa_late = (mag["station"] == "AAA") & (mag["date"] >= DAYS[100])
        mag.loc[aaa_late, "h_mean"] += 500                     # AAA 第 100 天換儀器
        mag.loc[aaa_late, "ulf_sz"] *= 30                      # 新儀器雜訊底不同
        out = CF.magnet_station_features(mag, set(), set(), indices=idx).set_index(["station", "date"])
        self.assertEqual(out.loc[("AAA", DAYS[99]), "mag_segment"] + 1, out.loc[("AAA", DAYS[100]), "mag_segment"])
        # 換段後的前 10 天湊不滿背景 → 沒有值，而不是出現一串假異常
        self.assertTrue(out.loc[[("AAA", d) for d in DAYS[100:109]], "mag_sz_z"].isna().all())
        self.assertLess(abs(out.loc[("AAA", DAYS[130]), "mag_sz_z"]), 4)

    def test_missing_mean_columns_means_single_segment(self):
        mag, _ = synthetic(stations=("AAA", "BBB", "CCC"))
        out = CF.magnet_station_features(mag, set(), set())
        self.assertEqual(out["mag_segment"].max(), 0)


if __name__ == "__main__":
    unittest.main()
