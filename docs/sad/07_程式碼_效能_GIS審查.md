# 07 程式碼審查、效能分析、GIS 分析

> 方法：盤點腳本（函式索引、引用次數、import 圖）＋針對性搜尋（過寬的例外處理、非空斷言、型別強制轉換、TODO、數值常數、重複的函式名稱）＋逐檔閱讀相關段落。
> 證據標記：【實測】已執行；【閱讀】讀程式判斷；【需確認】推論。行號以 `破專題` f372f92、APP 96cd43f 為準。
> **本輪也更正了第一輪的兩個錯誤**：G6（平滑圖層偏移）不成立；`hazard.py`、`schema.ts` 的行號有偏移。都已在 `docs/audit/2026-11-17_全面審查.md` 改正。

---

## 第一部分：程式碼審查（依嚴重度排序）

等級定義：
- **Critical**：會讓對外公布的數字錯誤，或可以被利用的安全漏洞
- **High**：特定條件下出錯、會中斷，或會誤導評審
- **Medium**：維護風險、設計缺陷
- **Low**：風格與一致性

### Critical

| # | 類別 | 檔案 | 函式 | 行號 | 原因 | 改善方式 |
|---|---|---|---|---|---|---|
| C1 | 邏輯錯誤 | `pipeline/src/01_build_catalog.py`、`06_hazard_model.py`、`eqforecast/hazard.py` | `main`、`Catalog.__init__` | 01:251-254；06:198；hazard:82 | `events.parquet` 是「gap ≤ 110° 的事件」加上「ML ≥ 5.5 不論 gap」的聯集，風險模型卻全部拿來用。大地震的比例被放大，b 值偏低（0.775）【需確認：本機沒有 events.parquet，無法實測】 | 06 讀檔時加讀 `use_for_features`，只用 `use_for_features == True` 的事件。修正後比較新舊 b 值（**本輪稍後會實作**） |

### High

| # | 類別 | 檔案 | 函式 | 行號 | 原因 | 改善方式 |
|---|---|---|---|---|---|---|
| H1 | 資安（未來） | APP `src/ui/expert.ts` | `viewVerify`、`viewMore` | 265、279 | `data_status.sources`、`recent[].source`、`depth_km` **沒有經過 `esc()`** 就放進 `innerHTML`。TypeScript 的型別（`"GDMS"\|"CWA"\|"USGS"`）只在編譯時檢查，Phase 2 的 feed.json 是外部資料，執行時不受限制 | 一律加上 `esc()`；數字欄位用 `Number()` 轉換後再輸出 |
| H2 | 錯誤處理 | APP `src/data/schema.ts`（整體） | — | 4-23 | feed.json **沒有執行時的格式驗證**。欄位缺少或型別錯誤時，畫面會在任意位置拋出例外 | 載入時用一個 `validateFeed()` 檢查版本、陣列長度、數值範圍；失敗就退回「資料暫時無法使用」 |
| H3 | 錯誤處理 | APP `src/core/hazard.ts`、`src/main.ts` | `interpHorizon`、`render` | hazard:12；main:69 | T 超出 1 小時到 30 天時，`interpHorizon` 直接丟出例外，而 `render` 沒有攔截，整頁空白（狀態事件 S-13） | `render` 外面包一層 try/catch，失敗時退回預設條件；`sanitize` 限制 T 的範圍（`state.ts:87`） |
| H4 | 邏輯（統計） | `06_hazard_model.py` | `backtest` | 80 | 均勻對照沒有做滾動校準，「增益（對均勻）」被高估 | 均勻對照也乘上同一套校準倍率（**本輪稍後會實作**） |
| H5 | 邏輯（統計） | `eqforecast/hazard.py` | `fit` | 232-234 | alpha、p、sigma_bg 的最佳值都落在搜尋範圍的下限 | 放寬搜尋範圍（**本輪稍後會實作**） |
| H6 | 錯誤處理（跨平台） | `pipeline/src/stream_io.py`、`02_clean_features.py` | `print` | stream_io:198, 257；02:371, 373 | `⚠`、`✗` 等符號在 Windows cp950 終端機會丟出 `UnicodeEncodeError`，**程式中斷**【實測：4 個測試失敗】 | 入口統一把 stdout 設成 UTF-8（**本輪稍後會實作**） |
| H7 | 邏輯（領域） | APP `src/state.ts`、`src/core/hazard.ts` | `MAGS`、`scaleMagnitude` | state:59；hazard:27 | M7 是用 b＝0.775 從 M3.5 外推 3.5 級；而且 ML 在大地震時會飽和 | 修正 C1 後重估 b；M ≥ 6.5 改成只顯示量級 |

