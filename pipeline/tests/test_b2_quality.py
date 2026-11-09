"""B2 資料品質報告（12_quality_report.py）的單元測試，全部用合成的 manifest 與 daily json。

執行：python -m unittest discover -s pipeline/tests -v
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

spec = importlib.util.spec_from_file_location("quality_report", SRC / "12_quality_report.py")
QR = importlib.util.module_from_spec(spec)
spec.loader.exec_module(QR)


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def mag_day(sta, date, coverage=1.0, h=30000.0, z=25000.0, lat=24.1, lon=121.6, data_type="variation"):
    return {"station": sta, "date": date, "coverage": coverage, "h_mean": h, "z_mean": z,
            "lat": lat, "lon": lon, "data_type": data_type}


def tec_day(sta, date, hours=24.0, inst="SN1|NETR9|5.4|A1|ANT", bias=10.0, age=0, dcb="P1P22001.DCB"):
    return {"station": sta, "date": date, "hours_covered": hours, "instrument_id": inst,
            "rx_bias_tecu": bias, "dcb_age_days": age, "dcb_file": dcb}


class TestQualityReport(unittest.TestCase):
    def test_coverage_counts_usable_days_and_lists_missing_stations(self):
        days = pd.date_range("2020-01-01", "2020-01-10")
        rows = [mag_day("XCG", str(d.date()), coverage=0.5 if i < 3 else 1.0) for i, d in enumerate(days)]
        daily = pd.DataFrame(rows)
        daily["date"] = pd.to_datetime(daily["date"])
        cov = QR.coverage_by_year(daily, "magnet", ["XCG", "KMN"], "2019-12-30", "2020-01-10")
        x20 = cov[(cov.station == "XCG") & (cov.year == 2020)].iloc[0]
        self.assertEqual(x20.expected_days, 10)
        self.assertEqual(x20.processed_days, 10)
        self.assertEqual(x20.usable_days, 7)            # 前 3 天涵蓋率 50% 不可用
        kmn = cov[cov.station == "KMN"]
        self.assertEqual(len(kmn), 2)                   # 沒資料的站也列出，2019、2020 各一列
        self.assertTrue((kmn.usable_pct == 0).all())
        self.assertEqual(cov[(cov.station == "XCG") & (cov.year == 2019)].iloc[0].expected_days, 2)

    def test_gaps(self):
        have = list(pd.date_range("2020-01-01", "2020-01-03")) + list(pd.date_range("2020-01-12", "2020-01-20"))
        daily = pd.DataFrame({"station": "XCG", "date": have})
        g = QR.find_gaps(daily, "magnet", ["XCG"], "2020-01-01", "2020-01-25", min_days=5)
        self.assertEqual(len(g), 2)                     # 1/4–1/11（8 天）、1/21–1/25（5 天）
        g = QR.find_gaps(daily, "magnet", ["XCG"], "2020-01-01", "2020-01-25", min_days=6)
        self.assertEqual(g.iloc[0]["days"], 8)
        self.assertEqual(str(g.iloc[0]["first_missing"]), "2020-01-04")

    def test_needs_redownload_uses_alternate_coverage(self):
        cov = pd.DataFrame([
            {"source": "tec", "station": "PLIN", "year": 2019, "expected_days": 365, "processed_days": 10,
             "usable_days": 10, "usable_pct": 2.7},
            {"source": "tec", "station": "TSHI", "year": 2019, "expected_days": 365, "processed_days": 300,
             "usable_days": 300, "usable_pct": 82.2},
        ])
        todo = QR.needs_redownload(cov, ["PLIN"], {"PLIN": ["TSHI", "FIVE"]})
        self.assertEqual(list(todo.station), ["PLIN"])  # 備選站本身不列入
        self.assertEqual(todo.iloc[0].alternates, "TSHI（300 天）、FIVE（0 天）")

    def test_failures_ignore_retried_and_replaced_files(self):
        man = pd.DataFrame([
            {"sha256": "a", "name": "plin0010.20o", "status": "error", "error": "ValueError('黎明前時段沒有足夠衛星')"},
            {"sha256": "b", "name": "plin0020.20o", "status": "error", "error": "缺少星曆或 DCB：x"},
            {"sha256": "b", "name": "plin0020.20o", "status": "ok"},          # 重試成功
            {"sha256": "c", "name": "plin0030.20o", "status": "error", "error": "EOFError()"},
        ])
        man = man.drop_duplicates("sha256", keep="last")
        daily = pd.DataFrame({"station": ["PLIN"], "date": [pd.Timestamp("2020-01-03")]})  # 1/3 有別的檔案成功
        f = QR.failures(man, daily, QR._name_parsers()["tec"], "tec")
        self.assertEqual(list(f.name), ["plin0010.20o"])
        self.assertEqual(f.iloc[0].reason, "黎明前衛星不足，無法估接收機偏差")
        self.assertEqual(QR.classify_error("EOFError()"), "檔案不完整（下載中斷？）")

    def test_dcb_fallback_counts_dates_once(self):
        d = pd.DataFrame([tec_day("PLIN", "2020-01-05", age=10), tec_day("HSIN", "2020-01-05", age=10),
                          tec_day("PLIN", "2020-02-20", age=36, dcb="P1P22001.DCB")])
        d["date"] = pd.to_datetime(d["date"])
        out = QR.dcb_fallback(d).iloc[0]
        self.assertEqual(out.dates, 2)                  # 兩站同一天只算一次
        self.assertEqual(out.fallback_dates, 1)
        self.assertEqual(out.max_age_days, 36)
        self.assertEqual(out.fallback_files, "P1P22001.DCB")

    def test_tec_instrument_changes_match_02_segments(self):
        rows = [tec_day("NDHU", f"2026-10-{d:02d}", bias=33.0) for d in range(1, 8)]
        rows += [tec_day("NDHU", f"2026-10-{d:02d}", inst="SN2|NETR9|5.5|A1|ANT", bias=-3.0) for d in range(8, 12)]
        rows += [tec_day("NDHU", f"2026-10-{d:02d}", inst="SN2|NETR9|5.5|A1|ANT", bias=20.0) for d in range(12, 14)]
        d = pd.DataFrame(rows)
        d["date"] = pd.to_datetime(d["date"])
        ch = QR.tec_instrument_changes(d)
        self.assertEqual([str(x) for x in ch.date], ["2026-10-08", "2026-10-12"])
        self.assertEqual(list(ch.reason), ["儀器識別改變", "接收機偏差跳動"])
        self.assertIn("接收機序號：SN1 → SN2", ch.iloc[0].detail)
        self.assertIn("韌體：5.4 → 5.5", ch.iloc[0].detail)

    def test_magnet_candidates(self):
        rows = [mag_day("XCG", f"2020-01-{d:02d}") for d in range(1, 8)]
        rows.append(mag_day("XCG", "2020-01-08", z=25300.0))                 # Z 跳 300 nT
        rows.append(mag_day("XCG", "2020-01-09", z=25300.0, lat=24.2))       # 座標改變
        rows.append(mag_day("XCG", "2020-01-10", z=26000.0, coverage=0.3))   # 涵蓋率不足 → 不判斷跳動
        d = pd.DataFrame(rows)
        d["date"] = pd.to_datetime(d["date"])
        c = QR.magnet_instrument_candidates(d)
        got = [(str(r.date), r.reason) for r in c.itertuples()]
        self.assertIn(("2020-01-08", "日平均 Z 跳動"), got)
        self.assertIn(("2020-01-09", "標頭座標改變"), got)
        self.assertFalse(any(dt == "2020-01-10" and "跳動" in rs for dt, rs in got))

    def test_end_to_end_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, out = Path(tmp) / "data", Path(tmp) / "out"
            for d in pd.date_range("2020-01-01", "2020-01-20"):
                write_json(data / "magnet" / "daily" / "XCG" / f"{d.date()}.json", mag_day("XCG", str(d.date())))
                write_json(data / "tec" / "daily" / "PLIN" / f"{d.date()}.json", tec_day("PLIN", str(d.date())))
            (data / "tec" / "manifest.jsonl").write_text(
                json.dumps({"sha256": "z", "name": "plin0250.20o", "status": "error", "error": "EOFError()"}) + "\n",
                encoding="utf-8")
            argv = sys.argv
            sys.argv = ["12_quality_report.py", "--data", str(data), "--out", str(out),
                        "--start", "2020-01-01", "--end", "2020-01-31"]
            try:
                QR.main()
            finally:
                sys.argv = argv
            md = (out / "README.md").read_text(encoding="utf-8")
            self.assertIn("# B2 資料品質報告", md)
            self.assertIn("plin0250.20o", md)
            cov = pd.read_csv(out / "coverage_by_station_year.csv")
            self.assertEqual(int(cov[(cov.station == "XCG")].usable_days.iloc[0]), 20)
            self.assertEqual(set(cov[cov.source == "magnet"].station), set(QR.MAGNET_STATIONS))
            gaps = pd.read_csv(out / "gaps.csv")
            self.assertIn("XCG", set(gaps.station))      # 1/21–1/31 缺 11 天


if __name__ == "__main__":
    unittest.main()
