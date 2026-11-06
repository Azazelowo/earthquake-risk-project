# 震災風險評估專題

## 專題目的
本專題利用中央氣象署（CWA）地震目錄及多種地球物理前驅資料（GPS 變形、地磁、地下水層、離電層 TEC、氣壓、雨量、潮汐等），建構 Taiwan 地區的地震風險評估模型，並透過自動化工作流程與驗證管道，產出「基於 X、Y、Z 觀測，區域風險上升 N%」的文字摘要與視覺化報告。

## 資料來源
- CWA 地震目錄 CSV（含時間、經緯度、ML、深度等欄位）
- GPS 變形資料（NSRC GNSS/CORS）
- 地磁觀測（國防部地磁觀測站）
- 地下水層水位（水資源署）
- 離電層 TEC（FORMOSAT-5/COSMIC）
- 氣壓、雨量（中央氣象署）
- 潮汐、水載荷（海洋研究院）
- 地殼應變/應力（地震研究所）

## 主要步驟
1. **資料匯入**：使用 `Import-EarthquakeCatalog.ps1` 讀取與檢查地震目錄。
2. **基線計算**：`Compute-Baseline.ps1` 計算最近 30 天區域平均作為基線（ML、深度或震頻）。
3. **前驅整合**：`Ingest-Precursors.ps1` 統一各前驅資料的時間戳與空間格網。
4. **風險評分**：`Risk-Scoring.ps1` 實作 Weighted Sum 或 Rule‑based 評分，並輸出風險摘要。
5. **視覺化**：`Visualize-Results.ps1` 呼叫 Python 的 matplotlib/seaborn 產出時間序列圖、熱力圖與地圖標記。

## 本地重現方式
1. 確認已安裝 PowerShell 5.1 以上與 Python 3（含 matplotlib、seaborn、pandas）。
2. 複製此專案至本地。
3. 將所需 CSV 資料放入 `earthquake_data` 資料夾（或依實際路徑修改腳本）。
4. 按順序執行 scripts 資料夾中的腳本：
   ```powershell
   .\scripts\Import-EarthquakeCatalog.ps1
   .\scripts\Compute-Baseline.ps1
   .\scripts\Ingest-Precursors.ps1
   .\scripts\Risk-Scoring.ps1
   .\scripts\Visualize-Results.ps1
   ```
5. 風險摘要將印至主控台，圖表將儲存於 `outputs/` 資料夾。

## 自動化工作流程
- 每次推送至 `main` 或開 PR 時，GitHub Actions (`.github/workflows/risk-assessment.yml`) 會自動執行 `tests` job：安裝 `requirements.txt` 並以合成資料跑 `pipeline/tests` 的單元測試。
- 上述 PowerShell 腳本需要 `earthquake_data/CWA_catalog.csv` 與 `precursor_data/`，這些資料不進版控，所以平常在本機執行；workflow 的 `risk-assessment` job 只能手動觸發，缺資料時會在第一步明確中止。

## 版控習慣
- 完成有意義的腳本或文件後，執行 `git add`, `git commit -m "描述此次變更的目的"`, `git push`。
- 使用分支嘗試新假設，實驗完畢再合併回 `main`。

## 安全與可靠性
- 各腳本皆以 `Test-Path` 檢查檔案存在，失敗時會寫入明確錯誤訊息。
- 網路下載的前驅資料會在腳本內記錄下載 URL、取得時間與版本。
- 風險分數輸出時會同時列出不確定度說明。

--- 

*此 README 為專案主要說明文件，請依實際需求調整內容。*
