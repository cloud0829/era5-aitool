# -*- coding: utf-8 -*-
"""QA：区域/时间几何边界（design-final.md §7.3 / §11：area=[N,W,S,E]，bbox 边界）。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from era5tool.acquisition.cds_request import build_cds_request
from era5tool.config.schema import Area, RequestSchema, Timerange
from era5tool.core.normalizer import Normalizer


def _schema(**over):
    base = {
        "dataset": "reanalysis-era5-single-levels",
        "dataset_family": "era5-single",
        "variables": ["2m_temperature"],
        "timerange": {"start": "2020-06-01", "end": "2020-06-30"},
    }
    base.update(over)
    return RequestSchema(**base)


def test_area_order_nwse():
    """CDS area 顺序固定 [north, west, south, east]。"""
    a = Area(west=-10, south=-5, east=10, north=5)
    assert a.cds_area() == [5, -10, -5, 10]


def test_area_global_default():
    a = Area()
    assert a.cds_area() == [90, -180, -90, 180]


def test_cross_zero_meridian_request():
    """跨 0° 经线 bbox：west=-10 east=10 正常构造。"""
    schema = _schema(area=Area(west=-10, south=-5, east=10, north=5))
    req = build_cds_request(schema, 2020, 6)
    assert req["area"] == [5, -10, -5, 10]


def test_negative_latlon_preserved():
    """南纬/西经负值原样保留（南半球区域）。"""
    schema = _schema(area=Area(west=-60, south=-35, east=-50, north=-30))
    req = build_cds_request(schema, 2020, 6)
    assert req["area"] == [-30, -60, -35, -50]


def test_area_out_of_range_rejected():
    """§7.3 契约：west 越界应被 pydantic 拒绝（1001 参数错误层）。"""
    with pytest.raises(ValidationError):
        _schema(area=Area(west=200, south=-90, east=180, north=90))


def test_area_lat_out_of_range_rejected():
    with pytest.raises(ValidationError):
        _schema(area=Area(west=-180, south=-95, east=180, north=90))


def test_timerange_invalid_normalizer_rejects():
    schema = _schema(timerange=Timerange(start="2020-12-31", end="2020-01-01"))
    with pytest.raises(ValueError):
        Normalizer().normalize(schema)


def test_land_monthly_time_only_0000():
    """land-monthly：time=['00:00']，无 day 字段。"""
    schema = _schema(dataset="reanalysis-era5-land-monthly-means",
                     dataset_family="land-monthly")
    req = build_cds_request(schema, 2020, 6)
    assert req["time"] == ["00:00"]
    assert "day" not in req
    assert "pressure_level" not in req


def test_hourly_family_has_24_times():
    schema = _schema()
    req = build_cds_request(schema, 2020, 6)
    assert len(req["time"]) == 24
    assert req["time"][0] == "00:00" and req["time"][-1] == "23:00"
    # 小时级 family 必须带 day（CDS v2 必填；缺 day → 400 真实下载必失败）
    assert "day" in req
    assert req["day"] == [f"{d:02d}" for d in range(1, 32)]


def test_hourly_day_default_full_month():
    """回归：不带 day_block 时 day 缺省整月（本 Bug 场景）。

    曾导致真实下载 400：build_cds_request(schema, 2005, 1) 生成的请求缺 day，
    CDS v2 报 "None of the data you have requested is available yet..."。
    """
    schema = _schema()
    req = build_cds_request(schema, 2005, 1)
    assert "day" in req
    assert req["day"] == [f"{d:02d}" for d in range(1, 32)]
    assert req["day"][0] == "01" and req["day"][-1] == "31"


def test_hourly_day_block_range():
    """带 day_block=(1,10) → day 精确为 ["01".."10"]（逐日切块路径）。"""
    schema = _schema()
    req = build_cds_request(schema, 2005, 1, day_block=(1, 10))
    assert "day" in req
    assert req["day"] == [f"{d:02d}" for d in range(1, 11)]
    assert req["day"][0] == "01" and req["day"][-1] == "10"
