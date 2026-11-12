"""scripts/archive_fractal_report.py 的單元測試（不連網，fetch 以假資料取代）。"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "archive_fractal_report.py"
spec = importlib.util.spec_from_file_location("archive_fractal_report", SCRIPT)
AF = importlib.util.module_from_spec(spec)
spec.loader.exec_module(AF)

DATE = "2026-11-12"
HOME = (f'<a href="/maps/{DATE}/big.png">x</a><img src="/research/mesoscale/{DATE}/hot-tw.webp">'
        f'<img src="/research/mesoscale/2026-11-08/old.webp"><img src="/research/mesoscale/{DATE}/hot-tw.webp">')


def fake_site(revision="rev1", fail=()):
    pages = {"/version.json": json.dumps({"report_date": DATE, "revision": revision}).encode(),
             "/": HOME.encode(), f"/statistics/{DATE}/": b"<p>stats</p>",
             f"/statistics/event-coverage/{DATE}/": b"<p>cov</p>", f"/mesoscale/{DATE}/": b"<p>meso</p>",
             "/operational/": b"<p>ops</p>", f"/maps/{DATE}/big.png": b"PNG",
             f"/research/mesoscale/{DATE}/hot-tw.webp": b"WEBP"}

    def fetch(path):
        if path in fail or path not in pages:
            return 404, None, "not found"
        return 200, pages[path], ""
    return fetch


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self._fetch = AF.fetch
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        AF.fetch = self._fetch
        self.tmp.cleanup()

    def run_archive(self, **kw):
        AF.fetch = fake_site(**kw)
        return AF.archive(self.out, pause=0)

    def test_local_path(self):
        self.assertEqual(AF.local_path("/"), "index.html")
        self.assertEqual(AF.local_path(f"/statistics/{DATE}/"), f"statistics/{DATE}/index.html")
        for bad in ("/../etc/passwd", "/a b", "//evil.example/x", "/a/../../b"):
            with self.subTest(bad=bad):
                if bad == "//evil.example/x":
                    self.assertFalse(AF.local_path(bad).startswith("/"))
                else:
                    self.assertRaises(ValueError, AF.local_path, bad)

    def test_image_links_only_today_and_deduplicated(self):
        self.assertEqual(AF.image_links(HOME, DATE),
                         [f"/maps/{DATE}/big.png", f"/research/mesoscale/{DATE}/hot-tw.webp"])

    def test_saves_pages_images_and_manifest(self):
        self.assertEqual(self.run_archive(), 0)
        snap = self.out / DATE / "rev1"
        man = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(man["files"]), 8)
        self.assertTrue(all("sha256" in f for f in man["files"]))
        self.assertEqual((snap / "research" / "mesoscale" / DATE / "hot-tw.webp").read_bytes(), b"WEBP")
        self.assertEqual(len((self.out / "index.jsonl").read_text(encoding="utf-8").splitlines()), 1)

    def test_same_revision_is_skipped_new_revision_is_kept_separately(self):
        self.run_archive()
        self.assertEqual(self.run_archive(), 0)
        self.assertEqual(len((self.out / "index.jsonl").read_text(encoding="utf-8").splitlines()), 1)
        self.run_archive(revision="rev2")
        self.assertTrue((self.out / DATE / "rev1" / "manifest.json").exists())
        self.assertTrue((self.out / DATE / "rev2" / "manifest.json").exists())

    def test_missing_subpage_is_recorded_not_fatal(self):
        self.assertEqual(self.run_archive(fail=("/operational/",)), 0)
        man = json.loads((self.out / DATE / "rev1" / "manifest.json").read_text(encoding="utf-8"))
        failed = [f for f in man["files"] if "file" not in f]
        self.assertEqual([f["url"] for f in failed], ["/operational/"])

    def test_missing_home_fails_and_leaves_no_manifest(self):
        self.assertEqual(self.run_archive(fail=("/",)), 1)
        self.assertFalse((self.out / DATE / "rev1" / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
