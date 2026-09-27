# -*- coding: utf-8 -*-
"""数据管理：netCDF4 头部元数据读取测试（design-data-manager.md T02/T05）。

覆盖：生成真实 netCDF4 样例 → 读元数据（变量/单位/维度/坐标/time_range）；
损坏文件（写坏字节）→ {ok:false, error}，绝不抛异常；无 time 变量 → time_range None。
"""
from __future__ import annotations

from pathlib import Path

import netCDF4
import numpy as np

from era5tool.data_manager.nc_meta import read_netcdf_header


def _make_nc(path: Path, with_time: bool = True) -> None:
    """写一个最小 NetCDF4 样例（time/latitude/longitude + 变量 t2m）。"""
    ds = netCDF4.Dataset(str(path), "w", format="NETCDF4")
    try:
        ds.title = "test dataset"
        ds.Conventions = "CF-1.6"
        ds.createDimension("time", None)          # unlimited
        ds.createDimension("latitude", 2)
        ds.createDimension("longitude", 3)
        if with_time:
            t = ds.createVariable("time", "f8", ("time",))
            t.units = "hours since 2020-01-01 00:00:00"
            t.calendar = "standard"
            t.long_name = "time"
            t[:] = np.array([0.0, 6.0, 12.0, 18.0])
        lat = ds.createVariable("latitude", "f8", ("latitude",))
        lat.units = "degrees_north"
        lat.long_name = "latitude"
        lat[:] = np.array([30.0, 40.0])
        lon = ds.createVariable("longitude", "f8", ("longitude",))
        lon.units = "degrees_east"
        lon.long_name = "longitude"
        lon[:] = np.array([110.0, 120.0, 130.0])
        var = ds.createVariable("t2m", "f4",
                                ("time", "latitude", "longitude"))
        var.units = "K"
        var.long_name = "2 metre temperature"
        var[:] = np.full((4, 2, 3), 288.15, dtype="f4")
        # 全局属性：字符串 + 数值
        ds.number_of_layers = 3
    finally:
        ds.close()


def test_read_header_success(tmp_path):
    p = tmp_path / "sample.nc"
    _make_nc(p)
    meta = read_netcdf_header(p)
    assert meta["ok"] is True
    assert meta["format"] == "NETCDF4"
    dim_names = {d["name"] for d in meta["dimensions"]}
    assert dim_names == {"time", "latitude", "longitude"}
    time_dim = next(d for d in meta["dimensions"] if d["name"] == "time")
    assert time_dim["is_unlimited"] is True

    var_names = {v["name"]: v for v in meta["variables"]}
    assert "t2m" in var_names
    assert var_names["t2m"]["units"] == "K"
    assert var_names["t2m"]["long_name"] == "2 metre temperature"
    assert var_names["t2m"]["dims"] == ["time", "latitude", "longitude"]
    assert var_names["t2m"]["shape"] == [4, 2, 3]
    assert var_names["t2m"]["is_coord"] is False
    assert var_names["time"]["is_coord"] is True

    assert sorted(meta["coords"]) == ["latitude", "longitude", "time"]

    tr = meta["time_range"]
    assert tr is not None
    assert tr["start"] == "2020-01-01 00:00:00"
    assert tr["end"] == "2020-01-01 18:00:00"
    assert "hours since" in tr["units"]
    assert meta["global_attrs"]["title"] == "test dataset"


def test_read_header_no_time_var(tmp_path):
    p = tmp_path / "no_time.nc"
    _make_nc(p, with_time=False)
    meta = read_netcdf_header(p)
    assert meta["ok"] is True
    assert meta["time_range"] is None


def test_read_header_corrupt_file_ok_false(tmp_path):
    p = tmp_path / "broken.nc"
    p.write_bytes(b"this is definitely not a netcdf file \x00\x01\xff")
    meta = read_netcdf_header(p)
    assert meta["ok"] is False
    assert meta["error"]
    assert "无法读取元数据" in meta["error"]


def test_read_header_missing_file_ok_false(tmp_path):
    """路径不存在也不抛（服务层负责 6001；底层永远 {ok:false}）。"""
    meta = read_netcdf_header(tmp_path / "missing.nc")
    assert meta["ok"] is False
    assert "无法读取元数据" in meta["error"]


def test_read_header_variable_without_units_ok(tmp_path):
    """缺少 units/long_name 的变量不阻断解析。"""
    p = tmp_path / "sparse.nc"
    ds = netCDF4.Dataset(str(p), "w", format="NETCDF4")
    try:
        ds.createDimension("x", 2)
        v = ds.createVariable("raw", "f4", ("x",))
        v[:] = np.array([1.0, 2.0])
    finally:
        ds.close()
    meta = read_netcdf_header(p)
    assert meta["ok"] is True
    raw = next(x for x in meta["variables"] if x["name"] == "raw")
    assert raw["units"] is None
    assert raw["long_name"] is None
