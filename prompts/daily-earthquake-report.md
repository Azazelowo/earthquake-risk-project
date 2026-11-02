# 每日地震風險情報彙整（Claude Code 排程任務）

> 用途：支援實務專題「基於地球物理與多維觀測資料打造地震風險評估 APP」。
> 執行方式：由 Claude Code 排程每日自動執行，無人在旁互動。
> 建議排程：每日 08:00（Asia/Taipei），cron `0 8 * * *`。

---

## 0. 執行前提（每次都要先做）

1. 這是**無人值守**的執行：不要提問、不要等待確認，遇到問題就記錄在報告的「執行日誌」並繼續。
2. 取得目前時間，以 **Asia/Taipei（UTC+8）** 為準，定義：
   - `TODAY` = 今日日期（YYYY-MM-DD）
   - 檢索窗口 = 過去 24 小時；學術與民間來源可放寬到過去 7 天
3. **切換到報告分支**（報告與回測資料只存在 `daily-reports` 分支，`main` 只放程式與提示）：
   ```bash
   git fetch origin
   WT="$(mktemp -d)/wt"   # 暫存目錄，不動使用者的工作目錄
   if git ls-remote --exit-code --heads origin daily-reports >/dev/null; then
     git worktree add -B daily-reports "$WT" origin/daily-reports
   else
     git worktree add -b daily-reports "$WT" origin/main
   fi
   cd "$WT"
   ```
   之後所有讀寫都在 `$WT` 內進行，`data/` 以這個分支的內容為準（上一次執行累積的回測資料在這裡）。
4. 專案根目錄下的資料位置（不存在就建立）：
   - `reports/YYYY/TODAY.md` — 今日報告
   - `data/predictions.jsonl` — 民間預測與前兆紀錄（回測資料庫，**只追加或更新狀態，不刪除**）
   - `data/events.jsonl` — 已收錄的真實地震事件
   - `data/backtest.csv` — 比對結果彙總
5. 若 `reports/YYYY/TODAY.md` 已存在（同日重跑），覆寫報告，但 `data/` 內的紀錄要以 `id` 去重，不可重複寫入。
6. 語言：繁體中文（台灣用語），專有名詞可保留英文。
7. **執行環境可能是 Windows（Git Bash）**，系統預設編碼為 cp950：
   - 每個 Bash 指令開頭先 `export PYTHONIOENCODING=utf-8`
   - Python 讀寫檔案一律寫明 `open(path, encoding="utf-8")`，JSONL 寫入時用 `json.dumps(obj, ensure_ascii=False)`
   - 不要在使用者的工作目錄直接切換分支；用 `git worktree add` 在暫存目錄操作 `daily-reports`，完成後 `git worktree remove`

---

## 1. 官方數據與主流學術進展

### 1.1 地震活動（必做）

優先使用可機讀的官方資料來源，網頁只作輔助：

| 來源 | 建議入口 | 取得內容 |
|---|---|---|
| USGS | `https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&starttime=<UTC 24h 前>&minmagnitude=4.5` | 全球 M4.5+ 事件 |
| USGS | 同上，加上 `minlatitude=21&maxlatitude=26.5&minlongitude=119&maxlongitude=123.5&minmagnitude=3` | 台灣周邊事件 |
| CWA 地震測報中心 | `https://scweb.cwa.gov.tw/` | 顯著有感地震報告、餘震序列說明、活動分析新聞稿 |
| CWA 開放資料 | `https://opendata.cwa.gov.tw/api/v1/rest/datastore/<資料集>`（需授權碼） | 若環境變數 `CWA_API_KEY` 存在則使用（取得震央座標），否則改用網頁 |

CWA 開放資料用法（僅在 `CWA_API_KEY` 有值時）：
```bash
curl -s -m 30 -H "Authorization: $CWA_API_KEY" \
  "https://opendata.cwa.gov.tw/api/v1/rest/datastore/E-A0015-001?format=JSON" -o cwa_eq.json   # 顯著有感地震報告
curl -s -m 30 -H "Authorization: $CWA_API_KEY" \
  "https://opendata.cwa.gov.tw/api/v1/rest/datastore/E-A0016-001?format=JSON" -o cwa_eq_local.json   # 小區域有感地震報告
```
若回傳 401／403 或欄位結構不符預期，改用網頁並在執行日誌註明。

