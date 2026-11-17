"""06_hazard_model.py 的回測：三個模型都要經過同一套滾動校準（審查 M4）。全部用合成目錄。"""
import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from eqforecast import hazard as hz  # noqa: E402
from test_hazard import GRID, synthetic  # noqa: E402

_spec = importlib.util.spec_from_file_location("hazard_model", SRC / "06_hazard_model.py")
hm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hm)


class UniformCalibrationTest(unittest.TestCase):
    def test_uniform_reference_is_recalibrated_like_the_others(self):
        base = synthetic(n=4000, seed=31, start="1995-01-01", end="2017-01-01")
        extra = synthetic(n=900, seed=32, start="2012-01-01", end="2017-01-01")   # 2012 起發生率約變成兩倍
        ev = pd.concat([base, extra]).sort_values("time").reset_index(drop=True)
        prm, _, info = hz.fit(ev, GRID, "2000-01-01", "2012-01-01", alphas=(0.8,), ps=(1.1,), sigmas_st=(0.5,),
                              sigmas_bg=(1.0,))
        bt = hm.backtest(ev, GRID, prm, info, "2015-01-01", "2017-01-01", mags=[3.5], horizons=[7.0])
        row = bt.iloc[0]
        # 訓練期的均勻發生率只有測試期的一半左右；沒校準時預測／實際約 0.5，校準後應接近 1
        self.assertGreater(row["calib_factor_uniform_median"], 1.5)
        self.assertAlmostEqual(row["n_ratio_uniform"], 1.0, delta=0.25)
        self.assertAlmostEqual(row["n_ratio_full"], 1.0, delta=0.25)
        self.assertTrue(np.isfinite(row["gain_vs_uniform"]))


if __name__ == "__main__":
    unittest.main()
