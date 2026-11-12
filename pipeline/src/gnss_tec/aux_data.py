"""下載 TEC 計算需要的輔助資料（公開、免登入）。

來源：BKG（德國聯邦製圖與大地測量局）的 IGS 資料中心鏡像
    廣播星曆：https://igs.bkg.bund.de/root_ftp/IGS/BRDC/YYYY/DDD/brdcDDD0.YYn.gz
    衛星 DCB：https://igs.bkg.bund.de/root_ftp/IGS/products/WWWW/CAS0OPSRAP_YYYYDDD0000_01D_01D_DCB.BIA.gz
              中國科學院（CAS）每日 Bias-SINEX，含 GPS C1W–C2W（即 P1–P2）的衛星偏差
"""
import gzip
import shutil
import urllib.request
from pathlib import Path

import pandas as pd

from .orbit import GPS_EPOCH

BKG = "https://igs.bkg.bund.de/root_ftp/IGS"


def _download(url, dest, timeout=60):
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size > 0:
        return dest, False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "eq-risk-student-project/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as out:
        shutil.copyfileobj(r, out)
    tmp.replace(dest)
    return dest, True


def brdc_url(date):
    d = pd.Timestamp(date)
    doy = d.dayofyear
    return f"{BKG}/BRDC/{d.year}/{doy:03d}/brdc{doy:03d}0.{d.year % 100:02d}n.gz"


def cas_dcb_url(date):
    d = pd.Timestamp(date)
    week = (d - GPS_EPOCH).days // 7
    return f"{BKG}/products/{week}/CAS0OPSRAP_{d.year}{d.dayofyear:03d}0000_01D_01D_DCB.BIA.gz"


def fetch_day(date, aux_dir):
    """下載某一天的廣播星曆與 DCB，回傳 (nav 路徑, dcb 路徑, 紀錄)。"""
    aux_dir = Path(aux_dir)
    log = []
    out = []
    for kind, url in (("brdc", brdc_url(date)), ("dcb", cas_dcb_url(date))):
        dest = aux_dir / kind / url.rsplit("/", 1)[1]
        try:
            p, new = _download(url, dest)
            log.append(f"{kind} {p.name}: {'下載' if new else '已存在'} ({p.stat().st_size / 1e3:.0f} kB)")
            out.append(p)
        except Exception as e:  # noqa: BLE001
            log.append(f"{kind} {url}: 失敗 {e}")
            out.append(None)
    return out[0], out[1], log


# ---------------------------------------------------------------------------
# 長期批次處理（2018 至今）用的來源。2026-11-08 實測 BKG 的檔案涵蓋範圍：
#   CAS 每日 DCB（上面的 cas_dcb_url）只有約 2024-02 ~ 2025-01，不夠用；
#   brdcDDD0.YYn（RINEX 2）在 2018–2021、2025 之後都沒有。
# 所以批次處理改用：
#   星曆：BRDC00WRD_R_YYYYDDD0000_01D_MN.rnx.gz（RINEX 3 混合檔，2018–2026 每天都有）
#   DCB： CODE 每月 P1−P2（products/dcb/p1p2YYMM.dcb.Z，2006-06 起每月都有）
#         與 CAS 每日值相差約 0.3 ns（≈ 0.9 TECU 斜向），同號。
# ---------------------------------------------------------------------------

def brdc3_url(date):
    d = pd.Timestamp(date)
    return f"{BKG}/BRDC/{d.year}/{d.dayofyear:03d}/BRDC00WRD_R_{d.year}{d.dayofyear:03d}0000_01D_MN.rnx.gz"


def code_p1p2_url(year, month, kind="p1p2"):
    """CODE 每月 DCB。kind="p1p2"（P1−P2）或 "p1c1"（P1−C1，給只記錄 C1 的接收機換算 P1）。"""
    return f"{BKG}/products/dcb/{kind}{year % 100:02d}{month:02d}.dcb.Z"


def unlzw(data):
    """解 Unix compress（.Z，LZW）格式。Python 標準函式庫沒有，檔案又小（約 40 kB），直接實作。"""
    if len(data) < 3 or data[0] != 0x1F or data[1] != 0x9D:
        raise ValueError("不是 .Z（compress）格式")
    flags = data[2]
    maxbits = flags & 0x1F
    block = bool(flags & 0x80)
    if flags & 0x60 or not 9 <= maxbits <= 16:
        raise ValueError("不支援的 .Z 旗標")
    n = len(data)
    pos = mark = 3
    bits, mask = 9, 0x1FF
    end = 256 if block else 255          # 最後一個已使用的代碼
    buf = left = 0
    prefix = [0] * (1 << 16)
    suffix = list(range(256)) + [0] * ((1 << 16) - 256)
    prev = None
    final = 0
    out = bytearray()

    def flush():
        # 換代碼寬度或清表時，要跳到「bits 個位元組為一組」的邊界（compress 格式的歷史包袱）
        nonlocal pos, mark, buf, left
        rem = (pos - mark) % bits
        if rem:
            pos += bits - rem
        buf = left = 0
        mark = pos

    while True:
        if end >= mask and bits < maxbits:
            flush()
            bits += 1
            mask = (mask << 1) | 1
        while left < bits:
            if pos >= n:
                return bytes(out)
            buf |= data[pos] << left
            pos += 1
            left += 8
        code = buf & mask
        buf >>= bits
        left -= bits
        if prev is None:
            if code > 255:
                raise ValueError(".Z 資料錯誤：第一個代碼不是字元")
            prev = final = code
            out.append(code)
            continue
        if code == 256 and block:
            flush()
            bits, mask, end, prev = 9, 0x1FF, 256, None
            continue
        temp = code
        stack = []
        if code > end:
            if code != end + 1:
                raise ValueError(".Z 資料錯誤：代碼超出字典")
            stack.append(final)
            code = prev
        while code >= 256:
            stack.append(suffix[code])
            code = prefix[code]
        stack.append(code)
        final = code
        out.extend(reversed(stack))
        if end < mask:
            end += 1
            prefix[end] = prev
            suffix[end] = final
        prev = temp


