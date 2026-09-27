# -*- coding: utf-8 -*-
"""请求/响应 Schema（design-final.md §7.3）与统一响应封装。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# 错误码（design-final.md §5）
# ---------------------------------------------------------------------------
ERROR_OK = 0
ERR_PARAM = 1001          # 参数错误
ERR_NO_CDS = 1002         # 缺 CDS 凭据
ERR_NO_LLM = 1003         # 缺 DeepSeek Key
ERR_LLM_PARSE = 1004      # LLM 解析失败
ERR_TASK_NOT_FOUND = 2001  # 任务不存在
ERR_TASK_STATE = 2002      # 任务状态不允许
ERR_PLOT = 3001            # 出图失败
ERR_ACCOUNT = 4001         # 账号校验失败
ERR_CONFIG = 5001          # 配置错误

# 数据管理错误码（design-data-manager.md §3.2；沿用 HTTP 200 + {code,data,message}）
ERR_FILE_NOT_FOUND = 6001  # 语义≈404：文件不存在或已被外部移除
ERR_FILE_BUSY = 6002       # 语义≈409：文件被 running/pending 下载任务占用，禁止删除
ERR_FILE_DELETE_FAILED = 6003  # 语义≈500：无权限或文件被其他程序占用，删除失败


class ApiResponse(BaseModel):
    """统一响应 {code, data, message}。"""

    code: int = ERROR_OK
    data: Any = None
    message: str = "ok"

    @classmethod
    def ok(cls, data: Any = None, message: str = "ok") -> "ApiResponse":
        return cls(code=ERROR_OK, data=data, message=message)

    @classmethod
    def err(cls, code: int, message: str, data: Any = None) -> "ApiResponse":
        return cls(code=code, data=data, message=message)


class ApiError(Exception):
    """业务异常：由路由层捕获并转为统一错误响应。"""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# ---------------------------------------------------------------------------
# 领域模型（design-final.md §7.3 JSON Schema）
# ---------------------------------------------------------------------------
class Area(BaseModel):
    """地理包围盒；CDS 参数顺序固定 [north, west, south, east]。

    §7.3 约束：west/east∈[-180,180]、south/north∈[-90,90]，且 north>south、east>west。
    """

    west: float = Field(-180.0, ge=-180.0, le=180.0)
    south: float = Field(-90.0, ge=-90.0, le=90.0)
    east: float = Field(180.0, ge=-180.0, le=180.0)
    north: float = Field(90.0, ge=-90.0, le=90.0)

    @model_validator(mode="after")
    def _check_consistency(self) -> "Area":
        if self.north <= self.south:
            raise ValueError("area north 必须大于 south")
        if self.east <= self.west:
            raise ValueError("area east 必须大于 west")
        return self

    def cds_area(self) -> List[float]:
        """按 CDS 规范输出 [lat_max(北), lon_min(西), lat_min(南), lon_max(东)]。"""
        return [self.north, self.west, self.south, self.east]


class Timerange(BaseModel):
    start: str = Field(..., description="ISO 8601 日期 YYYY-MM-DD")
    end: str = Field(..., description="ISO 8601 日期 YYYY-MM-DD")


DATASETS = [
    "reanalysis-era5-single-levels",
    "reanalysis-era5-pressure-levels",
    "reanalysis-era5-single-levels-monthly-means",
    "reanalysis-era5-land",
    "reanalysis-era5-land-monthly-means",
]
FAMILIES = ["era5-single", "era5-pressure", "era5-monthly", "land", "land-monthly"]


class RequestSchema(BaseModel):
    """NL 解析 / 下载提交的统一结构化请求。"""

    dataset: str = "reanalysis-era5-single-levels"
    dataset_family: str = "era5-single"
    variables: List[str] = Field(..., min_length=1)
    pressure_levels: Optional[List[int]] = None
    timerange: Timerange
    area: Area = Field(default_factory=Area)
    frequency: str = "hourly"
    aggregation: str = "raw"
    confidence: float = 0.8

    @field_validator("dataset")
    @classmethod
    def _check_dataset(cls, v: str) -> str:
        if v not in DATASETS:
            raise ValueError(f"未知数据集: {v}")
        return v

    @field_validator("dataset_family")
    @classmethod
    def _check_family(cls, v: str) -> str:
        if v not in FAMILIES:
            raise ValueError(f"未知数据集家族: {v}")
        return v

    @field_validator("variables")
    @classmethod
    def _check_variables(cls, v: List[str]) -> List[str]:
        v = [x.strip() for x in v if x and x.strip()]
        if not v:
            raise ValueError("variables 至少 1 个")
        return v

    @field_validator("frequency")
    @classmethod
    def _check_frequency(cls, v: str) -> str:
        if v not in ("hourly", "daily", "monthly"):
            raise ValueError(f"frequency 非法: {v}")
        return v

    @field_validator("aggregation")
    @classmethod
    def _check_aggregation(cls, v: str) -> str:
        if v not in ("raw", "mean", "sum", "max", "min"):
            raise ValueError(f"aggregation 非法: {v}")
        return v

    @model_validator(mode="after")
    def _check_family_rules(self) -> "RequestSchema":
        # 气压层：仅 era5-pressure 合法；land 系列必须无
        if self.dataset_family == "era5-pressure":
            if not self.pressure_levels:
                raise ValueError("era5-pressure 必须提供 pressure_levels")
        elif self.dataset_family in ("land", "land-monthly"):
            if self.pressure_levels:
                raise ValueError("ERA5-Land 系列不允许 pressure_levels")
        return self

    def model_dump_public(self) -> Dict[str, Any]:
        """输出给前端（不含内部字段）。"""
        return self.model_dump(mode="json")


# ---------------------------------------------------------------------------
# NL 解析返回
# ---------------------------------------------------------------------------
class NeedInfo(BaseModel):
    missing: List[str] = Field(default_factory=list)
    questions: List[str] = Field(default_factory=list)


class NlParseResult(BaseModel):
    request_schema: Optional[RequestSchema] = None
    need_info: Optional[NeedInfo] = None
    session_id: str = ""
    engine: str = "rule"          # deepseek | rule
    confirm: bool = False          # confidence<0.7 需确认
