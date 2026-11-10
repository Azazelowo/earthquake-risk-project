"""產生 B3 回測報告（Markdown + 圖）。報告內容全部來自 metrics.json 的數值，不手寫結論數字。"""
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

from . import metrics as M

MODEL_LABEL = {
    "random": "隨機警報",
    "ri": "相對強度 RI",
    "logistic": "邏輯斯迴歸（目錄特徵）",
}


def _label(name):
    if name in MODEL_LABEL:
        return MODEL_LABEL[name]
    if name.startswith("logistic+sanity_noise"):
        return "邏輯斯 + 純雜訊（自我檢查）"
    if name.startswith("logistic+sanity_oracle"):
        return "邏輯斯 + 合成正對照（自我檢查）"
    if name.startswith("logistic+"):
        return f"邏輯斯 + {name.split('+', 1)[1]}（前兆）"
    return name


def _fmt(x, nd=3):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    if isinstance(x, (int, np.integer)):
        return str(int(x))
    return f"{x:.{nd}f}"


def _p(x):
    if x is None or np.isnan(x):
        return "—"
    return "<0.001" if x < 0.001 else f"{x:.3f}"


def setup_matplotlib_cjk():
    """中文字型＋後備字型（微軟正黑體缺 ≥、− 等符號，交給 DejaVu Sans 補上）。"""
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    available = {f.name for f in font_manager.fontManager.ttflist}
    cjk = [f for f in ("Microsoft JhengHei", "Noto Sans CJK TC", "Noto Sans TC", "PingFang TC") if f in available]
    plt.rcParams["font.family"] = cjk[:1] + ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def _figures(summary, cat, grid, ranks, n_cells, cfg, rep_dir, split_map):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from scipy import stats

    setup_matplotlib_cjk()
    figs = {}

    # Molchan 圖（測試集、去群聚目標）
    fig, ax = plt.subplots(figsize=(5.5, 5))
    n_pos = None
    for key in summary["results"]:
        name, target = key.split("|")
        if target != "declustered" or (("test" not in summary["results"][key])):
            continue
        r, y = ranks[(key, "test")]
        tau, pod = M.molchan(r, y, n_cells)
        n_pos = int(np.sum(y))
        style = "--" if "sanity" in name else "-"
        ax.plot(tau, 1 - pod, style, label=f"{_label(name)}（ASS {summary['results'][key]['test']['ASS']:.2f}）")
    ax.plot([0, 1], [1, 0], color="gray", lw=1, label="隨機期望值")
    if n_pos:
        taus = np.linspace(0.001, 1, 400)
        h_crit = stats.binom.isf(0.05, n_pos, taus) + 1
        ax.plot(taus, np.clip(1 - h_crit / n_pos, 0, 1), ":", color="gray", lw=1, label="隨機 95% 界線")
    ax.set_xlabel("警報比例 τ")
    ax.set_ylabel("漏報率 ν = 1 − POD")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_title(f"Molchan 圖：測試集（{n_pos} 個目標網格窗）")
    ax.legend(fontsize=7, loc="upper right")
    fig.tight_layout()
    figs["molchan"] = "molchan_test.png"
    fig.savefig(rep_dir / figs["molchan"], dpi=150)
    plt.close(fig)

    # 目標事件逐年數量：原始 vs 主震
    in_region = cat["cell_id"] >= 0
    big = in_region & (cat["ml"] >= cfg.target_ml)
    if cfg.target_max_depth_km is not None:
        big &= cat["depth"] <= cfg.target_max_depth_km
    yr = cat["time"].dt.year
    raw = big.groupby(yr).sum()
    main = (big & cat["is_mainshock"]).groupby(yr).sum()
    fig, ax = plt.subplots(figsize=(8, 3.2))
    ax.bar(raw.index, raw.values, color="#c9c9c9", label=f"ML≥{cfg.target_ml} 全部")
    ax.bar(main.index, main.values, color="#2b6cb0", label="去群聚後的主震")
    for name, (s, e) in cfg.splits.items():
        ax.axvline(pd.Timestamp(s).year - 0.5, color="k", lw=0.6, ls=":")
        ax.text(pd.Timestamp(s).year, ax.get_ylim()[1] * 0.92, name, fontsize=8)
    ax.set_ylabel("事件數")
    ax.legend(fontsize=8)
    ax.set_title("研究區內目標事件逐年數量")
    fig.tight_layout()
    figs["annual"] = "targets_by_year.png"
    fig.savefig(rep_dir / figs["annual"], dpi=150)
    plt.close(fig)

    # 地圖：去群聚目標的位置（依切分著色）
    t0 = pd.Timestamp(cfg.issue_start)
    h = pd.Timedelta(days=cfg.horizon_days)
    mains = cat[big & cat["is_mainshock"]].copy()
    w = ((mains["time"] - t0) // h).astype(int)
    t0_of = t0 + w * h
    mains["split"] = t0_of.map(split_map).fillna("未評估").replace({"purged": "未評估"})
    fig, ax = plt.subplots(figsize=(5, 5.6))
    r = cfg.region
    colors = {"train": "#a0aec0", "val": "#ed8936", "test": "#c53030", "未評估": "#000000"}
    for s, g in mains.groupby("split"):
        ax.scatter(g["lon"], g["lat"], s=(g["ml"] - 5) ** 2 * 25, c=colors.get(s, "k"),
                   alpha=0.75, edgecolors="none", label=f"{s}（{len(g)}）")
    ax.set_xlim(r.lon_min, r.lon_max)
    ax.set_ylim(r.lat_min, r.lat_max)
    ax.set_xticks(np.arange(r.lon_min, r.lon_max + 1e-9, 1))
    ax.set_yticks(np.arange(r.lat_min, r.lat_max + 1e-9, 1))
    ax.grid(True, lw=0.3)
    ax.set_aspect(1 / np.cos(np.radians((r.lat_min + r.lat_max) / 2)))
    ax.set_title(f"去群聚後的目標主震（ML≥{cfg.target_ml}）")
    ax.legend(fontsize=7, loc="lower right")
    fig.tight_layout()
    figs["map"] = "target_map.png"
    fig.savefig(rep_dir / figs["map"], dpi=150)
    plt.close(fig)
    return figs


def write_report(summary, cat, grid, feats, ranks, n_cells, cfg, rep_dir, split_map):
    rep_dir = Path(rep_dir)
    figs = _figures(summary, cat, grid, ranks, n_cells, cfg, rep_dir, split_map)
    d = summary["data"]
    dc = summary["decluster"]
    res = summary["results"]
    L = []
    L.append(f"# B3 基準模型回測報告（{cfg.tag}）")
    L.append("")
    L.append(f"> 由 `pipeline/src/03_train_backtest.py` 自動產生於 {dt.datetime.now():%Y-%m-%d %H:%M}，"
             "所有數字來自同目錄設定下的 `pipeline/data/b3_<tag>/metrics.json`。重新執行會覆寫本檔。")
    L.append("")
    L.append("## 這份報告回答什麼")
    L.append("")
    L.append("只用地震目錄，能把「下一個預報窗內，哪些網格會發生 ML≥{0} 主震」預報到什麼程度？"
             "這條基準線是之後檢驗前兆的標準：**加入前兆後，必須在相同警報比例下顯著贏過它**，"
             "才能說前兆有額外資訊。".format(cfg.target_ml))
    L.append("")
    L.append("## 1. 設定")
    L.append("")
    r = cfg.region
    L.append("| 項目 | 值 |")
    L.append("|---|---|")
    L.append(f"| 研究區 | {r.lat_min}–{r.lat_max}°N、{r.lon_min}–{r.lon_max}°E |")
    L.append(f"| 網格 | {cfg.cell_deg}°（{grid.nlat}×{grid.nlon} = {n_cells} 格） |")
    L.append(f"| 預報窗 | {cfg.horizon_days} 天，不重疊；共 {d['n_issue_times']} 個發布時間點（{d['first_t0'][:10]} 起） |")
    depth = "不限" if cfg.target_max_depth_km is None else f"≤ {cfg.target_max_depth_km} km"
    L.append(f"| 目標事件 | ML ≥ {cfg.target_ml}、深度 {depth}、去群聚後的主震 |")
    L.append(f"| 去群聚 | {cfg.decluster_method}，前震視窗比例 {cfg.decluster_fs_time_prop} |")
    thr = (f"ML ≥ 各年門檻（`{cfg.feature_min_ml_by_year}`，不低於 {cfg.feature_min_ml}）"
           if getattr(cfg, "feature_min_ml_by_year", None) else f"ML ≥ {cfg.feature_min_ml}")
    L.append(f"| 特徵事件 | {thr}（`events.parquet` 全部列：Gap≤110 或未知，加上 ML≥5.5 目標） |")
    sp = cfg.splits
    L.append(f"| 時間切分 | 訓練 {sp['train'][0]}～{sp['train'][1]}、驗證 {sp['val'][0]}～{sp['val'][1]}、"
             f"測試 {sp['test'][0]}～{sp['test'][1] or d['catalog_end'][:10]}（右端不含） |")
    L.append(f"| 目錄期間 | {d['catalog_start'][:10]} ～ {d['catalog_end'][:10]} |")
    L.append("")
    L.append("**洩漏檢查**（每個切分最後一個標籤的結束時間，都早於下一個切分的第一個發布時間）：")
    L.append("")
    for k, v in summary["leakage_check"].items():
        L.append(f"- {k}：標籤結束 {v['last_label_end'][:10]} ≤ 下一段起點 {v['next_first_t0'][:10]} ✅")
    L.append("")

    L.append("## 2. 去群聚")
    L.append("")
    L.append(f"研究區內 ML≥{cfg.target_ml}（深度{depth}）：**{dc['n_target_raw']} 筆 → 主震 {dc['n_target_mainshock']} 筆**"
             f"（{1 - dc['n_target_mainshock'] / max(dc['n_target_raw'], 1):.0%} 屬於前震或餘震）。")
    L.append("")
    L.append(f"![逐年目標數]({figs['annual']})")
    L.append("")
    L.append("**事件間隔的群聚程度**（Weibull 形狀參數 k：1 = 隨機，< 1 = 群聚；CV：1 = 隨機，> 1 = 群聚）")
    L.append("")
    L.append("| 子集 | 間隔數 | Weibull k | CV |")
    L.append("|---|---|---|---|")
    for name, s in dc["k_reverification"].items():
        L.append(f"| {name} | {s['n_intervals']} | {_fmt(s['weibull_k'])} | {_fmt(s['cv'], 2)} |")
    L.append("")
    L.append("去群聚後 k 與 CV 越接近 1，代表餘震造成的群聚被移除得越完整。")
    L.append("")
    close = [n for n, s in dc["k_reverification"].items()
             if not np.isnan(s["weibull_k"]) and abs(s["weibull_k"] - 0.658) < 0.02]
    if close:
        L.append(f"**k = 0.658 的重新驗證**：子集 {', '.join(close)} 的 k 與 0.658 相差不到 0.02，"
                 "可能就是提案使用的子集，但仍需確認原始計算方式。")
    else:
        L.append("**k = 0.658 的重新驗證**：上表沒有任何子集的 k 落在 0.658 ± 0.02 內，"
                 "提案也沒有記錄計算方式，所以目前**無法重現**。建議報告不要再引用 0.658，"
                 "改用上表可重現的值，並註明子集與方法。")
    L.append("")
    L.append("**參數敏感度**（研究區內 ML≥{0} 主震數）".format(cfg.target_ml))
    L.append("")
    L.append("| 方法 | 前震視窗比例 | 主震數 |")
    L.append("|---|---|---|")
    for s in dc["sensitivity"]:
        L.append(f"| {s['method']} | {s['fs_time_prop']} | {s['n_mainshock_targets']} |")
    L.append("")
    L.append(f"![目標分布]({figs['map']})")
    L.append("")

    L.append("## 3. 樣本與目標")
    L.append("")
    L.append("| 切分 | 發布次數 | 目標網格窗（去群聚） | 目標網格窗（不去群聚） |")
    L.append("|---|---|---|---|")
    for s in ("train", "val", "test"):
        L.append(f"| {s} | {d['split_windows'].get(s, 0)} | {d['positives']['declustered'][s]} | "
                 f"{d['positives']['all'][s]} |")
    L.append(f"| purged | {d['split_windows'].get('purged', 0)} | — | — |")
    L.append("")
    L.append("一個「網格窗」= 一個網格 × 一個預報窗。同一格同一窗有兩個主震時只算一次。")
    L.append("")

    L.append("## 4. 結果（測試集、去群聚目標）")
    L.append("")
    L.append(f"![Molchan]({figs['molchan']})")
    L.append("")
    L.append("曲線越靠左下越好；落在灰色點線（隨機 95% 界線）左下方，才代表顯著優於隨機警報。")
    L.append("")
    keys = [k for k in res if k.endswith("|declustered")]
    L.append("**不依賴門檻的總結**")
    L.append("")
    L.append("| 模型 | 正則化 C | 驗證 ASS | 測試 ASS |")
    L.append("|---|---|---|---|")
    for k in keys:
        name = k.split("|")[0]
        L.append(f"| {_label(name)} | {_fmt(summary['chosen_C'].get(k))} | {_fmt(res[k]['val']['ASS'])} | "
                 f"**{_fmt(res[k]['test']['ASS'])}** |")
    L.append("")
    L.append("**固定警報比例 τ 下的 POD／FAR／CSI**（每個發布時間點，對分數最高的 τ 比例網格發警報）")
    L.append("")
    L.append("| 模型 | τ | 命中 | 誤報 | 漏報 | POD | FAR | CSI | 機率增益 | p（vs 隨機） |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for k in keys:
        name = k.split("|")[0]
        for tau, s in res[k]["test"]["at_tau"].items():
            L.append(f"| {_label(name)} | {float(tau):.0%} | {s['hits']} | {s['false_alarms']} | {s['misses']} | "
                     f"{_fmt(s['POD'], 2)} | {_fmt(s['FAR'], 3)} | {_fmt(s['CSI'], 3)} | {_fmt(s['gain'], 1)} | "
                     f"{_p(s['p_vs_random'])} |")
    L.append("")
    L.append("**在驗證集選出的最佳 τ\\***（驗證集 CSI 最大的 τ，原封不動套到測試集）")
    L.append("")
    L.append("| 模型 | τ* | POD | FAR | CSI | p（vs 隨機） |")
    L.append("|---|---|---|---|---|---|")
    for k in keys:
        name = k.split("|")[0]
        s = res[k]["test"]["at_tau_star"]
        L.append(f"| {_label(name)} | {res[k]['tau_star']:.1%} | {_fmt(s['POD'], 2)} | {_fmt(s['FAR'], 3)} | "
                 f"{_fmt(s['CSI'], 3)} | {_p(s['p_vs_random'])} |")
    L.append("")
    L.append("FAR 接近 1 是預期中的：ML≥5.5 主震在單一網格窗發生的機率很低，任何警報系統的大多數警報都會落空。"
             "所以比較模型時看「相同 τ 下的 POD」與 ASS，FAR 只用來提醒使用者警報的實際可信度。")
    L.append("")

    L.append("## 5. 配對比較")
    L.append("")
    L.append("同一批測試目標上，挑戰者（challenger）相對於基準（baseline）：")
    L.append("ΔASS 以目標事件重抽樣（bootstrap）估 95% 信賴區間；固定 τ 時兩個模型的警報總數相同，多一個命中就少一個誤報，"
             "所以只比較命中，用 McNemar 精確檢定（雙尾）。")
    L.append("")
    L.append("| 基準 → 挑戰者 | ΔASS [95% CI] | τ | 只有基準命中 | 只有挑戰者命中 | p |")
    L.append("|---|---|---|---|---|---|")
    for c in summary["comparisons"]:
        da = c["delta_ASS"]
        first = True
        for tau, m in c["mcnemar"].items():
            head = (f"{_label(c['baseline'])} → {_label(c['challenger'])} | "
                    f"{da['delta']:+.3f} [{da['ci_low']:+.3f}, {da['ci_high']:+.3f}]") if first else " | "
            L.append(f"| {head} | {float(tau):.0%} | {m['only_a']} | {m['only_b']} | {_p(m['p_two_sided'])} |")
            first = False
    L.append("")
    if summary["sanity_check"]:
        L.append("**框架自我檢查**：")
        L.append("")
        L.append("- 「純雜訊」特徵與目標無關。比較結果應該**不顯著**，否則代表比較方法本身會產生假發現。")
        L.append("- 「合成正對照」特徵是用測試答案加上雜訊合成的，**刻意作弊**。比較結果應該**顯著**，"
                 "否則代表樣本太少，檢定沒有能力偵測真正的改善。這一列只用來檢查方法，不是研究結果。")
        L.append("")

    L.append("## 6. 對照：不去群聚會怎樣")
    L.append("")
    L.append("模型改用「全部 ML≥{0}（含餘震）」重新訓練與評估：".format(cfg.target_ml))
    L.append("")
    L.append("| 模型 | 測試 ASS（去群聚） | 測試 ASS（不去群聚） | POD@5%（去群聚） | POD@5%（不去群聚） |")
    L.append("|---|---|---|---|---|")
    for name in ("random", "ri", "logistic"):
        a, b = res.get(f"{name}|declustered"), res.get(f"{name}|all")
        if a and b:
            L.append(f"| {_label(name)} | {_fmt(a['test']['ASS'])} | {_fmt(b['test']['ASS'])} | "
                     f"{_fmt(a['test']['at_tau']['0.05']['POD'], 2)} | {_fmt(b['test']['at_tau']['0.05']['POD'], 2)} |")
    L.append("")
    a, b = res.get("logistic|declustered"), res.get("logistic|all")
    if a and b:
        diff = b["test"]["ASS"] - a["test"]["ASS"]
        if diff > 0:
            L.append(f"不去群聚時，邏輯斯迴歸的測試 ASS 高出 {diff:.3f}。這段差距是「預報餘震」帶來的虛胖："
                     "前兆研究若沒有去群聚，就會把這部分誤算成前兆的功勞。")
        else:
            L.append(f"這次不去群聚並沒有讓邏輯斯迴歸的測試 ASS 變高（差 {diff:+.3f}）。"
                     "可能是因為兩種目標的正樣本組成不同，不能直接解讀成「餘震不會灌水」。")
    L.append("")

    lc = summary["coefficients"].get("logistic|declustered")
    if lc:
        L.append("## 7. 邏輯斯迴歸係數（標準化後，訓練＋驗證期）")
        L.append("")
        L.append("| 特徵 | 係數 |")
        L.append("|---|---|")
        for k, v in sorted(lc.items(), key=lambda kv: -abs(kv[1])):
            L.append(f"| `{k}` | {v:+.3f} |")
        L.append("")

    L.append("## 8. 限制")
    L.append("")
    L.append("- **樣本少**：測試期的目標網格窗只有數十個，POD 的不確定性很大（看 p 值與信賴區間，不要只看點估計）。")
    L.append("- **去群聚的人為效應**：視窗法會把大地震後 L 公里、T 天內的 ML≥5.5 全部移除（M7.2 的 T 約 2.5 年），"
             "所以「近期有大地震」的網格在去群聚目標下反而較少有正樣本。模型可能學到這個人為規律。")
    L.append("- **規模**：Gardner–Knopoff 視窗是用矩規模推導的，這裡直接套 ML。")
    L.append("- **外海偵測**：特徵事件只用 Gap≤110（或未知）的事件，外海的小地震被系統性低估；"
             "目標事件則不論 Gap 都保留（B1 的決定）。")
    L.append("- **非平穩**：偵測能力逐年提升，特徵已改用「占全區比例」緩解，但無法完全消除。")
    L.append("")
    L.append("## 9. 如何接上前兆特徵（B2 完成後）")
    L.append("")
    L.append("在設定檔加入 `external_features`，重新執行即可，報告會自動多出「邏輯斯 + 前兆」與配對比較：")
    L.append("")
    L.append("```json")
    L.append('"external_features": [')
    L.append('  {"name": "tec", "path": "pipeline/data/features.parquet",')
    L.append('   "lat_col": "lat_bin", "lon_col": "lon_bin", "time_col": "time_bin",')
    L.append('   "native_cell_deg": 0.2, "value_cols": ["tec"], "lookback_days": 30, "aggs": ["mean", "max"]}')
    L.append("]")
    L.append("```")
    L.append("")
    L.append("前兆資料通常只涵蓋部分年份與部分網格。比較時兩個模型用的是同一批樣本，"
             "沒有前兆資料的樣本以「缺值 + 缺值指示欄」處理，不補 0。")
    L.append("")
    (rep_dir / "README.md").write_text("\n".join(L), encoding="utf-8")
