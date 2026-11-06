# 資料字典

## 1. 地震目錄 (Earthquake Catalog) - CWA_catalog.csv

| 欄位名稱 | 資料型態 | 說明 | 範例值 | 約束條件 | 來源 |
|----------|----------|------|--------|----------|------|
| OriginTime | String | 地震發生時間 (UTC) | 2024-04-03 07:58:10 | 必填，格式 yyyy-MM-dd HH:mm:ss，不超過目前時間 | CWA |
| Latitude | Double | 震源緯度 (北緯為正) | 23.90 | 必填，-90 到 +90，台灣約 21-25.5°N | CWA |
| Longitude | Double | 震源經度 (東經為正) | 121.60 | 必填，-180 到 +180，台灣約 119-123°E | CWA |
| ML | Double | 地方規模 (Local Magnitude) | 7.20 | 必填，> 0，通常 0-8 | CWA |
| Depth | Double | 震源深度 (km) | 15.5 | 必填，>= 0，通常 0-200 km | CWA |
| ReferenceSource | String | 參考資料/報告編號 | 20240403075810 | 選填 | CWA |
| Region | String | 參考地區名稱 | 花蓮縣外海 | 選填 | CWA |
| DateTime | DateTime | 解析後的時間物件 | 2024-04-03 07:58:10 | 內部衍生欄位，供篩選使用 | 系統產生 |

## 2. 前驅觀測資料 (Precursor Data) - precursors_combined.csv

| 欄位名稱 | 資料型態 | 說明 | 範例值 | 約束條件 | 來源 |
|----------|----------|------|--------|----------|------|
| ObservationTime | String | 觀測時間 | 2024-04-01 12:00:00 | 必填，格式 yyyy-MM-dd HH:mm:ss | 各觀測站 |
| Source | String | 資料來源類型 | GPS, MAG, GW, TEC, PRESS, RAIN, TIDAL | 必填，固定 7 類之一 | 系統標記 |
| Value | Double | 觀測數值 | 0.05 | 必填，視來源單位而定 | 各觀測站 |
| Latitude | Double | 觀測站緯度 | 24.15 | 選填，-90 到 +90 | 各觀測站 |
| Longitude | Double | 觀測站經度 | 121.30 | 選填，-180 到 +180 | 各觀測站 |
| ObsTime | DateTime | 解析後時間物件 | 2024-04-01 12:00:00 | 內部衍生欄位 | 系統產生 |

## 3. 正規化特徵 (Normalized Features) - features_normalized.csv

| 欄位名稱 | 資料型態 | 說明 | 範圍 | 計算方式 |
|----------|----------|------|------|----------|
| Timestamp | DateTime | 對應時間點 | - | 取自前驅資料時間 |
| GPS_norm | Double | GPS 變形正規化值 | 0-1 | Min-Max 或 Z-score 正規化 |
| MAG_norm | Double | 地磁正規化值 | 0-1 | 同上 |
| GW_norm | Double | 地下水位正規化值 | 0-1 | 同上 |
| TEC_norm | Double | 離電層 TEC 正規化值 | 0-1 | 同上 |
| PRESS_norm | Double | 氣壓正規化值 | 0-1 | 同上 |
| RAIN_norm | Double | 雨量正規化值 | 0-1 | 同上 |
| TIDAL_norm | Double | 潮汐正規化值 | 0-1 | 同上 |

## 4. 基線值 (Baseline) - baseline.json

| 欄位名稱 | 資料型態 | 說明 | 範例值 |
|----------|----------|------|--------|
| BaselineML | Double | 30 天平均 ML | 2.15 |
| BaselineDepth | Double | 30 天平均深度 (km) | 18.3 |
| BaselineDailyRate | Double | 平均日震頻 (筆/日) | 45.2 |
| CalculationDate | String | 計算日期時間 | 2024-04-10T12:00:00 |
| DaysUsed | Integer | 使用天數 | 30 |

## 5. 風險評分 (Risk Scores) - risk_scores.csv

| 欄位名稱 | 資料型態 | 說明 | 範例值 |
|----------|----------|------|--------|
| Timestamp | DateTime | 評分時間點 | 2024-04-10 12:00:00 |
| RiskScorePercent | Double | 風險指數 (0-100) | 67.45 |
| BaselineScorePercent | Double | 基線風險指數 | 45.20 |
| IncreasePercent | Double | 相對基線增加百分比 | 49.23 |
| Top3Contributors | String | 前 3 大貢獻來源 | TEC、GPS、MAG |
| Summary | String | 風險摘要 | 基於 TEC、GPS、MAG 觀測，區域風險上升 67.45%（相對基線增加 49.23%）。 |

## 6. 權重配置 (Weight Configuration)

| 參數名稱 | 資料型態 | 預設值 | 說明 | 可調整範圍 |
|----------|----------|--------|------|------------|
| WeightGPS | Double | 0.20 | GPS 變形權重 | 0-1，總和=1 |
| WeightMAG | Double | 0.15 | 地磁權重 | 0-1，總和=1 |
| WeightGW | Double | 0.10 | 地下水權重 | 0-1，總和=1 |
| WeightTEC | Double | 0.15 | 離電層 TEC 權重 | 0-1，總和=1 |
| WeightPRESS | Double | 0.10 | 氣壓權重 | 0-1，總和=1 |
| WeightRAIN | Double | 0.10 | 雨量權重 | 0-1，總和=1 |
| WeightTIDAL | Double | 0.10 | 潮汐權重 | 0-1，總和=1 |

## 7. 驗證報告 (Validation Report) - validation_report.json

| 欄位名稱 | 資料型態 | 說明 | 範例值 |
|----------|----------|------|--------|
| TotalEvents | Integer | 驗證事件總數 (M>=5.5) | 123 |
| HitRate | Double | 命中率 (1/3/7 天前分數 > 閾值) | 0.68 |
| MissRate | Double | 漏報率 | 0.22 |
| FalseAlarmRate | Double | 誤報率 | 0.15 |
| AvgLeadTimeDays | Double | 平均預警提前天數 | 2.3 |
| ThresholdUsed | Double | 使用的風險閾值 | 60.0 |
| ValidationDate | String | 驗證日期 | 2024-04-10 |

## 8. 前驅資料來源對照表

| 前驅類型 | 代碼 | 台灣主要來源 | 單位 | 更新頻率 | 取得方式 |
|----------|------|--------------|------|----------|----------|
| GPS 變形 | GPS | NSRC GNSS/CORS | mm/yr | 日/週 | 官網下載/AP |
| 地磁觀測 | MAG | 國防部地磁觀測站 | nT | 分鐘/小時 | 申請/官網 |
| 地下水層水位 | GW | 水資源署地下水監測井 | m | 小時/日 | 官網開放資料 |
| 離電層 TEC | TEC | FORMOSAT-5/COSMIC | TECU | 衛星過境 | 官網/申請 |
| 大氣壓力 | PRESS | CWA 氣象站 | hPa | 10分鐘/小時 | 開放資料平台 |
| 雨量 | RAIN | CWA 雨量測站 | mm | 10分鐘/小時 | 開放資料平台 |
| 潮汐/水載荷 | TIDAL | 海洋研究院潮汐模型 | kPa | 小時 | 官網/模型輸出 |
| 地殼應力/應變 | STRAIN | 地震研究所應變儀 | με | 連續 | 學術合作 |

