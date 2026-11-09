# B2 前兆資料下載與處理清單（2018-01-01 ～ 2026-09-30）

> 方案 C：MAGNET＋GNSS＋GNSS_ETEC，代表站、全時段、邊下載邊處理。2026-11-08 訂定。

## 1. GDMS 下載限制（2026-11 實測）

| 條件 | 上限 |
|---|---|
| MAGNET，一次下載**全部**測站 | 選取時間 < 10 年 |
| 其他測網，一次下載**全部**測站 | 7 天 |
| 任何測網，**單一**或**部分**測站 | 選取時間 < 10 年 |
| 下載速度 | MAGNET XCG 7 天（43 MB）不到 1 秒 |
| 整站全期間 | MAGNET XCG 2018-01-01 ～ 2026-09-30 一個 zip 約 1.4 GB（2026-11-10 使用者實測，未下載） |

**建議（2026-11-10 更新）：MAGNET 每次下載「1 站 × 全期間」，zip 不要解壓，直接放進收件匣。**
每天的地磁檔解開約 6.2 MB，壓縮後約 0.7 MB，所以整站 zip 只有 1–2 GB，8 個站下載 8 次就好。
處理程式會直接從 zip 裡一個一個讀，同時只有幾個檔案在記憶體裡（實測 300 個日檔，主程序記憶體約 200 MB，
和 30 個日檔時相同），處理完全部成功才刪 zip。硬碟峰值 ≈ zip 大小。

GNSS 也可以整站下載；若 zip 超過約 2.5 GB，改成分 2–3 段（例如 2018–2020、2021–2023、2024–2026）以免超過 3 GB 硬碟峰值。
**不要先解壓縮**：解開後一站可能有 10–20 GB。

查詢結果為「無資料」通常是測站在那段時間沒有運作。例如 GNSS PEPU 的 station.info 最後一段紀錄在 2019 年第 218 天結束，GNSS_ETEC 測網的資料則到 2024-04-07 為止。

## 2. 要下載的測站

### MAGNET（8 站；1 秒資料，IAGA-2002）

| 測站 | 位置 | 角色 |
|---|---|---|
| XCG | 花蓮新城 | 花蓮（事件最多的區域） |
| HLN | 花蓮 | 花蓮，與 XCG 互相驗證 |
| YHG | 北部 | 北部 |
| NCG | 宜蘭 | 北部、東北 |
| CNU | 中部 | 中部 |
| TTN | 台東 | 東南 |
| LNU | 南部（恆春半島東側） | 南部 |
| **KMN** | 金門 | **參考站**：距本島 > 200 km，用來扣除磁層、太陽風造成的全區共同變化（`mag_szref_z`） |

### GNSS（10 站；RINEX 2.11 觀測檔，輸出格式選 `o`）

挑選條件：2010 年以前已開始運作、2018 年後接收機幾乎沒換過（station.info）、2026-10 仍有資料（E:\123\GNSS_ALL），並平均分布在全島。

| 測站 | 區域 | 啟用 | 2018 後的儀器段數 | 備選 |
|---|---|---|---|---|
| PLIN | 新北 | 2004 | 3 | TSHI、FIVE |
| HSIN | 新竹 | 1994 | 1 | YAME、WIPN |
| ILAN | 宜蘭 | 1994 | 1 | LTUN、SLNP |
| TUNM | 花蓮 | 2001 | 1 | BLOW、DNFU |
| CHIU | 中部山區 | 2001 | 1 | DAHU、YUSN |
| GAIS | 嘉南 | 1994 | 1 | ZEND、NEMN |
| DCHU | 台東 | 2005 | 1 | CHUL、TTCS |
| LIKN | 高屏 | 2004 | 1 | CTOU、SGAN |
| DANL | 恆春 | 2010 | 1 | HENC |
| PANG | 澎湖（西側遠端） | 1993 | 1 | — |

某站某年若顯示「無資料」，改抓同一列的備選站。

### GNSS_ETEC

測網資料到 2024-04-07 為止，NDH5 的 2024-03-17 ～ 04-07 已在 `E:\123\GNSS_ETEC`（03-31 在 GDMS 上本來就沒有資料）。**不必再下載。**

## 3. 下載量與處理時間（以實測檔案大小估算）

| 項目 | 數量 | 下載量 | 處理後保存 | 處理時間 |
|---|---|---|---|---|
| MAGNET | 8 站 × 3,195 天 | 約 154 GB | 約 1.1 GB | 約 5 小時（8 核心，每檔約 0.65 秒） |
| GNSS | 10 站 × 3,195 天 | 約 106 GB | 約 0.4 GB（另有星曆快取約 0.25 GB） | 約 14 小時（12 核心，每檔約 1.6 秒） |
| 合計 | 162 次下載（1 站 × 1 年）；MAGNET 改整站下載後約 8 ＋ 10～30 次 | 約 260 GB（不會同時存在） | 約 1.8 GB | 可分多次，隨下載進度處理 |

## 4. 處理流程

每抓完一批，就把檔案（資料夾或 zip 都可以）放進收件匣，然後執行：

```powershell
# 地磁：處理後刪原始檔；M≥5.5 主震前後 7 天的原始檔搬到 pipeline/data/magnet/raw_keep/
python pipeline\src\10_process_magnet.py --inbox "C:\Users\...\Downloads\GDMS_inbox" --delete-raw `
    --keep-events docs\b2_download_plan\event_list_M55_mainshocks_2018_2026.csv --keep-days 7

# GNSS TEC：同上
python pipeline\src\11_process_gnss_tec.py --inbox "C:\Users\...\Downloads\GDMS_inbox" --delete-raw --workers 12 `
    --keep-events docs\b2_download_plan\event_list_M55_mainshocks_2018_2026.csv --keep-days 7

# 每處理完一批：看哪些站年還缺、哪些檔案失敗，決定下一批要抓什麼
python pipeline\src\12_quality_report.py      # → docs\reports\b2_data_quality\README.md

# 全部下載完（或任何時候想看目前結果）：
python pipeline\src\00_download_space_weather.py --offline   # 已下載過就加 --offline
python pipeline\src\02_clean_features.py
python pipeline\src\03_train_backtest.py --config pipeline\configs\b3_precursor.json
```

- 同一個檔案放兩次不會重複處理（以 SHA-256 判斷）。處理失敗的檔案不會被刪除，下次執行會重試，失敗原因記在 `manifest.jsonl`。
- **保留事件窗口的原始檔（第七步）**：38 筆主震 × 前後 7 天，最多 570 天 × 18 站。MAGNET 約 25 GB、GNSS 約 19 GB；窗口有重疊，實際會更少。硬碟不夠時，去掉 `--keep-events` 即可全部刪除。處理後的 1 分鐘地磁和 15 分鐘 TEC 都會保存，所以大多數重算不需要原始檔。

## 5. 事件清單

[event_list_M55_mainshocks_2018_2026.csv](event_list_M55_mainshocks_2018_2026.csv)：研究區（21–26°N、119–123°E）2018-01 ～ 2026-09 的 ML≥5.5 **主震**，共 38 筆。去群聚方法和 B3 相同（Gardner–Knopoff，ML≥3 參與）。

| 年 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|
| 主震 | 4 | 4 | 4 | 6 | 4 | 1 | 5 | 9 | 1 |
