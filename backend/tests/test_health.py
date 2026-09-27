# -*- coding: utf-8 -*-
"""冒烟：health + 统一响应封装 + 配置接口。"""
from __future__ import annotations


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_api_health_shape(client):
    r = client.get("/api/config/llm")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"code", "data", "message"}
    assert body["code"] == 0
    assert body["data"]["provider"] == "deepseek"
    assert "has_key" in body["data"]
    assert "api_key" not in body["data"]          # 不回显 Key


def test_config_get(client):
    r = client.get("/api/config")
    assert r.json()["code"] == 0
    data = r.json()["data"]
    assert data["download"]["cds_max_workers"] == 2
    assert "deepseek_api_key" not in json_str(data)


def json_str(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


def test_variable_map(client):
    r = client.get("/api/config/variable-map")
    assert r.json()["code"] == 0
    syn = r.json()["data"]["variable_map"]["synonyms"]
    assert "2m_temperature" in syn
    assert syn["2m_temperature"]["datasets"] == ["era5-single", "land"]
    assert len(syn) >= 25
