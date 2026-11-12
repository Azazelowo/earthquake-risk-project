"""收件匣對 tar／tgz 壓縮檔的支援，以及壞檔、部分選取時的資料安全（stream_io.py）。全部用合成資料。

執行：python -m unittest discover -s pipeline/tests -v
"""
import gzip
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

import pandas as pd

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(HERE))

import stream_io  # noqa: E402


def is_sec(name):
    return name.endswith(".sec")


def date_of(name):
    """xcg20180105dsec.sec → 2018-01-05"""
    return pd.Timestamp(name[3:11])


def make_tar(path, members, mode="w:gz", top="MAGNETXCG"):
    """members：[(壓縮檔內路徑, bytes)]，照給定順序寫入。GDMS 的 tgz 在檔內不照日期排，最前面有一個資料夾項目。"""
    with tarfile.open(path, mode) as t:
        d = tarfile.TarInfo(top)
        d.type = tarfile.DIRTYPE
        t.addfile(d)
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))


def run_stream(items, manifest, disposer, fail=()):
    """用同步的假工作跑完 Stream。回傳 (stream, 送去處理的檔名清單)。"""
    stream = stream_io.Stream(manifest, disposer, date_of, max_inflight=3)
    order = []

    def submit(it, data, digest):
        f = Future()
        f.set_result(data)
        return f

    for job in stream.run(items, submit):
        ok = job.item.name not in fail
        order.append(job.item.name)
        manifest.add(sha256=job.digest, name=job.item.name, status="ok" if ok else "error")
        stream.done(job, ok)
    disposer.finish()
    return stream, order


def read_manifest(path):
    return [json.loads(s) for s in Path(path).read_text(encoding="utf-8").splitlines() if s.strip()]


def kill_worker(data, name):
    """給子程序執行：模擬子程序被系統強制結束（例如記憶體不足）。"""
    os._exit(1)


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # 後登記的先執行：先關掉開著的壓縮檔，Windows 才刪得掉暫存資料夾
        self.addCleanup(stream_io.close_archives)
        self.d = Path(tmp.name)
        self.inbox = self.d / "inbox"
        self.inbox.mkdir()
        self.manifest = stream_io.Manifest(self.d / "out" / "manifest.jsonl")


class TestTarInbox(ArchiveCase):
    def test_tar_members_keep_archive_order(self):
        make_tar(self.inbox / "MAGNETXCG.tgz", [
            ("MAGNETXCG/xcg20221223dsec.sec", b"c"),
            ("MAGNETXCG/notes.txt", b"x"),
            ("MAGNETXCG/xcg20180105dsec.sec", b"a"),
            ("MAGNETXCG/xcg20200801dsec.sec", b"b"),
        ])
        (self.inbox / "xcg20261101dsec.sec").write_bytes(b"z")
        with zipfile.ZipFile(self.inbox / "batch.zip", "w") as z:
            z.writestr("sub/xcg20190101dsec.sec", "y")
        items = stream_io.iter_inbox(self.inbox, is_sec)
        # 一般檔案與 zip 成員照檔名排；tar 成員排在後面，照壓縮檔內的順序（不是檔名順序）
        self.assertEqual([it.name for it in items],
                         ["xcg20190101dsec.sec", "xcg20261101dsec.sec",
                          "xcg20221223dsec.sec", "xcg20180105dsec.sec", "xcg20200801dsec.sec"])
        self.assertEqual([it.kind for it in items], ["zip", "file", "tar", "tar", "tar"])
        offsets = [it.tarinfo.offset_data for it in items if it.kind == "tar"]
        self.assertEqual(offsets, sorted(offsets))       # 位置遞增 → 讀的時候只會往後解壓
        self.assertEqual([it.read_bytes() for it in items], [b"y", b"z", b"c", b"a", b"b"])
        self.assertTrue(all(it.removable for it in items))

    def test_suffixes_and_single_file_gz_is_not_a_tar(self):
        make_tar(self.inbox / "a.tar", [("xcg20180101dsec.sec", b"1")], mode="w")
        make_tar(self.inbox / "b.tar.gz", [("xcg20180102dsec.sec", b"2")])
        make_tar(self.inbox / "C.TGZ", [("xcg20180104dsec.sec", b"4")])
        (self.inbox / "xcg20180103dsec.sec.gz").write_bytes(gzip.compress(b"3"))    # 單一檔案的 gzip，不是 tar
        items = stream_io.iter_inbox(self.inbox, lambda n: n.endswith((".sec", ".sec.gz")))
        self.assertEqual(sorted((it.name, it.kind) for it in items),
                         [("xcg20180101dsec.sec", "tar"), ("xcg20180102dsec.sec", "tar"),
                          ("xcg20180103dsec.sec.gz", "file"), ("xcg20180104dsec.sec", "tar")])

    def test_inbox_can_be_a_single_archive(self):
        tgz = self.inbox / "MAGNETKMN.tgz"
        make_tar(tgz, [("MAGNETKMN/kmn20180102dsec.sec", b"k2"), ("MAGNETKMN/kmn20180101dsec.sec", b"k1")])
        (self.inbox / "xcg20180101dsec.sec").write_bytes(b"other")
        items = stream_io.iter_inbox(tgz, is_sec)
        self.assertEqual([it.name for it in items], ["kmn20180102dsec.sec", "kmn20180101dsec.sec"])

    def test_not_a_tar_is_skipped_with_warning(self):
        (self.inbox / "broken.tgz").write_bytes(b"this is not a tar archive")
        (self.inbox / "xcg20180101dsec.sec").write_bytes(b"ok")
        items = stream_io.iter_inbox(self.inbox, is_sec)
        self.assertEqual([it.name for it in items], ["xcg20180101dsec.sec"])


