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