### Medium

| # | 類別 | 檔案 | 函式 | 行號 | 原因 | 改善方式 |
|---|---|---|---|---|---|---|
| M-1 | 重複程式碼、ECB 違反 | APP `src/ui/expert.ts` | `viewMag`、`viewTime` | 162、195 | 畫面自己算 `probability(scaleMagnitude(...))`，和 `hazard.ts:121-130` 重複；`:195` 把時間上限 30 寫死 | 移到 `hazard.ts` 的 `curveVsMagnitude`、`curveVsHorizon`；上限改用 `horizons_days.at(-1)` |
| M-2 | 重複程式碼 | `eqforecast/grid.py`、`eqforecast/hazard.py` | `Grid` | grid:8；hazard:33 | 兩個 `Grid` 類別，座標約定不同（中心點 vs 西南角） | 合併成一個類別，提供 `center()`、`sw_corner()` |
| M-3 | 程式異味（God module） | APP `src/main.ts` | 全檔 | 1-299 | 路由、事件、定位、儲存、組裝 HTML 都在同一個檔案，而且沒有測試 | 拆成 `router.ts` 與 `layout.ts` |
| M-4 | 程式異味（職責太多） | `06_hazard_model.py` | `plot_map`、`write_report` | 118-183 | 計算與報告混在同一支程式 | 拆出 `eqforecast/hazard_report.py` |
| M-5 | 程式異味（橫向耦合） | `04_estimate_mc.py`、`12_quality_report.py` | `_load_script` | 12:56-72 | 用 `spec_from_file_location` 動態載入其他腳本（因為檔名是數字開頭，無法直接 import） | 把共用函式搬進套件（例如 `eqforecast/catalog.py`） |
| M-6 | 遺留程式（Dead Code） | `scripts/*.ps1`、`README.md`、`SKILLS.md`、`docs/uml/`、`Chapter01/`、`Chapter08/`、`notebooks/analysis.ipynb` | — | — | 描述的是已淘汰的系統（見 01 專案掃描 3.2） | 移到 `legacy/`，並改寫 README |
| M-7 | 資料模型 | `06_hazard_model.py` | `export_latest` | 112-114 | 欄名直接嵌入浮點數（`n_ref_0.04167d`），下游必須用同樣的格式化方式才讀得到 | 改成長表，或在 `params.json` 中列出欄名 |
| M-8 | 可追溯性 | `06_hazard_model.py` | `main` | 221-226 | `params.json` 沒有記錄 events 的版本（雜湊值） | 加上 `events_sha256` 與筆數 |
| M-9 | 魔術數字（商業規則） | APP `src/format.ts` | `LEVEL_EDGES` | 10 | 狀態門檻放在呈現模組，而且是暫定值 | 移到 `core/`，並寫明決定的依據 |
| M-10 | 跳脫字元錯誤 | `pipeline/src/case_ndh5_tec.py` | `main` | 167 | `"E:\123\GNSS_ETEC"` 不是 raw string：`\123` 會變成字元 `S`，`\G` 是無效的跳脫序列【實測：`-W error` 時是 `SyntaxError`】 | 改成 `r"E:\123\GNSS_ETEC"`，或改用正斜線 |
| M-11 | 邏輯（統計） | `06_hazard_model.py`、`hazard_core.py`（分支） | `export_latest`、`background_rescale` | 06:113-114 | 「平常」被完整模型的校準倍率污染（第一輪 M5） | 只有背景的模型另外計算自己的校準倍率 |

### Low

