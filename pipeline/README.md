# 資料管線（pipeline）

以 Python 實作的地震風險評估資料管線。根目錄的 `scripts/*.ps1` 是早期 PowerShell 版本，已不再維護。

## 為什麼資料不在 git 裡

原始資料（CWA XML、GDMS CSV、RINEX）合計數百 MB，且可以從官方來源重新下載，所以只放在本機。
程式透過環境變數 `EQ_DATA_DIR` 找到資料位置；沒有設定時，預設為專案根目錄。

## 環境設定

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> Python 3.15 alpha 目前沒有對應的 numpy 版本，請用 3.13。

## 執行

```powershell
# 資料放在其他資料夾時才需要設定
$env:EQ_DATA_DIR = "C:\path\to\破專題"

python pipeline\src\01_build_catalog.py
python pipeline\src\02_clean_features.py          # 沒有前兆資料時會直接報錯
python pipeline\src\02_clean_features.py --demo   # 產生固定亂數種子的示範資料
```

## 各步驟

| 步驟 | 輸入（`EQ_DATA_DIR` 底下） | 輸出（`pipeline/data/`） | 說明 |
|---|---|---|---|
| `01_build_catalog.py` | `E-A0073-002/*.xml`、`earthquake_data/raw/GDMS地震目錄_*.csv` | `events.parquet` | 統一轉成 UTC、Mc=2.0、來源內與跨來源去重、標記特徵事件與目標事件（見下表） |
| `02_clean_features.py` | `地震預測/地磁及地電流/*.csv`、`地震預測/電離層電子密度變化/*.csv` | `features.parquet`（`--demo` 時為 `features_DEMO.parquet`） | 0.2° 網格 × 日、滑動統計、IQR 去除離群值 |
| `03_train_backtest.py` | — | — | 尚未實作 |

### `events.parquet` 的兩個標記欄位

| 欄位 | 條件 | 用途 | 筆數 |
|---|---|---|---|
| `use_for_features` | Gap ≤ 110° 或 Gap 未知 | 計算地震活動特徵（震頻、b 值等） | 124,776 |
| `is_target` | ML ≥ 5.5，**不論 Gap** | 回測的目標事件 | 443 |

**為什麼目標事件不看 Gap**：外海地震的測站只在陸地一側，Gap 本來就偏大。若用 Gap≤110 刪除目標事件，
443 筆會只剩 240 筆，被刪掉的 203 筆中有 173 筆在東部外海（例如 2004 年 ML 7.1，Gap 243°），
模型會系統性地看不到外海大地震。Gap 過大但規模不到 5.5 的事件，兩種用途都用不到，所以不輸出。

## 已知限制

- `02_clean_features.py` 只讀 CSV；`地震預測/測地法/` 底下的 RINEX（`.24o`）要先轉成 TEC 的 CSV。
- `02_clean_features.py` 的權重與 `CASCADE_K` 都是暫定值，尚未經過回測校準。
