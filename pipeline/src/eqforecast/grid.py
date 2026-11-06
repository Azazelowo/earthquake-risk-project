"""研究區網格。網格編號 cell_id = i * nlon + j，i 為緯度方向、j 為經度方向。"""
import numpy as np
import pandas as pd

EPS = 1e-9  # 避免 23.2 / 0.2 這類浮點誤差把邊界上的點分到隔壁格


class Grid:
    def __init__(self, region, cell_deg):
        self.region = region
        self.cell_deg = float(cell_deg)
        self.nlat = int(round((region.lat_max - region.lat_min) / cell_deg))
        self.nlon = int(round((region.lon_max - region.lon_min) / cell_deg))

    @property
    def n_cells(self):
        return self.nlat * self.nlon

    def cell_of(self, lat, lon):
        """回傳網格編號；研究區外為 -1。"""
        lat = np.asarray(lat, float)
        lon = np.asarray(lon, float)
        r = self.region
        i = np.floor((lat - r.lat_min) / self.cell_deg + EPS).astype(int)
        j = np.floor((lon - r.lon_min) / self.cell_deg + EPS).astype(int)
        inside = (i >= 0) & (i < self.nlat) & (j >= 0) & (j < self.nlon)
        return np.where(inside, i * self.nlon + j, -1)

    def cells(self):
        """所有網格的中心座標。"""
        ids = np.arange(self.n_cells)
        i, j = np.divmod(ids, self.nlon)
        r = self.region
        return pd.DataFrame({
            "cell_id": ids,
            "i": i,
            "j": j,
            "lat_c": r.lat_min + (i + 0.5) * self.cell_deg,
            "lon_c": r.lon_min + (j + 0.5) * self.cell_deg,
        })

    def neighborhood_sum(self, arr):
        """對形狀為 (n_cells, ...) 的陣列做 3×3 鄰域加總（研究區外視為 0）。"""
        a = arr.reshape(self.nlat, self.nlon, *arr.shape[1:])
        out = np.zeros_like(a)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                src_i = slice(max(0, di), self.nlat + min(0, di))
                dst_i = slice(max(0, -di), self.nlat + min(0, -di))
                src_j = slice(max(0, dj), self.nlon + min(0, dj))
                dst_j = slice(max(0, -dj), self.nlon + min(0, -dj))
                out[dst_i, dst_j] += a[src_i, src_j]
        return out.reshape(arr.shape)

    def neighborhood_max(self, values):
        """形狀為 (n_cells,) 的陣列取 3×3 鄰域最大值。"""
        a = values.reshape(self.nlat, self.nlon)
        out = np.full_like(a, -np.inf)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                src_i = slice(max(0, di), self.nlat + min(0, di))
                dst_i = slice(max(0, -di), self.nlat + min(0, -di))
                src_j = slice(max(0, dj), self.nlon + min(0, dj))
                dst_j = slice(max(0, -dj), self.nlon + min(0, -dj))
                out[dst_i, dst_j] = np.maximum(out[dst_i, dst_j], a[src_i, src_j])
        return out.ravel()

    def neighbors(self, cell_id):
        """包含自己在內的 3×3 鄰居網格編號。"""
        i, j = divmod(int(cell_id), self.nlon)
        out = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                ii, jj = i + di, j + dj
                if 0 <= ii < self.nlat and 0 <= jj < self.nlon:
                    out.append(ii * self.nlon + jj)
        return out
