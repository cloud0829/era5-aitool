# -*- coding: utf-8 -*-
"""ERA5/ERA5-Land 参数差异单测（design-final.md §3.4）。"""
from __future__ import annotations

import pytest

from era5tool.acquisition.cds_request import build_cds_request
from era5tool.config.schema import Area, RequestSchema, Timerange
from era5tool.core.normalizer import Normalizer


def _schema(family: str, variables=None, pressure=None, area=None):
    return RequestSchema(
        dataset_family=family,
        variables=variables or ["2m_temperature"],
        pressure_levels=pressure,
        timerange=Timerange(start="2020-01-01", end="2020-12-31"),
        area=area or Area(west=118, south=29, east=123, north=34),
    )


def test_area_order_cds():
    """CDS area 顺序固定 [north, west, south, east]。"""
    req = build_cds_request(_schema("era5-single"), 2020, 6)
    assert req["area"] == [34.0, 118.0, 29.0, 123.0]


def test_land_no_pressure():
    """ERA5-Land 请求无 pressure_levels。"""
    req = build_cds_request(_schema("land"), 2020, 6)
    assert "pressure_level" not in req
    assert req["time"] == [f"{h:02d}:00" for h in range(24)]
    assert req["month"] == ["06"]
    # 小时级 family 必须带 day（缺省整月；缺 day → CDS v2 400）
    assert req["day"] == [f"{d:02d}" for d in range(1, 32)]


def test_land_monthly_no_day():
    """land-monthly：time=["00:00"]，无 day 字段。"""
    req = build_cds_request(_schema("land-monthly"), 2020, None)
    assert req["time"] == ["00:00"]
    assert "day" not in req


def test_pressure_levels_required():
    """era5-pressure 必须带 pressure_levels。"""
    req = build_cds_request(_schema("era5-pressure", pressure=[850, 500]), 2020, 6)
    assert req["pressure_level"] == ["850", "500"]


def test_land_pressure_rejected():
    """land 系列不允许 pressure_levels → normalizer 抛参数错误。"""
    with pytest.raises(ValueError, match="不允许 pressure_levels"):
        Normalizer().normalize(_schema("land", pressure=[850]))


def test_normalizer_blocks():
    """切块：hourly = 变量×年×月；monthly = 变量×年（显式 month 粒度）。"""
    n = Normalizer(granularity="month")
    hourly = n.normalize(_schema("era5-single"))
    assert len(hourly.blocks) == 1 * 1 * 12          # 1 变量 × 1 年 × 12 月
    assert hourly.blocks[0]["key"] == "2m_temperature/2020/01"
    assert hourly.granularity == "month"
    monthly = n.normalize(_schema("land-monthly"))
    assert len(monthly.blocks) == 1 * 1              # 1 变量 × 1 年
    assert monthly.blocks[0]["key"] == "2m_temperature/2020"
    assert monthly.granularity == "monthly"


def test_normalizer_day_blocks():
    """day 粒度：hourly = 变量×年×天；每块 day 字段==单日，month 字段保持。"""
    n = Normalizer(granularity="day")
    daily = n.normalize(_schema("era5-single"))
    assert daily.granularity == "day"
    # 2020 闰年 → 366 天
    assert len(daily.blocks) == 366
    assert daily.blocks[0]["key"] == "2m_temperature/2020/01/01"
    b = daily.blocks[0]
    assert b["variable"] == "2m_temperature"
    assert b["year"] == 2020 and b["month"] == 1 and b["day"] == 1
    # 末块为 12-31
    assert daily.blocks[-1]["key"] == "2m_temperature/2020/12/31"


def test_family_table():
    """family 表：网格/气压层/时间粒度。"""
    n = Normalizer()
    assert n.family_table["land"]["grid_step"] == 0.1
    assert n.family_table["era5-single"]["has_pressure_levels"] is False
    assert n.family_table["era5-pressure"]["has_pressure_levels"] is True
