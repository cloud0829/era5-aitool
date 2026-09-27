# -*- coding: utf-8 -*-
"""QA：出图 profile 无效配置 → 回退/友好错误不崩溃（R6）。"""
from __future__ import annotations

import json

from era5tool.plot.profiles import DEFAULT_MAP_PROFILE


def _payload(family: str = "era5-single"):
    return {
        "dataset": {
            "era5-single": "reanalysis-era5-single-levels",
            "land": "reanalysis-era5-land",
        }[family],
        "dataset_family": family,
        "variables": ["2m_temperature"],
        "timerange": {"start": "2020-06-01", "end": "2020-06-28"},
        "area": {"west": 118, "south": 29, "east": 123, "north": 34},
        "frequency": "hourly", "aggregation": "raw", "confidence": 0.9,
    }


def test_invalid_profile_json_returns_plot_error_not_5000(client, app_state):
    """profile 文件损坏（非法 JSON）→ 3001 或回退，不得 5000。"""
    pdir = app_state.settings.plot_profiles_dir
    (pdir / "corrupt.json").write_text("{ this is not json", encoding="utf-8")
    r = client.post("/api/plot/render", json={
        "request_schema": _payload(), "profile": "corrupt"})
    body = r.json()
    assert body["code"] in (0, 3001), \
        f"损坏 profile 应 3001 或回退成功，实际 {body['code']}: {body['message']}"


def test_unknown_profile_returns_3001(client):
    r = client.post("/api/plot/render", json={
        "request_schema": _payload(), "profile": "no_such_profile"})
    body = r.json()
    assert body["code"] == 3001


def test_invalid_plot_type_falls_back_to_map(client, app_state):
    """非法 plot_type 值 → 引擎兜底到 map 分支，不崩溃。"""
    pdir = app_state.settings.plot_profiles_dir
    bad = dict(DEFAULT_MAP_PROFILE)
    bad["plot_type"] = "bogus_type"
    (pdir / "badtype.json").write_text(json.dumps(bad, ensure_ascii=False),
                                       encoding="utf-8")
    r = client.post("/api/plot/render", json={
        "request_schema": _payload(), "profile": "badtype"})
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["artifact"]["format"] == "png"


def test_plot_render_three_types(client):
    """三类图：map / timeseries / animation 均可产出。"""
    for ptype, fmt in (("map", "png"), ("timeseries", "png"), ("animation", "gif")):
        r = client.post("/api/plot/render", json={
            "request_schema": _payload("land"),
            "profile": "default_map",
            "overrides": {"plot_type": ptype},
        })
        assert r.json()["code"] == 0, f"{ptype} 渲染失败: {r.json()}"
        assert r.json()["data"]["artifact"]["format"] == fmt
