# -*- coding: utf-8 -*-
"""通用网格规整（design-final.md §3.4/§7.1：0.1°↔0.25° 重采样，E4 验证）。

- regrid_to：xarray interp 重采样（需 scipy）。
- region_mean：区域平均。
- 限制：对大 0.1° 网格按时间抽样，避免内存溢出（R11）。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import xarray as xr


def regrid_to(ds: xr.Dataset, target_step: float = 0.25,
              lat_range=None, lon_range=None) -> xr.Dataset:
    """把 ds 重采样到 target_step 网格（默认 0.25°）。"""
    if "latitude" not in ds.dims or "longitude" not in ds.dims:
        return ds
    lat0 = float(ds.latitude.min())
    lat1 = float(ds.latitude.max())
    lon0 = float(ds.longitude.min())
    lon1 = float(ds.longitude.max())
    if lat_range:
        lat0, lat1 = float(lat_range[0]), float(lat_range[1])
    if lon_range:
        lon0, lon1 = float(lon_range[0]), float(lon_range[1])
    lat = np.arange(round(lat0 / target_step) * target_step,
                    round(lat1 / target_step) * target_step + target_step / 2,
                    target_step)
    lon = np.arange(round(lon0 / target_step) * target_step,
                    round(lon1 / target_step) * target_step + target_step / 2,
                    target_step)
    return ds.interp(latitude=lat, longitude=lon)


def region_mean(ds: xr.Dataset, area=None) -> xr.Dataset:
    """区域平均（area: {north, west, south, east}；默认全球）。"""
    if "latitude" not in ds.dims or "longitude" not in ds.dims:
        return ds
    if area:
        ds = ds.sel(latitude=slice(area["north"], area["south"]),
                    longitude=slice(area["west"], area["east"]))
    return ds.mean(dim=["latitude", "longitude"], keep_attrs=True)


def downsample_time(ds: xr.Dataset, max_points: int = 48) -> xr.Dataset:
    """按时间抽样，限制帧数（R11 大文件保护）。"""
    if "time" not in ds.dims or ds.sizes["time"] <= max_points:
        return ds
    idx = np.linspace(0, ds.sizes["time"] - 1, max_points).astype(int)
    return ds.isel(time=idx)
