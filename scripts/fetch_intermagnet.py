"""從 INTERMAGNET（英國地質調查所 GIN）下載遠端地磁觀測站的 1 秒資料，並分批交給 10_process_magnet.py。

為什麼需要：原訂的參考站金門（KMN）只有 103 天可用；改用「台灣其他測站的中位數」當參考，
會連涵蓋全台的訊號一起扣掉。日本鹿屋（KNY，距台灣約 1,100 km）等觀測站離台灣夠遠、
不受台灣地震影響，又在相近的地方時，適合當「只有太陽活動、沒有台灣地震」的對照。

資料來源：https://imag-data.bgs.ac.uk/GIN_V1/GINServices （GetData，免註冊）
    - 格式 IAGA-2002，和 GDMS 的 MAGNET 日檔相同，可直接用現有的讀取器。
    - 1 秒資料單次請求上限 31 天；這裡一天一個請求，存成 <站><日期>dsec.sec.gz。
    - 使用條件見 https://intermagnet.org/data_conditions.html ，發表時須註明資料來源觀測站與 INTERMAGNET。
2026-11-13 實測：KAK、KNY、MMB 在 2019、2022、2025 都有 1 秒資料；LNP（龍坪）全為缺值；CBI 不提供。

1 秒日檔約 6 MB（gzip 後約 1.5 MB）。每抓滿 --batch-days 天就呼叫 10_process_magnet.py --delete-raw，
所以硬碟上最多只留一批原始檔。已處理過的日期記在 <out>/fetched.jsonl，重跑會跳過。

注意：--out 要和台灣測站的輸出分開（預設 magnet_remote），否則遠端站會混進「測站網中位數」。

執行：python scripts/fetch_intermagnet.py --station KNY --start 2020-10-01 --end 2026-09-30 \
          --inbox E:/123/REMOTE_INBOX --out E:/123/processed/magnet_remote
"""
import argparse
import gzip
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = "https://imag-data.bgs.ac.uk/GIN_V1/GINServices"
ROOT = Path(__file__).resolve().parents[1]
PROCESS = ROOT / "pipeline" / "src" / "10_process_magnet.py"
MISSING = ("99999", "88888")


def url_for(station, day):
    return (f"{BASE}?Request=GetData&Format=iaga2002&observatoryIagaCode={station.upper()}&samplesPerDay=Second"
            f"&dataStartDate={day:%Y-%m-%d}&dataDuration=1&publicationState=best-avail&orientation=XYZF"
            "&recordTermination=UNIX")


def fetch_day(station, day, timeout=180, attempts=4, backoff=15.0):
    """回傳 (bytes 或 None, 訊息)。HTTP 404／400 視為當天沒有資料，不重試。

    這個服務偶爾會在傳輸途中斷線（2026-11-13、11-15 實測：同一個請求有時連續失敗、幾分鐘後又正常），
    所以連線類的錯誤會隔一段時間重試。
    """
    req = urllib.request.Request(url_for(station, day), headers={"User-Agent": "research-client", "Accept": "*/*"})
    msg = ""
    for k in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                chunks = []
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks), ""
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                return None, f"HTTP {e.code}"
            msg = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            msg = f"{type(e).__name__}: {e}"
        if k < attempts - 1:
            time.sleep(backoff * (k + 1))
    return None, f"{msg}（重試 {attempts} 次）"


def valid_fraction(body):
    """資料列中不含缺值代碼的比例（整天都是 99999 的檔案不值得留）。"""
    rows = [ln for ln in body.decode("ascii", errors="replace").splitlines() if ln[:4].isdigit()]
    if not rows:
        return 0.0
    return sum(1 for ln in rows if not any(m in ln for m in MISSING)) / len(rows)


def load_ledger(path):
    done = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                done[(rec["station"], rec["date"])] = rec
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def process_batch(inbox, out, workers):
    cmd = [sys.executable, str(PROCESS), "--inbox", str(inbox), "--out", str(out), "--delete-raw",
           "--workers", str(workers)]
    return subprocess.run(cmd, check=False).returncode


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--station", required=True, help="IAGA 三碼，例如 KNY")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True, help="含當天")
    ap.add_argument("--inbox", required=True)
    ap.add_argument("--out", required=True, help="處理結果資料夾（要和台灣測站分開）")
    ap.add_argument("--batch-days", type=int, default=30)
    ap.add_argument("--pause", type=float, default=1.0, help="每個請求之間停幾秒")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--no-process", action="store_true", help="只下載，不呼叫 10_process_magnet.py")
    args = ap.parse_args()

    sta = args.station.lower()
    inbox, out = Path(args.inbox), Path(args.out)
    inbox.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    ledger_path = out / "fetched.jsonl"
    done = load_ledger(ledger_path)
    day, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    pending = n_ok = n_empty = n_fail = 0
    while day <= end:
        key = (sta.upper(), day.isoformat())
        if key in done and done[key]["status"] in ("ok", "empty"):
            day += timedelta(days=1)
            continue
        body, msg = fetch_day(sta, day)
        rec = {"station": sta.upper(), "date": day.isoformat(),
               "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if body is None:
            rec.update(status="empty" if msg.startswith("HTTP 4") else "failed", note=msg)
        else:
            frac = valid_fraction(body)
            if frac < 0.5:
                rec.update(status="empty", note=f"有效資料 {frac:.0%}")
            else:
                (inbox / f"{sta}{day:%Y%m%d}dsec.sec.gz").write_bytes(gzip.compress(body, 6))
                rec.update(status="ok", bytes=len(body), valid=round(frac, 4))
                pending += 1
        n_ok += rec["status"] == "ok"
        n_empty += rec["status"] == "empty"
        n_fail += rec["status"] == "failed"
        with open(ledger_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if day.day == 1 or rec["status"] == "failed":
            print(f"{sta.upper()} {day}：{rec['status']} {rec.get('note', '')}｜累計 可用 {n_ok}、無資料 {n_empty}、"
                  f"失敗 {n_fail}", flush=True)
        if pending >= args.batch_days and not args.no_process:
            process_batch(inbox, out, args.workers)
            pending = 0
        day += timedelta(days=1)
        time.sleep(args.pause)
    if pending and not args.no_process:
        process_batch(inbox, out, args.workers)
    print(f"完成 {sta.upper()} {args.start}～{args.end}：可用 {n_ok} 天、無資料 {n_empty} 天、失敗 {n_fail} 天"
          f"（失敗的日期重跑時會再試）", flush=True)


if __name__ == "__main__":
    main()
