"""gnss_tec：把 GNSS RINEX 2.11 觀測檔轉成電離層總電子含量（TEC）。

模組：
    rinex       讀 RINEX 2.11 觀測檔與 GPS 廣播星曆
    orbit       由廣播星曆計算衛星位置、仰角、電離層穿刺點（IPP）
    tec         斜向 TEC、相位平滑、DCB 修正、接收機偏差估計、垂直 TEC
    aux_data    下載輔助資料（廣播星曆、衛星 DCB）

只處理 GPS（GLONASS 各衛星頻率不同，需另外處理頻道號，留待之後擴充）。
"""
