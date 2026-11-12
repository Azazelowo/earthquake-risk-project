"""B2（地磁、TEC 串流處理與網格特徵）的單元測試。除了一個 834 位元組的 CODE DCB 樣本外都用合成資料。

執行：python -m unittest discover -s pipeline/tests -v
"""
import gzip
import importlib.util
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from eqforecast.config import ExternalFeatureSpec, Region  # noqa: E402
from eqforecast.features import external_features  # noqa: E402
from eqforecast.grid import Grid  # noqa: E402
from geomag import iaga, ulf  # noqa: E402
from gnss_tec import aux_data, rinex, tec  # noqa: E402
import stream_io  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), SRC / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B2 = load_script("02_clean_features.py")
MAG = load_script("10_process_magnet.py")
TEC = load_script("11_process_gnss_tec.py")


class TestAuxData(unittest.TestCase):
    def test_unlzw_and_code_dcb(self):
        # CODE 2024-04 每月 P1−P2；與檔案內容核對（G01 −7.003、G02 7.815 ns）
        dcb = aux_data.read_code_p1p2(FIX / "p1p22404.dcb.Z")
        self.assertGreaterEqual(len(dcb), 30)
        self.assertAlmostEqual(dcb["G01"], -7.003)
        self.assertAlmostEqual(dcb["G02"], 7.815)

    def test_unlzw_rejects_other_formats(self):
        with self.assertRaises(ValueError):
            aux_data.unlzw(b"not compressed")

    def test_urls(self):
        self.assertTrue(aux_data.code_p1p2_url(2018, 6).endswith("/dcb/p1p21806.dcb.Z"))
        self.assertTrue(aux_data.code_p1p2_url(2026, 10, "p1c1").endswith("/dcb/p1c12610.dcb.Z"))
        urls = aux_data.nav_candidate_urls("2026-10-08")
        self.assertIn("BRDC00WRD_R_20262810000_01D_MN.rnx.gz", urls[0])
        self.assertIn("BRDC00IGS_R_20262810000_01D_MN.rnx.gz", urls[1])


NAV3 = """     3.05           N: GNSS NAV DATA    M: MIXED            RINEX VERSION / TYPE
                                                            END OF HEADER
G01 2026 10 27 00 00 00 1.410893164575e-04-8.412825991400e-12 0.000000000000e+00
     5.000000000000e+01-1.092500000000e+02 4.877703130290e-09 1.343902036606e+00
    -5.794689059258e-06 2.117978059687e-03 1.648440957069e-06 5.153738475800e+03
     1.728000000000e+05 1.862645149231e-09-1.125057561403e+00 5.774199962616e-08
     9.558696738201e-01 3.452812500000e+02 1.455550033844e-01-8.629288394957e-09
    -3.117986979717e-10 1.000000000000e+00 2.442000000000e+03 0.000000000000e+00
     2.000000000000e+00 0.000000000000e+00-8.847564458847e-09 5.000000000000e+01
     1.295400000000e+05 4.000000000000e+00
R01 2026 10 27 00 15 00 1.000000000000e-05 0.000000000000e+00 1.000000000000e+00
     1.000000000000e+04 0.000000000000e+00 0.000000000000e+00 0.000000000000e+00
     1.000000000000e+04 0.000000000000e+00 0.000000000000e+00 1.000000000000e+00
     1.000000000000e+04 0.000000000000e+00 0.000000000000e+00 0.000000000000e+00
     0.000000000000e+00 0.000000000000e+00 0.000000000000e+00 0.000000000000e+00
"""


class TestRinex3Nav(unittest.TestCase):
    def test_gps_only_and_variable_record_length(self):
        # GLONASS 紀錄在 3.05 版有 5 列（比 GPS 少），解析器要靠「開頭是字母」切分紀錄
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "nav.rnx.gz"
            with gzip.open(p, "wt") as f:
                f.write(NAV3)
            self.assertGreaterEqual(rinex.nav_version(p), 3)
            nav = rinex.read_nav(p)
        self.assertEqual(list(nav["sv"]), ["G01"])
        r = nav.iloc[0]
        self.assertEqual(r["toc"], pd.Timestamp("2026-10-27 00:00:00"))
        self.assertAlmostEqual(r["sqrt_a"], 5153.7384758)
        self.assertAlmostEqual(r["toe"], 172800.0)
        self.assertEqual(r["sv_health"], 0.0)
        self.assertAlmostEqual(r["af0"], 1.410893164575e-04)