| # | 類別 | 檔案 | 函式 | 行號 | 原因 | 改善方式 |
|---|---|---|---|---|---|---|
| L-1 | Dead Code | `gnss_tec/aux_data.py` | `brdc3_url` | 72 | 沒有任何地方呼叫 | 刪除 |
| L-2 | Dead Code | `eqforecast/grid.py` | `Grid.neighbors` | 68 | 沒有任何地方呼叫 | 刪除 |
| L-3 | Dead Code | `stream_io.py` | `RawItem.open_binary` | 136 | 沒有任何地方呼叫 | 刪除 |
| L-4 | 重複程式碼 | `01_build_catalog.py`、`eqforecast/decluster.py`、`scripts/catalog_matching/backtest_cwa_usgs.py` | `haversine_km` | 104；45；91 | 同一個函式有三份 | 放進 `eqforecast/geo.py` |
| L-5 | 重複程式碼 | `01_build_catalog.py`、`backtest_cwa_usgs.py` | `epoch_s` | 111；98 | 兩份 | 同上 |
| L-6 | 重複程式碼 | `eqforecast/hazard.py`、`eqforecast/targets.py` | `issue_times` | 227；15 | 兩份，而且步長的定義不同 | 統一 |
| L-7 | 重複常數 | APP `core/hazard.ts`、`geo/towns.ts`、`ui/info.ts` | `KM_PER_DEG`、`M_PER_DEG`、`111` | 4；49；23, 120 | 每度的公里數有三種寫法（111.195、111_195、111） | 統一使用 `hazard.ts` 的 `KM_PER_DEG` |
| L-8 | 魔術數字 | APP `core/hazard.ts` | `isVerified`、`reliabilityNotes` | 172、183 | `6.5` 出現兩次 | 定義成 `VERIFIED_M_MAX` |
| L-9 | 寫死的文字 | APP `ui/expert.ts` | `viewVerify` | 252 | 公式說明把 `3.5` 寫死；`m_ref` 改變時會對不上 | 改用 `${f.params.m_ref}` |
| L-10 | 不一致 | `eqforecast/hazard.py` | `fit`、`trailing_factor` | 244；169 | 天數轉秒數，一處用 `int()`，另一處用 `int(round())` | 寫一個共用函式 |
| L-11 | 邊界不一致 | APP `core/hazard.ts`、`eqforecast/hazard.py` | `cellIndexAt`、`cell_index` | ts:104-105；py:45-51 | 剛好落在 `lat_max` 或 `lon_max` 上的點：Python 判定在區外，TS 判定在最後一格 | 統一規則（建議採用 Python 的半開區間） |
| L-12 | 型別 | APP `main.ts` | click 處理 | 225-227 | `dataset` 的值直接轉型（`as State["scope"]`），沒有檢查 | 用白名單檢查 |
| L-13 | 程式異味 | APP `core/hazard.ts` | `interpHorizon` | 12-13 | 先檢查範圍、不在範圍內就丟例外，下一行又做一次夾限 | 二選一（建議只做夾限，配合 H3） |
| L-14 | 重複的 CI | `.github/workflows/risk-assessment.yml` | tests job | — | 和 `pipeline-tests.yml` 重複 | 刪除 |
| L-15 | 日誌 | 管線全部 | `print` | — | 沒有使用 logging，無法調整輸出的詳細程度 | 長期才需要處理 |
| L-16 | 邏輯（細微） | `eqforecast/hazard.py` | `_smooth` | 108-115 | 邊界外流失的量是乘到**全區**，不只是補回邊界附近 | 改成對每一格依核的權重修正（sigma ≤ 2 格時影響很小） |

**刻意設計、不算問題的地方**：
- 7 處 `except Exception`（例如 `stream_io.py:380`）都有 `noqa` 和說明（單一壞檔不能讓整批中斷）。
- TypeScript 的 `strict: true`、`noUnusedLocals: true`。
- 兩個 repo 都沒有 TODO 或 FIXME。
- Python 端有 57 個具名的模組常數，寫死在程式裡的數字很少。

---

## 第二部分：效能分析

清單要求列出 Top 50。**實際有證據的問題是 18 項**，我不湊數。React 這一項不適用（APP 沒有使用框架，畫面以字串樣板產生）。改善幅度是依計算量推估的，**沒有實際量測**，請把它當成優先順序參考。

