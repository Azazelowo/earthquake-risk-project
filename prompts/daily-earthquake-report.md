# 每日地震風險情報彙整（Claude Code 排程任務）

> 用途：支援實務專題「基於地球物理與多維觀測資料打造地震風險評估 APP」。
> 執行方式：由 Claude Code 排程每日自動執行，無人在旁互動。
> 排程：每日 22:00（Asia/Taipei）＝ UTC 14:00，cron `0 14 * * *`。

---

## 0. 執行前提（每次都要先做）

1. 這是**無人值守**的執行：不要提問、不要等待確認，遇到問題就記錄在報告的「執行日誌」並繼續。
2. 取得目前時間，以 **Asia/Taipei（UTC+8）** 為準，定義：
   - `TODAY` = 今日日期（YYYY-MM-DD）
   - 檢索窗口：預設為過去 24 小時；學術與民間來源可放寬到過去 7 天
   - **補收漏掉的日子**：切到報告分支（第 3 步）後，找出 `reports/` 下 TODAY 以前**最新**一份報告的日期 `LAST`。若 `LAST` 早於昨天（代表中間有日子沒跑），地震檢索窗口（USGS、CWA）的起點改為 `LAST` 當天 21:00（Asia/Taipei），**最多往前 7 天**。補收到的事件照常寫入 `data/events.jsonl`（以 `id` 去重）。報告開頭寫明「補收期間：<起>～<訖>，原因：前次排程未執行」，並在 1.1 節的表格加一欄標示哪些事件屬於補收。漏掉的日子**不另外產生**當天的報告檔。
3. **切換到報告分支**（報告與回測資料只存在 `daily-reports` 分支，`main` 只放程式與提示）：
   ```bash
   git fetch origin
   # 清掉前次執行殘留、仍佔用 daily-reports 的 worktree（否則下一行會失敗）
   git worktree prune
   OLD=$(git worktree list --porcelain | awk '/^worktree /{p=$2} /^branch refs\/heads\/daily-reports$/{print p}')
   [ -n "$OLD" ] && git worktree remove --force "$OLD"
   WT="$(mktemp -d)/wt"   # 暫存目錄，不動使用者的工作目錄
   if git ls-remote --exit-code --heads origin daily-reports >/dev/null; then
     git worktree add -B daily-reports "$WT" origin/daily-reports
   else
     git worktree add -b daily-reports "$WT" origin/main
   fi
   cd "$WT"
   # 報告分支不保存提示檔，避免讀到過期版本；任務說明一律以 origin/main 為準
   [ -d prompts ] && git rm -r -q prompts
   ```
   之後所有讀寫都在 `$WT` 內進行，`data/` 以這個分支的內容為準（上一次執行累積的回測資料在這裡）。
4. 專案根目錄下的資料位置（不存在就建立）：
   - `reports/YYYY/TODAY.md` — 今日報告
   - `data/predictions.jsonl` — 民間預測與前兆紀錄（回測資料庫，**只追加或更新狀態，不刪除**）
   - `data/events.jsonl` — 已收錄的真實地震事件
   - `data/backtest.csv` — 比對結果彙總
5. 若 `reports/YYYY/TODAY.md` 已存在（同日重跑）：
   - **從頭重新產生整份報告**，不可讀取舊報告再局部修改（局部修改會留下前一版過期的敘述）
   - 只能從 `data/` 和本次抓到的資料取得內容；開頭註明「本檔覆寫同日稍早的版本」
   - `data/` 內的紀錄以 `id` 去重，不可重複寫入
6. 語言：繁體中文（台灣用語），專有名詞可保留英文。
7. **執行環境可能是 Windows（Git Bash）**，系統預設編碼為 cp950：
   - 每個 Bash 指令開頭先 `export PYTHONIOENCODING=utf-8`
   - Python 讀寫檔案一律寫明 `open(path, encoding="utf-8")`，JSONL 寫入時用 `json.dumps(obj, ensure_ascii=False)`
   - 不要在使用者的工作目錄直接切換分支；用 `git worktree add` 在暫存目錄操作 `daily-reports`，完成後 `git worktree remove`
