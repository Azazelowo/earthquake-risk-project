"""歷史類比（app/what_if.py）的單元測試：合成目錄，不需要真實資料。"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
import hazard_core as hc  # noqa: E402
import what_if as wi  # noqa: E402

LAT0, LON0 = 23.0, 120.5


def km_north(km):
    """從 (LAT0, LON0) 往正北 km 公里的緯度。"""
    return LAT0 + km / hc.KM_PER_DEG


def cat(rows):
    return pd.DataFrame(rows, columns=["time", "lat", "lon", "depth", "ml"])


class TestFindAnalogs(unittest.TestCase):
    def test_magnitude_boundary_inclusive(self):
        c = cat([("2000-01-01", LAT0, LON0, 10, 5.7), ("2001-01-01", LAT0, LON0, 10, 6.3),
                 ("2002-01-01", LAT0, LON0, 10, 6.31), ("2003-01-01", LAT0, LON0, 10, 5.69)])
        r = wi.find_analogs(c, LAT0, LON0, 50, 6.0, tol=0.3)
        self.assertEqual(sorted(r.analogs["ml"].round(2)), [5.7, 6.3])  # 等號算在內，差 0.31／0.31 不算

    def test_radius_boundary_inclusive_and_scope(self):
        # 用 haversine 算出的實際距離當半徑，確保「剛好等於」成立
        la = km_north(30)
        d = float(hc.haversine_km(LAT0, LON0, la, LON0))
        c = cat([("2000-01-01", la, LON0, 10, 6.0)])
        r = wi.find_analogs(c, LAT0, LON0, d, 6.0, near_margin_km=0)
        self.assertEqual((r.n_in, r.n_near), (1, 0))
        r2 = wi.find_analogs(c, LAT0, LON0, d - 0.01, 6.0, near_margin_km=0)
        self.assertEqual(len(r2.analogs), 0)
        r3 = wi.find_analogs(c, LAT0, LON0, d - 0.01, 6.0, near_margin_km=5)
        self.assertEqual((r3.n_in, r3.n_near), (0, 1))
        self.assertEqual(r3.analogs["scope"].iloc[0], wi.SCOPE_NEAR)

    def test_no_analogs(self):
        c = cat([("2000-01-01", 25.5, 121.5, 10, 6.0), ("2001-01-01", LAT0, LON0, 10, 4.0)])
        r = wi.find_analogs(c, LAT0, LON0, 50, 6.0)
        self.assertEqual((len(r.analogs), r.n_in, r.n_near), (0, 0, 0))
        self.assertEqual(len(r.largest), 1)  # 對照仍有（不限地點）
        self.assertEqual(wi.display_table(r.analogs, True).shape[0], 0)

    def test_empty_catalog(self):
        r = wi.find_analogs(cat([]), LAT0, LON0, 50, 6.0)
        self.assertEqual((len(r.analogs), len(r.largest), r.n_catalog), (0, 0, 0))
        self.assertEqual(wi.years_covered(r), 0.0)

    def test_distance_value(self):
        c = cat([("2000-01-01", km_north(100), LON0, 10, 6.0)])
        r = wi.find_analogs(c, LAT0, LON0, 150, 6.0)
        self.assertAlmostEqual(r.analogs["dist_km"].iloc[0], 100.0, delta=0.5)

    def test_sorting(self):
        c = cat([("2000-01-01", km_north(40), LON0, 10, 6.2),   # 規模差 0.2
                 ("2001-01-01", km_north(10), LON0, 10, 6.0),   # 差 0，近
                 ("2002-01-01", km_north(30), LON0, 10, 6.0),   # 差 0，遠
                 ("2003-01-01", km_north(20), LON0, 10, 5.9)])  # 差 0.1
        r = wi.find_analogs(c, LAT0, LON0, 100, 6.0)
        self.assertEqual(list(r.analogs["time"].dt.year), [2001, 2002, 2003, 2000])

    def test_largest_ignores_location_and_orders(self):
        c = cat([("1999-09-20", 40.0, 130.0, 8, 7.3), ("2024-04-02", 50.0, 140.0, 20, 7.2),
                 ("2010-01-01", LAT0, LON0, 10, 6.0), ("2011-01-01", LAT0, LON0, 10, 5.0)])
        r = wi.find_analogs(c, LAT0, LON0, 50, 6.0, n_largest=2)
        self.assertEqual(list(r.largest["ml"]), [7.3, 7.2])
        self.assertTrue((r.largest["dist_km"] > 1000).all())
        self.assertEqual(len(wi.find_analogs(c, LAT0, LON0, 50, 6.0).largest), 3)  # ml ≥ 6 只有 3 筆

    def test_no_depth_column(self):
        c = pd.DataFrame({"time": ["2000-01-01"], "lat": [LAT0], "lon": [LON0], "ml": [6.0]})
        r = wi.find_analogs(c, LAT0, LON0, 50, 6.0)
        self.assertFalse(r.has_depth)
        self.assertNotIn("深度 km", wi.display_table(r.analogs, r.has_depth).columns)

    def test_display_table_taipei_time_and_missing_depth(self):
        c = cat([("2000-01-01 20:00:00", LAT0, LON0, float("nan"), 6.0)])
        r = wi.find_analogs(c, LAT0, LON0, 50, 6.0)
        t = wi.display_table(r.analogs, r.has_depth)
        self.assertEqual(t["時間（台灣）"].iloc[0], "2000-01-02 04:00")  # UTC+8
        self.assertEqual(r.has_depth, False)

    def test_invalid_args_and_missing_columns(self):
        with self.assertRaises(ValueError):
            wi.find_analogs(cat([]), LAT0, LON0, -1, 6.0)
        with self.assertRaises(ValueError):
            wi.find_analogs(pd.DataFrame({"time": []}), LAT0, LON0, 50, 6.0)


if __name__ == "__main__":
    unittest.main()
