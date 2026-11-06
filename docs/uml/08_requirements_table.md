# 需求規格表

| 需求編號 | 需求名稱 | 需求類型 | 優先級 | 說明 | 驗收標準 | 相關用例 |
|----------|----------|----------|--------|------|----------|----------|
| FR-001 | 匯入地震目錄 | 功能性 | 高 | 讀取 CWA 地震目錄 CSV，驗證必要欄位 (OriginTime, Latitude, Longitude, ML, Depth) | 成功匯入 90 萬+ 筆資料，前 5 筆正確顯示，錯誤資料記錄警告 | UC-01 |
| FR-002 | 計算基線值 | 功能性 | 高 | 計算最近 30 天區域平均 ML、深度、日均震頻 | 輸出 baseline.json，包含 BaselineML, BaselineDepth, BaselineDailyRate | UC-02 |
| FR-003 | 整合前驅資料 | 功能性 | 高 | 讀取 7 類前驅資料 (GPS、地磁、地下水、TEC、氣壓、雨量、潮汐)，統一時間戳 | 產出 precursors_combined.csv，含 ObsTime、Source 標記 | UC-03 |
| FR-004 | 特徵正規化 | 功能性 | 高 | 將各前驅特徵正規化至 0-1 區間 | 產出 features_normalized.csv，各欄位 _norm 後綴 | UC-03 |
| FR-005 | 風險評分計算 | 功能性 | 高 | Weighted Sum: Σ(normalized_feature × weight) × 100，產出 0-100 風險指數 | 產出 risk_scores.csv，含 RiskScorePercent、IncreasePercent、Top3Contributors、Summary | UC-04, UC-05 |
| FR-006 | 風險摘要生成 | 功能性 | 高 | 格式：「基於 X、Y、Z 觀測，區域風險上升 N%」，明確列出前 3 大貢獻因子 | 每筆風險分數皆有對應摘要，前 3 大貢獻正確 | UC-05 |
| FR-007 | 時間序列視覺化 | 功能性 | 中 | 繪製風險指數時間序列圖 | 產出 risk_timeseries.png，X 軸時間、Y 軸風險指數 | UC-06 |
| FR-008 | 熱力圖視覺化 | 功能性 | 中 | 繪製前驅正規化貢獻度熱力圖 (最近 30 筆) | 產出 precursors_heatmap.png | UC-06 |
| FR-009 | 地圖熱圖視覺化 | 功能性 | 中 | 使用 folium 繪製風險熱力地圖 | 產出 risk_heatmap.html，可互動瀏覽 | UC-06 |
| FR-010 | 歷史驗證 | 功能性 | 高 | 對照歷史 M>=5.5 地震，計算 1/3/7 天前命中率、漏報率、誤報率 | 產出 validation_report.json，命中率 > 60%，漏報率 < 30% | UC-07 |
| FR-011 | 權重自動調整 | 功能性 | 中 | 根據驗證結果自動優化權重配置 | 權重更新後重新評分，指標改善 | UC-08 |
| FR-012 | 自動化排程執行 | 功能性 | 高 | GitHub Actions 推送至 main 時自動執行完整管道 | Workflow 成功，上傳 outputs/** 為 artifacts | UC-09 |
| NFR-001 | 資料完整性 | 非功能性 | 高 | 所有輸入檔案必須通過 Test-Path 檢查，欄位驗證 | 缺檔或缺欄位時輸出明確錯誤訊息並結束 | 全部 |
| NFR-002 | 可重現性 | 非功能性 | 高 | 固定隨機種子、記錄資料版本、參數版本 | 相同輸入產出相同結果，baseline.json 記錄計算日期 | 全部 |
| NFR-003 | 擴充性 | 非功能性 | 中 | 模組化設計，易於新增前驅類型或評分模型 | 新增前驅只需修改 Ingest-Precursors 與 WeightConfig | UC-03, UC-04 |
| NFR-004 | 效能 | 非功能性 | 中 | 完整管道執行時間 < 5 分鐘 (90 萬筆地震 + 7 類前驅) | GitHub Actions 執行時間在限制內 | 全部 |
| NFR-005 | 可維護性 | 非功能性 | 高 | 程式碼註解完整、遵循 PowerShell/Python 最佳實務 | 程式碼可讀性高，新手可理解修改 | 全部 |

