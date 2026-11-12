"""比較兩次以上的 B3 回測（不同設定檔），例如固定 ML≥3 與分段 Mc 門檻（FR-B2-20）。

同一個模型、同一批目標、同一個切分做配對比較：
    ASS 與 ΔASS（以目標事件重抽樣的 95% 信賴區間）、各 τ 的 POD 與 McNemar 精確檢定。
前提：各次回測的目標與切分必須相同（只改特徵），程式會檢查。

輸入：pipeline/data/b3_<tag>/predictions.parquet、targets.parquet
用法：
    python pipeline/src/05_compare_runs.py --base ml3fixed --runs baseline mc35 \\
        --out docs/reports/catalog_mc/b3_impact.md
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eqforecast import metrics as M  # noqa: E402

BASE_DIR = Path(__file__).resolve().parents[2]
DATA = BASE_DIR / "pipeline" / "data"
TAUS = [0.01, 0.02, 0.05, 0.10]


def load_run(tag, model, target, split, data_dir=DATA):
    d = Path(data_dir) / f"b3_{tag}"
    pred = pd.read_parquet(d / "predictions.parquet")
    tg = pd.read_parquet(d / "targets.parquet")
    p = pred[(pred["model"] == model) & (pred["target"] == target) & (pred["split"] == split)]
    p = p.merge(tg[["t0", "cell_id", f"y_{target}"]], on=["t0", "cell_id"], how="left")
    p = p.sort_values(["t0", "cell_id"]).reset_index(drop=True)
    return p.rename(columns={f"y_{target}": "y"})


def compare(base, other, n_cells, seed=42, n_boot=2000):
    """base、other：load_run 的結果。回傳 dict（ASS、POD、ΔASS、McNemar）。"""
    if not (np.array_equal(base["t0"].to_numpy(), other["t0"].to_numpy())
            and np.array_equal(base["cell_id"].to_numpy(), other["cell_id"].to_numpy())
            and np.array_equal(base["y"].to_numpy(), other["y"].to_numpy())):
        raise ValueError("兩次回測的樣本或目標不同，不能配對比較")
    y = base["y"].to_numpy(bool)
    ra = M.window_ranks(base["t0"], base["score"], seed)
    rb = M.window_ranks(other["t0"], other["score"], seed)
    out = {"n_targets": int(y.sum()), "n_windows": int(base["t0"].nunique()),
           "ASS_base": M.area_skill_score(ra, y, n_cells), "ASS": M.area_skill_score(rb, y, n_cells),
           "delta_ASS": M.bootstrap_delta_ass(ra, rb, y, n_cells, n_boot, seed), "tau": {}}
    for tau in TAUS:
        k = max(1, int(round(tau * n_cells)))
        out["tau"][tau] = {"hits_base": int((ra[y] <= k).sum()), "hits": int((rb[y] <= k).sum()),
                           **M.mcnemar_exact(ra[y] <= k, rb[y] <= k)}
    return out


def render(base_tag, rows, model, target, split, n_cells):
    L = [f"# B3 回測比較：{base_tag} vs " + "、".join(t for t, _ in rows) + "\n\n",
         f"> 由 `pipeline/src/05_compare_runs.py` 產生（{pd.Timestamp.now():%Y-%m-%d}）。"
         f"模型 {model}、目標 {target}、切分 {split}；同一批發布時間與目標，配對比較。\n\n",
         "| 設定 | ASS | ΔASS（95% CI） | " + " | ".join(f"POD@{t:.0%}" for t in TAUS) + " |\n",
         "|---|---|---|" + "---|" * len(TAUS) + "\n"]
    first = rows[0][1]
    pods = " | ".join(f"{first['tau'][t]['hits_base'] / first['n_targets']:.2f}" for t in TAUS)
    L.append(f"| {base_tag}（對照） | {first['ASS_base']:.3f} | — | {pods} |\n")
    for tag, r in rows:
        d = r["delta_ASS"]
        pods = " | ".join(f"{r['tau'][t]['hits'] / r['n_targets']:.2f}" for t in TAUS)
        L.append(f"| {tag} | {r['ASS']:.3f} | {d['delta']:+.3f}（{d['ci_low']:+.3f} ～ {d['ci_high']:+.3f}） | {pods} |\n")
    L.append(f"\n目標 {first['n_targets']} 個、發布 {first['n_windows']} 次、{n_cells} 格。\n\n")
    L.append("**McNemar 精確檢定**（只有對照命中／只有新設定命中，雙尾 p）：\n\n")
    L.append("| 設定 | " + " | ".join(f"τ={t:.0%}" for t in TAUS) + " |\n|---|" + "---|" * len(TAUS) + "\n")
    for tag, r in rows:
        L.append(f"| {tag} | " + " | ".join(
            f"{r['tau'][t]['only_a']}／{r['tau'][t]['only_b']}，p={r['tau'][t]['p_two_sided']:.2f}" for t in TAUS) + " |\n")
    return "".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="對照組的 tag，例如 baseline")
    ap.add_argument("--runs", nargs="+", required=True, help="要比較的 tag")
    ap.add_argument("--model", default="logistic")
    ap.add_argument("--target", default="declustered")
    ap.add_argument("--split", default="test")
    ap.add_argument("--out", default=None, help="Markdown 輸出路徑（不給則只印出）")
    args = ap.parse_args()

    base = load_run(args.base, args.model, args.target, args.split)
    n_cells = int(base["cell_id"].max()) + 1
    rows = [(t, compare(base, load_run(t, args.model, args.target, args.split), n_cells)) for t in args.runs]
    md = render(args.base, rows, args.model, args.target, args.split, n_cells)
    print(md)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()
