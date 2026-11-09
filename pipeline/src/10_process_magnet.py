"""B2 第三步：GDMS MAGNET（IAGA-2002 1 秒資料）串流處理。

流程（每個日檔）：
    收件匣中的 .sec / .sec.gz / zip
      → SHA-256 去重（manifest.jsonl）
      → 讀 IAGA-2002：88888／99999 → 缺值；F 由 X、Y、Z 計算
      → 1 分鐘平均（存檔，之後可重算任何特徵，不必重新下載）
      → 每日品質 + 夜間（台灣時間 00–04）ULF 特徵：Sz、Sh、極化比 Sz/Sh
      → 處理成功後刪除原始檔（--delete-raw）；落在事件窗口內的改存到 keep 資料夾
輸出（pipeline/data/magnet/，不進版控）：
    minute/<STA>/<YYYY>/<STA>_<YYYY-MM-DD>.parquet    1 分鐘 X、Y、Z、F
    daily/<STA>/<YYYY-MM-DD>.json                      每日特徵與品質
    magnet_daily.parquet                               所有站、所有日的彙整表（每次執行後重建）
    manifest.jsonl                                     每個原始檔的處理紀錄

用法：
    python pipeline/src/10_process_magnet.py --inbox "C:/Users/.../Downloads/GDMS_inbox"
    python pipeline/src/10_process_magnet.py --inbox <資料夾> --delete-raw \\
        --keep-events earthquake_data/processed/event_list_M55_mainshocks_2018_2026.csv --keep-days 7
"""
import argparse
import json
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geomag import iaga, ulf  # noqa: E402
from stream_io import Manifest, RawDisposer, Stream, iter_inbox, load_keep_windows  # noqa: E402

BASE_DIR = Path(__file__).resolve().parents[2]
OUT_DIR = BASE_DIR / "pipeline" / "data" / "magnet"
NAME_RE = re.compile(r"^([a-z]{3})(\d{8})d?sec\.sec(\.gz)?$", re.IGNORECASE)
VERSION = "magnet-1"


def parse_name(name):
    m = NAME_RE.match(name)
    if not m:
        return None, None
    return m.group(1).upper(), pd.Timestamp(m.group(2))


def process_bytes(data, name):
    """在子程序中執行：讀檔並計算特徵。回傳 (header, daily dict, minute DataFrame)。"""
    head, df = iaga.read_iaga(data, name=name)
    sta, date = parse_name(name)
    head["station"] = head["station"] or sta
    date = date if date is not None else df.index.min().normalize()
    summary = ulf.daily_summary(df, date)
    return head, summary, ulf.minute_means(df)


def write_outputs(out_dir, head, summary, minute):
    sta, d = head["station"], pd.Timestamp(summary["date"])
    mp = out_dir / "minute" / sta / f"{d.year}" / f"{sta}_{d.date()}.parquet"
    mp.parent.mkdir(parents=True, exist_ok=True)
    minute.astype("float32").to_parquet(mp)
    row = {"station": sta, "lat": head["lat"], "lon": head["lon"], "elev": head["elev"],
           "f_from_xyz": head["f_from_xyz"], "data_type": head["data_type"], **summary,
           "date": str(d.date())}
    jp = out_dir / "daily" / sta / f"{d.date()}.json"
    jp.parent.mkdir(parents=True, exist_ok=True)
    jp.write_text(json.dumps(row, ensure_ascii=False, default=float), encoding="utf-8")
    return row


def rebuild_daily(out_dir):
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in (out_dir / "daily").rglob("*.json")]
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["station", "date"]).reset_index(drop=True)
    df.to_parquet(out_dir / "magnet_daily.parquet", index=False)
    return df


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inbox", required=True, help="放 GDMS 下載檔的資料夾（會掃子資料夾與 zip）")
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--delete-raw", action="store_true", help="處理成功後刪除原始檔（預設保留）")
    ap.add_argument("--keep-events", default=None, help="事件清單 CSV（time 欄，UTC）；窗口內的原始檔改為保留")
    ap.add_argument("--keep-days", type=int, default=7, help="事件前後各保留幾天（預設 7）")
    ap.add_argument("--keep-dir", default=None, help="保留原始檔的資料夾（預設 <out>/raw_keep）")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    args = ap.parse_args()

    out_dir = Path(args.out)
    manifest = Manifest(out_dir / "manifest.jsonl")
    windows = load_keep_windows(args.keep_events, args.keep_days)
    keep_dir = Path(args.keep_dir) if args.keep_dir else (out_dir / "raw_keep" if windows else None)
    disposer = RawDisposer(args.delete_raw, keep_dir, windows)

    items = iter_inbox(args.inbox, lambda n: parse_name(n)[0] is not None)
    print(f"收件匣：{args.inbox}，找到 {len(items)} 個 MAGNET 日檔")
    # 分批串流：同時最多 workers × 2 個日檔在記憶體裡（每個約 6 MB），整站 9 年的 zip 也不會撐爆
    stream = Stream(manifest, disposer, lambda n: parse_name(n)[1], max_inflight=args.workers * 2)

    n_ok = n_fail = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for k, job in enumerate(stream.run(items, lambda it, data, _: ex.submit(process_bytes, data, it.name)), 1):
            it = job.item
            try:
                head, summary, minute = job.future.result()
                row = write_outputs(out_dir, head, summary, minute)
                manifest.add(sha256=job.digest, name=it.name, size=job.size, status="ok", station=row["station"],
                             date=row["date"], coverage=round(row["coverage"], 4), version=VERSION)
                n_ok += 1
                ok = True
            except Exception as e:  # noqa: BLE001
                manifest.add(sha256=job.digest, name=it.name, size=job.size, status="error", error=repr(e),
                             version=VERSION)
                print(f"  失敗 {it}: {e}")
                n_fail += 1
                ok = False
            stream.done(job, ok)
            if k % 100 == 0:
                print(f"  已處理 {k} 個（收件匣共 {len(items)} 個）")
    disposer.finish()
    if stream.n_dup:
        print(f"已處理過或重複（相同 SHA-256）：{stream.n_dup} 個，略過")
    daily = rebuild_daily(out_dir)
    print(f"完成：成功 {n_ok}、失敗 {n_fail}；刪除原始檔 {disposer.deleted}、保留 {disposer.kept}")
    if daily is not None:
        cov = daily.groupby("station").agg(days=("date", "size"), first=("date", "min"), last=("date", "max"),
                                           coverage=("coverage", "mean"), ulf_days=("ulf_pol", "count"))
        print(cov.to_string())
        print(f"彙整表：{out_dir / 'magnet_daily.parquet'}")


if __name__ == "__main__":
    main()
