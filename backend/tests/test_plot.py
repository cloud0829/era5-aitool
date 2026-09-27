# -*- coding: utf-8 -*-
"""出图引擎单测：合成数据三类图（E4 思路落地）。"""
from __future__ import annotations

import os

import pytest

from era5tool.config.schema import Area, RequestSchema, Timerange


@pytest.fixture(scope="module")
def schema():
    return RequestSchema(
        dataset="reanalysis-era5-land",
        dataset_family="land",
        variables=["2m_temperature"],
        timerange=Timerange(start="2020-06-01", end="2020-06-30"),
        area=Area(west=118, south=29, east=123, north=34),
        frequency="hourly", aggregation="raw", confidence=0.9,
    )


def test_render_map(app_state, schema):
    result = app_state.plot_engine.render([], schema, "land", "default_map")
    art = result["artifact"]
    assert art["format"] == "png"
    assert art["url"].startswith("/products/")
    assert 0 < art["size"] < 5 * 1024 * 1024


def test_render_timeseries(app_state, schema):
    result = app_state.plot_engine.render([], schema, "land", "default_map",
                                          overrides={"plot_type": "timeseries"})
    assert result["artifact"]["format"] == "png"


def test_render_animation(app_state, schema):
    result = app_state.plot_engine.render([], schema, "land", "default_map",
                                          overrides={"plot_type": "animation",
                                                     "animation_frames": 8})
    art = result["artifact"]
    assert art["format"] == "gif"
    assert 0 < art["size"] < 20 * 1024 * 1024


def test_render_land_regrid(app_state, schema):
    """land 0.1° 合成数据 → 出图（regrid 到 0.25° 目标网格）。"""
    result = app_state.plot_engine.render([], schema, "land", "default_map",
                                          overrides={"grid_step": 0.25})
    assert result["artifact"]["format"] == "png"
