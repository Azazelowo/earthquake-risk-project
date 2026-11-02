#requires -Version 5.1
# Import-EarthquakeCatalog.ps1
# 讀取 CWA 地震目錄 CSV，做基本欄位檢查（時間、經緯度、ML、深度）。

param(
    [Parameter(Mandatory=$false)]
    [string]$CsvPath = ".\earthquake_data\CWA_catalog.csv"
)

if (-Not (Test-Path -LiteralPath $CsvPath)) {
    Write-Error "找不到檔案: $CsvPath"
    exit 1
}

try {
    $catalog = Import-Csv -Path $CsvPath -ErrorAction Stop
} catch {
    Write-Error "匯入 CSV 失敗: $_"
    exit 1
}

# 必要欄位清單
$required = @("OriginTime","Latitude","Longitude","ML","Depth")
$missing = $required | Where-Object { -Not $catalog[0].PSObject.Properties.Name -contains $_ }
if ($missing) {
    Write-Error "缺少必要欄位: $($missing -join ', ')"
    exit 1
}

# 基本型別檢查（簡易）
foreach ($row in $catalog) {
    # 時間格式嘗試轉換
    if (-Not [datetime]::TryParseExact($row.OriginTime, "yyyy-MM-dd HH:mm:ss", $null, [System.Globalization.DateTimeStyles]::None, [ref]$null)) {
        Write-Warning "時間格式異常: $($row.OriginTime)"
    }
    # 經緯度範圍
    $lat = [double]$row.Latitude
    $lon = [double]$row.Longitude
    if ($lat -lt -90 -or $lat -gt 90) {
        Write-Warning "緯度超出範圍: $lat"
    }
    if ($lon -lt -180 -or $lon -gt 180) {
        Write-Warning "經度超出範圍: $lon"
    }
    # ML 和 Depth 必須為數值且非負（Depth 可為 0）
    $ml = [double]$row.ML
    $depth = [double]$row.Depth
    if ($ml -lt 0) {
        Write-Warning "ML 為負數: $ml"
    }
    if ($depth -lt 0) {
        Write-Warning "深度為負數: $depth"
    }
}

# 輸出前五筆供檢閱
Write-Output "成功匯入 $($catalog.Count) 筆地震紀錄，前五筆如下："
$catalog | Select-Object -First 5 | Format-List
