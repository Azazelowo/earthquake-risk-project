"""地震風險評估 APP：規模、時間、地點三項任選兩項固定，看第三項怎麼變（「3 中 2」）。

執行：streamlit run app/streamlit_app.py
資料：app/data/（由 scripts/export_app_snapshot.py 產生）；可用環境變數 HAZARD_APP_DATA 指定別的資料夾。
計算全部在 hazard_core.py，這個檔只負責畫面。
"""
import os
import sys
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import pydeck as pdk
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hazard_core as hc  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
BASE_DATA = Path(os.environ.get("HAZARD_APP_DATA", APP_DIR / "data"))
LIVE_DATA = APP_DIR / "live"

CITIES = {  # 市（縣）政府附近的概略座標，只用來快速填入點位
    "臺北市": (25.0375, 121.5637), "新北市": (25.0120, 121.4650), "桃園市": (24.9936, 121.3010),
    "新竹市": (24.8039, 120.9647), "臺中市": (24.1477, 120.6736), "南投縣": (23.9029, 120.6904),
    "嘉義市": (23.4801, 120.4491), "臺南市": (22.9999, 120.2270), "高雄市": (22.6273, 120.3014),
    "屏東縣": (22.6727, 120.4880), "宜蘭縣": (24.7570, 121.7533), "花蓮縣": (23.9871, 121.6015),
    "臺東縣": (22.7583, 121.1444),
}
DEFAULT_LINE = "23.99, 121.60\n23.33, 121.35\n22.76, 121.14"

st.set_page_config(page_title="地震風險評估", page_icon="🌏", layout="wide")


@st.cache_resource
def get_snapshot(path):
    return hc.load_snapshot(path)


def parse_line(text):
    pts = []
    for raw in text.strip().splitlines():
        raw = raw.replace("，", ",").strip()
        if not raw:
            continue
        lat, lon = (float(x) for x in raw.split(",")[:2])
        pts.append((lat, lon))
    return pts


DATA_DIR = hc.pick_data_dir(BASE_DATA, LIVE_DATA)
try:
    snap = get_snapshot(str(DATA_DIR))
except FileNotFoundError:
    st.error(f"找不到資料：{DATA_DIR}。請先執行 `python scripts/export_app_snapshot.py`。")
    st.stop()
status = snap.params.get("data_status") or {}
is_live = status.get("mode") == "live"

# ---------------- 側欄：三個條件 ----------------
st.sidebar.header("條件")
m = st.sidebar.slider("規模門檻（M 以上）", 1.0, 9.0, 5.0, 0.1,
                      help=f"模型以 M{snap.m_ref:g} 以上的地震擬合，其他規模用古騰堡–芮克特規律換算。")
labels = [lab for _, lab in hc.HORIZON_CHOICES]
T_label = st.sidebar.select_slider("時間長度（從發布時間起算）", options=labels, value="7 天")
T = dict((lab, d) for d, lab in hc.HORIZON_CHOICES)[T_label]

st.sidebar.subheader("地點")
mode = st.sidebar.radio("篩選方式", ["點震源（圓形範圍）", "線震源（帶狀範圍）", "全區"], label_visibility="collapsed")
region_desc, overlay = "全區（21–26°N、119–123°E）", None
if mode.startswith("點"):
    city = st.sidebar.selectbox("快速選擇", ["自訂"] + list(CITIES), index=1)
    lat0, lon0 = CITIES.get(city, (23.7, 121.0))
    c1, c2 = st.sidebar.columns(2)
    lat = c1.number_input("緯度", 21.0, 26.0, float(lat0), 0.01, format="%.3f", key=f"lat_{city}")
    lon = c2.number_input("經度", 119.0, 123.0, float(lon0), 0.01, format="%.3f", key=f"lon_{city}")
    radius = st.sidebar.slider("半徑（km）", 5, 200, 50, 5)
    weights = hc.point_weights(snap, lat, lon, radius)
    nominal_area = hc.circle_area_km2(radius)
    region_desc = f"{city if city != '自訂' else ''}（{lat:.2f}°N, {lon:.2f}°E）半徑 {radius} km"
    overlay = ("point", (lat, lon, radius))
