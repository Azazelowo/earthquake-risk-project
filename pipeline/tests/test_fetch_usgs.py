"""fetch_usgs 的單元測試（合成資料與假的 HTTP，不連網）。

執行：python -m unittest discover -s pipeline/tests -v
"""
import argparse
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import fetch_usgs as fu  # noqa: E402

HEADER = ("time,latitude,longitude,depth,mag,magType,nst,gap,dmin,rms,net,id,updated,place,type,"
          "horizontalError,depthError,magError,magNst,status,locationSource,magSource")


def row(t, eid, updated, mag="4.3", status="reviewed", lat="24.4", lon="121.8", place="38 km SSE of Yilan, Taiwan"):
    typ = "" if status == "deleted" else "earthquake"
    return (f'{t},{lat},{lon},12.8,{mag},mb,29,61,0.356,0.62,us,{eid},{updated},"{place}",{typ},'
            f"2.41,4.696,0.154,14,{status},us,us")


def utc(*a):
    return dt.datetime(*a, tzinfo=dt.timezone.utc)


class TestParse(unittest.TestCase):
    def test_fields_and_utc(self):
        text = "\n".join([
            HEADER,
            row("2026-11-03T15:22:04.984Z", "us1", "2026-11-03T20:05:55.175Z"),
            row("2003-01-19T19:45:59.780Z", "us2", "2014-11-07T01:17:36.538Z", mag=""),
            row("2015-11-03T00:46:20.000Z", "us3", "2015-12-14T22:29:58.040Z", status="deleted"),
        ])
        df = fu.parse_csv(text, utc(2026, 11, 4, 0, 0, 0))
        self.assertEqual(list(df.columns), fu.OUTPUT_COLUMNS)
        self.assertEqual(len(df), 3)
        # 時間轉成不帶時區的 UTC，與 events.parquet 一致（不可再出現 8 小時誤差）
        self.assertIsNone(df["time"].dt.tz)
        self.assertEqual(df.loc[0, "time"], pd.Timestamp("2026-11-03 15:22:04.984"))
        self.assertEqual(df.loc[0, "fetched_at"], pd.Timestamp("2026-11-04 00:00:00"))
        self.assertEqual(df.loc[0, "place"], "38 km SSE of Yilan, Taiwan")  # 地名內含逗號
        self.assertEqual(df.loc[0, "source"], "USGS")
        self.assertAlmostEqual(df.loc[0, "horiz_err_km"], 2.41)
        self.assertTrue(pd.isna(df.loc[1, "mag"]))
        self.assertEqual(df.loc[2, "status"], "deleted")
        self.assertTrue(pd.isna(df.loc[2, "event_type"]))

    def test_header_only(self):
        df = fu.parse_csv(HEADER + "\n", utc(2026, 11, 4))
        self.assertEqual(len(df), 0)
        self.assertEqual(list(df.columns), fu.OUTPUT_COLUMNS)


class TestParams(unittest.TestCase):
    def test_update_always_has_starttime(self):
        p = fu.build_params(utc(1990, 1, 1), utc(2026, 11, 4), fu.DEFAULT_BBOX,
                            updated_after=utc(2026, 11, 3, 22))
        # 只帶 updatedafter 時 USGS 預設只查 30 天，必須明確帶 starttime
        self.assertEqual(p["starttime"], "1990-01-01T00:00:00")
        self.assertEqual(p["updatedafter"], "2026-11-03T22:00:00")
        self.assertEqual(p["includedeleted"], "true")
        self.assertNotIn("minmagnitude", p)

    def test_count_drops_includedeleted(self):
        seen = []

        def get(url):
            seen.append(url)
            return '{"count": 7, "maxAllowed": 20000}'

        n = fu.count_events(fu.build_params(utc(2020, 1, 1), utc(2021, 1, 1), fu.DEFAULT_BBOX), get=get)
        self.assertEqual(n, 7)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(seen[0]).query)
        self.assertNotIn("includedeleted", q)  # /count 不接受這個參數（實測回 400）
        self.assertEqual(q["format"], ["geojson"])


class TestPlanChunks(unittest.TestCase):
    def setUp(self):
        # 10 筆事件集中在 2020 年 3 月（模擬餘震序列）
        self.times = [utc(2020, 3, 1) + dt.timedelta(hours=6 * i) for i in range(10)]

    def count(self, s, e):
        return sum(s <= t <= e for t in self.times)

    def test_split_until_under_limit(self):
        start, end = utc(2020, 1, 1), utc(2021, 1, 1)
        chunks = fu.plan_chunks(start, end, self.count, limit=3)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(n <= 3 for _, _, n in chunks))
        # 時間段首尾相接、完整覆蓋
        self.assertEqual(chunks[0][0], start)
        self.assertEqual(chunks[-1][1], end)
        for (_, e1, _), (s2, _, _) in zip(chunks, chunks[1:]):
            self.assertEqual(e1, s2)

    def test_no_split_when_small(self):
        chunks = fu.plan_chunks(utc(2020, 1, 1), utc(2021, 1, 1), self.count, limit=100)
        self.assertEqual(len(chunks), 1)

    def test_too_dense_raises(self):
        with self.assertRaises(RuntimeError):
            fu.plan_chunks(utc(2020, 1, 1), utc(2020, 1, 2), lambda s, e: 99, limit=3)