**授權碼保密**：`CWA_API_KEY` 的值**絕對不可**寫入報告、`data/`、執行日誌、commit 訊息或任何會被 push 的檔案；指令中只能以 `$CWA_API_KEY` 引用，不可 echo 出來。

整理項目：
- 時間（UTC+8）、震央、深度、規模（註明 ML / Mw）、最大震度
- 是否為某主震的餘震序列；CWA 若有發布活動分析或餘震預估，摘要其結論
- 每一筆事件寫入 `data/events.jsonl`（欄位：`id`、`source`、`time_utc`、`lat`、`lon`、`depth_km`、`mag`、`mag_type`、`url`）

### 1.2 學術與技術進展（過去 7 天）

以 WebSearch／WebFetch 檢索，主題：
- 地震機率評估（OEF、ETAS、時間相依機率模型）
- 地震早期預警演算法（含機器學習、P 波判釋）
- 地球物理異常：地磁、電離層 TEC、地下水位／水化學

來源優先序：期刊官網（AGU、Nature、Science、SSA、GJI、Elsevier）＞ arXiv `physics.geo-ph` ＞ 研究機構新聞稿。
每篇記錄：標題、作者／機構、日期、一句話結論、**證據等級**（同儕審查／預印本／新聞稿）、連結。
**最低檢索要求**（每次都要做完，不可只搜一次）：
- 三個主題**各自**至少搜尋 2 次（一次英文、一次中文或換關鍵字），查詢中加上當年年份（例如 `2026`）以避開舊資料
- 讀取 arXiv 最新清單：`https://arxiv.org/list/physics.geo-ph/recent`，挑出與上述主題相關的條目
- 只收錄發表或上線日期在過去 7 天內的項目；日期不明或較舊的不列入表格，可在執行日誌註明「搜尋結果皆為舊資料」

若無新論文，直接寫「本期無新增」，**不要補湊**。

---

## 2. 民間預測與多維指標紀錄

### 2.1 民間預測來源

- quakeforecast（台灣地震預測研究所）：首頁由 JavaScript 載入，直接抓首頁**讀不到文章**。改用 Blogger 的 feed：
  ```bash
  curl -s -m 30 "https://quakeforecast.blogspot.com/feeds/posts/default?alt=rss&max-results=15" -o qf.xml
  ```
  用 Python 解析 `<item>` 的 `title`、`link`、`pubDate`、`description`（內文為 HTML，需去標籤），篩出過去 7 天的文章。
  若 feed 失敗，再試 `https://quakeforecast.blogspot.com/feeds/posts/default`（Atom）；兩者都失敗才在執行日誌註明。

每則預測抽取成結構化紀錄，寫入 `data/predictions.jsonl`：

```json
{
  "id": "<來源>-<發文日期>-<序號>",
  "source": "quakeforecast",
  "post_url": "...",
  "posted_at": "YYYY-MM-DD",
  "window_start": "YYYY-MM-DD",
  "window_end": "YYYY-MM-DD",
  "region_text": "原文地點描述",
  "lat": null, "lon": null, "radius_km": null,
  "mag_min": null, "mag_max": null,
  "claim_quote": "原文關鍵句（25 字內）",
  "status": "pending"
}
```

規則：
- 原文沒寫的欄位一律填 `null`，**不可推測補值**；模糊描述（如「近期」「東部外海」）保留在 `region_text`，並在報告標註「無法量化」。
- 民間內容僅作為**比對輸入**，報告中不得當作事實陳述。

### 2.2 非常規多維物理因子

| 因子 | 建議資料來源 | 記錄內容 |
|---|---|---|
| Kp 指數／地磁擾動 | NOAA SWPC `https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json`；GFZ Potsdam Kp | 24h 最大 Kp、是否達 G1 以上磁暴 |
| 太陽耀斑 | NOAA SWPC（GOES X-ray flares） | M／X 級耀斑時間與等級 |
| 大氣電場／空氣電壓 | 以搜尋找公開監測站或研究討論 | 有資料才記錄，並註明來源可信度 |
| 降雨／水文負載 | 有 `CWA_API_KEY` 時用資料集 O-A0002-001（自動雨量站）；否則搜尋 CWA 豪雨特報、颱風消息 | 台灣過去 7 天顯著降雨事件、累積雨量 |

