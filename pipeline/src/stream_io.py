"""前兆資料的「邊下載、邊處理、處理完就刪」共用工具（MAGNET 與 GNSS 共用）。

設計：
- 輸入是一個「收件匣」資料夾：把 GDMS 下載的檔案（或整個 zip、tgz）放進去就好，子資料夾也會掃。
  收件匣也可以直接指定單一個壓縮檔。
- 每個原始檔處理前先算 SHA-256，寫進 manifest.jsonl；同樣內容的檔案只處理一次
  （GNSS 與 GNSS_ALL 有 13 組完全相同的檔案，就是靠這個去重）。
- 處理成功後，依設定刪除原始檔；但若檔案日期落在「保留事件窗口」內（M≥5.5 主震前後 N 天），
  改成搬到 keep 資料夾，以便日後用別的方法重算。
- 處理失敗、讀取失敗的檔案一律不刪，manifest 記下原因；單一壞檔不會讓整批中斷。
- 分批串流（Stream）：一次只讀進「同時處理中」的幾個檔案。GDMS 可以一次下載整站 9 年
  （例如 XCG 2018–2026 一個壓縮檔 1.4 GB，解開約 13 GB），全部讀進記憶體會不夠。

壓縮檔的兩種讀法（為什麼 tar 和 zip 的處理順序不同）：
- zip 有目錄，可以隨意跳著讀任何一個成員，所以 zip 成員和一般檔案一起依檔名排序。
- .tgz／.tar.gz 是「先 tar 再 gzip」，只能從頭往後解壓；往回讀一個成員就得從頭重新解壓一次。
  GDMS 整站下載給的是 .tgz（例如 MAGNETXCG.tgz 有 2,184 個日檔，而且在檔內不照日期排），
  若照檔名順序去讀，等於反覆重解 1.4 GB。所以 tar 成員一律「照壓縮檔內的順序」處理；
  落在保留窗口、之後要另存一份的檔案，在讀到的當下就把內容留著（RawItem.hold），不回頭再讀。

什麼時候不刪壓縮檔（即使加了 --delete-raw）：
- 有任何一個成員處理失敗或讀取失敗；
- 壓縮檔不完整（下載中斷），只列得出一部分成員；
- 壓縮檔裡還有「沒被選上、但屬於資料」的成員（例如只處理部分測站時，其他測站的檔案）。
"""
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import tarfile
import zipfile
import zlib
from collections import deque
from pathlib import Path

import pandas as pd

TAR_SUFFIXES = (".tgz", ".tar.gz", ".tar")
_TAR_ERRORS = (tarfile.TarError, EOFError, OSError, zlib.error)

MAX_DEFAULT_WORKERS = 4