elif mode.startswith("線"):
    text = st.sidebar.text_area("折線頂點（每行「緯度, 經度」）", DEFAULT_LINE, height=110,
                                help="預設是花蓮市→臺東市沿縱谷的示意線，不代表任何斷層的正式位置。")
    half = st.sidebar.slider("線兩側各延伸（km）", 2, 60, 15, 1)
    try:
        line = parse_line(text)
        weights = hc.line_weights(snap, line, half)
    except (ValueError, IndexError):
        st.sidebar.error("格式錯誤：每行要是「緯度, 經度」，至少兩行。")
        st.stop()
    nominal_area = hc.line_area_km2(line, half)
    region_desc = f"{len(line)} 個頂點的折線，兩側各 {half} km"
    overlay = ("line", (line, half))
else:
    weights = hc.all_weights(snap)
    nominal_area = None

st.sidebar.subheader("資料狀態")
cat_end = hc.to_taipei(snap.params.get("catalog_end", snap.issue_time))
st.sidebar.caption(f"發布時間：**{hc.to_taipei(snap.issue_time):%Y-%m-%d %H:%M}**（台灣時間）  \n"
                   f"正式目錄（GDMS／CWA）到：{cat_end:%Y-%m-%d %H:%M}")
if is_live:
    by = status.get("n_provisional_by_source", {})
    st.sidebar.caption(f"之後 {status.get('gap_days', 0):.0f} 天用暫時資料補：CWA 有感報告 {by.get('CWA', 0)} 筆、"
                       f"USGS {by.get('USGS', 0)} 筆（{hc.to_taipei(status['updated_at']):%m-%d %H:%M} 抓取）")
    for err in status.get("errors", []):
        st.sidebar.caption(f"⚠ {err}")
if st.sidebar.button("🔄 更新到現在", help="抓 USGS 與 CWA 有感地震報告，用同一組參數重算發布時間＝現在的機率（約 10 秒）"):
    import live_update  # noqa: E402  只有按下才載入（需要 scipy 與網路）
    with st.spinner("抓取最新地震資料並重算…"):
        try:
            res = live_update.update(base_dir=BASE_DATA, live_dir=LIVE_DATA, log=lambda *_: None)
        except Exception as e:  # noqa: BLE001  畫面上顯示原因，而不是整個 APP 當掉
            st.sidebar.error(f"更新失敗：{e}")
        else:
            get_snapshot.clear()
            st.rerun()

# ---------------- 標題與主要數字 ----------------
st.title("地震風險評估")
st.caption("規模、時間、地點三項任選兩項，看第三項。所有機率都是統計模型的估計，**不是地震預報**，"
           "也不能取代中央氣象署的官方資訊。")

if weights.sum() == 0:
    st.warning("選的範圍完全落在網格（21–26°N、119–123°E）外，沒有資料。")
    st.stop()

area_in = hc.region_area_km2(snap, weights)
if nominal_area and area_in < 0.95 * nominal_area:
    st.info(f"範圍有約 {(1 - area_in / nominal_area) * 100:.0f}% 落在網格外，那部分不計入。")

p_full = hc.region_probability(snap, weights, m, T, "full")
p_bg = hc.region_probability(snap, weights, m, T, "bg")
ratio = p_full / p_bg if p_bg > 0 else float("nan")

st.markdown(f"**{region_desc}**　·　**{T_label}內**　·　**M{m:.1f} 以上**")
c1, c2, c3 = st.columns(3)
c1.metric("現在的機率", hc.format_probability(p_full), help="長期背景＋近期活動加成，已依回測校準。")
c2.metric("平常時期的機率", hc.format_probability(p_bg),
          help="只用長期背景（這裡過去平均多常地震），不考慮最近的活動。這是判斷高低的參考點。")
