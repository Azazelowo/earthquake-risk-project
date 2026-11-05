# Phase 2 規格：地震目錄欄位擴充與跨目錄事件比對

> 狀態：**已確認**（2026-11-05 使用者確認提案；門檻依回測修正）
> 來源：2026-11-03「地震目錄欄位擴充提案：跨機構事件比對」
> 依據：[scripts/catalog_matching/README.md](../../scripts/catalog_matching/README.md)（CWA × USGS、CWA XML × GDMS 回測）
> 相關：PR #2 [`docs/phase2/data_source_comparison.md`](https://github.com/Azazelowo/earthquake-risk-project/blob/claude/phase-2-data-source-comparison-b140f9/docs/phase2/data_source_comparison.md) 第 4 節（三層儲存、第 2 層統一欄位草案）

## 1. 為什麼需要

同一個地震，各機構給的位置、深度和規模都不一樣（例：2026-11-01 臺灣東部海域，CWA ML 5.6 對 USGS mb 4.8，震央相距 4.95 km）。如果不做比對：

1. **重複計數**：同一地震算兩次，頻率統計和 ETAS 會被灌水。過去 `events.parquet` 就因為 CWA 時間沒轉 UTC，ML≥5.5 的 488 筆中有 247 筆重複。
2. **把尺度差當誤差**：ML 和 mb 是不同的規模尺度。回測顯示 ML − mb 是系統性的差距，而且隨深度變大（淺層約 +0.15，150 km 以上約 +0.75），不是量測誤差。

## 2. 設計原則

| 原則 | 為什麼 |
|---|---|
| 第 2 層一列 = 一個目錄產品的一筆紀錄，**不合併** | 合併會失去來源資訊，門檻改了也無法重做 |
| 比對結果另存在第 3 層的 `event_matches`，**不寫回第 2 層** | 比對可以重跑；門檻一改，群組就會變。寫回原始列等於覆寫原始資料 |
| 時間一律存 UTC | 沿用 PR #2 與 `events.parquet`：UTC、無時區的 datetime。**匯入時必須明確從來源時區轉換**（CWA XML 是 +08:00） |
| 規模和深度都**不當比對條件** | 規模尺度不同；深度差分不出真假配對（見 4.3） |
| 門檻依目錄配對分別設定 | 同機構的兩種產品幾乎相同，跨機構差異大很多 |

## 3. 欄位

### 3.1 第 2 層（各來源標準化）：沿用 PR #2，補四個欄位

PR #2 的欄位已經在 `fetch_usgs.py` 實作，**名稱不改**。提案的欄位對應如下：

| 提案欄位 | 第 2 層欄位 | 處理 |
|---|---|---|
| `source_agency` | **`agency`（新增）** | `CWA`、`USGS`、`JMA`。GDMS 和 CWA XML 都是 `CWA` |
| （無） | `source` | 目錄產品：`GDMS`、`CWA_XML`、`CWA_API`、`USGS`。**PR #2 的 `source` 列表要補上 `CWA_XML`** |
| `source_event_id` | `source_event_id` | 沿用 |
| `magnitude_type` | `mag_type` | 沿用，**必填**；保留來源原字串 |
| `origin_time_utc` | `time` | 沿用（UTC，無時區） |
| `origin_time_local` | — | **不存**，顯示時再轉換，避免兩個時間不一致 |
| `revision_time` | `source_updated` | 沿用 |
| `review_status` | `status` | 沿用（USGS：`reviewed`／`automatic`／`deleted`） |
| `horizontal_uncertainty_km` | `horiz_err_km` | 沿用 |
| `depth_uncertainty_km` | `depth_err_km` | 沿用 |
| `catalog_version` | `fetched_at` | 沿用 |
| （無） | **`depth_fixed`（新增）** | bool／NA。USGS 沒有明確欄位，**先填 NA**；不要用「深度 = 10／33／35 km」來推測（44% 的 USGS 事件是這些值，但這只是推測） |
| （無） | **`source_event_aliases`（新增）** | str／NA。USGS `ids` 欄（同一事件的其他編號） |
| （無） | **`mag_type_norm`（新增）** | 標準化後的規模類型：`ML`、`mb`、`Ms`、`Mw`（`mww`、`mwr`、`mwc`、`mwb`、`mw` 都歸為 `Mw`） |

第 2 層的鍵維持 PR #2 的 `(source, source_event_id)`，以 `source_updated` 新者為準。

### 3.2 第 3 層：`event_matches`（新表）

存放位置：`pipeline/data/catalog/event_matches.parquet`，由比對程式產生，可隨時重建。

