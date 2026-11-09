"""前兆資料的「邊下載、邊處理、處理完就刪」共用工具（MAGNET 與 GNSS 共用）。

設計：
- 輸入是一個「收件匣」資料夾：把 GDMS 下載的檔案（或整個 zip）放進去就好，子資料夾也會掃。
- 每個原始檔處理前先算 SHA-256，寫進 manifest.jsonl；同樣內容的檔案只處理一次
  （GNSS 與 GNSS_ALL 有 13 組完全相同的檔案，就是靠這個去重）。
- 處理成功後，依設定刪除原始檔；但若檔案日期落在「保留事件窗口」內（M≥5.5 主震前後 N 天），
  改成搬到 keep 資料夾，以便日後用別的方法重算。
- 處理失敗的檔案一律不刪，manifest 記下原因。
- 分批串流（Stream）：一次只讀進「同時處理中」的幾個檔案。GDMS 可以一次下載整站 9 年
  （例如 XCG 2018–2026 一個 zip 1.4 GB，解開約 10–20 GB），全部讀進記憶體會不夠。
"""
import datetime as dt
import hashlib
import io
import json
import shutil
import zipfile
from collections import deque
from pathlib import Path

import pandas as pd

_ZIPS = {}


def _zip(path):
    """重複使用已開啟的 zip（整站 9 年的 zip 有數千個成員，每讀一個就重開一次很慢）。"""
    z = _ZIPS.get(path)
    if z is None:
        z = _ZIPS[path] = zipfile.ZipFile(path)
    return z


def close_zips():
    """關閉所有開啟中的 zip。Windows 上 zip 開著就刪不掉，所以刪檔前要先呼叫。"""
    for z in _ZIPS.values():
        z.close()
    _ZIPS.clear()


class RawItem:
    """收件匣中的一個原始檔：可能是一般檔案，也可能是 zip 裡的一個成員。"""

    def __init__(self, name, path, member=None):
        self.name = name          # 檔名（不含資料夾），用來判斷測站與日期
        self.path = Path(path)    # 實體檔（zip 成員時是 zip 檔本身）
        self.member = member      # zip 內的路徑；一般檔案為 None

    def read_bytes(self):
        if self.member is None:
            return self.path.read_bytes()
        return _zip(self.path).read(self.member)

    def open_binary(self):
        return io.BytesIO(self.read_bytes())

    def __repr__(self):
        return f"{self.path.name}:{self.member}" if self.member else self.path.name


def iter_inbox(inbox, accept):
    """列出收件匣內 accept(檔名) 為 True 的原始檔（含 zip 內成員），依檔名排序。"""
    items = []
    for p in sorted(Path(inbox).rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(p) as z:
                    for m in z.namelist():
                        nm = m.rsplit("/", 1)[-1]
                        if nm and accept(nm):
                            items.append(RawItem(nm, p, m))
            except zipfile.BadZipFile:
                print(f"略過損壞的 zip：{p}")
        elif accept(p.name):
            items.append(RawItem(p.name, p))
    return sorted(items, key=lambda it: it.name)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class Manifest:
    """append-only 處理紀錄。一行一個原始檔：sha256、檔名、大小、狀態、處理時間、程式版本。"""

    def __init__(self, path):
        self.path = Path(path)
        self.done = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    if r.get("status") == "ok":
                        self.done[r["sha256"]] = r

    def seen(self, digest):
        return digest in self.done

    def add(self, **rec):
        rec.setdefault("processed_at", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
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

    zip 檔要等裡面所有成員都處理成功才刪；成員若需保留，只把該成員解出到 keep 資料夾。
    """

    def __init__(self, delete, keep_dir, windows):
        self.delete = delete
        self.keep_dir = Path(keep_dir) if keep_dir else None
        self.windows = windows
        self.zip_ok = {}       # zip 路徑 → 是否全部成功
        self.kept = 0
        self.deleted = 0

    def done(self, item, date, ok):
        keep = ok and self.keep_dir is not None and date is not None and in_windows(date, self.windows)
        if keep:
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
            self.zip_ok[item.path] = self.zip_ok.get(item.path, True) and ok
            return
        if ok and self.delete and item.path.exists():
            item.path.unlink()
            self.deleted += 1

    def finish(self):
        close_zips()
        for z, ok in self.zip_ok.items():
            if ok and self.delete and z.exists():
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
    - 依收件匣順序回傳，manifest 的順序和以前一樣。
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
        self.max_seen_inflight = 0

    def run(self, items, submit):
        queue, pending = deque(), {}
        for it in items:
            self.n_items += 1
            data = it.read_bytes()
            digest = sha256(data)
            if self.manifest.seen(digest):
                self.n_dup += 1
                self.disposer.done(it, self.date_of(it.name), True)
                continue
            if digest in pending:
                self.n_dup += 1
                pending[digest].dups.append(it)
                continue
            fut = submit(it, data, digest)
            size = len(data)
            del data
            if fut is None:
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
        for d in job.dups:
            self.disposer.done(d, self.date_of(d.name), ok)
