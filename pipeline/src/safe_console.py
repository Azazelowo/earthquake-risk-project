"""讓 print 在 Windows 的 cp950 終端機上不會因為 ⚠、✗、≥、−、² 這類符號而中斷。

用法：在入口程式最上面（設定 sys.path 之後）寫一行 import safe_console。stream_io 也會匯入它，
所以直接呼叫 stream_io 的程式（例如測試）同樣受到保護。

為什麼需要：
    Windows 中文版的終端機預設編碼是 cp950（Big5），裡面沒有 ⚠、✗、≥、≤、−、² 等符號。
    Python 遇到無法編碼的字元時，預設會丟出 UnicodeEncodeError，整支程式停在那一行；
    子程序的輸出接到管線（pipe）時也一樣。2026-11-17 實測：pipeline/tests 在 cp950 下有 4 個測試
    因此失敗，設 PYTHONUTF8=1 後全部通過。

做法：只改「遇到無法編碼的字元時怎麼辦」，改成 \\u26a0 這種跳脫寫法；編碼本身不變，
所以中文照原本的方式輸出，不會變成整片亂碼。寫檔（報告、CSV、JSON）一律另外指定 utf-8，不受影響。
"""
import sys


def configure(streams=None):
    """把輸出串流的錯誤處理改成 backslashreplace。回傳實際改到的串流數。"""
    changed = 0
    for stream in streams if streams is not None else (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # 被換成不支援 reconfigure 的物件（例如某些 IDE 或測試框架的輸出擷取器），保持原樣
        try:
            reconfigure(errors="backslashreplace")
            changed += 1
        except (ValueError, OSError):  # 串流已關閉，或不允許變更
            pass
    return changed


configure()
