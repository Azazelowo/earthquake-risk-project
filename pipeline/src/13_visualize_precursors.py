"""B2 視覺化（FR-B2-24）：測站異常指標時間序列、網格特徵地圖。供海報、簡報與 APP 使用。

輸入（02_clean_features.py 的輸出）：
    station_anomalies.parquet   測站層級的 z 值
    features_magnet.parquet     網格 × 日的 z 值
    主震清單 CSV（time、lat、lon、ml）

輸出（預設 docs/reports/b2_visuals/）：
    station_timeseries_<欄位>.png   每站一列：每日 z 值（淡點）＋ 15 日移動中位數（線），
                                    標出該站 150 km 內的 ML≥5.5 主震
    grid_map_<日期>_<欄位>.png      指定主震「之前 N 天」的網格平均 z 值地圖，標出震央與測站
    README.md                       圖的清單與讀圖須知

為什麼畫「主震前 N 天的平均」而不是單日：單日的 z 值雜訊大；而且圖只用主震發生前的資料，
和回測「只用過去資料」的原則一致。圖是用來看資料長什麼樣，不是證據——單一事件前出現高值，
不代表有前兆，必須看全部事件與隨機對照（見 B3 報告）。

執行：python pipeline/src/13_visualize_precursors.py --features-dir <含兩個 parquet 的資料夾>
"""
import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eqforecast.decluster import haversine_km  # noqa: E402

BASE_DIR = Path(__file__).resolve().parents[2]
DATA = BASE_DIR / "pipeline" / "data"
EVENTS = BASE_DIR / "docs" / "b2_download_plan" / "event_list_M55_mainshocks_2018_2026.csv"
OUT_DIR = BASE_DIR / "docs" / "reports" / "b2_visuals"

GRID_DEG = 0.2
EVENT_RADIUS_KM = 150.0       # 和 02_clean_features.py 的 MAG_RADIUS_KM 相同
ROLL_DAYS = 15
MIN_DAYS_FOR_PANEL = 200      # z 值少於這麼多天的測站不畫
LABELS = {"mag_sznet_z": "相對其他測站中位數", "mag_szres_z": "扣除 Kp、Dst 預期值",
          "mag_pol_z": "極化比 Sz/Sh", "mag_sz_z": "垂直功率 Sz（未扣共同項）"}
# 粗略的台灣本島輪廓（手工取點，只供辨識位置，不是測量成果）
APPROX_COAST = [(121.54, 25.30), (121.93, 25.02), (121.87, 24.60), (121.80, 24.30), (121.62, 23.98),
                (121.50, 23.50), (121.40, 23.10), (121.18, 22.75), (120.90, 22.30), (120.85, 21.90),
                (120.70, 22.05), (120.60, 22.35), (120.30, 22.58), (120.10, 23.05), (120.12, 23.55),
                (120.25, 23.85), (120.45, 24.20), (120.70, 24.60), (120.95, 24.90), (121.05, 25.05),
                (121.30, 25.15), (121.54, 25.30)]

plt.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "Noto Sans CJK TC", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def load_events(path):
    ev = pd.read_csv(path)
    ev["time"] = pd.to_datetime(ev["time"])
    return ev.sort_values("time").reset_index(drop=True)


def events_near(ev, lat, lon, radius_km=EVENT_RADIUS_KM):
    d = haversine_km(lat, lon, ev["lat"].to_numpy(), ev["lon"].to_numpy())
    return ev[d <= radius_km]