class TestP1FromC1(unittest.TestCase):
    def test_conversion(self):
        obs = pd.DataFrame({"sv": ["G01", "G02", "G03"], "C1": [2.0e7] * 3, "P1": [np.nan] * 3,
                            "C1_lli": [0, 1, 0]})
        out, conv = tec.fill_p1_from_c1(obs, {"G01": 1.0, "G02": -2.0})
        self.assertTrue(conv)
        m_per_ns = tec.C * 1e-9
        self.assertAlmostEqual(out["P1"][0] - 2.0e7, 1.0 * m_per_ns, places=6)
        self.assertAlmostEqual(out["P1"][1] - 2.0e7, -2.0 * m_per_ns, places=6)
        self.assertTrue(np.isnan(out["P1"][2]))          # 沒有 P1−C1 值的衛星不猜
        self.assertEqual(out["P1_lli"][1], 1)

    def test_keeps_real_p1(self):
        obs = pd.DataFrame({"sv": ["G01"], "C1": [1.0], "P1": [2.0]})
        out, conv = tec.fill_p1_from_c1(obs, {"G01": 5.0})
        self.assertFalse(conv)
        self.assertEqual(out["P1"][0], 2.0)


def iaga_text(n=7200, f_missing=True, gap_at=None):
    t0 = pd.Timestamp("2026-10-31 16:00:00")
    head = [
        " Format                 IAGA-2002                                    |",
        " Source of Data         Central Weather Administration               |",
        " Station Name           Xincheng                                     |",
        " IAGA Code                                                           |",
        " Geodetic Latitude      24.038                                       |",
        " Geodetic Longitude     121.609                                      |",
        " Elevation              16                                           |",
        " Reported               XYZF                                         |",
        " Digital Sampling       1 second                                     |",
        " Data Type              Definitive                                   |",
        "DATE       TIME         DOY     XCGX      XCGY      XCGZ      XCGF   |",
    ]
    rows = []
    for k in range(n):
        t = t0 + pd.Timedelta(seconds=k)
        x, y, z = 35000.0, -3000.0, 26600.0
        if gap_at is not None and k == gap_at:
            z = 99999.0
        rows.append(f"{t:%Y-%m-%d %H:%M:%S}.000 {t.dayofyear:03d}     {x:8.2f}  {y:8.2f}  {z:8.2f}  "
                    f"{88888.0 if f_missing else 44000.0:8.2f}")
    return "\r\n".join(head + rows) + "\r\n"


class TestIaga(unittest.TestCase):
    def test_sentinels_and_f_from_xyz(self):
        head, df = iaga.read_iaga(iaga_text(n=120, gap_at=5).encode())
        self.assertEqual(head["station"], "XCG")         # IAGA Code 空白時由欄名取得
        self.assertAlmostEqual(head["lat"], 24.038)
        self.assertTrue(head["f_from_xyz"])
        self.assertTrue(np.isnan(df["Z"].iloc[5]))        # 99999 → 缺值
        self.assertTrue(np.isnan(df["F"].iloc[5]))
        self.assertAlmostEqual(df["F"].iloc[0], np.sqrt(35000.0 ** 2 + 3000.0 ** 2 + 26600.0 ** 2), places=6)
        self.assertEqual(df.index[0], pd.Timestamp("2026-10-31 16:00:00"))

    def test_recorded_f_is_kept(self):
        _, df = iaga.read_iaga(iaga_text(n=60, f_missing=False).encode())
        self.assertTrue((df["F"] == 44000.0).all())

    def test_gzip_input(self):
        _, df = iaga.read_iaga(gzip.compress(iaga_text(n=60).encode()))
        self.assertEqual(len(df), 60)


