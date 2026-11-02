# 常用腳本技能（Skills）

以下為在 PowerShell、Python 或其他腳本中常用的指令、函式或工作流範例，方便未來直接呼叫重複使用。

## PowerShell

### 讀取 CWA 地震目錄 CSV
```powershell
# Import-EarthquakeCatalog.ps1
$csvPath = ".\earthquake_data\CWA_catalog.csv"
if (-Not (Test-Path $csvPath)) {
    Write-Error "找不到檔案: $csvPath"
    exit 1
}
$catalog = Import-Csv -Path $csvPath
# 欄位檢查
$required = @("OriginTime","Latitude","Longitude","ML","Depth")
foreach ($col in $required) {
    if (-Not $catalog.PSObject.Properties.Name -contains $col) {
        Write-Error "缺少必要欄位: $col"
        exit 1
    }
}
$catalog | Select-Object -First 5
```

### 計算 30 天區域平均作為基線
```powershell
# Compute-Baseline.ps1
param(
    [string]$CatalogPath = ".\earthquake_data\CWA_catalog.csv",
    [int]$Days = 30
)
$catalog = Import-Csv -Path $CatalogPath
$catalog.DateTime = [datetime]::ParseExact($catalog.OriginTime, "yyyy-MM-dd HH:mm:ss", $null)
$cutoff = (Get-Date).AddDays(-$Days)
$recent = $catalog | Where-Object {$_.DateTime -ge $cutoff}
$baselineML = ($recent.ML | Measure-Object -Average).Average
$baselineDepth = ($recent.Depth | Measure-Object -Average).Average
Write-Output "基線 ML: $baselineML"
Write-Output "基線 深度: $baselineDepth"
```

### 整合 GPS 變形、地磁、地下水層、離電層 TEC、氣壓、雨量、潮汐等前驅資料
```powershell
# Ingest-Precursors.ps1
# 此處示例：讀取 CSV 並統一時間戳（假設皆有 ObservationTime 欄位）
$gps      = Import-Csv ".\precursor_data\gps_deformation.csv"
$mag      = Import-Csv ".\precursor_data\geomagnetic.csv"
$gw       = Import-Csv ".\precursor_data\groundwater.csv"
$tec      = Import-Csv ".\precursor_data\ionosphere_tec.csv"
$pressure = Import-Csv ".\precursor_data\atmospheric_pressure.csv"
$rainfall = Import-Csv ".\precursor_data\rainfall.csv"
$tidal    = Import-Csv ".\precursor_data\tidal.csv"

# 統一時間格範例（轉為 datetime）
foreach ($arr in @($gps,$mag,$gw,$tec,$pressure,$rainfall,$tidal)) {
    foreach ($row in $arr) {
        $row.ObsTime = [datetime]::ParseExact($row.ObservationTime, "yyyy-MM-dd HH:mm:ss", $null)
    }
}
# 後續可依需求合併或格網化
```

### 風險評分 (Weighted Sum)
```powershell
# Risk-Scoring.ps1
param(
    [double]$WeightGPS = 0.20,
    [double]$WeightMag = 0.15,
    [double]$WeightGW  = 0.10,
    [double]$WeightTEC = 0.15,
    [double]$WeightPres= 0.10,
    [double]$WeightRain= 0.10,
    [double]$WeightTidal=0.10
)
# 假設已經正規化特徵向量 $features 為哈希表
$features = @{
    GPS      = 0.6   # 範例正規化後值
    Mag      = 0.4
    GW       = 0.3
    TEC      = 0.7
    Pres     = 0.5
    Rain     = 0.2
    Tidal    = 0.1
}
$score = 0
foreach ($key in $features.Keys) {
    $weight = Get-Variable -Name "Weight$($key.ToUpper())" -ValueOnly -ErrorAction SilentlyContinue
    if (-Not $weight) { $weight = 0 }
    $score += $features[$key] * $weight
}
$scorePercent = [math]::Round($score * 100, 2)
# 產出摘要
$contribs = $features.GetEnumerator() | Sort-Object {[Math]::Abs($_.Value * (Get-Variable -Name "Weight$($_.Key.ToUpper())" -ValueOnly))} -Descending | Select-Object -First 3
$summary = "基於 $($contribs[0].Key)、$($contribs[1].Key)、$($contribs[2].Key) 觀測，區域風險上升 $scorePercent%。"
Write-Output $summary
```

### 視覺化 (呼叫 Python)
```powershell
# Visualize-Results.ps1
$pythonScript = @"
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
df = pd.read_csv(r'output\risk_scores.csv')
plt.figure(figsize=(10,5))
sns.lineplot(data=df, x='Date', y='RiskScore')
plt.title('時間序列風險指標')
plt.ylabel('風險指數 (0-100)')
plt.xlabel('日期')
plt.tight_layout()
plt.savefig('output\risk_timeseries.png')
plt.close()
"@
$pythonScript | Set-Content -Path ".\temp_plot.py"
python .\temp_plot.py
Remove-Item .\temp_plot.py
```

## Python (Jupyter Notebook 範例)
```python
# 在 notebooks/ 內的範例 cell
import pandas as pd
import folium
from branca.colormap import linear

# 讀取地震目錄
df = pd.read_csv('../earthquake_data/CWA_catalog.csv')
df['date'] = pd.to_datetime(df['OriginTime'])

# 建立基本地圖
m = folium.Map(location=[23.5, 121.0], zoom_start=7)
# 添加熱點層 (簡化示例)
heat_data = df[['Latitude','Longitude','ML']].values.tolist()
folium.plugins.HeatMap(heat_data, radius=15).add_to(m)

m.save('../outputs/earthquake_heatmap.html')
```