| # | 範疇 | 位置 | 問題 | 改善建議 | 預估改善 |
|---|---|---|---|---|---|
| 1 | Python 管線 | `hazard.py:162-179` | 每一期發布要重算約 104 次 `expected_ref`（730 天 ÷ 7 天），匯出 10 個時間長度就是約 1,040 次 | 快取每週的總數，增量更新 | 匯出時間約降 1–2 個數量級 |
| 2 | Python 管線 | `06_hazard_model.py:69-72` | 回測時每個時間長度都重算整串校準窗 | 同 1 | 回測時間大幅縮短 |
| 3 | Python 管線 | `hazard.py:249-256` | 75 組格點搜尋，每組都重算全部訓練窗的 `short_term` | 先依 (alpha, p) 算好未平滑的量，再分別平滑 | 約 3 倍 |
| 4 | Python 管線 | `01_build_catalog.py:260` | `df["gap"].apply(gap_quality)` 逐列套用 | 改用 `pd.cut` 向量化 | 十萬筆時約 10–100 倍（實際只占幾秒） |
| 5 | Python 管線 | `01_build_catalog.py:116-123` | 用字串格式化產生事件 ID | 可以接受 | — |
| 6 | TS 計算 | `public.ts:21, 60` | 首頁的狀態和 3 張情境卡，對**同一組**（位置、半徑）重複計算 `pointWeights` 4 次（每次 100 個子點 × 範圍內的格子） | 依（lat、lon、半徑）快取權重 | 首頁計算量約減少 75% |
| 7 | TS 計算 | `hazard.ts:40-54` | 半徑 200 km 時約有 300 格 × 100 個子點，也就是約 3 萬次 haversine | 同 6；或改用解析式的圓與矩形面積交集 | 拖動滑桿時更順 |
| 8 | GIS 渲染 | `map.ts:139-149` | 不平滑時，每次重畫都新建 500 個 `L.rectangle` | 只更新樣式，或改用單一 canvas 圖層 | 重畫時間約降一半（未量測） |
| 9 | GIS 渲染 | `map.ts:151-165`、`smooth.ts:102-122` | 平滑時每次重畫都要取樣約 30 萬個像素（480×約 640），再 `toDataURL` 編碼成 PNG | 直接用 canvas 當圖層，省掉 PNG 編碼 | 中階手機上每次可能省數十毫秒【需確認】 |
| 10 | GIS 渲染 | `map.ts:130-131` | `cellProbabilities` 一次重畫呼叫兩次（現在、平常） | 可以接受（只有 500 格） | — |
| 11 | Bundle | `map.ts:6-7` → `main.ts:13` | Leaflet（含 CSS）是靜態匯入，大眾首頁不用地圖也會載入 | 用 `import()` 延遲載入地圖模組 | 首頁 JS 約可減少一半以上【需確認：要用 bundle 分析確認 Leaflet 的大小】 |
| 12 | GeoJSON／TopoJSON | `towns.ts:55-104` | 305 KB 的界線檔第一次定位時要解碼弧線並建索引 | 已經是延遲載入；可以在閒置時預先載入 | 第一次定位快幾百毫秒【需確認】 |
| 13 | GeoJSON／TopoJSON | `towns.ts:149-167` | 每次查詢都掃過全部鄉鎮的外框 | 有外框預先篩選，368 個鄉鎮可以接受 | — |
| 14 | 畫面 | `main.ts:85-111` | 每次狀態改變都整頁 `innerHTML` 重畫 | 目前可以接受；Phase 3 的地震列表要改成局部更新 | 列表變長後才有差 |
| 15 | 畫面 | `main.ts:285` | 每次 online、offline 事件都整頁重畫 | 只更新地圖提示 | 小 |
| 16 | PWA | `sw.js` 安裝階段 | 預先快取所有資源，包含 305 KB 的界線檔 | 界線檔改成第一次使用時才快取 | 第一次造訪少約 0.3 MB 流量 |
| 17 | API | 圖磚 | OSM、OpenTopoMap 的流量政策（第一輪 S7） | 預設底圖改成國土測繪中心 | 避免被封鎖 |
| 18 | API（規劃中） | 每小時發布 | 每小時重新訓練會太慢 | 只重算近期項；參數每月重訓一次 | 讓每小時排程能在幾分鐘內跑完 |

---

## 第三部分：GIS 分析

### 1. 座標參考系統（CRS、EPSG）

| 資料 | CRS | EPSG | 證據 | 判斷 |
|---|---|---|---|---|
| 地震目錄（CWA、GDMS、USGS） | WGS84 經緯度 | 4326 | 欄位 lat、lon 是十進位度數 | ✅ |
| 0.2° 模型網格 | WGS84 經緯度上的規則網格 | 4326 | `hazard.py:32-57` | ✅（不是等面積網格，見 3.3） |
| 鄉鎮界線 | TWD97 經緯度 | 3824 | `towns.ts:3`、`info.ts:73` | ✅ 和 WGS84 的差距在公尺級，遠小於 61 m 的簡化誤差【需確認：TWD97 是 1997 年的框架，台灣東部的板塊運動累積到今天可能有 1–2 m 的差距】 |
| 網路地圖 | Web Mercator | 3857 | Leaflet 預設 | ✅ 格子以經緯度邊界畫（`map.ts:145`）；平滑影像有做 Mercator 取樣（`smooth.ts:102-122`） |
| GNSS 衛星與測站 | WGS84 ECEF → 大地座標 | 4978 → 4979 | `orbit.py:63-73`（迭代法） | ✅ |
| 電離層穿刺點 | 單層 350 km 球殼 | — | `orbit.py:89-96` | ✅ 業界常用的假設 |

