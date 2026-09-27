# -*- coding: utf-8 -*-
"""假 cdsapi 客户端（供 E1/E2/E5 mock 复用）。

模拟官方 `cdsapi.Client` 的关键接口：
  - retrieve(request, target)：按配置耗时（delay）、按 fail_rate 抛 429 类
    RetryableError、成功时在 target 写入一个假产物文件；每次调用记录日志。
  - info(name)：返回内置的 ERA5/ERA5-Land 元数据（离线核对表）。

用法（worker 进程内新建实例）：
    from mocks.fake_cdsapi import FakeCdsClient, RetryableError
    client = FakeCdsClient(delay=1.5, fail_rate=0.1, log=JsonlLog(path))
    try:
        client.retrieve({"variable": "2m_temperature", ...}, target)
    except RetryableError:
        ...

设计要点（design-final.md §10.2/§10.6）：
  - mock 段零外部网络；错误类型/日志结构对齐官方 cdsapi（429 → RetryableError）。
"""
from __future__ import annotations

import json
import os
import random
import time
from typing import Any, Dict, List, Optional

from exp_common import JsonlLog, ensure_dir

# 与官方 cdsapi 错误类对齐：429 限速属于可重试错误
class RetryableError(Exception):
    """可重试错误（HTTP 429 等限速类）。"""

    def __init__(self, message: str = "HTTP 429 Too Many Requests", code: int = 429):
        super().__init__(message)
        self.code = code


class NonRetryableError(Exception):
    """不可重试错误（参数错误等）。"""

    def __init__(self, message: str = "Bad request", code: int = 400):
        super().__init__(message)
        self.code = code


# 内置离线核对表（数据来源：design-final.md §3.4 / §10.3 官方已知信息）
LAND_INFO = {
    "name": "reanalysis-era5-land",
    "grid": 0.1,
    "time": [f"{h:02d}:00" for h in range(24)],
    "day": [f"{d:02d}" for d in range(1, 32)],
    "has_pressure_levels": False,
    "license": "需勾选 Land 许可（CDS 数据集许可页）",
    "variables": [
        "2m_temperature", "total_precipitation", "surface_pressure",
        "2m_dewpoint_temperature", "10m_u_component_of_wind",
        "10m_v_component_of_wind", "10m_wind_speed", "skin_temperature",
        "soil_temperature_level_1", "volumetric_soil_water_layer_1",
        "snow_depth_water_equivalent", "surface_net_solar_radiation",
        "evaporation", "potential_evaporation", "2m_temperature_max",
        "2m_temperature_min", "10m_wind_direction",
    ],
    "typical_chunk": "变量×年×月（0.1° 文件大，必要时按 10 天块）",
}

LAND_MONTHLY_INFO = {
    "name": "reanalysis-era5-land-monthly-means",
    "grid": 0.1,
    "time": ["00:00"],
    "day": None,  # 月均无 day 维度
    "has_pressure_levels": False,
    "license": "需勾选 Land 许可（CDS 数据集许可页）",
    "variables": LAND_INFO["variables"],
    "typical_chunk": "变量×年",
}

ERA5_SINGLE_INFO = {
    "name": "reanalysis-era5-single-levels",
    "grid": 0.25,
    "time": [f"{h:02d}:00" for h in range(24)],
    "day": [f"{d:02d}" for d in range(1, 32)],
    "has_pressure_levels": False,
    "license": "需勾选 ERA5 许可",
    "variables": [
        "2m_temperature", "total_precipitation", "surface_pressure",
        "mean_sea_level_pressure", "2m_dewpoint_temperature",
        "10m_u_component_of_wind", "10m_v_component_of_wind",
        "relative_humidity", "total_cloud_cover",
        "surface_solar_radiation_downwards", "snow_depth",
        "sea_surface_temperature", "visibility", "specific_humidity",
        "2m_temperature_max", "2m_temperature_min", "10m_wind_direction",
    ],
    "typical_chunk": "变量×年×月",
}

ERA5_PRESSURE_INFO = {
    "name": "reanalysis-era5-pressure-levels",
    "grid": 0.25,
    "time": [f"{h:02d}:00" for h in range(24)],
    "day": [f"{d:02d}" for d in range(1, 32)],
    "has_pressure_levels": True,
    "license": "需勾选 ERA5 许可",
    "variables": [
        "geopotential", "temperature", "u_component_of_wind",
        "v_component_of_wind", "specific_humidity", "relative_humidity",
    ],
    "typical_chunk": "变量×年×月（必要时 10 天）",
}