| 欄位 | 型別 | 說明 |
|---|---|---|
| `match_group_id` | str | 同一地震的群組 ID，格式為 `EMG_<最早時間 UTC，yyyymmddHHMMSS>_<序號>` |
| `source` | str | 對應第 2 層 |
| `source_event_id` | str | 對應第 2 層 |
| `confidence` | str | `high`、`medium`、`manual`（見第 4 節） |
| `pair_with` | str | 配對對象的 `source:source_event_id` |
| `dt_s` | float | 時間差（對方 − 本筆，秒） |
| `dist_km` | float | 震央距離（Haversine，R = 6371 km） |
| `ddepth_km` | float | 深度差（只記錄，不參與判定） |
| `matcher_version` | str | 比對程式版本與門檻，例如 `v1:cwa_usgs=10s/75km;cwa_gdms=1s/1km` |
| `matched_at` | datetime（UTC） | 執行時間 |

沒有配到任何對象的紀錄**也要有一列**（單獨成群、`confidence` 為空），下游才能一律用 `match_group_id` 計數。

`events.parquet`（分析用）由此產生：每個 `match_group_id` 取一筆代表紀錄。

## 4. 比對規則

### 4.1 門檻（依回測）

| 目錄配對 | `high` | `medium`（需人工確認） | 依據 |
|---|---|---|---|
| CWA × USGS（跨機構） | \|dt\| ≤ 5 秒 且 距離 ≤ 30 km | 其餘 \|dt\| ≤ 10 秒 且 距離 ≤ 75 km | 回測結果 1、2、4；誤配上限 high 0.7%、medium 9.1% |
| CWA XML × GDMS（同機構） | \|dt\| ≤ 1 秒 且 距離 ≤ 1 km | 其餘 \|dt\| ≤ 2 秒 且 距離 ≤ 10 km | 回測結果 8；high 72,237 筆、巧合上限 4 筆 |
| 其他配對（例如 JMA） | 未回測：**先套用跨機構門檻，一律標 `medium`**，回測後再調整 | | |

`manual` 只由人工設定。

### 4.2 配對程序

1. 每一對目錄分別比對，產生候選（在 `medium` 門檻內）。
2. **一對一配對**：依分數 `|dt| / dt門檻 + 距離 / 距離門檻` 由小到大，兩邊都還沒被配到才配。
3. 跨目錄串接成群組：A–B、B–C 都配對成功，A、B、C 就同一群。
4. 同一群內，各目錄最多一筆。若發生衝突，保留分數較小的那組，另一組改為 `manual` 待查。

### 4.3 深度與規模

- **深度不參與判定**。回測中，巧合配對有 96.5% 的深度差在 30 km 內，真實配對只有 90.1%；用深度把關會誤傷真配對，卻擋不掉巧合。
- 唯一例外：兩邊 `depth_fixed` 都明確是 `False`，而且深度差 > 50 km 時，把 `high` 降為 `medium`。
- **規模不參與判定**。不同 `mag_type_norm` 之間不可直接相減；需要比較時，必須使用考慮深度的換算式（本專案尚未建立）。

## 5. 已知涵蓋缺口（影響統計，不影響規則）

| 缺口 | 影響 |
|---|---|
| CWA XML 在 123°E 以東幾乎沒有事件（36 年只有 61 筆，USGS 有 629 筆） | 東側外海只能靠 USGS |
| CWA XML 在地震序列期間缺少部分 M4 級事件（2024-04 花蓮、2021-05 宜蘭外海、2008-07 南方海域） | 序列期間只用 CWA 會少算 |
| 本機 GDMS 只涵蓋北緯 21–25°、東經 119–124.85° | 北緯 25° 以北只有 CWA XML（ML≥3）。需要小地震就要重新下載更大的範圍 |

## 6. TEC 異常的時段標籤（同一份提案，一併確認）

之後加入 TEC 異常紀錄時，每筆都要有 `seismic_phase` 欄位：

| 值 | 意思 |
|---|---|
| `pre-seismic` | 地震前 |
| `co-seismic` | 地震發生當下（地表震動傳到電離層） |
| `post-seismic` | 地震後持續的響應 |

只有 `pre-seismic` 才能拿來討論前兆，而且必須排除地磁擾動期（Kp／Dst）。各時段的時間窗等 TEC 資料到齊後再定。

## 7. 待實作（這份規格不包含程式修改）

| 項目 | 位置 | 說明 |
|---|---|---|
| `01_build_catalog.py` 改用本規格 | B3 分支（PR #1） | 現行 60 秒／0.3° 方框改為 1 秒／1 km；改成產生 `event_matches`，不直接刪除紀錄 |
| `fetch_usgs.py` 補新欄位 | PR #2 | `agency`、`source_event_aliases`、`mag_type_norm`、`depth_fixed`（NA） |
| PR #2 文件第 2 層 `source` 列表補 `CWA_XML` | PR #2 | |
| CWA × JMA 回測 | — | 等有 JMA 資料 |
| GDMS `fixed` 欄（`F`／`X`）的意義 | — | 查證後才能填 `depth_fixed` |
