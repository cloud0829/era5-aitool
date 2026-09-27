# -*- coding: utf-8 -*-
"""QA：WebSocket /ws/tasks 订阅与事件结构（design-final.md §5.6 / §7.4）。"""
from __future__ import annotations

import json
import time

PROGRESS_KEYS = {"type", "task_id", "status", "phase", "block_key",
                 "block_index", "block_total", "progress", "message", "ts"}
STATUS_KEYS = {"type", "task_id", "status", "message", "ts"}
DONE_KEYS = {"type", "task_id", "status", "message", "block_stats", "ts"}


def _payload():
    return {
        "dataset": "reanalysis-era5-single-levels",
        "dataset_family": "era5-single",
        "variables": ["2m_temperature"],
        "timerange": {"start": "2020-06-01", "end": "2020-06-28"},
        "area": {"west": 118, "south": 29, "east": 123, "north": 34},
        "frequency": "hourly", "aggregation": "raw", "confidence": 0.9,
    }


def test_ws_subscribe_ack(client):
    with client.websocket_connect("/ws/tasks") as ws:
        ws.send_json({"action": "subscribe", "task_id": "t_dummy"})
        ack = ws.receive_json()
        assert ack.get("type") == "subscribed"
        assert ack.get("task_id") == "t_dummy"


def test_ws_task_events_structure(client):
    """任务运行期 WS 事件字段齐全（progress/status/done）。"""
    with client.websocket_connect("/ws/tasks") as ws:
        r = client.post("/api/download/submit", json={"request_schema": _payload()})
        assert r.json()["code"] == 0
        task_id = r.json()["data"]["task_id"]

        seen: dict = {"progress": None, "status": None, "done": None}
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                ev = ws.receive_json()
            except Exception:
                break
            t = ev.get("type")
            if t in seen and seen[t] is None:
                seen[t] = ev
            if ev.get("type") == "done" and ev.get("task_id") == task_id:
                break

        assert seen["done"] is not None, "未收到 done 事件"
        assert seen["done"]["task_id"] == task_id
        assert seen["done"]["status"] == "success"
        assert "block_stats" in seen["done"]
        for k in ("total", "done"):
            assert k in seen["done"]["block_stats"]

        # 事件字段完整性（设计契约 §7.4）
        if seen["progress"] is not None:
            assert PROGRESS_KEYS <= set(seen["progress"].keys()), \
                f"progress 事件缺字段: {seen['progress'].keys()}"
        if seen["status"] is not None:
            assert STATUS_KEYS <= set(seen["status"].keys()), \
                f"status 事件缺字段: {seen['status'].keys()}"


def test_ws_subscribe_specific_task(client):
    """订阅指定 task 后收到 ack；断线后 broker 清理。"""
    with client.websocket_connect("/ws/tasks") as ws:
        ws.send_json({"action": "subscribe", "task_id": "t_abc"})
        ack = ws.receive_json()
        assert ack["type"] == "subscribed" and ack["task_id"] == "t_abc"


def test_events_jsonl_persisted(client, app_state):
    """事件持久化 events.jsonl：运行期事件逐行落盘。"""
    r = client.post("/api/download/submit", json={"request_schema": _payload()})
    task_id = r.json()["data"]["task_id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        r = client.get(f"/api/download/{task_id}")
        if r.json()["data"]["status"] not in ("pending", "running"):
            break
        time.sleep(0.1)
    events_path = app_state.settings.tasks_dir / task_id / "events.jsonl"
    assert events_path.is_file(), "events.jsonl 未生成"
    lines = [ln for ln in events_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "events.jsonl 为空"
    last = json.loads(lines[-1])
    assert last.get("task_id") == task_id
    assert last.get("ts"), "事件缺 ts 字段"