class TestUlf(unittest.TestCase):
    def test_band_power_of_sine(self):
        t = np.arange(1800.0)
        a = 2.0
        inband = band = ulf.band_power(a * np.sin(2 * np.pi * 0.02 * t))
        outband = ulf.band_power(a * np.sin(2 * np.pi * 0.2 * t))
        self.assertAlmostEqual(inband, a ** 2 / 2, delta=0.15 * a ** 2 / 2)   # 正弦波功率 = A²/2
        self.assertLess(outband, 0.01 * band)

    def test_night_window_and_polarization(self):
        date = pd.Timestamp("2026-10-31")
        idx = pd.date_range(date, date + pd.Timedelta(days=1), freq="1s", inclusive="left")
        rng = np.random.default_rng(0)
        k = np.arange(len(idx))
        wave = np.sin(2 * np.pi * 0.02 * k)
        df = pd.DataFrame({"X": 35000 + 0.1 * wave + 0.01 * rng.normal(size=len(k)),
                           "Y": -3000 + 0.01 * rng.normal(size=len(k)),
                           "Z": 26600 + 1.0 * wave, "F": np.nan}, index=idx)
        r = ulf.night_ulf(df, date)
        self.assertEqual(r["n_segments"], 8)                 # UTC 16–20 時，每段 30 分鐘
        self.assertGreater(r["ulf_pol"], 10)                 # Z 主導 → Sz/Sh 大
        df.loc[(idx.hour >= 16) & (idx.hour < 20), "Z"] = np.nan
        self.assertTrue(np.isnan(ulf.night_ulf(df, date)["ulf_pol"]))   # 夜間沒資料就不出值

    def test_despike(self):
        x = np.zeros(200)
        x[100] = 50.0
        y, n = ulf.despike(x)
        self.assertEqual(n, 1)
        self.assertTrue(np.isnan(y[100]))


class TestMagnetScript(unittest.TestCase):
    def test_name_parsing(self):
        self.assertEqual(MAG.parse_name("xcg20261031dsec.sec"), ("XCG", pd.Timestamp("2026-10-31")))
        self.assertEqual(MAG.parse_name("KMN20180101dsec.sec.gz")[0], "KMN")
        self.assertEqual(MAG.parse_name("readme.txt"), (None, None))

    def test_tec_name_parsing(self):
        self.assertEqual(TEC.parse_name("hual3000.26o.gz"), ("HUAL", pd.Timestamp("2026-10-27")))
        self.assertEqual(TEC.parse_name("ndh50770.24o"), ("NDH5", pd.Timestamp("2024-03-17")))
        self.assertEqual(TEC.parse_name("hual3000.26d.gz"), (None, None))   # Hatanaka 壓縮不支援


