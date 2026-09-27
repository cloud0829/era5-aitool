# -*- coding: utf-8 -*-
"""CDS 请求构造与 family 参数表（design-final.md §3.4）。

- CDS area 顺序固定 [north, west, south, east]。
- ERA5-Land 一律无 pressure_levels；monthly 系列 time=["00:00"]。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from era5tool.config.schema import RequestSchema

# family 参数表（design-final.md §3.4）
FAMILY_TABLE: Dict[str, Dict[str, Any]] = {
    "era5-single": {
        "dataset": "reanalysis-era5-single-levels",
        "grid_step": 0.25,
        "time_granularity": "hour",
        "has_pressure_levels": False,
        "chunk": "variable-year-month",
    },
    "era5-pressure": {
        "dataset": "reanalysis-era5-pressure-levels",
        "grid_step": 0.25,
        "time_granularity": "hour",
        "has_pressure_levels": True,
        "chunk": "variable-year-month",
    },
    "era5-monthly": {
        "dataset": "reanalysis-era5-single-levels-monthly-means",
        "grid_step": 0.25,
        "time_granularity": "month",
        "has_pressure_levels": False,
        "chunk": "variable-year",
    },
    "land": {
        "dataset": "reanalysis-era5-land",
        "grid_step": 0.1,
        "time_granularity": "hour",
        "has_pressure_levels": False,
        "chunk": "variable-year-month",
    },
    "land-monthly": {
        "dataset": "reanalysis-era5-land-monthly-means",
        "grid_step": 0.1,
        "time_granularity": "month",
        "has_pressure_levels": False,
        "chunk": "variable-year",
    },
}

DATASET_TO_FAMILY: Dict[str, str] = {v["dataset"]: k for k, v in FAMILY_TABLE.items()}


def family_of_dataset(dataset: str) -> str:
    return DATASET_TO_FAMILY.get(dataset, "era5-single")


def build_cds_request(schema: RequestSchema, year: int,
                      month: Optional[int] = None,
                      day_block: Optional[Tuple[int, int]] = None,
                      variables: Optional[List[str]] = None,
                      day: Optional[int] = None) -> Dict[str, Any]:
    """按 family 构造 CDS 请求（design-final.md §3.4 伪代码落地）。

    variables 允许单块请求只包含该块自己的变量（默认 None → 使用
    schema.variables 全部变量，保持历史调用/测试行为不变）。prepare_blocks
    传入 variables=[b["variable"]]，修复多变量任务每块重复下载全部变量的缺陷。

    day：单日便捷参数（design-speedup-download.md §3.3）。day 非空 → 等价
    day_block=(day, day) → req["day"] == ["%02d" % day]。与 day_block 同时给出
    时，day_block 优先（显式区间胜出），不报错。hourly 家族 day/day_block 均为空
    → 保持现状 day=["01".."31"]（整月）；monthly 家族一律不写 day。
    """
    # day 便捷参数：仅在未显式给 day_block 时转换为单日区间（day_block 优先）
    if day_block is None and day is not None:
        day_block = (day, day)
    family = schema.dataset_family
    req: Dict[str, Any] = {
        "product_type": ["reanalysis"],
        "variable": list(variables) if variables else schema.variables,
        "year": [str(year)],
        # 注意：CDS 新后端已弃用 "format" 键，改用 "data_format"（官方报错
        # `The 'format' key for requests is deprecated, please use 'data_format' instead`）。
        # 用老字段会触发 400 Bad Request，导致真实下载直接失败。此处必须用 data_format。
        "data_format": "netcdf",
        "area": [schema.area.north, schema.area.west, schema.area.south, schema.area.east],
    }
    if family in ("era5-single", "era5-pressure", "land"):
        req["month"] = [f"{month:02d}"] if month else [f"{m:02d}" for m in range(1, 13)]
        if day_block:
            req["day"] = [f"{d:02d}" for d in range(day_block[0], day_block[1] + 1)]
        else:
            # 缺省整月：CDS v2 要求 day 必填，31 天覆盖任意月份（标准做法）。
            # 缺失 day 会导致 400 Bad Request（"None of the data you have
            # requested is available yet..."，真实下载必失败 P0 根因）。
            req["day"] = [f"{d:02d}" for d in range(1, 32)]
        req["time"] = [f"{h:02d}:00" for h in range(24)]
    if family in ("land-monthly", "era5-monthly"):
        req["month"] = [f"{month:02d}"] if month else [f"{m:02d}" for m in range(1, 13)]
        req["time"] = ["00:00"]
    if family == "era5-pressure":
        req["pressure_level"] = [str(p) for p in (schema.pressure_levels or [])]
    return req
