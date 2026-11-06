#requires -Version 5.1
# Visualize-Results.ps1
# 利用 Python 的 matplotlib、seaborn 產出時間序列風險指標圖、熱力圖（前驅貢獻度）、地圖標記（使用 GPS 或 USGS 圖塊）。
# 此腳本假設已安裝 Python 並有 matplotlib、seaborn、pandas 套件。

param(
    [Parameter(Mandatory=$false)]
    [string]$RiskScoreCsv = ".\outputs\risk_scores.csv",
    [Parameter(Mandatory=$false)]
    [string]$OutputDir = ".\outputs"
)

if (-Not (Test-Path -LiteralPath $RiskScoreCsv)) {
    Write-Error "找不到風險分數檔案: $RiskScoreCsv"
    exit 1
}

# 確保輸出目錄存在
if (-Not (Test-Path -LiteralPath $OutputDir)) {
    try {
        New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
    } catch {
        Write-Error "無法建立輸出目錄 $OutputDir：$_"
        exit 1
    }
}

# Python 腳本內容
$pythonScript = @"
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import os

# 讀取風險分數
df = pd.read_csv(r"$RiskScoreCsv")
# 假設有 Timestamp 欄位，轉為 datetime
if 'Timestamp' in df.columns:
    df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    df = df.sort_values('Timestamp')
else:
    # 若無 Timestamp，則使用索引
    df['Timestamp'] = pd.RangeIndex(len(df))

# 時間序列圖
plt.figure(figsize=(12, 6))
sns.lineplot(data=df, x='Timestamp', y='RiskScorePercent')
plt.title('時間序列風險指標')
plt.ylabel('風險指數 (%)')
plt.xlabel('時間')
plt.xticks(rotation=45)
plt.tight_layout()
ts_path = os.path.join(r"$OutputDir", 'risk_timeseries.png')
plt.savefig(ts_path, dpi=150)
plt.close()

# 前驅貢獻度熱力圖（假設有前驅欄位 GPS_norm, MAG_norm, ...）
precursor_cols = [c for c in df.columns if c.endswith('_norm')]
if precursor_cols:
    # 取最近 30 筆作為示例
    plot_df = df[precursor_cols].tail(30).copy()
    plot_df.index = df['Timestamp'].tail(30)
    plt.figure(figsize=(10, 8))
    sns.heatmap(plot_df.T, cmap='YlOrRd', cbar_kws={'label': '正規化貢獻'})
    plt.title('最近 30 筆前驅正規化貢獻度熱力圖')
    plt.ylabel('前驅類型')
    plt.xlabel('時間 (最近 30 筆)')
    plt.tight_layout()
    heat_path = os.path.join(r"$OutputDir", 'precursors_heatmap.png')
    plt.savefig(heat_path, dpi=150)
    plt.close()
else:
    print('未偵測到前驅正規化欄位，跳過熱力圖。')

# 簡易地圖標記（假設有 Latitude, Longitude 欄位）
if {'Latitude','Longitude'} <= set(df.columns):
    try:
        import folium
        from folium.plugins import HeatMap
        # 取最近 100 筆作為熱點
        map_df = df[['Latitude','Longitude','RiskScorePercent']].tail(100).copy()
        # 建立基礎地圖，中心取平均座標
        m = folium.Map(location=[map_df['Latitude'].mean(), map_df['Longitude'].mean()], zoom_start=8)
        # 準備熱點資料：[lat, lon, weight]
        heat_data = [[row['Latitude'], row['Longitude'], row['RiskScorePercent']/100.0] for _, row in map_df.iterrows()]
        HeatMap(heat_data, radius=12, blur=15, max_zoom=1).add_to(m)
        map_path = os.path.join(r"$OutputDir", 'risk_heatmap.html')
        m.save(map_path)
        print(f'地圖熱圖已儲存至 {map_path}')
    except ImportError:
        print('folium 未安裝，跳過地圖繪製。')
else:
    print('未偵測到 Latitude/Longitude 欄位，跳過地圖繪製。')

print('視覺化完成。')
"@

# 寫入臨時 Python 檔案並執行
$pythonFile = Join-Path $env:TEMP "visualize_$([guid]::NewGuid()).py"
$pythonScript | Set-Content -Path $pythonFile -Encoding UTF8
Write-Output "正在執行 Python 視覺化腳本..."
python $pythonFile
if (Test-Path $pythonFile) {
    Remove-Item $pythonFile -Force
}
Write-Output "視覺化結果已放置於 $OutputDir"