INFO_TABLE: Dict[str, Dict[str, Any]] = {
    "reanalysis-era5-land": LAND_INFO,
    "reanalysis-era5-land-monthly-means": LAND_MONTHLY_INFO,
    "reanalysis-era5-single-levels": ERA5_SINGLE_INFO,
    "reanalysis-era5-pressure-levels": ERA5_PRESSURE_INFO,
}


def block_key_from_request(request: Dict[str, Any]) -> str:
    """从 CDS 请求参数派生块 key（变量/年/月，形如 t2m/2020/05）。

    支持两种请求形态：
      - 单块请求：{"variable": "2m_temperature", "year": ["2020"], "month": ["05"]}
      - 多值请求：取第一个变量与年份、第一个月份
    取不到时退化为请求 JSON 摘要哈希。
    """
    var = request.get("variable")
    if isinstance(var, list):
        var = var[0] if var else "unknown"
    year = request.get("year")
    if isinstance(year, list):
        year = year[0] if year else "?"
    month = request.get("month")
    if isinstance(month, list):
        month = month[0] if month else "?"
    if var and year:
        return f"{var}/{year}/{month or 'all'}"
    digest = json.dumps(request, sort_keys=True, default=str)[:80]
    return digest


class FakeCdsClient:
    """stub cdsapi.Client。

    参数：
      delay      每次 retrieve 的固定耗时（秒）
      fail_rate  429 失败概率（0~1）
      seed       随机种子（固定种子保证 mock 可复现）
      log        JsonlLog 实例；None 则不写共享日志（仍保留 self.calls）
      info_table 离线元数据表（默认内置核对表）
    """

    def __init__(self, delay: float = 1.5, fail_rate: float = 0.1,
                 seed: Optional[int] = None, log: Optional[JsonlLog] = None,
                 info_table: Optional[Dict[str, Dict[str, Any]]] = None):
        self.delay = float(delay)
        self.fail_rate = float(fail_rate)
        self._rng = random.Random(seed)
        self.log = log
        self.info_table = info_table if info_table is not None else INFO_TABLE
        self.calls: List[Dict[str, Any]] = []   # 进程内调用记录（worker 内可读）

    # ------------------------------------------------------------------
    def retrieve(self, request: Dict[str, Any], target: str) -> Dict[str, Any]:
        """模拟一次 CDS retrieve。

        成功：在 target 写入假产物文件，返回元数据；
        失败：按 fail_rate 抛 RetryableError(429)。
        """
        key = block_key_from_request(request)
        started = time.time()
        if self.delay > 0:
            time.sleep(self.delay)

        if self._rng.random() < self.fail_rate:
            entry = {"event": "fail", "block": key, "elapsed": round(time.time() - started, 4)}
            self.calls.append(entry)
            if self.log is not None:
                self.log.append(event="fail", block=key, elapsed=entry["elapsed"])
            raise RetryableError(f"HTTP 429 Too Many Requests (block={key})")

        # 写假产物（内容仅用于标记块完成）
        target_dir = os.path.dirname(os.path.abspath(target))
        ensure_dir(target_dir)
        payload = {
            "fake": True,
            "block": key,
            "request": request,
            "created_at": time.time(),
        }
        with open(target, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        entry = {"event": "ok", "block": key, "elapsed": round(time.time() - started, 4)}
        self.calls.append(entry)
        if self.log is not None:
            self.log.append(event="ok", block=key, elapsed=entry["elapsed"])
        return {"block": key, "status": "done", "target": target}

    # ------------------------------------------------------------------
    def info(self, name: str) -> Dict[str, Any]:
        """返回数据集元数据（离线核对表）。"""
        if name not in self.info_table:
            raise NonRetryableError(f"Unknown dataset: {name}", code=404)
        return self.info_table[name]


def make_request(variable: str, year: str, month: str,
                 family: str = "era5-single", area=None) -> Dict[str, Any]:
    """构造一个单块 CDS 请求（供实验编排用）。"""
    req: Dict[str, Any] = {
        "product_type": ["reanalysis"],
        "variable": [variable],
        "year": [year],
        "month": [month],
        "format": "netcdf",
    }
    if family in ("era5-single", "era5-pressure", "land"):
        req["day"] = [f"{d:02d}" for d in range(1, 32)]
        req["time"] = [f"{h:02d}:00" for h in range(24)]
    elif family in ("land-monthly", "era5-monthly"):
        req["time"] = ["00:00"]
    if family == "era5-pressure":
        req["pressure_level"] = ["850", "500"]
    if area is not None:
        # CDS area 顺序固定 [north, west, south, east]
        req["area"] = [area["north"], area["west"], area["south"], area["east"]]
    return req
