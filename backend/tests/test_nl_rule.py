# -*- coding: utf-8 -*-
"""规则解析单测（design-final.md §3.1 兜底 + E3 样例思路）。"""
from __future__ import annotations

import pytest

from era5tool.config.settings import Settings
from era5tool.nl.rule_parser import RuleParser
from era5tool.nl.variable_map import VariableMap


@pytest.fixture(scope="module")
def rule_parser():
    settings = Settings.load()
    var_map = VariableMap(settings)
    return RuleParser(var_map)


def test_parse_temperature_land(rule_parser):
    """『华北平原2023年1月土壤湿度』→ land + 土壤湿度。"""
    out = rule_parser.parse("华北平原2023年1月土壤湿度，按月平均")
    assert out.schema is not None
    assert out.schema.dataset_family == "land"
    assert "volumetric_soil_water_layer_1" in out.schema.variables
    assert out.schema.timerange.start == "2023-01-01"
    assert out.schema.timerange.end == "2023-01-31"
    assert out.schema.aggregation == "mean"


def test_parse_yangtze(rule_parser):
    """『下载最近五年长三角五六月地表温度』→ 温度 + 区域 + 近五年。"""
    out = rule_parser.parse("下载最近五年长三角五六月地表温度")
    assert out.schema is not None
    assert "2m_temperature" in out.schema.variables
    assert out.schema.area.cds_area() == [34.0, 118.0, 29.0, 123.0]
    assert out.schema.frequency == "hourly"


def test_need_info_missing_timerange(rule_parser):
    """『下载降水数据』缺时间 → need_info。"""
    out = rule_parser.parse("下载降水数据")
    assert out.schema is None
    assert "timerange" in out.missing
    assert out.questions


def test_need_info_missing_variables(rule_parser):
    """『2020年6月的数据』缺变量 → need_info。"""
    out = rule_parser.parse("2020年6月的数据")
    assert out.schema is None
    assert "variables" in out.missing


def test_wind_heuristic(rule_parser):
    """『风』未精确命中时补充 u/v 分量。"""
    out = rule_parser.parse("2020年6月长江三角洲的风")
    assert out.schema is not None
    assert "10m_u_component_of_wind" in out.schema.variables
    assert "10m_v_component_of_wind" in out.schema.variables


def test_english_parse(rule_parser):
    """英文：Get 2m temperature for the Yangtze River Delta, June 2020。"""
    out = rule_parser.parse("Get 2m temperature for the Yangtze River Delta, June 2020")
    assert out.schema is not None
    assert "2m_temperature" in out.schema.variables
    assert out.schema.timerange.start.startswith("2020")


def test_era5_monthly(rule_parser):
    """『2020年月均温度』→ era5-monthly，time 维度 monthly。"""
    out = rule_parser.parse("2020年月均温度")
    assert out.schema is not None
    assert out.schema.dataset_family == "era5-monthly"
