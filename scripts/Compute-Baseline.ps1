#requires -Version 5.1
# Compute-Baseline.ps1
# 依 30 天區域平均作為基線（可參考 ML、深度或震頻）。

param(
    [Parameter(Mandatory=$false)]
    [string]$CatalogPath = ".\earthquake_data\CWA_catalog.csv",
    [Parameter(Mandatory=$false)]
    [int]$Days = 30
)

if (-Not (Test-Path -LiteralPath $CatalogPath)) {
    Write-Error "找不到檔案: $CatalogPath"
    exit 1
}

try {
    $catalog = Import-Csv -Path $CatalogPath -ErrorAction Stop
} catch {
    Write-Error "匯入 CSV 失敗: $_"
    exit 1
}

# 轉換時間欄位
try {
    $catalog.DateTime = [datetime]::ParseExact($catalog.OriginTime, "yyyy-MM-dd HH:mm:ss", $null)
} catch {
    Write-Error "時間欄位轉換失敗，請確認 OriginTime 格式為 yyyy-MM-dd HH:mm:ss"
    exit 1
}

$cutoff = (Get-Date).AddDays(-$Days)
$recent = $catalog | Where-Object { $_.DateTime -ge $cutoff }

if ($recent.Count -eq 0) {
    Write-Warning "最近 $Days 天內沒有資料，將使用全部資料計算基線。"
    $recent = $catalog
}

# 計算基線（平均 ML、平均 Depth，以及震頻＝筆數/天數）
$baselineML = ($recent.ML | Measure-Object -Average).Average
$baselineDepth = ($recent.Depth | Measure-Object -Average).Average
$baselineRate = $recent.Count / $Days   # 平均每日震次

Write-Output "=== 基線計算結果（最近 $Days 天）===
Baseline ML : $([math]::Round($baselineML,3))
Baseline Depth (km) : $([math]::Round($baselineDepth,3))
Average daily event count : $([math]::Round($baselineRate,2))
"

# 可選擇將基線寫入檔案供後續腳本使用
$baselineInfo = @{
    BaselineML = $baselineML
    BaselineDepth = $baselineDepth
    BaselineDailyRate = $baselineRate
    CalculationDate = Get-Date
    DaysUsed = $Days
}
$baselineInfo | ConvertTo-Json -Depth 3 | Set-Content -Path ".\outputs\baseline.json" -Encoding UTF8
Write-Output "基線資料已寫入 .\outputs\baseline.json"
