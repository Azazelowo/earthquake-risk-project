import argparse
import os
import sys
from glob import glob

import numpy as np
import pandas as pd

# ========== Configuration ==========
GRID_SIZE = 0.2  # degrees
TIME_AGG = '1D'  # daily aggregation; can be '1H' for hourly
BASE_DIR = os.path.join(os.path.dirname(__file__), '..', '..')
DATA_DIR = os.environ.get('EQ_DATA_DIR', BASE_DIR)  # raw data may live outside git
RANDOM_SEED = 42  # only used by --demo

# 前兆範圍：只用地震電磁學相關資料（2026-11-02 決定）。
# GNSS 在本專題是用來推算電離層 TEC，不是地殼形變；RINEX（.24o）要先轉成 CSV 才能讀取。
PRECURSOR_FOLDERS = {
    'geomagnetic': os.path.join(DATA_DIR, '地震預測', '地磁及地電流'),
    'tec': os.path.join(DATA_DIR, '地震預測', '電離層電子密度變化'),
}
# Placeholder weights: equal until calibrated by backtest
WEIGHTS = {
    'geomagnetic': 0.5,
    'tec': 0.5,
}
# Placeholder: 0.658 could not be reproduced from events.parquet (Weibull k ≈ 0.41–0.54).
# The multiplication in apply_weights() has no validated basis; review before use.
CASCADE_K = 0.658

def load_precursor(folder_path, var_name):
    """Load all CSV files in folder, concatenate, ensure columns: lat, lon, time, value."""
    if not os.path.isdir(folder_path):
        print(f"Folder not found: {folder_path}")
        return pd.DataFrame()
    files = glob(os.path.join(folder_path, '*.csv'))
    if not files:
        print(f"No CSV files found in {folder_path}")
        return pd.DataFrame()
    dfs = []
    for f in files:
        try:
            df = pd.read_csv(f)
        except Exception as e:
            print(f"Failed to read {f}: {e}")
            continue
        # Expect columns: latitude, longitude, timestamp, value (adjust as needed)
        # Standardize column names
        cols = {c.lower().strip(): c for c in df.columns}
        # Try to map
        lat_col = cols.get('latitude') or cols.get('lat') or cols.get('latitude (°)') or None
        lon_col = cols.get('longitude') or cols.get('lon') or cols.get('longitude (°)') or None
        time_col = cols.get('time') or cols.get('timestamp') or cols.get('date') or cols.get('datetime') or None
        val_col = cols.get('value') or cols.get('measurement') or cols.get('observation') or cols.get('value') or None
        if not all([lat_col, lon_col, time_col, val_col]):
            print(f"Could not identify required columns in {f}. Columns: {df.columns.tolist()}")
            continue
        df = df.rename(columns={lat_col: 'latitude', lon_col: 'longitude', time_col: 'time', val_col: 'value'})
        df['time'] = pd.to_datetime(df['time'], errors='coerce')
        df = df.dropna(subset=['latitude', 'longitude', 'time', 'value'])
        df['var'] = var_name
        dfs.append(df[['latitude', 'longitude', 'time', 'value', 'var']])
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)