表中**四項都要實際嘗試取得**，不可跳過；拿不到資料時，在報告該小節寫明「已嘗試的來源與失敗原因」，而不是省略整節。

每項標註：**已確認的觀測值** vs **與地震關聯的主張**（後者需註明屬於「理論」或「推測」，以及目前主流學界的態度）。

---

## 3. 實證比對與回測筆記

### 3.1 結算到期預測

讀取 `data/predictions.jsonl` 中 `status = "pending"` 且 `window_end < TODAY` 的紀錄，對照 `data/events.jsonl`（不足時以 USGS API 補查該時間窗）：

| 判定 | 條件 |
|---|---|
| `hit` | 時間在窗口內、距離 ≤ `radius_km`（未給則預設 100 km）、規模落在宣稱範圍 ±0.5 |
| `partial` | 三項條件中符合兩項 |
| `miss` | 窗口內無符合事件 |
| `unverifiable` | 預測缺乏可量化的時間、地點或規模 |

更新該筆 `status`，並追加一列到 `data/backtest.csv`：
`prediction_id,source,window_start,window_end,matched_event_id,time_offset_h,distance_km,mag_error,status,checked_at`

### 3.2 基準率檢查（避免偽陽性）

對每個 `hit`，估算「隨機猜測也會命中」的機率：以 USGS 查詢同區域、同半徑、同規模門檻在過去 10 年的年發生率，換算為該窗口長度內至少發生一次的機率（Poisson）。
- 若基準機率 > 50%，標註「**命中不具鑑別力**」。

### 3.3 前兆指標對照

將今日顯著地震（M ≥ 5 或台灣有感）與前 1–7 天的 Kp、耀斑、降雨紀錄並列，只描述時序關係，**不下因果結論**。

### 3.4 累計統計

從 `data/backtest.csv` 計算各來源累計：預測數、hit／partial／miss／unverifiable 數、命中率。

---

## 4. 報告格式（寫入 `reports/YYYY/TODAY.md`）

```markdown
# 地震風險情報日報 — TODAY

## 今日重點
（3–5 點，最重要的先寫）

## 一、官方數據與學術進展
### 1.1 過去 24 小時地震活動（表格）
### 1.2 餘震序列與官方分析
### 1.3 學術新進展（表格：標題｜證據等級｜結論｜連結）

## 二、民間預測與多維指標紀錄
### 2.1 新增民間預測（表格）
### 2.2 太空天氣與地磁（Kp、耀斑）
### 2.3 大氣電場
### 2.4 降雨與水文負載

## 三、實證比對與回測筆記
### 3.1 今日結算的預測（表格）
### 3.2 基準率檢查
### 3.3 前兆指標時序對照
### 3.4 各來源累計命中統計

## 執行日誌
（無法存取的來源、缺少的資料、需要人工確認的事項）

## 資料來源
（本報告引用的所有網址）
```

標註規則：
- 每項敘述後附來源連結。
- 以【事實】【理論】【推測】標記性質不明確的敘述。
- 數值保留原始單位與資料來源，不自行換算規模類型。

---

## 5. 驗證與防幻覺（輸出前自檢）

- [ ] 報告中每個地震事件都能在 `data/events.jsonl` 找到對應紀錄與網址
- [ ] 沒有任何論文、數值、網址是未實際讀取過的
- [ ] 民間預測只出現在第二、三區塊，且未被寫成事實
- [ ] `data/` 內沒有重複 `id`
- [ ] 抓取失敗的來源已寫入執行日誌，而非被省略
- [ ] 覆蓋率：1.2 的三個主題都至少搜尋 2 次、arXiv 清單已讀；2.1 的 feed 已嘗試；2.2 的四項因子都有對應小節
- [ ] 若 quakeforecast feed 有過去 7 天的文章，`predictions.jsonl` 卻沒有新增，要說明原因（例如文章不含可量化預測）

## 6. 完成後

1. 只在 `daily-reports` 分支 commit 並 push，**不可動 `main`**、不可 force push：
   ```bash
   git add reports/ data/
   git commit -m "daily report: TODAY"
   git push origin daily-reports
   ```
   push 失敗（例如遠端有新 commit）時，先 `git pull --rebase origin daily-reports` 再重試一次；仍失敗就在摘要中說明。
2. 在對話最後輸出 5 行以內的摘要：今日顯著地震數、新增預測數、結算結果、報告檔案路徑、push 是否成功。