def read_code_p1p2(path):
    """讀 CODE 每月 DCB（P1−P2 或 P1−C1 格式相同），回傳 {PRN: ns}（只取衛星列，例如 'G01   -7.003'）。"""
    raw = Path(path).read_bytes()
    text = (unlzw(raw) if raw[:2] == b"\x1f\x9d" else raw).decode("ascii", errors="replace")
    out = {}
    for line in text.splitlines():
        if len(line) >= 3 and line[0] == "G" and line[1:3].isdigit() and not line[3:26].strip():
            out[line[:3]] = float(line[26:38])
    return out


def nav_candidate_urls(date):
    """同一天的星曆有好幾個來源；2026-11 實測：有些日子只有 IGS 版，有些日子 WRD 版不完整（只到中午）。"""
    d = pd.Timestamp(date)
    stem = f"{BKG}/BRDC/{d.year}/{d.dayofyear:03d}/"
    tag = f"{d.year}{d.dayofyear:03d}0000_01D_MN.rnx.gz"
    return [stem + f"BRDC00WRD_R_{tag}", stem + f"BRDC00IGS_R_{tag}", stem + f"BRDC00WRD_S_{tag}", brdc_url(d)]


NAV_FULL_HOURS = 20.0     # 最後一筆星曆的 toc 晚於當天 20 時，才算涵蓋全天（星曆有效 ±2 小時）


def get_nav(date, aux_dir):
    """某一天的 GPS 星曆。依序試各來源，取第一個涵蓋全天的；都不完整時取涵蓋最久的。

    下載的 RINEX 3 混合檔約 1.3 MB，只留 GPS 存成 parquet（約 60 kB），原檔刪除。
    回傳 (DataFrame 或 None, 紀錄)。
    """
    from .rinex import read_nav
    d = pd.Timestamp(date).normalize()
    cache = Path(aux_dir) / "nav_gps" / f"{d.year}" / f"{d.dayofyear:03d}.parquet"
    if cache.exists():
        return pd.read_parquet(cache), f"nav {d.date()}: 快取"
    best, best_h, notes = None, -1.0, []
    for url in nav_candidate_urls(d):
        raw = cache.with_name(url.rsplit("/", 1)[1])
        try:
            _download(url, raw)
            nav = read_nav(raw)
        except Exception as e:  # noqa: BLE001
            notes.append(f"{url.rsplit('/', 1)[1]}: {e}")
            continue
        finally:
            raw.unlink(missing_ok=True)
        if nav.empty:
            notes.append(f"{url.rsplit('/', 1)[1]}: 沒有 GPS 星曆")
            continue
        nav = nav[(nav["toc"] >= d - pd.Timedelta(hours=2)) & (nav["toc"] < d + pd.Timedelta(hours=26))]
        h = (nav["toc"].max() - d) / pd.Timedelta(hours=1)
        if h > best_h:
            best, best_h, src = nav, h, url.rsplit("/", 1)[1]
        if h >= NAV_FULL_HOURS:
            break
        notes.append(f"{url.rsplit('/', 1)[1]}: 只到 {h:.1f} 時")
    if best is None:
        return None, f"nav {d.date()}: 全部來源失敗（{'；'.join(notes)}）"
    best.to_parquet(cache, index=False)
    full = "" if best_h >= NAV_FULL_HOURS else f"（⚠ 不完整，只到 {best_h:.1f} 時）"
    return best, f"nav {d.date()}: {src}，{len(best)} 筆{full}"


def get_dcb(date, aux_dir, max_months_back=3, kind="p1p2"):
    """某一天適用的衛星 DCB（CODE 每月；kind 見 code_p1p2_url）。當月還沒公布時往前找，最多 max_months_back 個月。

    回傳 (dict 或 None, 來源檔名, 距離所用月份中點的天數, 紀錄)。
    """
    d = pd.Timestamp(date)
    y, m = d.year, d.month
    logs = []
    for _ in range(max_months_back + 1):
        url = code_p1p2_url(y, m, kind)
        dest = Path(aux_dir) / "dcb_code" / url.rsplit("/", 1)[1]
        try:
            _download(url, dest)
            dcb = read_code_p1p2(dest)
            mid = pd.Timestamp(year=y, month=m, day=15)
            return dcb, dest.name, abs((d - mid).days), logs
        except Exception as e:  # noqa: BLE001
            logs.append(f"dcb {url}: {e}")
            y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return None, None, None, logs


def read_cas_dsb(path, obs1="C1W", obs2="C2W"):
    """讀 Bias-SINEX 中 GPS 衛星的 DSB（obs1 − obs2），單位 ns。回傳 {PRN: ns}。"""
    opener = gzip.open if str(path).endswith(".gz") else open
    out = {}
    with opener(path, "rt", encoding="ascii", errors="replace") as f:
        for line in f:
            if not line.startswith(" DSB "):
                continue
            tok = line.split()
            # 衛星列：DSB SVN PRN OBS1 OBS2 START END UNIT VALUE STD
            if len(tok) >= 10 and tok[2].startswith("G") and tok[2][1:].isdigit() \
                    and tok[3] == obs1 and tok[4] == obs2 and tok[7] == "ns":
                out[tok[2]] = float(tok[8])
    return out