c3.metric("現在 ÷ 平常", (f"{ratio:.2f} 倍" if ratio < 10 else f"{ratio:.0f} 倍") if np.isfinite(ratio) else "—",
          help="大於 1：最近這附近比較活躍（例如剛有較大地震）。小於 1：最近很安靜，模型把一部分"
               "機率分給了最近活躍的地方，所以這裡比長期平均低。")
st.caption(f"換個說法：{hc.format_odds(p_full)}。")

rel = hc.reliability(snap, m, T)
if is_live:
    g = hc.gap_ratio_at(snap.params, T)
    if g is not None and g < 0.97:
        st.info(f"正式目錄只到 {cat_end:%m/%d}，之後的 {status.get('gap_days', 0):.0f} 天用 USGS 與 CWA 有感報告補，"
                f"小地震不完整。在過去資料上模擬同樣的缺口，{T_label}的總數最多低估約 {(1 - g) * 100:.0f}%，"
                "實際機率可能比顯示的略高。")
else:
    st.info(f"目前顯示的是正式目錄最後一天（{hc.to_taipei(snap.issue_time):%Y-%m-%d}）的預報，"
            "按左側「更新到現在」可推進到現在。")
for note in rel["notes"]:
    st.warning(note)

tab_map, tab_m, tab_t, tab_rel = st.tabs(
    ["🗺️ 規模＋時間 → 看地點", "📈 地點＋時間 → 看規模", "⏱️ 地點＋規模 → 看時間", "🔍 可信度與限制"])

# ---------------- 地圖 ----------------
with tab_map:
    p_cell = hc.probability(hc.expected(snap, m, T, "full"), snap.kappa)
    p_cell_bg = hc.probability(hc.expected(snap, m, T, "bg"), snap.kappa)
    p_max = max(float(np.quantile(p_cell, 0.995)), 1e-4)
    p_min = p_max / 1000
    colors = hc.color_scale(p_cell, p_min, p_max)
    d = snap.deg
    cells = snap.cells.copy()
    cells["polygon"] = [[[lo, la], [lo + d, la], [lo + d, la + d], [lo, la + d]]
                        for la, lo in zip(cells["lat_bin"], cells["lon_bin"])]
    cells["color"] = colors.tolist()
    cells["tip"] = [f"{la:.1f}–{la + d:.1f}°N, {lo:.1f}–{lo + d:.1f}°E\n現在 {hc.format_probability(p)}"
                    f"｜平常 {hc.format_probability(q)}"
                    for la, lo, p, q in zip(cells["lat_bin"], cells["lon_bin"], p_cell, p_cell_bg)]
    layers = [pdk.Layer("PolygonLayer", cells, get_polygon="polygon", get_fill_color="color",
                        stroked=False, pickable=True)]

    show_ev = st.checkbox("顯示發布前 30 天的地震（M3 以上）", value=True)
    if show_ev and len(snap.events):
        ev = snap.events[snap.events["time"] >= snap.issue_time - pd.Timedelta(days=30)].copy()
        ev["radius"] = 1500 * 2.0 ** (ev["ml"] - 3)
        src = ev["source"].fillna("目錄") if "source" in ev else pd.Series("目錄", index=ev.index)
        ev["tip"] = [f"{hc.to_taipei(t):%Y-%m-%d %H:%M}（台灣）  M{ml:.1f}  深 {dp:.0f} km｜{s}"
                     for t, ml, dp, s in zip(ev["time"], ev["ml"], ev["depth"], src)]
        prov = src.str.contains("暫時")
        ev["fill"] = [[230, 120, 0, 170] if p else [30, 60, 160, 120] for p in prov]
        layers.append(pdk.Layer("ScatterplotLayer", ev, get_position=["lon", "lat"], get_radius="radius",
                                get_fill_color="fill", get_line_color=[20, 30, 90, 220],
                                stroked=True, line_width_min_pixels=1, pickable=True))
        if prov.any():
            st.caption("藍點：正式目錄；橘點：目錄結束後的暫時資料（CWA 有感報告、USGS）。")
    if overlay and overlay[0] == "point":
        la, lo, r = overlay[1]
        layers.append(pdk.Layer("ScatterplotLayer", pd.DataFrame({"lat": [la], "lon": [lo], "tip": ["選取範圍"]}),
                                get_position=["lon", "lat"], get_radius=r * 1000, filled=False, stroked=True,
                                get_line_color=[0, 120, 200, 255], line_width_min_pixels=3))
    elif overlay and overlay[0] == "line":
        pts, half = overlay[1]
        layers.append(pdk.Layer("PathLayer", pd.DataFrame({"path": [[[lo, la] for la, lo in pts]], "tip": ["選取範圍"]}),
                                get_path="path", get_width=half * 2000, get_color=[0, 120, 200, 90],
                                width_min_pixels=2, cap_rounded=True, joint_rounded=True))

    view = pdk.ViewState(latitude=23.6, longitude=121.0, zoom=6.3)
    st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=view, map_provider="carto", map_style="light",
                             tooltip={"text": "{tip}"}), use_container_width=True, height=620)
    st.caption(f"每格 {d:g}°×{d:g}°（約 22 km）。顏色是對數色階：最深紅 ≈ {hc.format_probability(p_max)}，"
               f"最淡 ≈ {hc.format_probability(p_min)}，更低的不上色。把滑鼠移到格子上可看數字。")

