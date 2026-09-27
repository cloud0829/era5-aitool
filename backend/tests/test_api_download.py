# -*- coding: utf-8 -*-
"""API 下载全链路冒烟（mock CDS）：submit → 状态 → 完成。"""
from __future__ import annotations

import time

import pytest

from era5tool.config.schema import Area, RequestSchema, Timerange


def _payload(family: str = "era5-single", variables=None,
            start: str = "2020-01-01", end: str = "2020-12-31"):
    return {
        "dataset": {
            "era5-single": "reanalysis-era5-single-levels",
            "land": "reanalysis-era5-land",
        }[family],
        "dataset_family": family,
        "variables": variables or ["2m_temperature"],
        "timerange": {"start": start, "end": end},
        "area": {"west": 118, "south": 29, "east": 123, "north": 34},
        "frequency": "hourly", "aggregation": "raw", "confidence": 0.9,
    }


def _wait_task(client, task_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/download/{task_id}")
        body = r.json()
        assert body["code"] == 0
        status = body["data"]["status"]
        if status not in ("pending", "running"):
            return body["data"]
        time.sleep(0.1)
    raise TimeoutError(f"task {task_id} 未在 {timeout}s 内完成")


def test_submit_and_success(client):
    r = client.post("/api/download/submit", json={"request_schema": _payload()})
    assert r.json()["code"] == 0
    task_id = r.json()["data"]["task_id"]
    assert task_id.startswith("t_")

    task = _wait_task(client, task_id)
    assert task["status"] == "success"
    # 1 变量 × 1 年 × 12 月 = 12 块（timerange 覆盖整年 6 月）
    assert task["block_stats"]["total"] == 12
    assert task["block_stats"]["done"] == 12
    assert task["block_stats"]["failed"] == 0


def test_list_and_get(client):
    r = client.get("/api/download/list")
    assert r.json()["code"] == 0
    data = r.json()["data"]
    assert "tasks" in data and "total" in data
    assert data["total"] >= 1


def test_nl_parse_rule_fallback(client):
    """无 DEEPSEEK_API_KEY → 规则兜底。"""
    r = client.post("/api/nl/parse", json={"text": "下载2020年6月长三角温度"})
    assert r.json()["code"] == 0
    data = r.json()["data"]
    assert data["engine"] == "rule"
    assert "2m_temperature" in data["request_schema"]["variables"]


def test_nl_parse_need_info(client):
    r = client.post("/api/nl/parse", json={"text": "下载降水数据"})
    assert r.json()["code"] == 0
    data = r.json()["data"]
    assert data.get("need_info") is not None
    assert "timerange" in data["need_info"]["missing"]


def test_plot_render_api(client):
    """出图接口：request_schema 直出（无任务）→ 合成数据。"""
    r = client.post("/api/plot/render", json={
        "request_schema": _payload("land"), "profile": "default_map",
        "overrides": {"plot_type": "timeseries"},
    })
    assert r.json()["code"] == 0
    assert r.json()["data"]["artifact"]["format"] == "png"


def test_missing_credentials_error(client, app_state, monkeypatch, tmp_path):
    """非 mock 且无凭据 → ERR_NO_CDS(1002)。"""
    # 隔离用户主目录，确保 ~/.cdsapirc 不存在（避免受本机真实凭据影响）
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    app_state.settings.download.mock = False
    try:
        r = client.post("/api/download/submit",
                        json={"request_schema": _payload()})
        assert r.json()["code"] == 1002
    finally:
        app_state.settings.download.mock = True
