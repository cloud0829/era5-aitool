# -*- coding: utf-8 -*-
"""合成样例数据（mock/无任务出图时使用；与 experiments/mocks/make_sample_data.py 同思路）。"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
import xarray as xr


def make_sample_data(variables: List[str], family: str = "era5-single",
                     area=None, times: Optional[int] = 24) -> xr.Dataset:
    """生成带空间梯度 + 时间变化的合成数据。"""
    if area is not None and hasattr(area, "model_dump"):
        area = area.model_dump()
    area = area or {"north": 34.0, "west": 118.0, "south": 29.0, "east": 123.0}
    grid = 0.1 if family in ("land", "land-monthly") else 0.25
    lat = np.arange(area["south"], area["north"] + grid / 2, grid)
    lon = np.arange(area["west"], area["east"] + grid / 2, grid)
    n_t = max(2, times or 24)
    time = np.arange("2020-06-01T00", f"2020-06-0{min(3, n_t)}", dtype="datetime64[h]")[:n_t]
    if len(time) < n_t:
        time = np.arange(n_t, dtype="datetime64[h]") + np.datetime64("2020-06-01T00")

    # 2D 基场：纬向梯度的 sin 组合（真实感）
    LON, LAT = np.meshgrid(lon, lat)
    base = 280 + 10 * np.sin(np.deg2rad(LAT)) + 5 * np.cos(np.deg2rad(LON))
    data_vars = {}
    for i, var in enumerate(variables):
        trend = np.linspace(0, 3 + i, n_t)[:, None, None]
        wave = np.sin(np.linspace(0, 4 * np.pi, n_t))[:, None, None]
        field = base[None, :, :] + trend + wave * (0.5 + i * 0.1)
        data_vars[var] = (("time", "latitude", "longitude"), field.astype("float32"))
    return xr.Dataset(
        data_vars=data_vars,
        coords={"time": time, "latitude": lat, "longitude": lon},
        attrs={"family": family, "grid_step": grid, "synthetic": True},
    )
