"""每日存檔「全球強震碎形分析日報」的當日頁面，供日後驗證其警戒區命中率。

為什麼要存：該網站首頁只顯示當天的警戒區，事後無法得知某一天「當時」圈了哪些區域。
要檢驗它宣稱的命中率是否高於隨機基準，必須有我們自己在當天留下、之後不會被改動的副本。

做法：
1. 讀 /version.json 取得 report_date 與 revision。
2. 若 <out>/<report_date>/<revision>/manifest.json 已存在 → 這一版已存過，直接結束。
   同一天若對方改版（revision 不同），會另存一份，舊的不覆蓋。
3. 抓首頁、當日統計頁、事件覆蓋頁、中尺度開窗頁、戰情頁，以及這些頁面連到的當日圖檔。
4. 每個檔案記錄網址、HTTP 狀態、大小、SHA256、抓取時間（UTC），寫進 manifest.json；
   並在 <out>/index.jsonl 追加一行摘要。

只用標準函式庫。執行：python scripts/archive_fractal_report.py --out <資料夾>
結束代碼：0 = 已存檔或已存過；1 = 首頁或 version.json 抓不到（當天需重試）。
"""
import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://global-seismic-fractal-analysis-dailyreport.unclejoe0306.chatgpt.site"
USER_AGENT = "Mozilla/5.0 (compatible; research-archiver)"
TIMEOUT = 60
MAX_BYTES = 8 * 1024 * 1024       # 單一頁面上限
MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 單張圖上限：全球大圖 PNG 約 4.8 MB，同圖另有 webp 縮圖，跳過以免分支每年長 2 GB
IMG_EXT_RE = re.compile(r"\.(?:webp|png|jpe?g)$")
MAX_IMAGES = 80                   # 單次最多抓幾張圖
PAUSE_S = 1.0                     # 每個請求之間停一下，不對對方主機造成負擔
PAGES = ["/", "/statistics/{date}/", "/statistics/event-coverage/{date}/", "/mesoscale/{date}/", "/operational/"]
IMG_RE = re.compile(r'(?:href|src)="(/[A-Za-z0-9._/\-]+\.(?:webp|png|jpe?g))"')
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REV_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def fetch(path):
    """回傳 (HTTP 狀態, 內容 bytes 或 None, 錯誤訊息)。"""
    req = urllib.request.Request(BASE + path, headers={"User-Agent": USER_AGENT})
    limit = MAX_IMAGE_BYTES if IMG_EXT_RE.search(path) else MAX_BYTES
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read(limit + 1)
            if len(body) > limit:
                return r.status, None, f"超過 {limit} bytes，未存"
            return r.status, body, ""
    except urllib.error.HTTPError as e:
        return e.code, None, str(e)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, None, str(e)


def local_path(url_path):
    """網址路徑 → 存檔的相對路徑。只接受安全字元，結尾是 / 的補上 index.html。"""
    p = url_path.split("?")[0]
    if not re.fullmatch(r"/[A-Za-z0-9._/\-]*", p) or ".." in p:
        raise ValueError(f"不安全的路徑：{url_path}")
    if p.endswith("/"):
        p += "index.html"
    return p.lstrip("/")


def image_links(html, date):
    """頁面中屬於當日（路徑含 /<date>/）的圖檔連結，去重、保持出現順序。"""
    seen = []
    for m in IMG_RE.finditer(html):
        u = m.group(1)
        if f"/{date}/" in u and u not in seen:
            seen.append(u)
    return seen


def archive(out_dir, pause=PAUSE_S):
    status, body, err = fetch("/version.json")
    if body is None:
        print(f"錯誤：抓不到 version.json（HTTP {status} {err}）")
        return 1
    version = json.loads(body.decode("utf-8"))
    date, rev = str(version.get("report_date", "")), str(version.get("revision", ""))
    if not DATE_RE.match(date) or not REV_RE.match(rev):
        print(f"錯誤：version.json 格式不符預期：{version}")
        return 1
    snap = Path(out_dir) / date / rev
    if (snap / "manifest.json").exists():
        print(f"{date} 的版次 {rev} 已存過，略過")
        return 0

    files, todo, done, n_img = [], [p.format(date=date) for p in PAGES], set(), 0

    def save(url_path, content):
        dest = snap / local_path(url_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        return dest.relative_to(snap).as_posix()

    files.append({"url": "/version.json", "status": status, "bytes": len(body),
                  "sha256": hashlib.sha256(body).hexdigest(), "file": save("/version.json", body),
                  "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    while todo:
        path = todo.pop(0)
        if path in done:
            continue
        done.add(path)
        time.sleep(pause)
        st, content, err = fetch(path)
        rec = {"url": path, "status": st, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if content is None:
            rec["error"] = err
            if path == "/":
                print(f"錯誤：抓不到首頁（HTTP {st} {err}）")
                return 1
        else:
            rec.update(bytes=len(content), sha256=hashlib.sha256(content).hexdigest(), file=save(path, content))
            if rec["file"].endswith(".html"):
                for u in image_links(content.decode("utf-8", errors="replace"), date):
                    if u not in done and u not in todo and n_img < MAX_IMAGES:
                        todo.append(u)
                        n_img += 1
        files.append(rec)

    ok = [f for f in files if "file" in f]
    manifest = {"source": BASE, "report_date": date, "revision": rev, "version": version,
                "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": files}
    (snap / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(Path(out_dir) / "index.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"report_date": date, "revision": rev, "report_cutoff": version.get("report_cutoff"),
                             "archived_at": manifest["archived_at"], "files_ok": len(ok),
                             "files_failed": len(files) - len(ok),
                             "bytes": sum(f["bytes"] for f in ok)}, ensure_ascii=False) + "\n")
    print(f"已存檔 {date}（版次 {rev}）：{len(ok)} 個檔案、{sum(f['bytes'] for f in ok) / 1e6:.2f} MB；"
          f"失敗 {len(files) - len(ok)} 個 → {snap}")
    for f in files:
        if "file" not in f:
            print(f"  未存：{f['url']}（HTTP {f['status']} {f.get('error', '')}）")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="存檔根目錄")
    args = ap.parse_args()
    sys.exit(archive(args.out))


if __name__ == "__main__":
    main()