def spatial_temporal_bin(df):
    """Bin data into 0.2° grid and daily intervals."""
    if df.empty:
        return df
    df['lat_bin'] = (df['latitude'] // GRID_SIZE) * GRID_SIZE
    df['lon_bin'] = (df['longitude'] // GRID_SIZE) * GRID_SIZE
    df['time_bin'] = df['time'].dt.floor(TIME_AGG)
    # Aggregate: mean value per bin
    binned = df.groupby(['lat_bin', 'lon_bin', 'time_bin', 'var'])['value'].mean().reset_index()
    binned.rename(columns={'value': 'value_mean'}, inplace=True)
    return binned

def sliding_stats(df_bin, window=3):
    """Compute rolling mean and std per grid cell over time."""
    if df_bin.empty:
        return df_bin
    # Sort
    df_bin = df_bin.sort_values(['lat_bin', 'lon_bin', 'time_bin'])
    # Group by grid cell
    grouped = df_bin.groupby(['lat_bin', 'lon_bin'])
    def rolling_stats(g):
        g = g.set_index('time_bin').sort_index()
        # Ensure regular frequency? We'll just roll on index
        g['rolling_mean'] = g['value_mean'].rolling(window=window, min_periods=1).mean()
        g['rolling_std'] = g['value_mean'].rolling(window=window, min_periods=1).std().fillna(0)
        return g.reset_index()
    stats = grouped.apply(rolling_stats).reset_index(drop=True)
    return stats

def denoise_iqr(df_stats):
    """Remove outliers using ±1.5 IQR per grid cell."""
    if df_stats.empty:
        return df_stats
    def iqr_filter(g):
        q1 = g['value_mean'].quantile(0.25)
        q3 = g['value_mean'].quantile(0.75)
        iqr = q3 - q1
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr
        return g[(g['value_mean'] >= lower) & (g['value_mean'] <= upper)]
    filtered = df_stats.groupby(['lat_bin', 'lon_bin']).apply(iqr_filter).reset_index(drop=True)
    return filtered

def combine_features(feature_dfs):
    """Merge multiple precursor features into a single DataFrame with columns per precursor."""
    if not feature_dfs:
        return pd.DataFrame()
    # Start with first
    combined = feature_dfs[0].copy()
    # Rename value columns to precursor name
    for i, (var_name, df) in enumerate(zip(PRECURSOR_FOLDERS.keys(), feature_dfs)):
        if i == 0:
            df_renamed = df.rename(columns={'value_mean': var_name,
                                            'rolling_mean': f'{var_name}_rolling_mean',
                                            'rolling_std': f'{var_name}_rolling_std'})
            combined = df_renamed[['lat_bin', 'lon_bin', 'time_bin', var_name,
                                   f'{var_name}_rolling_mean', f'{var_name}_rolling_std']]
        else:
            df_renamed = df.rename(columns={'value_mean': var_name,
                                            'rolling_mean': f'{var_name}_rolling_mean',
                                            'rolling_std': f'{var_name}_rolling_std'})
            # Merge on grid and time
            combined = pd.merge(combined,
                                df_renamed[['lat_bin', 'lon_bin', 'time_bin', var_name,
                                            f'{var_name}_rolling_mean', f'{var_name}_rolling_std']],
                                on=['lat_bin', 'lon_bin', 'time_bin'],
                                how='outer')
    return combined

def apply_weights(df_features):
    """Apply placeholder weights and cascading factor to compute risk index (0-100)."""
    if df_features.empty:
        return df_features
    # Compute weighted sum of normalized features (0-1 per feature)
    weighted_sum = 0
    total_weight = 0
    for var, w in WEIGHTS.items():
        if var in df_features.columns:
            # Min-max normalize per column (handle NaN)
            col = df_features[var]
            if col.notna().any():
                col_norm = (col - col.min()) / (col.max() - col.min() + 1e-9)
                weighted_sum += w * col_norm.fillna(0)
                total_weight += w
    if total_weight > 0:
        df_features['risk_index_raw'] = weighted_sum / total_weight * 100
    else:
        df_features['risk_index_raw'] = 0
    # Apply cascading factor (example: multiply by (1 + CASCADE_K * risk_index_raw/100))
    df_features['risk_index'] = df_features['risk_index_raw'] * (1 + CASCADE_K * df_features['risk_index_raw'] / 100)
    # Clip to 0-100
    df_features['risk_index'] = df_features['risk_index'].clip(0, 100)
    return df_features

def main():
    parser = argparse.ArgumentParser(description="Clean precursor data into gridded daily features.")
    parser.add_argument('--demo', action='store_true',
                        help='when no precursor data exists, write seeded random demo data '
                             'to features_DEMO.parquet instead of failing')
    args = parser.parse_args()

    print("Loading precursor data...")
    feature_list = []
    for var_name, folder in PRECURSOR_FOLDERS.items():
        print(f"Processing {var_name} from {folder}...")
        df_raw = load_precursor(folder, var_name)
        if df_raw.empty:
            print(f"  No data for {var_name}, skipping.")
            continue
        df_bin = spatial_temporal_bin(df_raw)
        print(f"  After binning: {df_bin.shape[0]} rows")
        df_stats = sliding_stats(df_bin, window=3)
        print(f"  After sliding stats: {df_stats.shape[0]} rows")
        df_clean = denoise_iqr(df_stats)
        print(f"  After denoising: {df_clean.shape[0]} rows")
        feature_list.append((var_name, df_clean))
    out_name = 'features.parquet'
    if not feature_list:
        # Previously this silently wrote random numbers to features.parquet,
        # which looked like real output. Now it fails unless --demo is given.
        if not args.demo:
            sys.exit("ERROR: no precursor data found in PRECURSOR_FOLDERS. "
                     "Add CSV files, or run with --demo for seeded fake data.")
        print(f"No precursor data loaded. --demo: writing seeded random data (seed={RANDOM_SEED}).")
        rng = np.random.default_rng(RANDOM_SEED)
        times = pd.date_range('2024-01-01', periods=10, freq='1D')
        lat_bin = 22.0  # example
        lon_bin = 120.0
        dummy = []
        for t in times:
            row = {'lat_bin': lat_bin, 'lon_bin': lon_bin, 'time_bin': t}
            for var in PRECURSOR_FOLDERS.keys():
                row[var] = rng.random()
                row[f'{var}_rolling_mean'] = rng.random()
                row[f'{var}_rolling_std'] = rng.random()
            dummy.append(row)
        df_features = pd.DataFrame(dummy)
        df_features['is_demo'] = True
        print("Applying weights to dummy data...")
        df_features = apply_weights(df_features)
        out_name = 'features_DEMO.parquet'
    else:
        print("Combining features...")
        df_features = combine_features([df for _, df in feature_list])
        print(f"Combined shape: {df_features.shape}")
        print("Applying weights...")
        df_features = apply_weights(df_features)
    # Ensure output directory
    out_dir = os.path.join(BASE_DIR, 'pipeline', 'data')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, out_name)
    print(f"Saving to {out_path}...")
    df_features.to_parquet(out_path, index=False)
    print("Done.")

if __name__ == '__main__':
    main()