8. **數值一律由程式計算**：報告中所有從 `data/` 推導出的數字，包括事件數、群組數、合併窗口的起訖日、最近結算日、命中統計、`base_rate_p`，都必須用 Python 讀取 `data/` 計算後，直接把輸出填進報告，**不可憑記憶或從前一版報告抄寫**。報告寫完後，再用程式重新計算一次，和報告中的數字比對；不一致就修正報告，並在執行日誌記錄。

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
  "region_list": ["JP", "TW"],
  "lat": null, "lon": null, "radius_km": null,
  "mag_min": null, "mag_max": null,
  "claim_quote": "原文關鍵句（25 字內）",
  "group_id": "<見 3.1 合併規則>",
  "status": "pending"
}
```

規則：
- 原文沒寫的欄位一律填 `null`，**不可推測補值**；模糊描述（如「近期」「東部外海」）保留在 `region_text`，並在報告標註「無法量化」。
- `region_list`：原文列出**國家或地區名稱**時，轉成 3.1「地區代碼表」的代碼；原文是點位或半徑時填 `null`，改填 `lat`／`lon`／`radius_km`。表中沒有的地名保留在 `region_text`，並在執行日誌註明「代碼表缺少：<地名>」，不要自行新增代碼。
- 規模：原文只寫單一數值（如「M7.0」）且沒有「以上」等字眼時，`mag_min` 和 `mag_max` 都填該值；寫了「M7 以上」則 `mag_min = 7.0`、`mag_max = null`。
- **欄位補齊**：已存在、但缺少 `region_list` 或 `group_id` 的 `pending` 紀錄，依本節和 3.1 的規則補上。這是唯一允許修改舊紀錄的情況（另一個是更新 `status`），並在執行日誌列出補了哪些 id。
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

#### (a) 重複預測合併

同一來源反覆發布同一模板的預測（例如每隔幾天就發一次「30 天內，某些國家，M7.0」），如果每筆各算一次，命中率會被灌水。所以：

- `group_id` = `<source>-<region_list 排序後以 + 連接>-M<mag_min>-<窗口天數>d-<群組內最早的 window_start>`
- 新紀錄的 source、`region_list`（或點位與半徑）、規模、窗口天數都和某個群組相同，**而且** `window_start ≤ 該群組第一筆的 window_end`，就加入該群組並沿用它的 `group_id`；否則自己開一個新群組
- 只和**第一筆**比較，是為了避免定期重發的預測一直串連，導致群組永遠無法結算；這樣一個群組最長約涵蓋兩個窗口
- **一個群組只結算一次**：等群組內**最晚**的 `window_end < TODAY` 才結算。合併窗口 = 最早的 `window_start` 到最晚的 `window_end`，群組內每筆的 `status` 都更新成同一個結果
- `backtest.csv` 每個群組只寫一列，`prediction_id` 填 `group_id`
- 累計統計（3.4）以**群組數**為準，另欄列出原始發文數

#### (b) 判定空間是否符合

1. 有 `lat`／`lon`：計算距離，≤ `radius_km`（未給則預設 100 km）就算符合。
2. 有 `region_list`：事件符合下列任一條件，就算符合：
   - USGS 事件的 `place` 字串（通常以「, 國名」結尾）含有代碼表中對應的英文名稱
   - `place` 只寫海域名稱（如 "Banda Sea"、"Philippine Islands region"），而震央落在代碼表的**經緯度範圍**內
   在 `backtest.csv` 的 `match_basis` 欄記錄依據（`place` 或 `bbox`）。經緯度範圍彼此有重疊，而且只是粗略涵蓋陸地和近海；以 `bbox` 判定時，要在報告註明「依粗略範圍判定」。

**地區代碼表**（範圍含近海，數值為粗略涵蓋，只用於判定，不代表國界）：

| 代碼 | 名稱 | USGS `place` 比對字串 | 緯度 | 經度 |
|---|---|---|---|---|
| JP | 日本 | Japan | 24 ~ 46 | 122 ~ 154 |
| TW | 台灣 | Taiwan | 21 ~ 26.5 | 119 ~ 123.5 |
| CN | 中國大陸 | China | 18 ~ 54 | 73 ~ 135 |
| MM | 緬甸 | Myanmar | 9.5 ~ 28.5 | 92 ~ 101.5 |
| NP | 尼泊爾 | Nepal | 26 ~ 30.5 | 80 ~ 88.5 |
| PH | 菲律賓 | Philippines | 4 ~ 21.5 | 116 ~ 127.5 |
| ID | 印尼 | Indonesia | -11.5 ~ 6.5 | 94.5 ~ 141.5 |
| MY | 馬來西亞 | Malaysia | 0.5 ~ 7.5 | 99.5 ~ 119.5 |
| PG | 巴布亞紐幾內亞 | Papua New Guinea | -12 ~ 0 | 140.5 ~ 156 |

#### (c) 判定與記錄

規模一律採用 USGS 的 preferred magnitude（優先 Mww），**不可**用 CWA ML 或 mb 去和 M7 等級的宣稱比較；在 `mag_type` 欄記錄所用的規模類型。

| 判定 | 條件 |
|---|---|
| `hit` | 合併窗口內，有事件同時符合空間條件，且規模落在宣稱範圍 ±0.5 |
| `partial` | 三項條件（時間、空間、規模）中符合兩項，例如窗口內同一地區有 M6.3，或其他地區有 M7 |
| `miss` | 合併窗口內沒有符合兩項以上的事件 |
| `unverifiable` | 時間、空間、規模中任何一項無法量化 |

有多個符合的事件時，取規模誤差最小的一個，並在報告列出所有符合事件的數量。

追加一列到 `data/backtest.csv`（檔案已存在但標頭是舊格式時，改用新標頭重寫該檔，保留舊資料列，缺少的欄位留空）：
`prediction_id,source,raw_post_count,window_start,window_end,matched_event_id,match_basis,time_offset_h,distance_km,mag_type,mag_error,status,base_rate_p,checked_at`

### 3.2 基準率檢查（避免偽陽性）

對每個 `hit` 和 `partial`，估算「隨機猜測也會命中」的機率 `base_rate_p`：

1. 規模門檻 = 宣稱下限 − 0.5（例如宣稱 M7.0，就用 M ≥ 6.5）。
2. 查詢 USGS 過去 10 年（到 TODAY 為止）符合門檻的事件：
   - 點位預測：用 `latitude`、`longitude`、`maxradiuskm` 查詢
   - 地區清單：**每個代碼的經緯度範圍各查一次**，再以事件 `id` **去重**（範圍有重疊，不去重會重複計數），並同樣套用 (b) 的空間判定
3. 年發生率 λ = 去重後事件數 ÷ 10；窗口長度 T（天）= 合併窗口的天數。
4. `base_rate_p = 1 − exp(−λ × T / 365)`（Poisson，至少發生一次的機率）。
5. `base_rate_p > 0.5` 時標註「**命中不具鑑別力**」；`> 0.9` 時標註「**幾乎必然命中，該預測沒有資訊量**」。

報告中要寫出計算過程（事件數、λ、T、p），讓讀者可以重算。這項檢查在群組**結算當天**做一次即可，結果寫進 `backtest.csv` 的 `base_rate_p` 欄。

### 3.3 前兆指標對照

將今日顯著地震（M ≥ 5 或台灣有感）與前 1–7 天的 Kp、耀斑、降雨紀錄並列，只描述時序關係，**不下因果結論**。

### 3.4 累計統計

從 `data/backtest.csv` 計算各來源累計：**群組數**（另欄列原始發文數）、hit／partial／miss／unverifiable 數、命中率，以及「扣除不具鑑別力的命中後」的命中率。尚未結算的群組另列 pending 數與最近的結算日。

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
- [ ] 報告中由 `data/` 推導的數字（窗口起訖、結算日、群組數、命中統計）已用程式重算比對，和資料一致（第 0 節第 8 點）
- [ ] 若有漏掉的日子，報告開頭已寫明補收期間，補收的事件也已標示（第 0 節第 2 點）
- [ ] 報告是整份重新產生，沒有殘留前一版的敘述

## 6. 完成後

1. 只在 `daily-reports` 分支 commit 並 push，**不可動 `main`**、不可 force push：
   ```bash
   git add reports/ data/
   git commit -m "daily report: TODAY"
   git push origin daily-reports
   ```
   push 失敗（例如遠端有新 commit）時，先 `git pull --rebase origin daily-reports` 再重試一次；仍失敗就在摘要中說明。
   push 成功後移除 worktree：`cd` 回原目錄，執行 `git worktree remove "$WT"`。
2. 在對話最後輸出 5 行以內的摘要：今日顯著地震數、新增預測數、結算結果、報告檔案路徑、push 是否成功。