# ---------------- 機率 vs 規模 ----------------
with tab_m:
    mags = np.round(np.arange(1.0, 9.01, 0.1), 2)
    cm = hc.curve_vs_magnitude(snap, weights, T, mags)
    long = cm.melt("m", ["p_full", "p_bg"], var_name="series", value_name="p")
    long["series"] = long["series"].map({"p_full": "現在", "p_bg": "平常時期"})
    long = long[long["p"] > 1e-7]
    band = pd.DataFrame({"from": [1.0, 6.5], "to": [snap.m_ref, 9.0], "why": ["小規模外推", "樣本極少"]})
    chart = (alt.Chart(band).mark_rect(opacity=0.08, color="gray").encode(x="from:Q", x2="to:Q")
             + alt.Chart(long).mark_line().encode(
                 x=alt.X("m:Q", title="規模門檻 M"),
                 y=alt.Y("p:Q", title="至少發生一次的機率", scale=alt.Scale(type="log"), axis=alt.Axis(format="%")),
                 color=alt.Color("series:N", title=None, scale=alt.Scale(range=["#c0392b", "#7f8c8d"])),
                 tooltip=[alt.Tooltip("m:Q", title="M", format=".1f"),
                          alt.Tooltip("p:Q", title="機率", format=".3%"), "series:N"])
             + alt.Chart(pd.DataFrame({"m": [m]})).mark_rule(strokeDash=[4, 4]).encode(x="m:Q"))
    st.altair_chart(chart.properties(height=420), use_container_width=True)
    st.caption("灰底：左邊是目錄不完整、往下外推的範圍；右邊是 M6.5 以上、回測樣本極少的範圍。")
    rows = []
    for target in (0.5, 0.1, 0.01):
        mm = hc.magnitude_at_probability(snap, weights, T, target)
        rows.append({"機率達到": f"{target:.0%}", "規模門檻（現在）": f"M{mm:.1f}" if np.isfinite(mm) else "—",
                     "規模門檻（平常）": f"M{hc.magnitude_at_probability(snap, weights, T, target, 'bg'):.1f}"})
    st.markdown(f"**反查：{T_label}內、這個範圍，機率達到多少時對應的規模**")
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=False)

