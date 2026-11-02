"""03_train_backtest.py：B3 基準模型（只用地震目錄）與回測。

核心問題：前兆特徵有沒有比「只用地震目錄」的基準模型更好？
這支程式先把基準線建好；之後 B2 的前兆特徵只要寫進設定檔的 external_features，
就會自動多出「基準 + 前兆」模型，並在相同目標、相同切分、相同警報比例下與基準比較。

流程：
    events.parquet
      → 去群聚（Gardner–Knopoff）           decluster.py
      → 發布時間點與目標標籤                targets.py
      → 時間切分（purge）與洩漏檢查          splits.py
      → 目錄特徵（＋外部前兆特徵）            features.py
      → 模型：random／ri／logistic（＋前兆）  models.py
      → POD／FAR／CSI、Molchan、顯著性        metrics.py
      → pipeline/data/b3_<tag>/ 與 docs/reports/b3_<tag>/

用法：
    python pipeline/src/03_train_backtest.py                                  # 預設設定
    python pipeline/src/03_train_backtest.py --config pipeline/configs/b3_baseline.json
    python pipeline/src/03_train_backtest.py --sanity-check                   # 加跑框架自我檢查
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eqforecast import metrics as M  # noqa: E402
from eqforecast.config import Config  # noqa: E402
from eqforecast.decluster import decluster, interevent_stats  # noqa: E402
from eqforecast.features import catalog_features, external_features  # noqa: E402
from eqforecast.grid import Grid  # noqa: E402
from eqforecast.models import ColumnScore, LogisticModel, RandomModel  # noqa: E402
from eqforecast.report import write_report  # noqa: E402
from eqforecast.splits import assign_splits, check_no_leakage  # noqa: E402
from eqforecast.targets import build_targets, issue_times  # noqa: E402

BASE_DIR = Path(__file__).resolve().parents[2]
RI_COLUMN = "f_n4_all_nbr"


def load_events():
    path = BASE_DIR / "pipeline" / "data" / "events.parquet"
    if not path.exists():
        sys.exit(f"找不到 {path}，請先執行 01_build_catalog.py")
    return pd.read_parquet(path)


def decluster_diagnostics(cat, grid, cfg):
    """去群聚的檢查：數量、事件間隔的群聚程度、參數敏感度。"""
    in_region = grid.cell_of(cat["lat"], cat["lon"]) >= 0
    if cfg.target_max_depth_km is not None:
        in_region &= (cat["depth"] <= cfg.target_max_depth_km).to_numpy()
    big = in_region & (cat["ml"] >= cfg.target_ml)
    diag = {
        "n_target_raw": int(big.sum()),
        "n_target_mainshock": int((big & cat["is_mainshock"]).sum()),
        "interevent": {
            "raw": interevent_stats(cat.loc[big, "time"]),
            "declustered": interevent_stats(cat.loc[big & cat["is_mainshock"], "time"]),
        },
        "sensitivity": [],
    }
    # 嘗試重現提案的 k = 0.658（只是重新驗證，不保證能找到來源）
    k_check = {}
    for thr in (5.5, 6.0):
        sub = in_region & (cat["ml"] >= thr)
        k_check[f"ML>={thr} raw"] = interevent_stats(cat.loc[sub, "time"])
        k_check[f"ML>={thr} declustered"] = interevent_stats(cat.loc[sub & cat["is_mainshock"], "time"])
    diag["k_reverification"] = k_check

    sub = cat[cat["ml"] >= cfg.target_ml - 1.0]   # 只需比目標小 1 級以內的事件就足以判定目標是否為主震
    for method in ("gardner_knopoff", "uhrhammer"):
        for fs in (0.0, 0.1, 1.0):
            dc = decluster(sub, method, fs)
            ok = (grid.cell_of(sub["lat"], sub["lon"]) >= 0) & (sub["ml"] >= cfg.target_ml)
            if cfg.target_max_depth_km is not None:
                ok &= (sub["depth"] <= cfg.target_max_depth_km).to_numpy()
            diag["sensitivity"].append({
                "method": method, "fs_time_prop": fs,
                "n_mainshock_targets": int((ok & dc["is_mainshock"]).sum()),
            })
    return diag


def fit_and_score(make_model, feats, y, split, cfg, tune_C=False):
    """在 train 上訓練→驗證集評分；在 train+val 重新訓練→測試集評分。

    tune_C=True 時，依驗證集 ASS 選擇邏輯斯迴歸的正則化強度 C。
    """
    tr, va, te = (split == "train"), (split == "val"), (split == "test")
    n_cells = feats["cell_id"].nunique()
    chosen = None
    if tune_C:
        best = (-1.0, None)
        for C in cfg.logistic_C_grid:
            m = make_model(C).fit(feats[tr], y[tr])
            r = M.window_ranks(feats.loc[va, "t0"], m.score(feats[va]), cfg.seed)
            ass = M.area_skill_score(r, y[va], n_cells)
            if ass > best[0]:
                best = (ass, C)
        chosen = best[1]
    model_val = make_model(chosen).fit(feats[tr], y[tr])
    score = np.full(len(feats), np.nan)
    score[va] = model_val.score(feats[va])
    trva = tr | va
    model_test = make_model(chosen).fit(feats[trva], y[trva])
    score[te] = model_test.score(feats[te])
    return score, model_test, chosen


def evaluate(pred, y, split, n_cells, cfg, tau_star_k=None):
    """回傳某模型在某切分的指標。"""
    sel = split
    r = M.window_ranks(pred.loc[sel, "t0"], pred.loc[sel, "score"], cfg.seed)
    yy = y[sel]
    out = {
        "n_windows": int(pred.loc[sel, "t0"].nunique()),
        "n_samples": int(sel.sum()),
        "n_positive": int(yy.sum()),
        "ASS": M.area_skill_score(r, yy, n_cells),
        "at_tau": {},
    }
    for tau in cfg.taus:
        k = max(1, int(round(tau * n_cells)))
        out["at_tau"][f"{tau:g}"] = M.at_k(r, yy, k, n_cells)
    if tau_star_k is not None:
        out["at_tau_star"] = M.at_k(r, yy, tau_star_k, n_cells)
    return out, r


def main():
    ap = argparse.ArgumentParser(description="B3 基準模型與回測")
    ap.add_argument("--config", default=str(BASE_DIR / "pipeline" / "configs" / "b3_baseline.json"))
    ap.add_argument("--sanity-check", action="store_true",
                    help="加入純雜訊與合成正對照特徵，確認比較方法不會誤報、也有能力偵測改善")
    args = ap.parse_args()

    cfg = Config.load(args.config)
    grid = Grid(cfg.region, cfg.cell_deg)
    out_dir = BASE_DIR / "pipeline" / "data" / f"b3_{cfg.tag}"
    rep_dir = BASE_DIR / "docs" / "reports" / f"b3_{cfg.tag}"
    out_dir.mkdir(parents=True, exist_ok=True)
    rep_dir.mkdir(parents=True, exist_ok=True)
    print(f"設定：{args.config}（tag = {cfg.tag}）；網格 {grid.nlat}×{grid.nlon} = {grid.n_cells}")

    # ---------- 1. 去群聚 ----------
    events = load_events()
    cat = events[events["ml"] >= cfg.decluster_min_ml].sort_values("time").reset_index(drop=True)
    cat = cat.join(decluster(cat, cfg.decluster_method, cfg.decluster_fs_time_prop))
    cat["cell_id"] = grid.cell_of(cat["lat"], cat["lon"])
    depth_ok = (np.ones(len(cat), bool) if cfg.target_max_depth_km is None
                else (cat["depth"] <= cfg.target_max_depth_km).to_numpy())
    is_tgt = (cat["ml"] >= cfg.target_ml).to_numpy() & depth_ok
    print(f"去群聚（{cfg.decluster_method}, fs={cfg.decluster_fs_time_prop}）："
          f"ML>={cfg.decluster_min_ml} 共 {len(cat)} 筆，主震 {int(cat['is_mainshock'].sum())} 筆")
    diag = decluster_diagnostics(cat, grid, cfg)
    print(f"研究區內 ML>={cfg.target_ml}：{diag['n_target_raw']} 筆 → 主震 {diag['n_target_mainshock']} 筆")

    # ---------- 2. 發布時間點與目標 ----------
    data_end = pd.Timestamp(events["time"].max()).floor("D")
    t0s = issue_times(cfg.issue_start, data_end, cfg.horizon_days)
    tgt_decl = build_targets(cat, cat["cell_id"].to_numpy(), t0s, grid, cfg.horizon_days,
                             is_tgt & cat["is_mainshock"].to_numpy())
    tgt_all = build_targets(cat, cat["cell_id"].to_numpy(), t0s, grid, cfg.horizon_days, is_tgt)

    # ---------- 3. 時間切分 ----------
    split_per_t0 = assign_splits(t0s, cfg.horizon_days, cfg.splits, cfg.embargo_days, data_end)
    leakage = check_no_leakage(t0s, split_per_t0, cfg.horizon_days)
    split_map = dict(zip(t0s, split_per_t0))
    print("切分（發布次數）：", pd.Series(split_per_t0).value_counts().to_dict())

    # ---------- 4. 特徵 ----------
    feat_events = events[events["ml"] >= cfg.feature_min_ml]
    feats = catalog_features(feat_events, grid.cell_of(feat_events["lat"], feat_events["lon"]),
                             t0s, grid, cfg.feature_min_ml)
    catalog_cols = [c for c in feats.columns if c.startswith("f_")]
    ext_cols = {}
    for spec in cfg.external_features:
        ext = external_features(spec, t0s, grid, BASE_DIR)
        cols = [c for c in ext.columns if c.startswith("f_")]
        feats = feats.merge(ext, on=["t0", "cell_id"], how="left")
        ext_cols[spec.name] = cols
        print(f"外部特徵 {spec.name}：{len(cols)} 欄，覆蓋率 > 0 的樣本 "
              f"{(feats[f'f_{spec.name}_coverage'] > 0).mean():.1%}")

    if args.sanity_check:
        rng = np.random.default_rng(cfg.seed + 1)
        feats["f_sanity_noise"] = rng.normal(size=len(feats))
        # 合成正對照：直接使用答案（加上雜訊）。只用來確認檢定有偵測能力，絕不能當成結果。
        feats["f_sanity_oracle"] = tgt_decl["y"].to_numpy(float) + rng.normal(size=len(feats))

    split = feats["t0"].map(split_map).to_numpy()
    feats["split"] = split
    y_decl = tgt_decl["y"].to_numpy()
    y_all = tgt_all["y"].to_numpy()
    in_eval = np.isin(split, ["train", "val", "test"])
    for name, y in (("declustered", y_decl), ("all", y_all)):
        print(f"目標（{name}）正樣本數：",
              {s: int(y[split == s].sum()) for s in ("train", "val", "test")})

    # ---------- 5. 模型 ----------
    n_cells = grid.n_cells
    model_specs = []   # (name, factory, tune_C, target_name)
    for name in cfg.models:
        if name == "random":
            model_specs.append(("random", lambda C: RandomModel(cfg.seed), False))
        elif name == "ri":
            model_specs.append(("ri", lambda C: ColumnScore(RI_COLUMN, "ri"), False))
        elif name == "logistic":
            model_specs.append(("logistic", lambda C, cols=catalog_cols: LogisticModel(cols, C, seed=cfg.seed), True))
    for ext_name, cols in ext_cols.items():
        model_specs.append((f"logistic+{ext_name}",
                            lambda C, cols=catalog_cols + cols: LogisticModel(cols, C, seed=cfg.seed), True))
    if args.sanity_check:
        for s in ("noise", "oracle"):
            model_specs.append((f"logistic+sanity_{s}",
                                lambda C, s=s: LogisticModel(catalog_cols + [f"f_sanity_{s}"], C, seed=cfg.seed),
                                True))

    preds, results, coefs, chosen_C, ranks = [], {}, {}, {}, {}
    for target_name, y in (("declustered", y_decl), ("all", y_all)):
        for name, factory, tune in model_specs:
            if target_name == "all" and "+" in name:
                continue   # 「不去群聚」只作對照，不必跑前兆與自我檢查模型
            f = feats[in_eval].reset_index(drop=True)
            yy = y[in_eval]
            sp = split[in_eval]
            score, model, C = fit_and_score(factory, f, yy, sp, cfg, tune_C=tune)
            key = f"{name}|{target_name}"
            chosen_C[key] = C
            if isinstance(model, LogisticModel):
                coefs[key] = model.coefficients()
            pred = pd.DataFrame({"t0": f["t0"], "cell_id": f["cell_id"], "split": sp,
                                 "model": name, "target": target_name, "score": score})
            preds.append(pred)
            # 驗證集選 τ*（CSI 最大），再套到測試集
            val = sp == "val"
            r_val = M.window_ranks(pred.loc[val, "t0"], pred.loc[val, "score"], cfg.seed)
            k_star, _ = M.best_k_by_csi(r_val, yy[val], n_cells)
            res = {}
            for s in ("val", "test"):
                res[s], r = evaluate(pred, yy, sp == s, n_cells, cfg, k_star)
                ranks[(key, s)] = (r, yy[sp == s])
            res["tau_star"] = k_star / n_cells
            results[key] = res
            t = res["test"]
            print(f"[{target_name:11s}] {name:24s} C={C}  val ASS={res['val']['ASS']:.3f}  "
                  f"test ASS={t['ASS']:.3f}  test POD@5%={t['at_tau']['0.05']['POD']:.2f}  "
                  f"(正樣本 {t['n_positive']})")

    # ---------- 6. 配對比較 ----------
    comparisons = []
    def compare(a, b, target="declustered", s="test"):
        ra, ya = ranks[(f"{a}|{target}", s)]
        rb, _ = ranks[(f"{b}|{target}", s)]
        row = {"baseline": a, "challenger": b, "target": target, "split": s,
               "delta_ASS": M.bootstrap_delta_ass(ra, rb, ya, n_cells, cfg.n_bootstrap, cfg.seed),
               "mcnemar": {}}
        for tau in cfg.taus:
            k = max(1, int(round(tau * n_cells)))
            row["mcnemar"][f"{tau:g}"] = M.mcnemar_exact(ra[ya] <= k, rb[ya] <= k)
        comparisons.append(row)
    names = [m[0] for m in model_specs]
    if "ri" in names and "random" in names:
        compare("random", "ri")
    if "ri" in names and "logistic" in names:
        compare("ri", "logistic")
    for n in names:
        if n.startswith("logistic+"):
            compare("logistic", n)

    # ---------- 7. 輸出 ----------
    pred_all = pd.concat(preds, ignore_index=True)
    pred_all.to_parquet(out_dir / "predictions.parquet", index=False)
    samples = feats[["t0", "cell_id", "split"]].copy()
    samples["y_declustered"] = y_decl
    samples["y_all"] = y_all
    samples.to_parquet(out_dir / "targets.parquet", index=False)
    feats.drop(columns=["split"]).to_parquet(out_dir / "features.parquet", index=False)
    cat_out = cat[["time", "lat", "lon", "depth", "ml", "gap", "source", "cluster_id", "is_mainshock", "cell_id"]]
    cat_out.to_parquet(out_dir / "declustered_catalog.parquet", index=False)

    summary = {
        "config": cfg.to_dict(),
        "data": {
            "events_parquet_rows": int(len(events)),
            "catalog_start": str(events["time"].min()),
            "catalog_end": str(events["time"].max()),
            "n_issue_times": int(len(t0s)),
            "first_t0": str(t0s[0]), "last_t0": str(t0s[-1]),
            "n_cells": n_cells,
            "split_windows": pd.Series(split_per_t0).value_counts().to_dict(),
            "positives": {name: {s: int(y[split == s].sum()) for s in ("train", "val", "test")}
                          for name, y in (("declustered", y_decl), ("all", y_all))},
        },
        "leakage_check": leakage,
        "decluster": diag,
        "chosen_C": chosen_C,
        "results": results,
        "comparisons": comparisons,
        "coefficients": coefs,
        "sanity_check": bool(args.sanity_check),
    }
    (out_dir / "metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=float),
                                          encoding="utf-8")
    write_report(summary, cat, grid, feats, ranks, n_cells, cfg, rep_dir, split_map)
    print(f"輸出：{out_dir}")
    print(f"報告：{rep_dir / 'README.md'}")


if __name__ == "__main__":
    main()
