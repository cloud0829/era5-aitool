# -*- coding: utf-8 -*-
"""NL 输出 JSON Schema（实验内等价实现，对齐 design-final.md §7.3）。

供 E3 使用：pydantic 校验 LLM 产出的结构化 JSON。
land 系列强制无 pressure_levels；area bbox 范围校验；日期格式校验。
"""
from __future__ import annotations

from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

DATASETS = [
    "reanalysis-era5-single-levels",
    "reanalysis-era5-pressure-levels",
    "reanalysis-era5-single-levels-monthly-means",
    "reanalysis-era5-land",
    "reanalysis-era5-land-monthly-means",
]
FAMILIES = ["era5-single", "era5-pressure", "era5-monthly", "land", "land-monthly"]
FREQUENCIES = ["hourly", "daily", "monthly"]
AGGREGATIONS = ["raw", "mean", "sum", "max", "min"]

FAMILY_OF_DATASET = {
    "reanalysis-era5-single-levels": "era5-single",
    "reanalysis-era5-pressure-levels": "era5-pressure",
    "reanalysis-era5-single-levels-monthly-means": "era5-monthly",
    "reanalysis-era5-land": "land",
    "reanalysis-era5-land-monthly-means": "land-monthly",
}


class Area(BaseModel):
    west: float = Field(default=-180, ge=-180, le=180)
    south: float = Field(default=-90, ge=-90, le=90)
    east: float = Field(default=180, ge=-180, le=180)
    north: float = Field(default=90, ge=-90, le=90)


class Timerange(BaseModel):
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError(f"日期格式必须为 YYYY-MM-DD: {v!r}") from exc
        return v


class RequestSchema(BaseModel):
    dataset: str = "reanalysis-era5-single-levels"
    dataset_family: str
    variables: List[str] = Field(min_length=1)
    pressure_levels: Optional[List[int]] = None
    timerange: Timerange
    area: Area = Field(default_factory=Area)
    frequency: str = "hourly"
    aggregation: str = "raw"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("dataset")
    @classmethod
    def _check_dataset(cls, v: str) -> str:
        if v not in DATASETS:
            raise ValueError(f"dataset 不在枚举内: {v}")
        return v

    @field_validator("dataset_family")
    @classmethod
    def _check_family(cls, v: str) -> str:
        if v not in FAMILIES:
            raise ValueError(f"dataset_family 不在枚举内: {v}")
        return v

    @field_validator("variables")
    @classmethod
    def _check_variables(cls, v: List[str]) -> List[str]:
        cleaned = [x.strip() for x in v if x and x.strip()]
        if not cleaned:
            raise ValueError("variables 不能为空")
        return cleaned

    @field_validator("frequency")
    @classmethod
    def _check_frequency(cls, v: str) -> str:
        if v not in FREQUENCIES:
            raise ValueError(f"frequency 不在枚举内: {v}")
        return v

    @field_validator("aggregation")
    @classmethod
    def _check_aggregation(cls, v: str) -> str:
        if v not in AGGREGATIONS:
            raise ValueError(f"aggregation 不在枚举内: {v}")
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> "RequestSchema":
        # 1) dataset 与 dataset_family 一致性
        expected_family = FAMILY_OF_DATASET.get(self.dataset)
        if expected_family is not None and self.dataset_family != expected_family:
            raise ValueError(
                f"dataset_family({self.dataset_family}) 与 dataset({self.dataset}) 不一致"
            )
        # 2) land 系列禁止 pressure_levels（§3.4 / §7.3）
        if self.dataset_family.startswith("land") and self.pressure_levels:
            raise ValueError("land 系列禁止 pressure_levels")
        # 3) era5-pressure 必须有 pressure_levels
        if self.dataset_family == "era5-pressure" and not self.pressure_levels:
            raise ValueError("era5-pressure 必须提供 pressure_levels")
        # 4) bbox 合理性
        if self.area.west >= self.area.east or self.area.south >= self.area.north:
            raise ValueError(f"area bbox 不合法: {self.area}")
        # 5) 时间先后
        if self.timerange.start > self.timerange.end:
            raise ValueError("timerange.start 晚于 timerange.end")
        return self


class NeedInfo(BaseModel):
    """LLM 追问响应。"""
    need_info: List[str]
    questions: List[str]


def parse_llm_json(raw: str) -> dict:
    """清洗 LLM 原始输出并解析为 dict。

    处理：Markdown 围栏（```json ... ```）、首尾空白、多余说明文字。
    解析失败抛 ValueError。
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        # 去掉第一行围栏（```json / ```）
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # 尝试直接解析
    try:
        import json
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 兜底：提取第一个 { ... } 块（最外层大括号配对）
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        import json
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
    raise ValueError(f"无法从 LLM 输出解析 JSON: {raw[:120]!r}")