class TestTarStream(ArchiveCase):
    def test_keep_window_without_rereading_then_delete(self):
        tgz = self.inbox / "MAGNETXCG.tgz"
        make_tar(tgz, [
            ("MAGNETXCG/xcg20261101dsec.sec", b"in-window"),
            ("MAGNETXCG/xcg20180105dsec.sec", b"far-away"),
            ("MAGNETXCG/xcg20261108dsec.sec", b"in-window-2"),
        ])
        ev = self.d / "events.csv"
        pd.DataFrame({"time": ["2026-11-05 03:00:00"]}).to_csv(ev, index=False)
        disp = stream_io.RawDisposer(True, self.d / "keep", stream_io.load_keep_windows(ev, 7))
        items = stream_io.iter_inbox(self.inbox, is_sec)
        with mock.patch.object(stream_io, "_tar_read", wraps=stream_io._tar_read) as spy:
            stream, order = run_stream(items, self.manifest, disp)
        self.assertEqual(spy.call_count, 3)              # 每個成員只解壓一次；保留時沒有回頭再讀
        self.assertEqual(order, ["xcg20261101dsec.sec", "xcg20180105dsec.sec", "xcg20261108dsec.sec"])
        self.assertEqual((self.d / "keep" / "xcg20261101dsec.sec").read_bytes(), b"in-window")
        self.assertEqual((self.d / "keep" / "xcg20261108dsec.sec").read_bytes(), b"in-window-2")
        self.assertFalse((self.d / "keep" / "xcg20180105dsec.sec").exists())
        self.assertEqual(disp.kept, 2)
        self.assertFalse(tgz.exists())                   # 全部成功 → 刪除（Windows 上要先關閉）
        self.assertTrue(all(it._held is None for it in items))    # 暫留的內容都已釋放

    def test_without_delete_flag_nothing_is_removed(self):
        tgz = self.inbox / "MAGNETXCG.tgz"
        make_tar(tgz, [("MAGNETXCG/xcg20180105dsec.sec", b"a"), ("MAGNETXCG/xcg20180104dsec.sec", b"b")])
        loose = self.inbox / "xcg20180103dsec.sec"
        loose.write_bytes(b"c")
        run_stream(stream_io.iter_inbox(self.inbox, is_sec), self.manifest, stream_io.RawDisposer(False, None, []))
        self.assertTrue(tgz.exists())
        self.assertTrue(loose.exists())
        self.assertEqual(len(read_manifest(self.manifest.path)), 3)

    def test_failed_member_keeps_archive(self):
        tgz = self.inbox / "MAGNETXCG.tgz"
        make_tar(tgz, [("MAGNETXCG/xcg20180105dsec.sec", b"a"), ("MAGNETXCG/xcg20180104dsec.sec", b"b")])
        run_stream(stream_io.iter_inbox(self.inbox, is_sec), self.manifest,
                   stream_io.RawDisposer(True, None, []), fail={"xcg20180104dsec.sec"})
        self.assertTrue(tgz.exists())

    def test_already_processed_members_are_skipped(self):
        tgz = self.inbox / "MAGNETXCG.tgz"
        make_tar(tgz, [("MAGNETXCG/xcg20180105dsec.sec", b"a"), ("MAGNETXCG/xcg20180104dsec.sec", b"b")])
        self.manifest.add(sha256=stream_io.sha256(b"a"), name="x", status="ok")      # 以前處理過
        stream, order = run_stream(stream_io.iter_inbox(self.inbox, is_sec), self.manifest,
                                   stream_io.RawDisposer(False, None, []))
        self.assertEqual(order, ["xcg20180104dsec.sec"])
        self.assertEqual(stream.n_dup, 1)

    def test_truncated_tgz_is_salvaged_and_never_deleted(self):
        full = self.d / "full.tgz"
        members = [(f"MAGNETXCG/xcg201801{k:02d}dsec.sec", os.urandom(200_000)) for k in range(1, 7)]
        make_tar(full, members)
        raw = full.read_bytes()
        tgz = self.inbox / "MAGNETXCG.tgz"
        tgz.write_bytes(raw[: len(raw) * 6 // 10])       # 模擬下載到 60% 就中斷
        items = stream_io.iter_inbox(self.inbox, is_sec)
        self.assertTrue(0 < len(items) < len(members))
        self.assertTrue(all(not it.removable for it in items))
        stream, order = run_stream(items, self.manifest, stream_io.RawDisposer(True, None, []))
        self.assertTrue(tgz.exists())                    # 不完整的壓縮檔，即使 --delete-raw 也不刪
        recs = read_manifest(self.manifest.path)
        ok = [r for r in recs if r["status"] == "ok"]
        err = [r for r in recs if r["status"] == "error"]
        self.assertEqual(len(ok) + len(err), len(items))
        self.assertGreaterEqual(len(ok), 1)              # 完整的成員照常處理
        self.assertEqual(stream.n_read_errors, len(err))
        good = {stream_io.sha256(data) for _, data in members}
        self.assertTrue(all(r["sha256"] in good for r in ok))     # 讀出來的內容和原始資料一致
        self.assertTrue(all(r["error"].startswith("讀取失敗") for r in err))


class TestDataSafety(ArchiveCase):
    def test_corrupt_zip_member_does_not_abort_batch(self):
        z = self.inbox / "all.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("xcg20180101dsec.sec", b"A" * 1000)
            zf.writestr("xcg20180102dsec.sec", b"B" * 1000)
            zf.writestr("xcg20180103dsec.sec", b"C" * 1000)
        raw = bytearray(z.read_bytes())
        i = raw.find(b"B" * 1000)
        raw[i + 10] ^= 0xFF                              # 弄壞中間那個成員（CRC 會對不上）
        z.write_bytes(bytes(raw))
        stream, order = run_stream(stream_io.iter_inbox(self.inbox, is_sec), self.manifest,
                                   stream_io.RawDisposer(True, None, []))
        self.assertEqual(order, ["xcg20180101dsec.sec", "xcg20180103dsec.sec"])    # 壞的那個跳過，其餘照常
        self.assertEqual(stream.n_read_errors, 1)
        self.assertTrue(z.exists())                      # 有成員失敗 → zip 不刪
        err = [r for r in read_manifest(self.manifest.path) if r["status"] == "error"]
        self.assertEqual([r["name"] for r in err], ["xcg20180102dsec.sec"])
        self.assertTrue(err[0]["error"].startswith("讀取失敗"))
        self.assertFalse(stream_io.Manifest(self.manifest.path).seen(err[0]["sha256"]))    # 下次會重試

    def test_partially_selected_archive_is_not_deleted(self):
        z = self.inbox / "two_stations.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("xcg20180101dsec.sec", "a")
            zf.writestr("kmn20180101dsec.sec", "b")
            zf.writestr("readme.txt", "r")
        tgz = self.inbox / "two_stations.tgz"
        make_tar(tgz, [("x/xcg20180102dsec.sec", b"c"), ("x/kmn20180102dsec.sec", b"d")])
        only_xcg = lambda n: is_sec(n) and n.startswith("xcg")  # noqa: E731
        items = stream_io.iter_inbox(self.inbox, only_xcg, protect=is_sec)
        self.assertEqual(sorted(it.name for it in items), ["xcg20180101dsec.sec", "xcg20180102dsec.sec"])
        run_stream(items, self.manifest, stream_io.RawDisposer(True, None, []))
        self.assertTrue(z.exists())                      # 裡面還有沒處理的 KMN 資料 → 不能刪
        self.assertTrue(tgz.exists())

    def test_archive_with_only_non_data_leftovers_is_deleted(self):
        z = self.inbox / "one_station.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("xcg20180101dsec.sec", "a")
            zf.writestr("readme.txt", "r")               # 不是資料檔，不影響刪除
        items = stream_io.iter_inbox(self.inbox, is_sec, protect=is_sec)
        run_stream(items, self.manifest, stream_io.RawDisposer(True, None, []))
        self.assertFalse(z.exists())

    def test_archive_not_fully_handled_is_never_deleted(self):
        """程式中途停掉（只處置了一部分成員）時，即使處置過的都成功，也不能刪壓縮檔。"""
        tgz = self.inbox / "MAGNETXCG.tgz"
        make_tar(tgz, [(f"MAGNETXCG/xcg2018010{k}dsec.sec", b"x%d" % k) for k in (3, 1, 2)])
        z = self.inbox / "batch.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for k in (4, 5, 6):
                zf.writestr(f"xcg2018010{k}dsec.sec", f"y{k}")
        items = stream_io.iter_inbox(self.inbox, is_sec)
        disp = stream_io.RawDisposer(True, None, [])
        for it in items:
            if it.name not in ("xcg20180102dsec.sec", "xcg20180106dsec.sec"):     # 各留一個沒處置
                disp.done(it, date_of(it.name), ok=True)
        disp.finish()
        self.assertTrue(tgz.exists())
        self.assertTrue(z.exists())
        self.assertEqual(sorted(p.name for p in disp.unfinished), ["MAGNETXCG.tgz", "batch.zip"])
        self.assertEqual(disp.deleted, 0)

    def test_manifest_survives_interrupted_write(self):
        p = self.d / "m" / "manifest.jsonl"
        p.parent.mkdir()
        p.write_text(json.dumps({"sha256": "aa", "status": "ok"}) + "\n" + '{"sha256": "bb", "sta', encoding="utf-8")
        m = stream_io.Manifest(p)
        self.assertTrue(m.seen("aa"))
        self.assertFalse(m.seen("bb"))
        self.assertEqual(m.n_bad_lines, 1)
        m.add(sha256="cc", name="n", status="ok")
        m2 = stream_io.Manifest(p)
        self.assertTrue(m2.seen("cc"))                   # 新紀錄沒有黏在寫到一半的那一行後面
        self.assertEqual(m2.n_bad_lines, 1)


class TestMagnetScriptTgz(ArchiveCase):
    def test_magnet_script_streams_tgz(self):
        from test_b2_precursor import iaga_text
        out = self.d / "out_magnet"
        days = ["2026-10-31", "2026-10-29", "2026-10-30"]           # 故意不照日期排
        make_tar(self.inbox / "MAGNETXCG.tgz",
                 [(f"MAGNETXCG/xcg{day.replace('-', '')}dsec.sec",
                   iaga_text().replace("2026-10-31", day).encode("ascii")) for day in days])
        r = subprocess.run([sys.executable, str(SRC / "10_process_magnet.py"), "--inbox", str(self.inbox),
                            "--out", str(out), "--delete-raw", "--workers", "1"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.inbox / "MAGNETXCG.tgz").exists())
        self.assertEqual(sorted(p.stem for p in (out / "daily" / "XCG").glob("*.json")), sorted(days))
        recs = read_manifest(out / "manifest.jsonl")
        self.assertEqual([r["status"] for r in recs], ["ok", "ok", "ok"])
        self.assertEqual([r["date"] for r in recs], days)            # manifest 照壓縮檔內的順序
        daily = pd.read_parquet(out / "magnet_daily.parquet")
        self.assertEqual(len(daily), 3)

    def test_worker_crash_stops_cleanly_and_keeps_archive(self):
        """子程序被系統結束時：程式要正常收尾、以代碼 2 結束、不怪罪檔案、不刪壓縮檔。"""
        from test_b2_precursor import MAG, iaga_text
        out = self.d / "out_magnet"
        tgz = self.inbox / "MAGNETXCG.tgz"
        make_tar(tgz, [(f"MAGNETXCG/xcg{day.replace('-', '')}dsec.sec",
                        iaga_text(n=60).replace("2026-10-31", day).encode("ascii"))
                       for day in ("2026-10-31", "2026-10-29", "2026-10-30")])
        argv = ["10_process_magnet.py", "--inbox", str(self.inbox), "--out", str(out), "--delete-raw",
                "--workers", "1"]
        with mock.patch.object(MAG, "process_bytes", kill_worker), mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as cm:
                MAG.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertTrue(tgz.exists())
        manifest = out / "manifest.jsonl"
        self.assertEqual(read_manifest(manifest) if manifest.exists() else [], [])


class TestBlasThreads(unittest.TestCase):
    def run_py(self, code, extra_env):
        env = {k: v for k, v in os.environ.items()
               if k not in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")}
        env.update(extra_env)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                           cwd=str(SRC), timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_defaults_to_one_thread_but_respects_user_setting(self):
        code = "import blas_threads, os; print(os.environ['OPENBLAS_NUM_THREADS'], os.environ['OMP_NUM_THREADS'])"
        self.assertEqual(self.run_py(code, {}), "1 1")
        self.assertEqual(self.run_py(code, {"OPENBLAS_NUM_THREADS": "3"}), "3 1")

    def test_processing_scripts_set_it_before_numpy(self):
        for script in ("10_process_magnet.py", "11_process_gnss_tec.py"):
            text = (SRC / script).read_text(encoding="utf-8")
            self.assertIn("import blas_threads", text)
            self.assertLess(text.index("import blas_threads"), text.index("import pandas"), script)


if __name__ == "__main__":
    unittest.main()
