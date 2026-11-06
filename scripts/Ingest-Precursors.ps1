#requires -Version 5.1
# Ingest-Precursors.ps1
# 整合 GPS 變形、地磁、地下水層、離電層 TEC、氣壓、雨量、潮汐等前驅資料
# 假設檔案位於 .\precursor_data\ 下，檔名如下：
#   gps_deformation.csv
#   geomagnetic.csv
#   groundwater.csv
#   ionosphere_tec.csv
#   atmospheric_pressure.csv
#   rainfall.csv
#   tidal.csv
# 每個檔案均需包含 ObservationTime 欄位（格式 yyyy-MM-dd HH:mm:ss）

param(
    [Parameter(Mandatory=$false)]
    [string]$DataDir = ".\precursor_data"
)

if (-Not (Test-Path -LiteralPath $DataDir)) {
    Write-Error "找不到資料夾: $DataDir"
    exit 1
}

# 定義要處理的檔案及其對應的名稱（用於後續欄位前綴）
$files = @{
    GPS        = "gps_deformation.csv"
    MAG        = "geomagnetic.csv"
    GW         = "groundwater.csv"
    TEC        = "ionosphere_tec.csv"
    PRESSURE   = "atmospheric_pressure.csv"
    RAINFALL   = "rainfall.csv"
    TIDAL      = "tidal.csv"
}

$allData = @()

foreach ($key in $files.Keys) {
    $filePath = Join-Path $DataDir $files[$key]
    if (-Not (Test-Path -LiteralPath $filePath)) {
        Write-Warning "找不到前驅檔案：$filePath，將跳過此類型。"
        continue
    }
    try {
        $data = Import-Csv -Path $filePath -ErrorAction Stop
    } catch {
        Write-Error "匯入 $filePath 失敗：$_"
        continue
    }
    # 檢查 ObservationTime 欄位
    if (-Not $data[0].PSObject.Properties.Name -contains "ObservationTime") {
        Write-Warning "檔案 $filePath 缺少 ObservationTime 欄位，將跳過。"
        continue
    }
    # 加入時間戳與來源標記
    foreach ($row in $data) {
        try {
            $row.ObsTime = [datetime]::ParseExact($row.ObservationTime, "yyyy-MM-dd HH:mm:ss", $null)
        } catch {
            Write-Warning "時間格式錯誤於 $filePath 的 ObservationTime: $($row.ObservationTime)"
            continue
        }
        $row.Source = $key   # 加入來源標識
        $allData += $row
    }
}

if ($allData.Count -eq 0) {
    Write-Error "未成功匯入任何前驅資料。"
    exit 1
}

# 依時間排序（可選）
$sorted = $allData | Sort-Object ObsTime

# 輸出統一後的資料（可視需求另存或直接傳給下一步腳本）
$outputPath = ".\outputs\precursors_combined.csv"
$sorted | Export-Csv -Path $outputPath -NoTypeInformation -Encoding UTF8
Write-Output "已將 $($sorted.Count) 筆前驅觀測統一時間戳並合併儲存至：$outputPath"
Write-Output "前幾筆範例："
$sorted | Select-Object -First 5 | Format-List
