# -*- coding: utf-8 -*-
"""数据管理：netCDF4 头部元数据读取（design-data-manager.md §3.1/§4.2）。

read_netcdf_header(abs_path)：
- 成功 → {ok:True, format, dimensions:[...], variables:[...], coords:[...],
          time_range:{start,end,units,calendar}|None, global_attrs:{...}}；
- 任何异常 → {ok:False, error:"无法读取元数据：<原因>"}（**绝不 raise**，UI 兜底）。

实现约束（design-data-manager.md §1.1/§6）：
- 仅 netCDF4 只读头（已装 1.7.4，无新增依赖）；不 import xarray；
- 时间坐标只取 var[0]/var[-1] 首尾两个值做 num2date，**绝不整体 load 数据数组**；
- 写中/损坏文件等打开即崩的场景由统一 catch 兜底，不阻塞列表/删除流程。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import netCDF4

# 常见时间坐标变量名（按优先级匹配）；fallback 再按名字含 "time" 且一维探测
_TIME_NAMES = ("time", "valid_time", "forecast_time", "time1")
# 单文件点数极小（day=24 / month≈744 / monthly=12），取首尾足够且安全
_TIME_ERROR = "无法读取元数据"


def _to_jsonable(value: Any) -> Any:
    """把 netCDF 属性值转成可 JSON 序列化的基础类型（bytes 解码为 str）。"""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return repr(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:  # numpy 标量 / 数组等
        return value.item()
    except Exception:
        pass
    try:
        return [v.item() if hasattr(v, "item") else v for v in value]
    except Exception:
        return str(value)


def _global_attrs(ds: "netCDF4.Dataset") -> Dict[str, Any]:
    """全局属性（逐条转换；单条异常跳过，不阻断元数据返回）。"""
    attrs: Dict[str, Any] = {}
    for name in ds.ncattrs():
        try:
            attrs[name] = _to_jsonable(ds.getncattr(name))
        except Exception:
            continue
    return attrs


def _find_time_var(ds: "netCDF4.Dataset") -> Optional["netCDF4.Variable"]:
    """找 1D 时间坐标变量；找不到返回 None。"""
    for name in _TIME_NAMES:
        if name in ds.variables:
            v = ds.variables[name]
            if v.ndim == 1 and v.shape and v.shape[0] >= 1:
                return v
    for name, v in ds.variables.items():
        if v.ndim == 1 and v.shape and v.shape[0] >= 1 and "time" in name.lower():
            return v
    return None


def _format_time(t: Any) -> str:
    """num2date 结果（datetime / cftime）格式化为展示字符串。"""
    try:
        fmt = t.strftime("%Y-%m-%d %H:%M:%S")
        if fmt:
            return fmt
    except Exception:
        pass
    return str(t)


def _read_time_range(ds: "netCDF4.Dataset") -> Optional[Dict[str, str]]:
    """读取 time 坐标首尾（var[0]/var[-1]，不整体 load），失败返回 None。"""
    var = _find_time_var(ds)
    if var is None:
        return None
    units = getattr(var, "units", None)
    if not isinstance(units, str) or not units:
        return None
    calendar = getattr(var, "calendar", "standard") or "standard"
    try:
        first = var[0]
        last = var[-1]
        # var[0]/var[-1] 是 numpy 标量，不会把整列时间载入内存
        if getattr(first, "mask", False) or getattr(last, "mask", False):
            return None
        t0 = netCDF4.num2date(first, units, calendar=calendar)
        t1 = netCDF4.num2date(last, units, calendar=calendar)
    except Exception:
        return None
    return {"start": _format_time(t0), "end": _format_time(t1),
            "units": units, "calendar": calendar}


def read_netcdf_header(abs_path: Path) -> Dict[str, Any]:
    """只读 NetCDF 头部元数据；任何异常 → {ok:False, error}（绝不 raise）。"""
    path = Path(abs_path)
    try:
        ds = netCDF4.Dataset(str(path), "r")
    except Exception as exc:
        reason = (str(exc) or type(exc).__name__)[:200]
        return {"ok": False, "error": f"{_TIME_ERROR}：{reason}"}

    try:
        fmt = getattr(ds, "data_model", None) or getattr(ds, "file_format", "unknown")
        # 维度：{name, length, is_unlimited}
        dimensions: List[Dict[str, Any]] = []
        dim_names = list(ds.dimensions.keys())
        for name in dim_names:
            try:
                dim = ds.dimensions[name]
                dimensions.append({
                    "name": name,
                    "length": int(len(dim)),
                    "is_unlimited": bool(dim.isunlimited()),
                })
            except Exception:
                continue

        # 变量：{name, long_name, units, dims, shape, is_coord}
        variables: List[Dict[str, Any]] = []
        for name in ds.variables.keys():
            try:
                var = ds.variables[name]
                long_name = getattr(var, "long_name", None)
                units = getattr(var, "units", None)
                variables.append({
                    "name": name,
                    "long_name": _to_jsonable(long_name) if long_name is not None else None,
                    "units": _to_jsonable(units) if units is not None else None,
                    "dims": list(var.dimensions),
                    "shape": [int(x) for x in var.shape],
                    "is_coord": name in dim_names,
                })
            except Exception:
                continue

        # 坐标 = 既是维度又是变量的名字
        coords = [n for n in dim_names if n in ds.variables]

        time_range = _read_time_range(ds)
        global_attrs = _global_attrs(ds)
        return {
            "ok": True,
            "format": str(fmt),
            "dimensions": dimensions,
            "variables": variables,
            "coords": coords,
            "time_range": time_range,
            "global_attrs": global_attrs,
        }
    except Exception as exc:
        reason = (str(exc) or type(exc).__name__)[:200]
        return {"ok": False, "error": f"{_TIME_ERROR}：{reason}"}
    finally:
        try:
            ds.close()
        except Exception:
            pass
