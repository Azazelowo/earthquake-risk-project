#requires -Version 5.1
# Risk-Scoring.ps1
# 實作 Weighted Sum（Σ正規化特徵 × 權重 × 100）或 Rule‑based 評分，
# 並輸出「基於 X、Y、Z 觀測，區域風險上升 N%」的文字摘要。

param(
    [Parameter(Mandatory=$false)]
    [string]$FeaturesCsv = ".\outputs\features_normalized.csv",   # 上一步產出的正規化特徵
    [Parameter(Mandatory=$false)]
    [string]$BaselineJson = ".\outputs\baseline.json",          # 基線（用於計算相對增加）
    [Parameter(Mandatory=$false)]
    [double]$WeightGPS = 0.20,
    [double]$WeightMag = 0.15,
    [double]$WeightGW  = 0.10,
    [double]$WeightTEC = 0.15,
    [double]$WeightPres= 0.10,
    [double]$WeightRain= 0.10,
    [double]$WeightTidal=0.10
)

# 載入特徵
if (-Not (Test-Path -LiteralPath $FeaturesCsv)) {
    Write-Error "找不到特徵檔案: $FeaturesCsv"
    exit 1
}
try {
    $features = Import-Csv -Path $FeaturesCsv -ErrorAction Stop
} catch {
    Write-Error "匯入特徵 CSV 失敗: $_"
    exit 1
}

# 載入基線（選擇性）
$baselineScore = 0
if (Test-Path -LiteralPath $BaselineJson) {
    try {
        $baselineObj = Get-Content -Path $BaselineJson -Raw | ConvertFrom-Json
        # 假設基線分數為先前計算的平均風險分數（此處示例：使用特徵平均值*權重）
        # 若基線檔案不含分數，則設為 0
        if ($baselineObj.PSObject.Properties.Name -contains "BaselineScore") {
            $baselineScore = [double]$baselineObj.BaselineScore
        }
    } catch {
        Write-Warning "讀取基線 JSON 失敗，將基線設為 0。"
    }
}

# 權重哈希表
$weights = @{
    GPS    = $WeightGPS
    MAG    = $WeightMag
    GW     = $WeightGW
    TEC    = $WeightTEC
    PRESS  = $WeightPres
    RAIN   = $WeightRain
    TIDAL  = $WeightTidal
}

# 確保特徵欄位存在（允許不同命名）
$expectedCols = @("GPS_norm","MAG_norm","GW_norm","TEC_norm","PRESS_norm","RAIN_norm","TIDAL_norm")
$missingCols = $expectedCols | Where-Object { -Not $features[0].PSObject.Properties.Name -contains $_ }
if ($missingCols) {
    Write-Error "特徵檔案缺少以下正規化欄位：$($missingCols -join ', ')"
    exit 1
}

$results = @()
foreach ($row in $features) {
    $score = 0
    foreach ($col in $expectedCols) {
        $src = $col.Substring(0, $col.Length - "_norm".Length)   # 取前綴 GPS, MAG,...
        $val = [double]$row.$col
        $w   = $weights[$src.ToUpper()]
        $score += $val * $w
    }
    $scorePercent = [math]::Round($score * 100, 2)
    # 計算相對基線增加百分比
    $increasePct = if ($baselineScore -gt 0) { [math]::Round(($scorePercent - $baselineScore) / $baselineScore * 100, 2) } else { 0 }
    # 找出前三大貢獻來源
    $contribs = @()
    foreach ($col in $expectedCols) {
        $src = $col.Substring(0, $col.Length - "_norm".Length)
        $val = [double]$row.$col
        $w   = $weights[$src.ToUpper()]
        $contrib = $val * $w   # 未乘以 100 的部分
        $contribs += [PSCustomObject]@{Source=$src; Contribution=$contrib}
    }
    $top3 = $contribs | Sort-Object Contribution -Descending | Select-Object -First 3
    $top3Names = $top3.Source -join '、'
    $summary = "基於 $top3Names 觀測，區域風險上升 $scorePercent%（相對基線增加 $increasePct%）。"
    $results += [PSCustomObject]@{
        Timestamp = if ($row.PSObject.Properties.Name -contains "Timestamp") { $row.Timestamp } else { "" }
        RiskScorePercent = $scorePercent
        BaselineScorePercent = $baselineScore
        IncreasePercent = $increasePct
        Top3Contributors = $top3Names
        Summary = $summary
    }
}

# 輸出結果
$outputPath = ".\outputs\risk_scores.csv"
$results | Export-Csv -Path $outputPath -NoTypeInformation -Encoding UTF8
Write-Output "風險評分已儲存至：$outputPath"
Write-Output "最近 5 筆摘要："
$results | Select-Object -Last 5 | Format-List Summary
