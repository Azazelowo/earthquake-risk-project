"""評分模型。每個模型對每個樣本 (t0, cell) 給一個分數，分數越高越該發警報。

隨機（random）：     參考線，期望 POD = τ。任何模型都必須明顯贏過它。
相對強度（ri）：     分數 = 長期累積的 ML>=4 鄰域比例。不需訓練，代表
                     「大地震會發生在本來就常有地震的地方」這個最簡單的假設。
邏輯斯迴歸（logistic）：用目錄特徵學習權重。之後加入前兆特徵時，用同一個模型
                     比較「有前兆」與「沒前兆」，差異才能歸因於前兆。
"""
import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


class RandomModel:
    name = "random"

    def __init__(self, seed=0):
        self.seed = seed

    def fit(self, X, y):
        return self

    def score(self, X):
        return np.random.default_rng(self.seed).random(len(X))


class ColumnScore:
    """直接拿某一欄當分數（不訓練）。"""

    def __init__(self, column, name):
        self.column = column
        self.name = name

    def fit(self, X, y):
        return self

    def score(self, X):
        return X[self.column].to_numpy(float)


class LogisticModel:
    """缺值以訓練集中位數補上，並加一個「是否缺值」指示欄，讓模型知道那是未知而不是 0。"""

    def __init__(self, feature_cols, C=1.0, name="logistic", seed=0):
        self.feature_cols = list(feature_cols)
        self.C = C
        self.name = name
        self.seed = seed
        self.pipe = None

    def fit(self, X, y):
        self.pipe = make_pipeline(
            SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True),
            StandardScaler(),
            LogisticRegression(C=self.C, max_iter=2000, random_state=self.seed),
        )
        self.pipe.fit(X[self.feature_cols].to_numpy(float), np.asarray(y, int))
        return self

    def score(self, X):
        return self.pipe.predict_proba(X[self.feature_cols].to_numpy(float))[:, 1]

    def coefficients(self):
        """標準化後的係數（含缺值指示欄），用來看模型依賴哪些特徵。"""
        imp = self.pipe.named_steps["simpleimputer"]
        names = list(self.feature_cols)
        if imp.indicator_ is not None:
            names += [f"missing({self.feature_cols[i]})" for i in imp.indicator_.features_]
        coef = self.pipe.named_steps["logisticregression"].coef_.ravel()
        return dict(zip(names, coef.tolist()))
