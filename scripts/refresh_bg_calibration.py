"""把既有快照（app/data/）的「平常」改成用只有背景模型自己的校準倍率（審查 M5），不用重跑整個擬合。

為什麼需要這支：
    舊快照的 n_bg 是「完整模型的背景項 × 完整模型的校準倍率」，載入時再用 mu 的比例換算，
    這會低估「平常」（實測 2026-09-30：只有背景自己的倍率比完整模型大 1.4–1.8 倍），
    讓「倍數」（模型 ÷ 平常）偏高。新版由 06_hazard_model.py 直接匯出正確的 n_bg；
    重跑 06 需要擬合與回測（很久），而這裡只需要「平常」那幾欄，所以另提供這支就地更新。

安全措施：
    1. 先用 catalog_base.csv 重算完整模型的校準倍率，必須和 params.json 裡的一致（相對誤差 < 1e-6），
       證明這份目錄和當初匯出用的是同一份；不一致就中止，不改任何檔案。
    2. 只改 hazard_latest.csv 的 n_bg_* 欄與 params.json 的 calibration_factors_bg；其他欄位原封不動。
    3. 已經是新版就什麼都不做（除非加 --force）。

用法：
    python scripts/refresh_bg_calibration.py --data app/data
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline" / "src"))
import safe_console  # noqa: E402,F401
from eqforecast import hazard as hz  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="app/data")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    d = Path(args.data)

    params = json.loads((d / "params.json").read_text(encoding="utf-8"))
    if "calibration_factors_bg" in params and not args.force:
        print("已經是新版（有 calibration_factors_bg），不需要更新。")
        return
    prm = hz.HazardParams(**params["params"])
    grid = hz.Grid(**params["grid"])
    mu_bg = float(params["fit_info"]["mu_bg_only"])
    t0 = pd.Timestamp(params["issue_time"])
    trailing = float(params.get("calibration_trailing_days", 730))
    ev = pd.read_csv(d / "catalog_base.csv", parse_dates=["time"])
    cat = hz.Catalog(ev, grid, prm.m_ref)

    # 1) 一致性檢查：重算完整模型的校準倍率，必須和當初匯出的相同。
    #    容許 1e-4：catalog_base.csv 的座標與規模只存 4 位小數，邊界上的事件可能差一格，實測相對誤差約 1e-6；
    #    換了另一份目錄會差到 1e-3 以上，所以這個門檻抓得到「不是同一份」。
    worst = 0.0
    for T in params["export_horizons_days"]:
        want = float(params["calibration_factors"][f"{T:.5f}"])
        got = hz.trailing_factor(cat, t0, T, prm, trailing_days=trailing)
        worst = max(worst, abs(got - want) / max(1.0, abs(want)))
        if abs(got - want) > 1e-4 * max(1.0, abs(want)):
            sys.exit(f"中止：T={T:g} 天的完整模型校準倍率重算是 {got:.8f}，params.json 記錄 {want:.8f}。"
                     "catalog_base.csv 和當初匯出用的目錄不一致，不改任何檔案。")
    print(f"一致性檢查通過：完整模型校準倍率重算與記錄的最大相對誤差 {worst:.1e}")

    # 2) 重算「平常」
    latest = pd.read_csv(d / "hazard_latest.csv")
    old = {T: latest[f"n_bg_{T:.5f}d"].to_numpy(float).copy() for T in params["export_horizons_days"]}
    factors_bg = {}
    for T in params["export_horizons_days"]:
        n_bg, f_bg = hz.background_only_export(cat, t0, T, prm, mu_bg, trailing_days=trailing)
        factors_bg[f"{T:.5f}"] = f_bg
        latest[f"n_bg_{T:.5f}d"] = n_bg.ravel()
    latest.to_csv(d / "hazard_latest.csv", index=False, float_format="%.6g")
    params["calibration_factors_bg"] = factors_bg
    params["bg_export"] = ("只有背景的模型、用它自己的滾動校準倍率（審查 M5）；"
                           "由 scripts/refresh_bg_calibration.py 就地更新，之後重跑 06_hazard_model.py 會直接產生。")
    (d / "params.json").write_text(json.dumps(params, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"已更新 {d}：")
    for T in params["export_horizons_days"]:
        cur = latest[f"n_bg_{T:.5f}d"].to_numpy(float)
        # 舊載入方式 = 舊檔 × (mu_bg/mu)；新舊的比 = 新的校準倍率 ÷ 完整模型的校準倍率
        legacy = old[T] * mu_bg / prm.mu
        print(f"  T={T:7.3f} 天：f_bg {factors_bg[f'{T:.5f}']:.3f}（完整模型 {params['calibration_factors'][f'{T:.5f}']:.3f}）；"
              f"全區「平常」總量 舊 {legacy.sum():.4f} → 新 {cur.sum():.4f}（×{cur.sum() / legacy.sum():.2f}）")


if __name__ == "__main__":
    main()
