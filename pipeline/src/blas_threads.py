"""讓這個程序（以及它啟動的子程序）的數值函式庫只用 1 條執行緒。

用法：在 import numpy／pandas／scipy「之前」寫一行 import blas_threads。

為什麼需要：
    10_process_magnet.py、11_process_gnss_tec.py 用多個程序平行處理日檔。numpy 與 scipy 各自帶一份
    OpenBLAS，預設每個程序各開「CPU 核心數」條執行緒，並替每條執行緒配置一塊緩衝區。16 核心的機器
    開 8 個程序，光是啟動就要配置數百塊緩衝區。2026-11-11 第一次用實際資料（MAGNETHLN.tgz）試跑時，
    因此出現「OpenBLAS error: Memory allocation still failed after 10 retries」，子程序被系統終止。
    這些程式的平行化靠的是多程序，單一程序內不需要多執行緒，所以固定為 1；計算結果不受影響。

使用者若自己設了這些環境變數，以使用者的設定為準（setdefault 不會覆蓋）。
"""
import os

VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")

for _var in VARS:
    os.environ.setdefault(_var, "1")
