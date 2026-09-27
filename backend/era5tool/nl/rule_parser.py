# -*- coding: utf-8 -*-
"""规则解析器（design-final.md §3.1 兜底路径）。

模板匹配 + 变量映射词典 → RequestSchema；缺关键参数返回 need_info。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from era5tool.config.schema import Area, RequestSchema, Timerange
from era5tool.nl.variable_map import VariableMap

# 内置区域词典（design-final.md §3.2：区域词 → bbox）
REGION_BBOX: Dict[str, Dict[str, float]] = {
    "长三角": {"north": 34.0, "west": 118.0, "south": 29.0, "east": 123.0},
    "华北平原": {"north": 40.0, "west": 113.0, "south": 34.0, "east": 119.0},
    "珠三角": {"north": 24.0, "west": 112.0, "south": 21.0, "east": 115.0},
    "京津冀": {"north": 42.0, "west": 113.0, "south": 36.0, "east": 119.0},
    "四川盆地": {"north": 33.0, "west": 103.0, "south": 28.0, "east": 108.0},
}


@dataclass
class RuleOutcome:
    schema: Optional[RequestSchema] = None
    missing: List[str] = field(default_factory=list)
    questions: List[str] = field(default_factory=list)


def _last_day(year: int, month: int) -> int:
    if month == 12:
        return 31
    nxt = date(year, month + 1, 1) - timedelta(days=1)
    return nxt.day


EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_num(s: str) -> int:
    """中文数字（1-99）→ int；失败返回 0。"""
    if not s:
        return 0
    if "十" in s:
        parts = s.split("十")
        tens = _CN_DIGITS.get(parts[0], 1) if parts[0] else 1
        ones = _CN_DIGITS.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(s, 0)


class RuleParser:
    """规则兜底解析。"""

    def __init__(self, var_map: VariableMap):
        self.var_map = var_map

    # ------------------------------------------------------------------
    def detect_family(self, text: str) -> str:
        t = text.lower()
        land = any(k in t for k in ("land", "陆地", "地面", "土壤", "0.1度"))
        # 月度数据集：显式月均/月度/逐月/monthly；「按月平均」不切换数据集
        monthly = (any(k in t for k in ("月均", "月度", "逐月", "monthly"))
                   or bool(re.search(r"(?<!按)月平均", t)))
        pressure = any(k in t for k in ("气压层", "pressure level", "pressure_level"))
        if pressure:
            return "era5-pressure"
        if land and monthly:
            return "land-monthly"
        if land:
            return "land"
        if monthly:
            return "era5-monthly"
        return "era5-single"

    def detect_variables(self, text: str, family: str) -> List[str]:
        hits = self.var_map.find_all(text, family)
        # 「风」启发式：未精确命中时补充 u/v 分量
        if "风" in text or "wind" in text.lower():
            for extra in ("10m_u_component_of_wind", "10m_v_component_of_wind"):
                if family in self.var_map.datasets_for(extra) and extra not in hits:
                    hits.append(extra)
        return hits

    def detect_timerange(self, text: str) -> Optional[Timerange]:
        t = text.strip()
        today = date.today()
        # 最近 N 年（支持中文数字，如「最近五年」）
        m = re.search(r"最近\s*([0-9一二两三四五六七八九十]+)\s*年", t)
        if m:
            raw = m.group(1)
            n = int(raw) if raw.isdigit() else _cn_num(raw)
            if n > 0:
                start = date(today.year - n, today.month, 1)
                return Timerange(start=start.isoformat(), end=today.isoformat())
        # YYYY年M月到M'月（同年）
        m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*到\s*(\d{1,2})\s*月", t)
        if m:
            y, m1, m2 = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if m1 <= m2 <= 12:
                return Timerange(start=f"{y:04d}-{m1:02d}-01",
                                 end=f"{y:04d}-{m2:02d}-{_last_day(y, m2):02d}")
        # YYYY年M月 至 YYYY年M'月
        m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*至\s*(\d{4})\s*年\s*(\d{1,2})\s*月", t)
        if m:
            y1, m1, y2, m2 = (int(m.group(i)) for i in range(1, 5))
            return Timerange(start=f"{y1:04d}-{m1:02d}-01",
                             end=f"{y2:04d}-{m2:02d}-{_last_day(y2, m2):02d}")
        # YYYY年M月
        m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", t)
        if m:
            y, mo = int(m.group(1)), int(m.group(2))
            return Timerange(start=f"{y:04d}-{mo:02d}-01",
                             end=f"{y:04d}-{mo:02d}-{_last_day(y, mo):02d}")
        # YYYY年（整年）
        m = re.search(r"(\d{4})\s*年", t)
        if m:
            y = int(m.group(1))
            return Timerange(start=f"{y:04d}-01-01", end=f"{y:04d}-12-31")
        # ISO 日期对
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})\s*[到至~]\s*(\d{4})-(\d{2})-(\d{2})", t)
        if m:
            return Timerange(start=f"{m.group(1)}-{m.group(2)}-{m.group(3)}",
                             end=f"{m.group(4)}-{m.group(5)}-{m.group(6)}")
        # 英文：Month YYYY（如 June 2020）
        m = re.search(r"(january|february|march|april|may|june|july|august|"
                      r"september|october|november|december)\s+(\d{4})", t)
        if m:
            mo = EN_MONTHS[m.group(1)]
            y = int(m.group(2))
            return Timerange(start=f"{y:04d}-{mo:02d}-01",
                             end=f"{y:04d}-{mo:02d}-{_last_day(y, mo):02d}")
        # 英文/裸 4 位年份兜底
        m = re.search(r"(?<!\d)(\d{4})(?!\d)", t)
        if m:
            y = int(m.group(1))
            return Timerange(start=f"{y:04d}-01-01", end=f"{y:04d}-12-31")
        return None

    def detect_area(self, text: str) -> Area:
        for name, bbox in REGION_BBOX.items():
            if name in text:
                return Area(**bbox)
        return Area()   # 全球

    def detect_frequency(self, text: str) -> str:
        t = text.lower()
        if any(k in t for k in ("逐日", "daily", "日平均")):
            return "daily"
        if any(k in t for k in ("逐月", "月度", "月均", "月平均", "monthly")):
            return "monthly"
        return "hourly"

    def detect_aggregation(self, text: str) -> str:
        t = text.lower()
        if any(k in t for k in ("平均", "均值", "mean")):
            return "mean"
        if any(k in t for k in ("求和", "总计", "sum")):
            return "sum"
        if any(k in t for k in ("最大", "最高", "max")):
            return "max"
        if any(k in t for k in ("最小", "最低", "min")):
            return "min"
        return "raw"

    # ------------------------------------------------------------------
    def parse(self, text: str) -> RuleOutcome:
        family = self.detect_family(text)
        variables = self.detect_variables(text, family)
        timerange = self.detect_timerange(text)
        area = self.detect_area(text)
        frequency = self.detect_frequency(text)
        aggregation = self.detect_aggregation(text)

        missing: List[str] = []
        questions: List[str] = []
        if not variables:
            missing.append("variables")
            questions.append("请告诉我需要下载哪些变量，例如：温度、降水、风速、土壤湿度")
        if timerange is None:
            missing.append("timerange")
            questions.append("请提供时间范围，例如「2020年6月」「最近三年」「2020-01-01 到 2020-12-31」")

        confidence = 0.85
        if area != Area():
            confidence = min(confidence + 0.05, 1.0)
        if timerange is None or not variables:
            confidence = min(confidence - 0.2, confidence)

        if missing:
            return RuleOutcome(missing=missing, questions=questions)

        dataset_family = family
        dataset = {
            "era5-single": "reanalysis-era5-single-levels",
            "era5-pressure": "reanalysis-era5-pressure-levels",
            "era5-monthly": "reanalysis-era5-single-levels-monthly-means",
            "land": "reanalysis-era5-land",
            "land-monthly": "reanalysis-era5-land-monthly-means",
        }[dataset_family]
        try:
            schema = RequestSchema(
                dataset=dataset,
                dataset_family=dataset_family,
                variables=variables,
                pressure_levels=[850, 500] if dataset_family == "era5-pressure" else None,
                timerange=timerange,
                area=area,
                frequency=frequency,
                aggregation=aggregation,
                confidence=round(confidence, 2),
            )
            return RuleOutcome(schema=schema)
        except ValueError as exc:
            return RuleOutcome(missing=["schema"], questions=[str(exc)])
