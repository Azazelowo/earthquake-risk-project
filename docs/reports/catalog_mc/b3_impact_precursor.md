# B3 回測比較：precursor vs precursor_mcyear

> 由 `pipeline/src/05_compare_runs.py` 產生（2026-11-10）。模型 logistic、目標 declustered、切分 test；同一批發布時間與目標，配對比較。

| 設定 | ASS | ΔASS（95% CI） | POD@1% | POD@2% | POD@5% | POD@10% |
|---|---|---|---|---|---|---|
| precursor（對照） | 0.771 | — | 0.13 | 0.13 | 0.20 | 0.33 |
| precursor_mcyear | 0.771 | +0.000（+0.000 ～ +0.000） | 0.13 | 0.13 | 0.20 | 0.33 |

目標 15 個、發布 32 次、500 格。

**McNemar 精確檢定**（只有對照命中／只有新設定命中，雙尾 p）：

| 設定 | τ=1% | τ=2% | τ=5% | τ=10% |
|---|---|---|---|---|
| precursor_mcyear | 0／0，p=1.00 | 0／0，p=1.00 | 0／0，p=1.00 | 0／0，p=1.00 |