def default_workers():
    """--workers 的預設值：CPU 邏輯核心數的一半，但最多 4。

    為什麼設上限：每個子程序都要各自載入 numpy、pandas、scipy 並在記憶體放一個日檔。
    2026-11-11 在 16 核心、8 GB 記憶體的筆電上用 8 個程序試跑，啟動時就因記憶體不足而當掉；
    核心多不代表記憶體夠。記憶體充裕的機器可以自己用 --workers 調高。
    """
    return max(1, min(MAX_DEFAULT_WORKERS, (os.cpu_count() or 2) // 2))


POOL_BROKEN_MSG = ("✗ 有子程序被系統強制結束（最常見的原因是記憶體不足），處理提前停止。\n"
                   "  已完成的結果都已保存，原始檔與壓縮檔都沒有被刪除。\n"
                   "  請用較小的 --workers（例如 2 或 4）重新執行同一個指令，會從中斷的地方接著做。")

_ZIPS = {}
_TARS = {}
_TAR_BROKEN = set()


def _zip(path):
    """重複使用已開啟的 zip（整站 9 年的 zip 有數千個成員，每讀一個就重開一次很慢）。"""
    z = _ZIPS.get(path)
    if z is None:
        z = _ZIPS[path] = zipfile.ZipFile(path)
    return z


def _tar(path):
    """重複使用已開啟的 tar。gzip 壓縮的 tar 重開一次，等於下次讀取要從頭解壓。"""
    t = _TARS.get(path)
    if t is None:
        t = _TARS[path] = tarfile.open(path, "r:*")
    return t


def _tar_read(path, info):
    """讀出 tar 內的一個成員。照壓縮檔內的順序呼叫時只會往後解壓；往回讀也讀得到，但很慢。

    一旦某個成員讀取失敗（壓縮檔不完整或損毀），同一個壓縮檔之後的成員直接回報失敗，
    不再嘗試解壓，避免每個成員都重跑一次會失敗的解壓。
    """
    if path in _TAR_BROKEN:
        raise OSError(f"{path.name} 先前讀取失敗（壓縮檔不完整或損毀），其餘成員略過")
    try:
        f = _tar(path).extractfile(info)
        if f is None:
            raise tarfile.ReadError(f"{info.name} 不是一般檔案")
        data = f.read()
        if len(data) != info.size:
            raise EOFError(f"{info.name} 只讀到 {len(data)} / {info.size} 位元組")
        return data
    except _TAR_ERRORS:
        _TAR_BROKEN.add(path)
        raise


def close_archives():
    """關閉所有開啟中的 zip 與 tar。Windows 上壓縮檔開著就刪不掉，所以刪檔前要先呼叫。"""
    for a in list(_ZIPS.values()) + list(_TARS.values()):
        a.close()
    _ZIPS.clear()
    _TARS.clear()
    _TAR_BROKEN.clear()


close_zips = close_archives      # 舊名稱，保留相容


class RawItem:
    """收件匣中的一個原始檔：一般檔案，或 zip／tar 壓縮檔裡的一個成員。"""

    def __init__(self, name, path, member=None, kind=None, tarinfo=None):
        self.name = name          # 檔名（不含資料夾），用來判斷測站與日期
        self.path = Path(path)    # 實體檔（壓縮檔成員時是壓縮檔本身）
        self.member = member      # 壓縮檔內的路徑；一般檔案為 None
        self.kind = kind or ("file" if member is None else "zip")     # "file"、"zip"、"tar"
        self.tarinfo = tarinfo    # kind == "tar" 時：成員在壓縮檔內的位置資訊
        self.removable = True     # False：所屬壓縮檔不完整，或裡面還有沒被選上的資料檔 → 處理完也不能刪
        self.siblings = None      # 所屬壓縮檔內被選上的成員總數（iter_inbox 填入）；全部處置過才可能刪壓縮檔
        self._held = None

    def read_bytes(self):
        if self._held is not None:
            return self._held
        if self.member is None:
            return self.path.read_bytes()
        if self.kind == "tar":
            return _tar_read(self.path, self.tarinfo)
        return _zip(self.path).read(self.member)

    def open_binary(self):
        return io.BytesIO(self.read_bytes())

    def hold(self, data):
        """把已讀出的內容留在記憶體，之後 read_bytes() 直接回傳（給 tar 成員的保留流程用）。"""
        self._held = data

    def release(self):
        self._held = None

    def __repr__(self):
        return f"{self.path.name}:{self.member}" if self.member else self.path.name


def _basename(member):
    return member.replace("\\", "/").rsplit("/", 1)[-1]


def _zip_items(p, accept, protect):
    items, removable = [], True
    try:
        with zipfile.ZipFile(p) as z:
            for m in z.namelist():
                nm = _basename(m)
                if not nm:
                    continue
                if accept(nm):
                    items.append(RawItem(nm, p, m, kind="zip"))
                elif protect is not None and protect(nm):
                    removable = False
    except zipfile.BadZipFile:
        print(f"略過損壞的 zip：{p}")
        return []
    for it in items:
        it.removable = removable
        it.siblings = len(items)
    return items


def _tar_items(p, accept, protect):
    """tar 成員依壓縮檔內的順序列出。

    gzip 壓縮的 tar 沒有目錄，要從頭解到尾才知道裡面有哪些檔案（1.4 GB 的壓縮檔約需 1 分鐘）。
    壓縮檔不完整時，列得出來的成員照樣處理，但整個壓縮檔不會被刪。
    """
    items, removable = [], True
    print(f"讀取壓縮檔目錄：{p.name} …", flush=True)
    try:
        with tarfile.open(p, "r:*") as t:
            for info in t:
                if not info.isfile():
                    continue
                nm = _basename(info.name)
                if not nm:
                    continue
                if accept(nm):
                    items.append(RawItem(nm, p, info.name, kind="tar", tarinfo=info))
                elif protect is not None and protect(nm):
                    removable = False
    except _TAR_ERRORS as e:
        removable = False
        reason = (str(e).splitlines() or [""])[0]
        print(f"⚠ 壓縮檔不完整或無法讀取：{p}（{type(e).__name__}: {reason}）。"
              f"已列出的 {len(items)} 個檔案照常處理，這個壓縮檔不會被刪除；請重新下載。")
    items.sort(key=lambda it: it.tarinfo.offset_data)
    for it in items:
        it.removable = removable
        it.siblings = len(items)
    return items


def iter_inbox(inbox, accept, protect=None):
    """列出收件匣內 accept(檔名) 為 True 的原始檔（含 zip、tar 內的成員）。

    順序：一般檔案與 zip 成員依檔名排序；tar 成員排在後面，每個壓縮檔依檔內順序（理由見模組說明）。
    inbox 可以是資料夾（會掃子資料夾），也可以是單一個檔案。
    protect(檔名)：沒有被 accept、但屬於「不能弄丟的資料檔」時回傳 True（例如只處理部分測站時，
    其他測站的檔案）。壓縮檔裡只要有這種成員，處理完也不會刪除整個壓縮檔。
    """
    root = Path(inbox)
    paths = [root] if root.is_file() else sorted(root.rglob("*"))
    items, tar_items = [], []
    for p in paths:
        if not p.is_file():
            continue
        low = p.name.lower()
        if low.endswith(".zip"):
            items.extend(_zip_items(p, accept, protect))
        elif low.endswith(TAR_SUFFIXES):
            tar_items.extend(_tar_items(p, accept, protect))
        elif accept(p.name):
            items.append(RawItem(p.name, p))
    return sorted(items, key=lambda it: it.name) + tar_items


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class Manifest:
    """append-only 處理紀錄。一行一個原始檔：sha256、檔名、大小、狀態、處理時間、程式版本。"""

    def __init__(self, path):
        self.path = Path(path)
        self.done = {}
        self.n_bad_lines = 0
        self._needs_newline = False
        if self.path.exists():
            text = self.path.read_text(encoding="utf-8")
            self._needs_newline = bool(text) and not text.endswith("\n")
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    self.n_bad_lines += 1      # 上次執行被中斷時，可能留下寫到一半的一行
                    continue
                if r.get("status") == "ok" and r.get("sha256"):
                    self.done[r["sha256"]] = r
            if self.n_bad_lines:
                print(f"⚠ {self.path} 有 {self.n_bad_lines} 行無法解析（可能是上次中斷時寫到一半），已略過；"
                      "對應的檔案會重新處理。")

    def seen(self, digest):
        return digest in self.done

    def add(self, **rec):
        rec.setdefault("processed_at", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            if self._needs_newline:            # 上一行沒有換行結尾：先補上，新紀錄才不會黏在壞掉的那一行後面
                f.write("\n")
                self._needs_newline = False
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        if rec.get("status") == "ok":
            self.done[rec["sha256"]] = rec


def load_keep_windows(event_csv, days):
    """讀事件清單（需有 time 欄，UTC），回傳 [(開始日, 結束日)]。沒有給檔案時回傳空清單。"""
    if not event_csv:
        return []
    ev = pd.read_csv(event_csv, parse_dates=["time"])
    d = pd.Timedelta(days=days)
    return [((t - d).normalize(), (t + d).normalize()) for t in ev["time"]]


def in_windows(date, windows):
    date = pd.Timestamp(date).normalize()
    return any(a <= date <= b for a, b in windows)


class RawDisposer:
    """處理成功後的原始檔處置：刪除、保留到 keep 資料夾，或什麼都不做。

    壓縮檔（zip、tar）要同時符合三個條件才刪：被選上的成員「每一個」都處置過（數量對得上）、
    全部成功、而且 removable。所以程式中途停掉（例如子程序當掉）時，只處理了一部分的壓縮檔不會被刪。
    成員若需保留，只把該成員的內容寫到 keep 資料夾。
    """

    def __init__(self, delete, keep_dir, windows):
        self.delete = delete
        self.keep_dir = Path(keep_dir) if keep_dir else None
        self.windows = windows
        self.zip_ok = {}       # 壓縮檔路徑 → 目前處置過的成員是否都成功且 removable
        self.zip_n = {}        # 壓縮檔路徑 → 已處置的成員數
        self.zip_total = {}    # 壓縮檔路徑 → 被選上的成員總數
        self.unfinished = []   # finish() 時還沒處置完、因此沒刪的壓縮檔
        self.kept = 0
        self.deleted = 0

    def wants_keep(self, date):
        """這個日期的原始檔，處理成功後是否要另存一份到 keep 資料夾。"""
        return self.keep_dir is not None and date is not None and in_windows(date, self.windows)

    def done(self, item, date, ok):
        if ok and self.wants_keep(date):
            dest = self.keep_dir / item.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if item.member is None:
                if self.delete:
                    shutil.move(str(item.path), dest)
                else:
                    shutil.copy2(item.path, dest)
            else:
                dest.write_bytes(item.read_bytes())
            self.kept += 1
        if item.member is not None:
            self.zip_ok[item.path] = self.zip_ok.get(item.path, True) and ok and item.removable
            self.zip_n[item.path] = self.zip_n.get(item.path, 0) + 1
            if item.siblings is not None:
                self.zip_total[item.path] = item.siblings
            return
        if ok and self.delete and item.path.exists():
            item.path.unlink()
            self.deleted += 1

    def finish(self):
        close_archives()
        for z, ok in self.zip_ok.items():
            complete = z not in self.zip_total or self.zip_n.get(z, 0) >= self.zip_total[z]
            if not complete:
                self.unfinished.append(z)
            elif ok and self.delete and z.exists():
                z.unlink()
                self.deleted += 1


class Stream:
    """分批串流：逐一讀檔、算 SHA-256、送去處理，同時處理中的檔案最多 max_inflight 個。

    用法：
        stream = Stream(manifest, disposer, date_of, max_inflight=workers * 2)
        for job in stream.run(items, submit):      # submit(item, data, digest) → Future 或 None
            ok = ...處理 job.future.result()，寫 manifest...
            stream.done(job, ok)
    - manifest 已有的（相同內容處理成功過）直接當成功處置，不再處理。
    - 同一批裡內容相同的檔案（例如 GNSS 與 GNSS_ALL 各放一份）只處理一次，結果套用到兩份。
    - submit 回傳 None 表示它自己已經記錄失敗並處置原始檔，這個檔案就跳過。
    - 讀不出來的檔案（壓縮檔成員損毀、檔案被占用）記為失敗後繼續處理下一個，次數在 n_read_errors。
    - 依收件匣順序回傳，manifest 的順序和收件匣順序相同。
    """

    class Job:
        def __init__(self, item, digest, size, future):
            self.item, self.digest, self.size, self.future = item, digest, size, future
            self.dups = []

    def __init__(self, manifest, disposer, date_of, max_inflight=8):
        self.manifest, self.disposer, self.date_of = manifest, disposer, date_of
        self.max_inflight = max(1, int(max_inflight))
        self.n_dup = 0
        self.n_items = 0
        self.n_read_errors = 0
        self.max_seen_inflight = 0

    def run(self, items, submit):
        queue, pending = deque(), {}
        for it in items:
            self.n_items += 1
            date = self.date_of(it.name)
            try:
                data = it.read_bytes()
            except Exception as e:  # noqa: BLE001  單一壞檔不能讓整批中斷（NFR-B2-07）
                self.n_read_errors += 1
                self.manifest.add(sha256=f"unread:{it!r}", name=it.name, size=None, status="error",
                                  error=f"讀取失敗：{e!r}")
                self.disposer.done(it, date, False)
                print(f"  讀取失敗 {it}: {e}")
                continue
            digest = sha256(data)
            if it.kind == "tar" and self.disposer.wants_keep(date):
                it.hold(data)      # tar 往回讀很慢：要另存的檔案先把內容留著，處置時直接寫出
            if self.manifest.seen(digest):
                self.n_dup += 1
                self.disposer.done(it, date, True)
                it.release()
                continue
            if digest in pending:
                self.n_dup += 1
                pending[digest].dups.append(it)
                continue
            fut = submit(it, data, digest)
            size = len(data)
            del data
            if fut is None:
                it.release()
                continue
            job = self.Job(it, digest, size, fut)
            pending[digest] = job
            queue.append(job)
            self.max_seen_inflight = max(self.max_seen_inflight, len(queue))
            while len(queue) >= self.max_inflight:
                job = queue.popleft()
                pending.pop(job.digest, None)
                yield job
        while queue:
            job = queue.popleft()
            pending.pop(job.digest, None)
            yield job

    def done(self, job, ok):
        """處置這個檔案以及同內容的重複檔案。"""
        date = self.date_of(job.item.name)
        self.disposer.done(job.item, date, ok)
        job.item.release()
        for d in job.dups:
            self.disposer.done(d, self.date_of(d.name), ok)
            d.release()
