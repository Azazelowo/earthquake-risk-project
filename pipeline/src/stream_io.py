"""前兆資料的「邊下載、邊處理、處理完就刪」共用工具（MAGNET 與 GNSS 共用）。

設計：
- 輸入是一個「收件匣」資料夾：把 GDMS 下載的檔案（或整個 zip）放進去就好，子資料夾也會掃。
- 每個原始檔處理前先算 SHA-256，寫進 manifest.jsonl；同樣內容的檔案只處理一次
  （GNSS 與 GNSS_ALL 有 13 組完全相同的檔案，就是靠這個去重）。
- 處理成功後，依設定刪除原始檔；但若檔案日期落在「保留事件窗口」內（M≥5.5 主震前後 N 天），
  改成搬到 keep 資料夾，以便日後用別的方法重算。
- 處理失敗的檔案一律不刪，manifest 記下原因。
"""
import datetime as dt
import hashlib
import io
import json
import shutil
import zipfile
from pathlib import Path

import pandas as pd


class RawItem:
    """收件匣中的一個原始檔：可能是一般檔案，也可能是 zip 裡的一個成員。"""

    def __init__(self, name, path, member=None):
        self.name = name          # 檔名（不含資料夾），用來判斷測站與日期
        self.path = Path(path)    # 實體檔（zip 成員時是 zip 檔本身）
        self.member = member      # zip 內的路徑；一般檔案為 None

    def read_bytes(self):
        if self.member is None:
            return self.path.read_bytes()
        with zipfile.ZipFile(self.path) as z:
            return z.read(self.member)

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
        for z, ok in self.zip_ok.items():
            if ok and self.delete and z.exists():
                z.unlink()
                self.deleted += 1