class TestUpsert(unittest.TestCase):
    def frame(self, rows, fetched):
        return fu.parse_csv("\n".join([HEADER] + rows), fetched)

    def test_newer_version_wins(self):
        old = self.frame([row("2026-11-01T00:00:00Z", "usA", "2026-11-01T01:00:00Z", mag="4.3"),
                          row("2026-11-02T00:00:00Z", "usB", "2026-11-02T01:00:00Z")], utc(2026, 11, 2, 2))
        new = self.frame([row("2026-11-01T00:00:00Z", "usA", "2026-11-03T05:00:00Z", mag="4.6"),
                          row("2026-11-02T00:00:00Z", "usB", "2026-11-03T06:00:00Z", status="deleted")],
                         utc(2026, 11, 3, 7))
        out = fu.upsert(old, new).set_index("source_event_id")
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out.loc["usA", "mag"], 4.6)
        self.assertEqual(out.loc["usB", "status"], "deleted")

    def test_older_version_does_not_overwrite(self):
        new = self.frame([row("2026-11-01T00:00:00Z", "usA", "2026-11-03T05:00:00Z", mag="4.6")],
                         utc(2026, 11, 3, 7))
        stale = self.frame([row("2026-11-01T00:00:00Z", "usA", "2026-11-01T01:00:00Z", mag="4.3")],
                           utc(2026, 11, 4))
        out = fu.upsert(new, stale)
        self.assertAlmostEqual(out.loc[0, "mag"], 4.6)

    def test_empty(self):
        self.assertEqual(len(fu.upsert(None, None)), 0)


class FakeResponse(io.BytesIO):
    pass


class TestHttpGet(unittest.TestCase):
    def test_retry_on_503(self):
        calls, sleeps = [], []

        def opener(req, timeout):
            calls.append(req.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b""))
            return FakeResponse(b"ok")

        self.assertEqual(fu.http_get("https://x.test/q", opener=opener, sleep=sleeps.append), "ok")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(sleeps), 1)

    def test_400_not_retried(self):
        calls = []

        def opener(req, timeout):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {},
                                         io.BytesIO(b"Error 400: exceeds search limit of 20000"))

        with self.assertRaises(fu.UsgsQueryError) as cm:
            fu.http_get("https://x.test/q", opener=opener, sleep=lambda s: None)
        self.assertEqual(len(calls), 1)
        self.assertIn("20000", str(cm.exception))


class FakeUsgs:
    """依查詢參數回應的假 USGS：events 為 [(time, id, updated, status)]。"""

    def __init__(self, events):
        self.events = events
        self.urls = []

    def _match(self, q):
        s = pd.Timestamp(q["starttime"][0], tz="UTC")
        e = pd.Timestamp(q["endtime"][0], tz="UTC")
        ua = pd.Timestamp(q["updatedafter"][0], tz="UTC") if "updatedafter" in q else None
        out = []
        for t, eid, upd, status in self.events:
            if not (s <= pd.Timestamp(t) <= e):
                continue
            if ua is not None and pd.Timestamp(upd) <= ua:
                continue
            if status == "deleted" and q.get("includedeleted") != ["true"]:
                continue
            out.append((t, eid, upd, status))
        return out

    def __call__(self, url):
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(parsed.query)
        hits = self._match(q)
        if parsed.path.endswith("/count"):
            return json.dumps({"count": sum(h[3] != "deleted" for h in hits), "maxAllowed": 20000})
        return "\n".join([HEADER] + [row(t, eid, upd, status=st) for t, eid, upd, st in hits]) + "\n"


class TestEndToEnd(unittest.TestCase):
    def test_backfill_update_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir, raw_dir = Path(tmp) / "catalog", Path(tmp) / "raw"
            kw = dict(sleep=lambda s: None)
            args = argparse.Namespace(start="1990-01-01", end=None, bbox=list(fu.DEFAULT_BBOX), min_mag=None)

            # 1) 回補：兩筆事件
            fake = FakeUsgs([("1999-09-20T17:47:15Z", "us921", "2015-01-01T00:00:00Z", "reviewed"),
                             ("2026-11-02T02:23:08Z", "usNew", "2026-11-02T09:00:00Z", "reviewed")])
            fu.run_backfill(args, out_dir, raw_dir, get=fake, now=lambda: utc(2026, 11, 3, 0), **kw)
            state = json.loads((out_dir / "usgs_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["last_success_utc"], "2026-11-03T00:00:00")
            self.assertEqual(len(pd.read_parquet(out_dir / "usgs.parquet")), 2)

            # 2) 增量：usNew 被修訂、新增 usLate、921 沒變（不應被重抓）
            fake2 = FakeUsgs([("1999-09-20T17:47:15Z", "us921", "2015-01-01T00:00:00Z", "reviewed"),
                              ("2026-11-02T02:23:08Z", "usNew", "2026-11-03T10:00:00Z", "deleted"),
                              ("2026-11-03T15:22:04Z", "usLate", "2026-11-03T20:00:00Z", "reviewed")])
            fu.run_update(args, out_dir, raw_dir, get=fake2, now=lambda: utc(2026, 11, 4, 0), **kw)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(fake2.urls[-1]).query)
            self.assertEqual(q["starttime"], ["1990-01-01T00:00:00"])
            self.assertEqual(q["updatedafter"], ["2026-11-02T22:00:00"])  # 上次成功時間往前 2 小時
            df = pd.read_parquet(out_dir / "usgs.parquet").set_index("source_event_id")
            self.assertEqual(sorted(df.index), ["us921", "usLate", "usNew"])
            self.assertEqual(df.loc["usNew", "status"], "deleted")

            # 3) 不連網重建，結果應與增量後相同
            before = pd.read_parquet(out_dir / "usgs.parquet")
            fu.run_rebuild(out_dir, raw_dir)
            after = pd.read_parquet(out_dir / "usgs.parquet")
            pd.testing.assert_frame_equal(before, after)

    def test_update_without_backfill_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace()
            with self.assertRaises(SystemExit):
                fu.run_update(args, Path(tmp), Path(tmp) / "raw", get=lambda u: "", sleep=lambda s: None)


if __name__ == "__main__":
    unittest.main()
