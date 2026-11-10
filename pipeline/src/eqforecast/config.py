"""設定檔：所有會影響結果的參數都集中在這裡，並寫進輸出的報告，方便重現。"""
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Region:
    lat_min: float = 21.0
    lat_max: float = 26.0
    lon_min: float = 119.0
    lon_max: float = 123.0


@dataclass
class ExternalFeatureSpec:
    """外部（前兆）特徵的來源。B2 的輸出接到這裡。

    path:            parquet 檔，每列是「一個網格 × 一天」
    lat_col/lon_col: 網格左下角座標欄位（02_clean_features.py 的 lat_bin／lon_bin）
    native_cell_deg: 該檔的網格大小，用來換算網格中心
    time_col:        日期欄位（UTC）
    value_cols:      要用的數值欄位；空白表示所有數值欄位
    lookback_days:   每個發布時間點往回彙整的天數（只用發布前的資料）
    aggs:            彙整方式，例如 mean、max、min、last
    """
    name: str
    path: str
    lat_col: str = "lat_bin"
    lon_col: str = "lon_bin"
    native_cell_deg: float = 0.2
    time_col: str = "time_bin"
    value_cols: list = field(default_factory=list)
    lookback_days: int = 30
    aggs: list = field(default_factory=lambda: ["mean", "max"])


@dataclass
class Config:
    tag: str = "baseline"
    region: Region = field(default_factory=Region)
    cell_deg: float = 0.2
    horizon_days: int = 30
    issue_start: str = "1991-01-01"
    # 目標事件
    target_ml: float = 5.5
    target_max_depth_km: float | None = None
    # 去群聚
    decluster_method: str = "gardner_knopoff"     # gardner_knopoff | uhrhammer
    decluster_fs_time_prop: float = 0.1           # 前震視窗 = 此比例 × 餘震時間視窗
    decluster_min_ml: float = 3.0                 # 只對 ML >= 此值的事件做去群聚
    # 特徵
    feature_min_ml: float = 3.0                   # 目錄特徵門檻的下限（設定 feature_min_ml_by_year 時，各年門檻不低於此值）
    # 隨年份變動的門檻（04_estimate_mc.py 產生的 CSV：year, threshold）；設定時每個事件用它那一年的門檻，
    # 且不低於 feature_min_ml。null = 全期間用 feature_min_ml。路徑相對於專案根目錄。
    feature_min_ml_by_year: str | None = None
    # 時間切分：[start, end)，end 為 null 表示到目錄最後一個完整預報窗
    splits: dict = field(default_factory=lambda: {
        "train": ["1991-01-01", "2013-01-01"],
        "val": ["2013-01-01", "2018-01-01"],
        "test": ["2018-01-01", None],
    })
    embargo_days: int = 0
    # 評估
    taus: list = field(default_factory=lambda: [0.01, 0.02, 0.05, 0.10])
    models: list = field(default_factory=lambda: ["random", "ri", "logistic"])
    logistic_C_grid: list = field(default_factory=lambda: [0.01, 0.1, 1.0])
    n_bootstrap: int = 2000
    seed: int = 42
    external_features: list = field(default_factory=list)

    @staticmethod
    def load(path):
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        raw = {k: v for k, v in raw.items() if not k.startswith("_")}  # "_comment" 等說明欄位
        region = Region(**raw.pop("region", {}))
        ext = [ExternalFeatureSpec(**e) for e in raw.pop("external_features", [])]
        cfg = Config(region=region, external_features=ext, **raw)
        cfg.validate()
        return cfg

    def validate(self):
        r = self.region
        for lo, hi in [(r.lat_min, r.lat_max), (r.lon_min, r.lon_max)]:
            n = (hi - lo) / self.cell_deg
            if abs(n - round(n)) > 1e-6:
                raise ValueError(f"研究區範圍 {lo}~{hi} 不是網格大小 {self.cell_deg} 的整數倍")
        if self.decluster_method not in ("gardner_knopoff", "uhrhammer", "none"):
            raise ValueError(f"未知的去群聚方法：{self.decluster_method}")
        if not set(self.splits) >= {"train", "val", "test"}:
            raise ValueError("splits 必須包含 train、val、test")

    def to_dict(self):
        return asdict(self)