### 2. GeoJSON、TopoJSON

- **TopoJSON 解碼**：量化座標是差分累加（`towns.ts:59-70`），負的弧線編號 `~i` 代表反向（`:74`）。✅ 符合 TopoJSON 規格。
- **多邊形**：每個 MultiPolygon 的第一個環是外框、其餘是洞；用奇偶規則判斷點在不在多邊形內（`towns.ts:117-122`）。✅ 不需要依賴環的方向，比較穩健。
- **拓撲保持的簡化**：相鄰鄉鎮的交界只存一次（共用弧線），所以簡化後**不會產生縫隙或重疊**。✅
- **驗證**：`towns.test.ts` 用「原始界線」產生的標準答案驗證簡化後的結果；`build-towns.mjs` 用隨機點與界線附近的抖動點檢查（`RANDOM_POINTS`、`JITTER_MAX_M`）。✅ 這是**業界水準的做法**。

### 3. 距離與面積計算

| 用途 | 方法 | 位置 | 誤差 |
|---|---|---|---|
| 圓形範圍 | Haversine，R＝6371 km | `hazard.ts:32-37` | 50 km 尺度下，球體近似誤差 < 0.5% ✅ |
| 離鄉鎮界線的距離 | 以查詢點為原點的平面近似（經度乘上 cos 緯度） | `towns.ts:124-140` | 20 km 以內的誤差 < 0.1% ✅ |
| 網格面積 | (0.2° × 111.195)² × cos(緯度) | `hazard.ts:61-66` | ✅ 有考慮緯度 |
| 每度公里數 | 111.195、111_195、111 三種寫法 | L-7 | 111 和 111.195 差 0.18%，只影響說明文字（`info.ts:23, 120`） |
| 去群聚、事件比對 | Haversine（三份重複） | L-4 | ✅ 計算正確，只是重複 |

### 4. 是否有誤差

| 類型 | 項目 | 嚴重度 | 證據 |
|---|---|---|---|
| **資料誤差** | 3.5–5.5 級事件有 gap 篩選、5.5 以上沒有，造成外海小地震被低估 | 高 | C1；`hazard_v1` 報告的限制第 4 點 |
| **資料誤差** | 所有深度的地震都投影到地表同一格（琉球隱沒帶 70–300 km 的地震也算在地表） | 中 | `hazard.py` 沒有任何深度條件（第一輪 M6） |
| **資料誤差** | ML 和 Mw 混用（大規模時 ML 會飽和） | 中 | H7 |
| **精度問題** | 本機存檔時座標降到 0.01°（約 1.1 km）；重新開啟時，評估會以降精度後的座標計算 | 低 | `state.ts:103-107`。相對於 20 km 的網格可以忽略 |
| **精度問題** | CWA XML 的座標只到小數第 3 位（約 100 m） | 低 | 跨來源比對門檻是 1 km，已經考慮 |
| **精度問題** | 0.2° 網格約 20 km，但範圍半徑最小可以選 5 km | 中 | `info.ts:23` 已經會提醒「比一格還小」 ✅ |
| **邊界問題** | 剛好落在 `lat_max`、`lon_max` 上的點，Python 和 TS 判斷不一致 | 低 | L-11 |
| **邊界問題** | 圓形範圍跨出網格：涵蓋率 < 50% 不顯示；50–90% 加提醒 | ✅ | `hazard.ts:81-88` |
| **邊界問題** | 「整個台灣」實際上是整個網格（含海域與部分福建沿海） | 低 | 03 Use Case 第 4 節 |
| **邊界問題** | 金門、連江在網格外 | ✅ 已處理 | `state.ts:40-52`；`public.ts:152, 155` |
| **投影誤差** | 平滑影像在 Mercator 上的位置 | ✅ **沒有誤差**（第一輪說有偏移，經複查已更正） | `smooth.ts:102-122`、`smooth.test.ts:90` |
| **投影誤差** | 平滑核以「格」為單位，東西向與南北向的公里數差約 9% | 低 | `hazard.py:108-115`（第一輪 G7） |

**結論**：GIS 的工程實作（CRS、拓撲、點在多邊形、距離、Mercator 取樣）**符合業界標準**，而且有可以重跑的驗證。主要的地理誤差來源不在 GIS 程式碼，而在**資料處理**：篩選混用、深度、規模類型。
