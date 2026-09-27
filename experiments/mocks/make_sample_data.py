# -*- coding: utf-8 -*-
"""合成 xarray 样例数据（供 E4 mock 复用）。

生成一个小区域的 `2m_temperature` 场：
  - time   ：hourly N 点（默认 24 点）
  - latitude / longitude：按 grid_step（0.25° 或 0.1°）生成小区域网格
  - 数据   ：空间梯度（lat 方向线性）+ 时间变化（日循环正弦）+ 噪声

全部离线合成，无需任何数据文件。
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

try:
    import xarray as xr
    HAS_XARRAY = True
except Exception:  # pragma: no cover - 环境缺 xarray 时给出清晰错误
    xr = None
    HAS_XARRAY = False


def make_sample_data(grid_step: float = 0.25,
                     n_time: int = 24,
                     lat_range: Tuple[float, float] = (28.0, 34.0),
                     lon_range: Tuple[float, float] = (118.0, 124.0),
                     base_temp: float = 20.0,
                     seed: int = 7) -> "xr.Dataset":
    """合成 ERA5/ERA5-Land 风格温度场。

    参数：
      grid_step  网格步长（ERA5=0.25，ERA5-Land=0.1）
      n_time     时间点数（小时）
      lat_range  纬度范围 (min, max)
      lon_range  经度范围 (min, max)
      base_temp  基准温度（°C）
      seed       随机种子
    返回：
      xr.Dataset，含 2m_temperature(time, latitude, longitude)，单位 °C。
    """
    if not HAS_XARRAY:
        raise RuntimeError("缺少 xarray 依赖，请先安装：pip install xarray numpy")

    rng = np.random.default_rng(seed)
    lat = np.arange(lat_range[0], lat_range[1] + grid_step / 2, grid_step)
    lon = np.arange(lon_range[0], lon_range[1] + grid_step / 2, grid_step)
    times = np.arange(0, n_time, dtype="float64")

    # 空间梯度：随纬度升高降温 0.5°C/度；随经度略增 0.1°C/度
    lat_grad = (lat - lat.mean()) * -0.5
    lon_grad = (lon - lon.mean()) * 0.1
    # 时间变化：日循环 ±4°C + 缓慢趋势
    diurnal = 4.0 * np.sin(2 * np.pi * times / 24.0)
    trend = 0.05 * times

    field = base_temp + lat_grad[:, None] + lon_grad[None, :]
    field = field[None, :, :] + (diurnal[:, None, None] + trend[:, None, None])
    field = field + rng.normal(0, 0.05, size=field.shape)  # 微小噪声

    ds = xr.Dataset(
        data_vars={
            "2m_temperature": (("time", "latitude", "longitude"), field.astype("float32")),
        },
        coords={
            "time": times.astype("int64"),
            "latitude": lat,
            "longitude": lon,
        },
        attrs={
            "dataset": "synthetic",
            "grid_step": float(grid_step),
            "units": "°C",
        },
    )
    return ds


def region_mean_series(ds: "xr.Dataset", var: str = "2m_temperature") -> "xr.DataArray":
    """区域平均时间序列（E4 时序图用）。"""
    return ds[var].mean(dim=["latitude", "longitude"])


def regrid_to(ds: "xr.Dataset", target_step: float = 0.25,
              var: str = "2m_temperature") -> "xr.Dataset":
    """把数据插值到目标网格（0.1° → 0.25°），E4 通过标准 ⑤ 用。"""
    if not HAS_XARRAY:
        raise RuntimeError("缺少 xarray 依赖")
    lat = np.arange(ds.latitude.min().item(), ds.latitude.max().item() + target_step / 2, target_step)
    lon = np.arange(ds.longitude.min().item(), ds.longitude.max().item() + target_step / 2, target_step)
    return ds.interp(latitude=lat, longitude=lon)
