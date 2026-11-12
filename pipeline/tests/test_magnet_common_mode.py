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


if __name__ == "__main__":
    unittest.main()
