# -*- coding: utf-8 -*-
"""QA：编排层边界/错误路径（mock CDS，不消耗真实配额）。

覆盖：无凭据友好错误、cancel→paused、resume 状态机、失败后 resume、
非法 schema 错误码、timerange 非法、land 气压层拒绝。
"""
from __future__ import annotations

import time

import pytest

from era5tool.acquisition.cds_channel import CdsChannel


def _payload(family: str = "era5-single", variables=None,
            start: str = "2020-01-01", end: str = "2020-12-31"):
    return {
        "dataset": {
            "era5-single": "reanalysis-era5-single-levels",
            "era5-pressure": "reanalysis-era5-pressure-levels",
            "era5-monthly": "reanalysis-era5-single-levels-monthly-means",
            "land": "reanalysis-era5-land",
            "land-monthly": "reanalysis-era5-land-monthly-means",
        }[family],
        "dataset_family": family,
        "variables": variables or ["2m_temperature"],
        "timerange": {"start": start, "end": end},
        "area": {"west": 118, "south": 29, "east": 123, "north": 34},
        "frequency": "hourly", "aggregation": "raw", "confidence": 0.9,
    }


def _wait_task(client, task_id, timeout=25.0):
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


def _wait_status(client, task_id, status, timeout=15.0):
    """等待任务进入指定状态（确保后台线程已开始，避免 PENDING 取消竞态）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/download/{task_id}")
        assert r.json()["code"] == 0
        if r.json()["data"]["status"] == status:
            return r.json()["data"]
        time.sleep(0.05)
    raise TimeoutError(f"task {task_id} 未进入 {status}")


# ---------------------------------------------------------------------------
# 无凭据 / 错误路径
# ---------------------------------------------------------------------------
def test_no_credentials_friendly_error_no_quota(client, app_state, monkeypatch, tmp_path):
    """非 mock 且无 ~/.cdsapirc → 1002 友好错误；不落缓存（不消耗配额）。"""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    cache_before = sorted(str(p) for p in app_state.settings.cache_dir.rglob("*"))
    app_state.settings.download.mock = False
    try:
        r = client.post("/api/download/submit",
                        json={"request_schema": _payload()})
        body = r.json()
        assert body["code"] == 1002
        assert "凭据" in body["message"] or "cdsapirc" in body["message"].lower()
        assert body["data"] is None
    finally:
        app_state.settings.download.mock = True
    cache_after = sorted(str(p) for p in app_state.settings.cache_dir.rglob("*"))
    assert cache_before == cache_after, "无凭据错误不应写入任何缓存文件"


def test_invalid_schema_returns_param_error_not_5000(client):
    """era5-pressure 缺 pressure_levels：应 1001 参数错误而非 5000。"""
    payload = _payload("era5-pressure")
    payload.pop("pressure_levels", None)
    r = client.post("/api/download/submit", json={"request_schema": payload})
    body = r.json()
    assert body["code"] == 1001, f"期望 1001 参数错误，实际 {body['code']}: {body['message']}"


def test_timerange_start_gt_end_returns_1001(client):
    payload = _payload()
    payload["timerange"] = {"start": "2020-12-31", "end": "2020-01-01"}
    r = client.post("/api/download/submit", json={"request_schema": payload})
    assert r.json()["code"] == 1001


def test_land_pressure_levels_rejected_1001(client):
    payload = _payload("land")
    payload["pressure_levels"] = [850]
    r = client.post("/api/download/submit", json={"request_schema": payload})
    assert r.json()["code"] == 1001


# ---------------------------------------------------------------------------
# cancel / resume 状态机
# ---------------------------------------------------------------------------
def _slow_cfg(self):
    d = self.settings.download
    return {
        "mock": True, "mock_delay": 0.4, "fail_rate": 0.0, "seed": 7,
        "retry_max": d.retry_max, "backoff_base": d.backoff_base,
        "backoff_factor": d.backoff_factor, "backoff_max": d.backoff_max,
        "backoff_jitter": d.backoff_jitter,
    }


def _fast_cfg(self):
    d = self.settings.download
    return {
        "mock": True, "mock_delay": 0.01, "fail_rate": 0.0, "seed": 7,
        "retry_max": d.retry_max, "backoff_base": d.backoff_base,
        "backoff_factor": d.backoff_factor, "backoff_max": d.backoff_max,
        "backoff_jitter": d.backoff_jitter,
    }


def test_cancel_running_task_ends_paused(client, app_state, monkeypatch):
    """cancel → paused（未完成块保留，可续传）。"""
    monkeypatch.setattr(CdsChannel, "worker_cfg", _slow_cfg)
    try:
        r = client.post("/api/download/submit",
                        json={"request_schema": _payload()})
        assert r.json()["code"] == 0
        task_id = r.json()["data"]["task_id"]
        # 等任务真正进入 running 再取消（规避 PENDING 取消竞态）
        _wait_status(client, task_id, "running")
        r = client.post(f"/api/download/{task_id}/cancel")
        assert r.json()["code"] == 0
        task = _wait_task(client, task_id)
        assert task["status"] == "paused", f"cancel 后应为 paused，实际 {task['status']}"
        assert task["block_stats"]["done"] < task["block_stats"]["total"]
    finally:
        monkeypatch.setattr(CdsChannel, "worker_cfg", _fast_cfg)


def test_resume_completed_task_rejected(client, app_state):
    """success 不可逆：对已完成任务 resume → 2002（不重复下载）。"""
    r = client.post("/api/download/submit", json={"request_schema": _payload()})
    task_id = r.json()["data"]["task_id"]
    task = _wait_task(client, task_id)
    assert task["status"] == "success"
    r = client.post(f"/api/download/{task_id}/resume")
    assert r.json()["code"] == 2002


def test_resume_after_failure_redownloads_failed_blocks(client, app_state, monkeypatch):
    """失败任务 resume 应重下失败块并最终 success（§8.2 契约）。"""
    orig = CdsChannel.worker_cfg

    def _fail_cfg(self):
        d = self.settings.download
        return {
            "mock": True, "mock_delay": 0.01, "fail_rate": 1.0, "seed": 7,
            "retry_max": 1, "backoff_base": d.backoff_base,
            "backoff_factor": d.backoff_factor, "backoff_max": d.backoff_max,
            "backoff_jitter": d.backoff_jitter,
        }

    monkeypatch.setattr(CdsChannel, "worker_cfg", _fail_cfg)
    r = client.post("/api/download/submit", json={"request_schema": _payload()})
    assert r.json()["code"] == 0
    task_id = r.json()["data"]["task_id"]
    task = _wait_task(client, task_id)
    assert task["status"] == "failed"

    monkeypatch.setattr(CdsChannel, "worker_cfg", orig)
    r = client.post(f"/api/download/{task_id}/resume")
    assert r.json()["code"] == 0, f"resume 应可提交，实际 {r.json()}"
    task = _wait_task(client, task_id)
    assert task["status"] == "success", \
        f"失败块 resume 后应 success，实际 {task['status']} error={task.get('error')}"
    assert task["block_stats"]["done"] == task["block_stats"]["total"]
    assert task["block_stats"]["failed"] == 0


def test_resume_paused_skips_done_blocks_no_duplicate(client, app_state, monkeypatch):
    """paused 任务 resume：已 done 块不得重复下载（基于 events 日志验证）。"""
    monkeypatch.setattr(CdsChannel, "worker_cfg", _slow_cfg)
    try:
        r = client.post("/api/download/submit",
                        json={"request_schema": _payload()})
        task_id = r.json()["data"]["task_id"]
        _wait_status(client, task_id, "running")
        client.post(f"/api/download/{task_id}/cancel")
        task = _wait_task(client, task_id)
        assert task["status"] == "paused"
    finally:
        monkeypatch.setattr(CdsChannel, "worker_cfg", _fast_cfg)
    time.sleep(0.3)   # 等待原后台线程完全退出（避免双线程竞态）

    task_dir = app_state.settings.tasks_dir / task_id
    done_before = set()
    for p in task_dir.rglob("*.done"):
        if p.read_text(encoding="utf-8").strip() == "done":
            rel = p.relative_to(task_dir).as_posix()
            done_before.add(rel[:-len(".done")])

    events_path = task_dir / "events.jsonl"
    size_before = events_path.stat().st_size if events_path.is_file() else 0

    r = client.post(f"/api/download/{task_id}/resume")
    assert r.json()["code"] == 0
    task = _wait_task(client, task_id)
    assert task["status"] == "success", \
        f"resume 后应 success，实际 {task['status']} error={task.get('error')}"

    # resume 阶段新增日志中不得出现对 done 块的重复下载
    new_lines = []
    if events_path.is_file():
        with open(events_path, "r", encoding="utf-8") as f:
            f.seek(size_before)
            new_lines = [line for line in f if line.strip()]
    dup = []
    for line in new_lines:
        import json as _json
        try:
            ev = _json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "log" and "下载完成" in ev.get("message", ""):
            bk = ev.get("block_key")
            if bk in done_before:
                dup.append(bk)
    assert not dup, f"resume 重复下载了已完成块: {dup}"


# ---------------------------------------------------------------------------
# 并发上限配置（R1）
# ---------------------------------------------------------------------------
def test_worker_cap_configured(app_state):
    assert app_state.settings.download.cds_max_workers >= 1
    assert app_state.orchestrator.channel.max_workers == \
        app_state.settings.download.cds_max_workers