class TestStreamIO(unittest.TestCase):
    def test_inbox_zip_manifest_and_keep(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            inbox = d / "inbox"
            inbox.mkdir()
            (inbox / "xcg20261031dsec.sec").write_text("a")
            (inbox / "xcg20261101dsec.sec").write_text("b")
            with zipfile.ZipFile(inbox / "batch.zip", "w") as z:
                z.writestr("sub/xcg20180105dsec.sec", "c")
                z.writestr("notes.txt", "x")
            items = stream_io.iter_inbox(inbox, lambda n: n.endswith(".sec"))
            self.assertEqual([it.name for it in items],
                             ["xcg20180105dsec.sec", "xcg20261031dsec.sec", "xcg20261101dsec.sec"])
            self.assertEqual(items[0].read_bytes(), b"c")

            m = stream_io.Manifest(d / "out" / "manifest.jsonl")
            m.add(sha256=stream_io.sha256(b"a"), name="x", status="ok")
            m.add(sha256=stream_io.sha256(b"b"), name="y", status="error")
            m2 = stream_io.Manifest(d / "out" / "manifest.jsonl")
            self.assertTrue(m2.seen(stream_io.sha256(b"a")))
            self.assertFalse(m2.seen(stream_io.sha256(b"b")))    # 失敗的檔案下次會重試

            ev = d / "events.csv"
            pd.DataFrame({"time": ["2026-11-05 03:00:00"]}).to_csv(ev, index=False)
            win = stream_io.load_keep_windows(ev, 7)
            disp = stream_io.RawDisposer(True, d / "keep", win)
            for it in items:
                disp.done(it, MAG.parse_name(it.name)[1], ok=True)
            disp.finish()
            self.assertTrue((d / "keep" / "xcg20261101dsec.sec").exists())    # 窗口內：保留
            self.assertFalse((d / "keep" / "xcg20180105dsec.sec").exists())
            self.assertFalse((inbox / "xcg20261031dsec.sec").exists())        # 窗口內但已搬走
            self.assertFalse((inbox / "batch.zip").exists())                  # zip 全部成功才刪
            self.assertEqual(disp.kept, 2)

    def test_failed_zip_member_keeps_zip(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            with zipfile.ZipFile(d / "b.zip", "w") as z:
                z.writestr("xcg20261031dsec.sec", "a")
                z.writestr("xcg20261101dsec.sec", "b")
            items = stream_io.iter_inbox(d, lambda n: n.endswith(".sec"))
            disp = stream_io.RawDisposer(True, None, [])
            disp.done(items[0], None, ok=True)
            disp.done(items[1], None, ok=False)
            disp.finish()
            self.assertTrue((d / "b.zip").exists())

    def test_stream_bounds_inflight_and_dedupes(self):
        from concurrent.futures import Future
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            with zipfile.ZipFile(d / "all.zip", "w") as z:
                for k in range(20):
                    z.writestr(f"xcg201801{k + 1:02d}dsec.sec", f"day{k}")
            (d / "copy").mkdir()
            (d / "copy" / "xcg20180103dsec.sec").write_text("day2")      # 和 zip 內的 1/3 內容相同
            (d / "copy" / "xcg20180130dsec.sec").write_text("bad")
            m = stream_io.Manifest(d / "out" / "manifest.jsonl")
            m.add(sha256=stream_io.sha256(b"day0"), name="x", status="ok")    # 1/1 以前處理過
            disp = stream_io.RawDisposer(True, None, [])
            stream = stream_io.Stream(m, disp, lambda n: MAG.parse_name(n)[1], max_inflight=4)
            submitted, inflight, peak = [], [0], [0]

            def submit(it, data, digest):
                submitted.append(it.name)
                inflight[0] += 1
                peak[0] = max(peak[0], inflight[0])
                f = Future()
                f.set_result(data)
                return f

            seen = []
            for job in stream.run(stream_io.iter_inbox(d, lambda n: n.endswith(".sec")), submit):
                inflight[0] -= 1
                ok = job.future.result() != b"bad"
                seen.append((job.item.name, [x.name for x in job.dups]))
                stream.done(job, ok)
            disp.finish()
            self.assertLessEqual(peak[0], 4)
            self.assertEqual(len(submitted), 20)                  # 21 個檔 − 1/1 已處理 − 1/3 重複 + bad
            self.assertEqual(stream.n_dup, 2)
            self.assertEqual([n for n, _ in seen], sorted(n for n, _ in seen))    # 依檔名順序
            self.assertIn(("xcg20180103dsec.sec", ["xcg20180103dsec.sec"]), seen)
            self.assertFalse((d / "all.zip").exists())             # zip 全部成功 → 刪除（Windows 上要先關閉）
            self.assertFalse((d / "copy" / "xcg20180103dsec.sec").exists())    # 重複檔跟著原檔處置
            self.assertTrue((d / "copy" / "xcg20180130dsec.sec").exists())     # 失敗的不刪

    def test_magnet_script_streams_zip(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            inbox, out = d / "inbox", d / "out"
            inbox.mkdir()
            with zipfile.ZipFile(inbox / "XCG.zip", "w", zipfile.ZIP_DEFLATED) as z:
                for day in ("2026-10-29", "2026-10-30", "2026-10-31"):
                    z.writestr(f"XCG/xcg{day.replace('-', '')}dsec.sec", iaga_text().replace("2026-10-31", day))
            r = subprocess.run([sys.executable, str(SRC / "10_process_magnet.py"), "--inbox", str(inbox),
                                "--out", str(out), "--delete-raw", "--workers", "1"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse((inbox / "XCG.zip").exists())
            self.assertEqual(len(list((out / "daily" / "XCG").glob("*.json"))), 3)
            lines = (out / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)


class TestB2Features(unittest.TestCase):
    def test_robust_z_uses_only_past(self):
        dates = pd.date_range("2026-01-01", periods=30)
        v = np.r_[np.random.default_rng(1).normal(0, 1, 25), [0.0, 0.0, 0.0, 0.0, 100.0]]
        ok = np.ones(30, bool)
        z1 = B2.robust_z(v, dates, ok, ok)
        v2 = v.copy()
        v2[-1] = -100.0                                   # 改變未來的值，過去的 z 不能變
        z2 = B2.robust_z(v2, dates, ok, ok)
        np.testing.assert_allclose(z1.iloc[:-1].to_numpy(), z2.iloc[:-1].to_numpy(), equal_nan=True)
        self.assertTrue(z1.iloc[:10].isna().all())         # 前 10 天背景不足
        self.assertGreater(z1.iloc[-1], 20)

    def test_background_skips_disturbed_days(self):
        dates = pd.date_range("2026-01-01", periods=40)
        bg_ok = np.ones(40, bool)
        bg_ok[20:30] = False
        m = B2.background_days(dates, bg_ok, 35)
        self.assertEqual(int(m.sum()), 15)
        self.assertFalse(m[20:30].any())
        self.assertTrue(m[5:20].any() and m[30:35].all())

    def test_instrument_segments(self):
        ids = ["A"] * 4 + ["B"] * 3
        self.assertEqual(list(B2.instrument_segments(ids, [1.0] * 7)), [0, 0, 0, 0, 1, 1, 1])
        # 同一識別碼、偏差突然跳 30 TECU（NDHU 的情形）→ 換段
        seg = B2.instrument_segments(["A"] * 6, [33, 34, 33, -3, -2, -1])
        self.assertEqual(list(seg), [0, 0, 0, 1, 1, 1])

    def test_to_grid_radius_and_availability(self):
        cells = B2.grid_cells({"lat_min": 23.0, "lat_max": 24.0, "lon_min": 120.0, "lon_max": 122.0})
        st = pd.DataFrame({
            "station": ["A", "B", "A", "B"], "lat": [23.1, 23.9, 23.1, 23.9], "lon": [120.1, 121.9, 120.1, 121.9],
            "date": pd.to_datetime(["2026-01-01", "2026-01-01", "2026-01-02", "2026-01-02"]),
            "v": [1.0, 2.0, np.nan, 5.0]})
        g = B2.to_grid(st, ["v"], cells, 60.0, "x")
        d1 = g[g.time_bin == "2026-01-01"]
        self.assertTrue((d1["x_dist_km"] <= 60).all())
        self.assertEqual(set(d1["x_station"]), {"A", "B"})
        d2 = g[g.time_bin == "2026-01-02"]
        self.assertEqual(set(d2["x_station"]), {"B"})       # A 當天沒值 → 不能拿 A 的位置填
        self.assertTrue((d2["v"] == 5.0).all())
        self.assertFalse(g["is_demo"].any())

    def test_external_features_without_overlap(self):
        grid = Grid(Region(23.0, 24.0, 120.0, 121.0), 0.5)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.parquet"
            pd.DataFrame({"lat_bin": [23.0], "lon_bin": [120.0], "time_bin": [pd.Timestamp("2026-10-01")],
                          "a": [1.0], "is_demo": [False]}).to_parquet(p)
            spec = ExternalFeatureSpec(name="x", path=str(p), native_cell_deg=0.5, value_cols=["a"],
                                       lookback_days=10)
            out = external_features(spec, pd.DatetimeIndex(["2020-01-01", "2020-02-01"]), grid)
        self.assertIn("f_x_a_mean", out)
        self.assertIn("f_x_a_max", out)
        self.assertTrue(out["f_x_a_mean"].isna().all())
        self.assertTrue((out["f_x_coverage"] == 0).all())


if __name__ == "__main__":
    unittest.main()