# ---------------- 機率 vs 時間 ----------------
with tab_t:
    Ts = np.exp(np.linspace(np.log(snap.horizons[0]), np.log(snap.horizons[-1]), 60))
    ct = hc.curve_vs_horizon(snap, weights, m, Ts)
    ct["hours"] = ct["T"] * 24
    long = ct.melt(["T", "hours"], ["p_full", "p_bg"], var_name="series", value_name="p")
    long["series"] = long["series"].map({"p_full": "現在", "p_bg": "平常時期"})
    long["label"] = [hc.horizon_label(t) if t >= 1 else f"{t * 24:.1f} 小時" for t in long["T"]]
    chart = (alt.Chart(long).mark_line().encode(
        x=alt.X("hours:Q", title="時間長度（小時，對數軸；24 = 1 天、168 = 7 天、720 = 30 天）",
                scale=alt.Scale(type="log")),
        y=alt.Y("p:Q", title="至少發生一次的機率", scale=alt.Scale(type="log"), axis=alt.Axis(format="%")),
        color=alt.Color("series:N", title=None, scale=alt.Scale(range=["#c0392b", "#7f8c8d"])),
        tooltip=[alt.Tooltip("label:N", title="時間"), alt.Tooltip("p:Q", title="機率", format=".3%"), "series:N"])
        + alt.Chart(pd.DataFrame({"hours": [T * 24]})).mark_rule(strokeDash=[4, 4]).encode(x="hours:Q"))
    st.altair_chart(chart.properties(height=420), use_container_width=True)
    st.caption("「現在」和「平常」的差距在短時間最大：剛發生的地震帶來的加成會隨時間衰減（大森定律）。")

# ---------------- 可信度 ----------------
with tab_rel:
    st.markdown("""
**這個模型在做什麼**：每一格先估「發生率」＝ 這裡平常多常地震（長期背景）＋ 最近剛震過的加成（大地震後常有餘震），
再用規模分布換算成任意規模、用卜瓦松分布換算成任意時間長度的機率。

**怎麼驗證的**：參數只用 1995–2014 的資料決定，再拿 2015–2026 年 9 月的實際地震來檢查（模型沒看過這段）。
每次預報都只用當時以前的資料。
""")
    bt = snap.backtest.dropna(subset=["n_ratio_full"]).copy()
    bt["時間"] = bt["horizon_days"].map(hc.horizon_label)
    show = pd.DataFrame({
        "規模": bt["m"].map(lambda v: f"M≥{v:g}"), "時間": bt["時間"], "地震數": bt["n_events"].astype(int),
        "隨機一格一窗有地震的比例": bt["base_rate"].map(lambda v: f"{v * 100:.3f}%"),
        "排序能力 AUC（0.5＝亂猜）": bt["auc_full"].round(3),
        "只用長期背景的 AUC": bt["auc_bg"].round(3),
        "預測總數 ÷ 實際（1＝剛好）": bt["n_ratio_full"].round(2),
    })
    st.dataframe(show, hide_index=True, use_container_width=True)
    st.markdown("""
**怎麼讀**：
- **AUC** 高代表「機率高的格子，真的比較常有地震」。完整模型的 AUC 都比「只用長期背景」高，差距就是近期活動加成的貢獻。
- **預測 ÷ 實際** 看的是機率的「絕對大小」準不準。1 天的窗、M6 以上的組合偏高（約 1.3–1.8），APP 會在這些組合跳出提醒。

**已知限制**（不隱藏）：
1. 預報的是**全部地震（含餘震）**。技巧主要來自「大地震後會有餘震」，這是已知規律，**不是預測主震**。
2. 我們也檢驗了地磁資料能不能讓預報更好：用循環時間平移的置換檢定，**沒有找到證據**（增益 −0.021 nat，p＝0.50），所以這個 APP 沒有使用地磁。
3. M3.5 以下是往下外推（目錄不完整）；M6.5 以上樣本極少。
4. 目錄以測站包覆角篩過，外海事件偏少，外海的機率偏低。
5. 正式目錄約落後一個半月。「更新到現在」用 CWA 有感地震報告與 USGS 補上之後的地震，但小地震不完整，
   近期加成會偏低（畫面上會顯示模擬出來的低估幅度）；校準倍率沿用正式目錄期間的值。
""")
    st.caption(f"參數：b＝{snap.b:.3f}、M_ref＝{snap.m_ref:g}；訓練期 {snap.params['train'][0]}～{snap.params['train'][1]}、"
               f"測試期 {snap.params['test'][0]}～{snap.params['test'][1]}。")
