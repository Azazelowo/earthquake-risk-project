"""前兆調整項（eqforecast/modifier.py）的單元測試，全部用合成資料。"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from eqforecast import modifier as md  # noqa: E402


def ar1(rng, shape, rho=0.9):
    """沿第 0 軸有時間相關的雜訊（模擬前兆特徵的連續性）。"""
    x = rng.normal(size=shape)
    for t in range(1, shape[0]):
        x[t] = rho * x[t - 1] + np.sqrt(1 - rho ** 2) * x[t]
    return x


class PoissonFitTest(unittest.TestCase):
    def test_recovers_coefficients_with_offset(self):
        rng = np.random.default_rng(0)
        X = rng.normal(size=(40000, 2))
        off = np.log(rng.uniform(0.05, 0.5, 40000))
        y = rng.poisson(np.exp(off + 0.3 + 0.5 * X[:, 0] - 0.2 * X[:, 1]))
        b = md.fit_poisson_offset(X, y, off)
        np.testing.assert_allclose(b, [0.3, 0.5, -0.2], atol=0.04)

    def test_intercept_only_matches_closed_form(self):
        rng = np.random.default_rng(1)
        off = np.log(rng.uniform(0.01, 0.3, 5000))
        y = rng.poisson(1.7 * np.exp(off))
        b = md.fit_poisson_offset(np.empty((5000, 0)), y, off)
        self.assertAlmostEqual(float(np.exp(b[0])), y.sum() / np.exp(off).sum(), places=6)

    def test_collinear_features_do_not_diverge(self):
        rng = np.random.default_rng(2)
        x = rng.normal(size=3000)
        y = rng.poisson(np.exp(-2 + 0.4 * x))
        b = md.fit_poisson_offset(np.column_stack([x, x]), y, np.zeros(3000))
        self.assertTrue(np.all(np.isfinite(b)))
        self.assertAlmostEqual(b[1] + b[2], 0.4, delta=0.08)


class RollingTest(unittest.TestCase):
    def test_uses_only_previous_days(self):
        a = np.arange(20, dtype=float)[:, None]
        r = md.rolling_before(a, 5)
        self.assertTrue(np.isnan(r[:3]).all())                  # 之前不足 3 天（5 × 0.5 取整）
        self.assertAlmostEqual(r[10, 0], np.mean([5, 6, 7, 8, 9]))
        b = a.copy()
        b[10:] = 1e6                                            # 改掉當天與未來
        np.testing.assert_allclose(md.rolling_before(b, 5)[:11], r[:11], equal_nan=True)

    def test_missing_days_and_min_fraction(self):
        a = np.full((12, 1), np.nan)
        a[[4, 5], 0] = [2.0, 4.0]
        r = md.rolling_before(a, 4, min_frac=0.5)
        self.assertAlmostEqual(r[6, 0], 3.0)                    # 視窗 2–5 有 2 天有值
        self.assertTrue(np.isnan(r[9, 0]))                      # 視窗 5–8 只有 1 天


class ShiftTestTest(unittest.TestCase):
    def make(self, effect, seed):
        rng = np.random.default_rng(seed)
        T, C = 160, 60
        feat = ar1(rng, (T, C, 1))
        off = np.log(rng.uniform(0.02, 0.2, (T, C)))
        y = rng.poisson(np.exp(off + effect * feat[:, :, 0]))
        is_train = np.arange(T) < 100
        return feat, y.astype(float), off, is_train, ~is_train

    def test_real_effect_is_detected(self):
        real, null, p = md.circular_shift_test(*self.make(0.5, 3), n_shifts=60)
        self.assertGreater(real["gain"], 0.02)
        self.assertLess(p, 0.05)
        self.assertAlmostEqual(real["beta"][1], 0.5, delta=0.1)

    def test_no_effect_gives_unremarkable_p(self):
        ps = [md.circular_shift_test(*self.make(0.0, s), n_shifts=40)[2] for s in (4, 5, 6, 7)]
        self.assertGreater(np.median(ps), 0.1)

    def test_rows_with_missing_features_are_dropped_for_both_models(self):
        feat, y, off, tr, te = self.make(0.0, 8)
        feat[::3, ::2, 0] = np.nan
        real, _, _ = md.circular_shift_test(feat, y, off, tr, te, n_shifts=5)
        self.assertLess(real["n_test_rows"], int(te.sum()) * feat.shape[1])
        self.assertTrue(np.isfinite(real["gain"]))

    def test_too_few_events_returns_nan(self):
        feat, y, off, tr, te = self.make(0.0, 9)
        real = md.evaluate(feat.reshape(-1, 1), np.zeros(y.size), off.ravel(), np.repeat(tr, feat.shape[1]),
                           np.repeat(te, feat.shape[1]))
        self.assertTrue(np.isnan(real["gain"]))


if __name__ == "__main__":
    unittest.main()