def rolling_median(series, days=ROLL_DAYS):
    """以日期為索引的移動中位數；視窗內有效天數不到一半時為缺值。"""
    s = series.asfreq("D")
    return s.rolling(days, min_periods=max(2, days // 2)).median()


def plot_station_timeseries(st, ev, col, out_path):
    st = st[st[col].notna()]
    counts = st.groupby("station")[col].count()
    stations = [s for s in counts.sort_values(ascending=False).index if counts[s] >= MIN_DAYS_FOR_PANEL]
    if not stations:
        return None
    fig, axes = plt.subplots(len(stations), 1, figsize=(11, 1.9 * len(stations) + 1), sharex=True, squeeze=False)
    for ax, sta in zip(axes[:, 0], stations):
        g = st[st["station"] == sta].drop_duplicates("date").set_index("date").sort_index()
        ax.plot(g.index, g[col].clip(-8, 8), ".", ms=2, color="#9aa7b5", alpha=0.6)
        ax.plot(rolling_median(g[col]), color="#1f5fa8", lw=1.2)
        ax.axhline(0, color="k", lw=0.5)
        for y in (-3, 3):
            ax.axhline(y, color="#c0392b", lw=0.6, ls="--")
        near = events_near(ev, g["lat"].iloc[0], g["lon"].iloc[0])
        near = near[(near["time"] >= g.index.min()) & (near["time"] <= g.index.max())]
        for _, e in near.iterrows():
            ax.axvline(e["time"], color="#e67e22", lw=0.4 + 0.9 * (e["ml"] - 5.5), alpha=0.85)
        ax.set_ylim(-8, 8)
        ax.set_ylabel(f"{sta}\nz 值", fontsize=9)
        ax.text(0.995, 0.93, f"{len(g)} 天｜150 km 內主震 {len(near)} 個", transform=ax.transAxes,
                ha="right", va="top", fontsize=8, color="#444")
    axes[0, 0].set_title(f"地磁測站異常指標：{LABELS.get(col, col)}（{col}）\n"
                         f"灰點＝每日值、藍線＝{ROLL_DAYS} 日移動中位數、紅虛線＝±3、橘線＝ML 5.5 以上主震（越粗規模越大）",
                         fontsize=10)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return stations


def window_mean_grid(grid, col, end, days):
    """[end − days, end) 的每格平均 z 值與有效天數。end 當天不含（只用事件之前的資料）。"""
    t = pd.to_datetime(grid["time_bin"])
    end = pd.Timestamp(end).normalize()
    w = grid[(t >= end - pd.Timedelta(days=days)) & (t < end) & grid[col].notna()]
    if w.empty:
        return pd.DataFrame(columns=["lat_bin", "lon_bin", "mean", "n"])
    return w.groupby(["lat_bin", "lon_bin"])[col].agg(mean="mean", n="count").reset_index()


def plot_grid_map(grid, st, ev_row, col, days, region, out_path, min_days=5):
    cells = window_mean_grid(grid, col, ev_row["time"], days)
    cells = cells[cells["n"] >= min_days]
    if cells.empty:
        return False
    fig, ax = plt.subplots(figsize=(6.2, 7.2))
    lat_b = np.arange(region[0], region[1] + 1e-9, GRID_DEG)
    lon_b = np.arange(region[2], region[3] + 1e-9, GRID_DEG)
    z = np.full((len(lat_b) - 1, len(lon_b) - 1), np.nan)
    i = np.rint((cells["lat_bin"].to_numpy() - region[0]) / GRID_DEG).astype(int)
    j = np.rint((cells["lon_bin"].to_numpy() - region[2]) / GRID_DEG).astype(int)
    ok = (i >= 0) & (i < z.shape[0]) & (j >= 0) & (j < z.shape[1])
    z[i[ok], j[ok]] = cells["mean"].to_numpy()[ok]
    pm = ax.pcolormesh(lon_b, lat_b, z, cmap="RdBu_r", vmin=-2, vmax=2, shading="flat")
    fig.colorbar(pm, ax=ax, shrink=0.75, label=f"前 {days} 天平均 z 值")
    ax.plot(*zip(*APPROX_COAST), color="k", lw=1.0)
    end = pd.Timestamp(ev_row["time"]).normalize()
    used = st[(st["date"] >= end - pd.Timedelta(days=days)) & (st["date"] < end) & st[col].notna()]
    pos = used.groupby("station")[["lat", "lon"]].first()
    ax.plot(pos["lon"], pos["lat"], "^", color="k", ms=7, mfc="w", label="地磁測站（當期有資料）")
    for sta, r in pos.iterrows():
        ax.annotate(sta, (r["lon"], r["lat"]), xytext=(4, 4), textcoords="offset points", fontsize=8)
    ax.plot(ev_row["lon"], ev_row["lat"], "*", color="#f1c40f", mec="k", ms=18, label=f"震央 ML {ev_row['ml']:.1f}")
    ax.set_xlim(region[2], region[3])
    ax.set_ylim(region[0], region[1])
    ax.set_aspect(1 / np.cos(np.deg2rad(23.5)))
    ax.set_xlabel("經度")
    ax.set_ylabel("緯度")
    ax.legend(loc="lower right", fontsize=8)
    ax.set_title(f"{pd.Timestamp(ev_row['time']):%Y-%m-%d} ML {ev_row['ml']:.1f} 主震前 {days} 天\n"
                 f"{LABELS.get(col, col)}（{col}）｜空白＝150 km 內沒有可用測站", fontsize=10)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--features-dir", default=str(DATA))
    ap.add_argument("--events", default=str(EVENTS))
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--cols", nargs="+", default=["mag_sznet_z", "mag_szres_z"])
    ap.add_argument("--map-days", type=int, default=30)
    ap.add_argument("--n-maps", type=int, default=3, help="畫規模最大的幾個主震（需有網格資料）")
    ap.add_argument("--region", nargs=4, type=float, default=[21.0, 26.0, 119.0, 123.0],
                    metavar=("LAT_MIN", "LAT_MAX", "LON_MIN", "LON_MAX"))
    args = ap.parse_args()

    fdir, out = Path(args.features_dir), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    st = pd.read_parquet(fdir / "station_anomalies.parquet")
    st = st[st.get("source", "magnet") == "magnet"].copy()
    st["date"] = pd.to_datetime(st["date"]).dt.normalize()
    grid = pd.read_parquet(fdir / "features_magnet.parquet")
    ev = load_events(args.events)

    lines = ["# B2 視覺化", "",
             f"> 由 `pipeline/src/13_visualize_precursors.py` 產生（{pd.Timestamp.now():%Y-%m-%d %H:%M}）。對應需求 FR-B2-24。", "",
             "**讀圖須知**：這些圖用來看資料長什麼樣，不是前兆存在的證據。單一地震前出現高值可能只是巧合；"
             "有沒有預報價值要看全部事件與隨機對照（B3 回測）。地圖只用主震發生前的資料。"
             "海岸線是手工取點的粗略輪廓，只供辨識位置。", ""]
    for col in args.cols:
        if col not in st.columns:
            continue
        name = f"station_timeseries_{col}.png"
        stations = plot_station_timeseries(st, ev, col, out / name)
        if stations:
            lines += [f"## 測站時間序列：{LABELS.get(col, col)}", "", f"![{col}]({name})", "",
                      f"測站（依有值天數排序）：{'、'.join(stations)}", ""]
            print(f"時間序列 {col}：{len(stations)} 站 → {name}")
    t_grid = pd.to_datetime(grid["time_bin"])
    cand = ev[(ev["time"] >= t_grid.min() + pd.Timedelta(days=args.map_days)) & (ev["time"] <= t_grid.max())]
    cand = cand.sort_values("ml", ascending=False)
    for col in args.cols:
        made = 0
        for _, e in cand.iterrows():
            if made >= args.n_maps:
                break
            name = f"grid_map_{pd.Timestamp(e['time']):%Y%m%d}_{col}.png"
            if plot_grid_map(grid, st, e, col, args.map_days, args.region, out / name):
                if made == 0:
                    lines += [f"## 網格地圖：{LABELS.get(col, col)}", ""]
                lines += [f"![{name}]({name})", ""]
                made += 1
                print(f"地圖 {col}：{pd.Timestamp(e['time']):%Y-%m-%d} ML {e['ml']:.1f} → {name}")
    (out / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"清單：{out / 'README.md'}")


if __name__ == "__main__":
    main()
