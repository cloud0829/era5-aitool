# -*- coding: utf-8 -*-
"""QA 独立回归补充（不依赖工程师的 9 条，独立盲区验证）。

覆盖工程师测试未覆盖的盲区：
1. mock 端到端：提交任务 → SUCCESS 且缓存目录确实产出 FakeCdsClient 假产物文件
   （证明"能正常下载"链路通，而不只是 block_stats 数字正确）。
2. is_retryable_error 额外边界：
   - 非 requests 类、但带 .response.status_code 的异常（模拟 cdsapi.ClientError
     的鸭子类型），分类不应依赖具体异常类；
   - status_code 为字符串 "429"（requests 可能返回 str）也能正确分类；
   - 带 .response 但无 status_code → 落到文本/保守兜底；
   - 文本兜底：未知错误 → True（保守可重试）。
3. 不可重试分支：monkeypatch time.sleep 为 spy，404 时断言一次 sleep 都没调用
   （比"耗时<0.5s"更直接地证明不 sleep 不重试）。
4. 重试耗尽分支：429 一直失败 → attempts==retry_max、retried==True、
   编排器归为 BUSY_AFTER_RETRIES（"重试耗尽"），且 error 为最后一次失败原因。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from era5tool.acquisition import cds_channel
from era5tool.acquisition.cds_channel import _fetch_one_block, is_retryable_error
from era5tool.core.orchestrator import failed_task_error


# ---------------------------------------------------------------------------
# 盲区 2：is_retryable_error 额外边界
# ---------------------------------------------------------------------------
class FakeCdsClientError(Exception):
    """模拟 cdsapi.ClientError：鸭子类型带 .response，但非 requests 异常类。"""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.response = SimpleNamespace(status_code=status_code) \
            if status_code is not None else None


class FakeResponseNoStatus:
    """带 .response 但无 status_code 属性（响应对象不完整）。"""


def test_is_retryable_error_duck_typed_status_code():
    """非 requests 类异常（cdsapi ClientError 风格）也按状态码分类。"""
    assert is_retryable_error(
        FakeCdsClientError("HTTP 429 Too Many Requests", status_code=429)) is True
    assert is_retryable_error(
        FakeCdsClientError("Internal Server Error", status_code=500)) is True
    assert is_retryable_error(
        FakeCdsClientError("Not Found", status_code=404)) is False
    assert is_retryable_error(
        FakeCdsClientError("Forbidden", status_code=403)) is False


def test_is_retryable_error_string_status_code():
    """status_code 是字符串（requests 可能返回 str）也应正确分类。"""
    exc = FakeCdsClientError("Too Many Requests", status_code="429")
    assert is_retryable_error(exc) is True
    exc = FakeCdsClientError("Not Found", status_code="404")
    assert is_retryable_error(exc) is False


def test_is_retryable_error_response_without_status_code_falls_back():
    """带 .response 但无 status_code → 按文本/保守兜底，不崩溃。"""
    exc = Exception("boom")
    exc.response = FakeResponseNoStatus()
    # 文本不含任何特征 → 保守 True
    assert is_retryable_error(exc) is True
    exc2 = Exception("license not accepted")
    exc2.response = FakeResponseNoStatus()
    assert is_retryable_error(exc2) is False


def test_is_retryable_error_status_code_priority_over_text():
    """状态码优先于文本：文本说 not found 但状态码 503 → 可重试（瞬时优先）。"""
    exc = FakeCdsClientError("not found but actually transient", status_code=503)
    assert is_retryable_error(exc) is True
    # 反向：文本说 timeout 但状态码 404 → 不可重试（永久优先）
    exc2 = FakeCdsClientError("request timed out", status_code=404)
    assert is_retryable_error(exc2) is False


def test_is_retryable_error_unknown_exception_is_true():
    """未知异常 → 保守 True（宁可重试，避免瞬时故障误判为终态）。"""
    assert is_retryable_error(ValueError("weird internal error")) is True


# ---------------------------------------------------------------------------
# 盲区 3：不可重试分支真的不 sleep 不重试（sleep spy 直证）
# ---------------------------------------------------------------------------
class SpyRealClient:
    def __init__(self, errors: Optional[List[BaseException]] = None):
        self.errors = list(errors or [])
        self.calls = 0

    def retrieve(self, name, request=None, target=None):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return {"status": "done", "target": target}


def _http_error(status_code: int, text: str) -> Exception:
    resp = SimpleNamespace(status_code=status_code)
    return Exception(f"{status_code} {text}") if False else \
        __import__("requests").exceptions.HTTPError(f"{status_code} {text}",
                                                    response=resp)


def _cfg(retry_max: int = 3) -> Dict[str, Any]:
    return {
        "mock": False, "mock_delay": 0.001, "fail_rate": 0.0, "seed": 7,
        "retry_max": retry_max, "backoff_base": 0.01, "backoff_factor": 2.0,
        "backoff_max": 0.1, "backoff_jitter": 0.0,
    }


def _block() -> Dict[str, Any]:
    return {
        "key": "2m_temperature/2020/01",
        "dataset": "reanalysis-era5-single-levels",
        "request": {"variable": ["2m_temperature"], "year": ["2020"],
                    "month": ["01"]},
        "rel_target": "reanalysis-era5-single-levels/2m_temperature/hourly/2020/01.nc",
    }


def test_non_retryable_404_no_sleep_called(monkeypatch, tmp_path):
    """404 → 立即失败：attempts==1、retried==False、只调 1 次、sleep 一次都没被调用。"""
    sleeps: List[float] = []

    def spy_sleep(secs):
        sleeps.append(secs)

    monkeypatch.setattr(cds_channel.time, "sleep", spy_sleep)
    fake = SpyRealClient(errors=[_http_error(404, "Not Found")])
    monkeypatch.setattr(cds_channel, "_make_client", lambda cfg_: fake)

    task_dir = tmp_path / "task"
    cache_dir = tmp_path / "cache"
    task_dir.mkdir(exist_ok=True)
    cache_dir.mkdir(exist_ok=True)
    result = _fetch_one_block((_block(), _cfg(), str(task_dir),
                               str(cache_dir), "t_x"))

    assert result["status"] == "failed"
    assert result["attempts"] == 1
    assert result["retried"] is False
    assert fake.calls == 1
    assert sleeps == [], f"404 不可重试分支不应 sleep，实际 sleep 了 {len(sleeps)} 次"


# ---------------------------------------------------------------------------
# 盲区 4：重试耗尽分支 retried 语义
# ---------------------------------------------------------------------------
def test_retry_exhausted_429_sets_retried_true(monkeypatch, tmp_path):
    """429 一直失败 → attempts==retry_max、retried==True、error=最后一次原因。"""
    monkeypatch.setattr(cds_channel.time, "sleep", lambda secs: None)
    fake = SpyRealClient(errors=[
        _http_error(429, "Too Many Requests"),
        _http_error(429, "Too Many Requests"),
        _http_error(429, "Too Many Requests"),
    ])
    monkeypatch.setattr(cds_channel, "_make_client", lambda cfg_: fake)

    task_dir = tmp_path / "task"
    cache_dir = tmp_path / "cache"
    task_dir.mkdir(exist_ok=True)
    cache_dir.mkdir(exist_ok=True)
    result = _fetch_one_block((_block(), _cfg(retry_max=3), str(task_dir),
                               str(cache_dir), "t_y"))

    assert result["status"] == "failed"
    assert result["attempts"] == 3
    assert result["retried"] is True          # attempts>1 才算真重试过
    assert fake.calls == 3
    assert "429" in result["error"]
    # 编排器：存在 retried=True → BUSY_AFTER_RETRIES
    err = failed_task_error([result])
    assert err["code"] == "BUSY_AFTER_RETRIES"
    assert "重试耗尽" in err["message"]


def test_retry_success_after_one_failure_attempts_2(monkeypatch, tmp_path):
    """429 一次失败后成功：attempts==2 但 retried 不用于成功块（不报错）。"""
    monkeypatch.setattr(cds_channel.time, "sleep", lambda secs: None)
    fake = SpyRealClient(errors=[_http_error(429, "Too Many Requests")])
    monkeypatch.setattr(cds_channel, "_make_client", lambda cfg_: fake)

    task_dir = tmp_path / "task"
    cache_dir = tmp_path / "cache"
    task_dir.mkdir(exist_ok=True)
    cache_dir.mkdir(exist_ok=True)
    result = _fetch_one_block((_block(), _cfg(), str(task_dir),
                               str(cache_dir), "t_z"))

    assert result["status"] == "done"
    assert result["attempts"] == 2
    assert fake.calls == 2


# ---------------------------------------------------------------------------
# 盲区 1：mock 端到端 → 假产物文件真实产出 + 任务 SUCCESS
# ---------------------------------------------------------------------------
def _payload() -> Dict[str, Any]:
    return {
        "dataset": "reanalysis-era5-single-levels",
        "dataset_family": "era5-single",
        "variables": ["2m_temperature"],
        "timerange": {"start": "2020-06-01", "end": "2020-06-28"},
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


def test_mock_e2e_produces_fake_artifact_files(client, app_state):
    """mock 端到端：SUCCESS 且缓存目录产出 FakeCdsClient 假产物（证明下载链路通）。

    注意：cache_dir 是 pytest 会话级共享目录，其他测试也会写入 .nc 产物，
    因此不能断言整个 cache_dir 的文件数 == 本任务块数（会因共享污染误报）。
    改为只检查本任务各块 rel_target 对应的产物文件真实存在。
    """
    r = client.post("/api/download/submit", json={"request_schema": _payload()})
    assert r.json()["code"] == 0
    task_id = r.json()["data"]["task_id"]

    task = _wait_task(client, task_id)
    assert task["status"] == "success", \
        f"mock 任务应 success，实际 {task['status']} error={task.get('error')}"
    assert task["block_stats"]["done"] == task["block_stats"]["total"]
    assert task["block_stats"]["failed"] == 0

    # 关键断言：每个 done 块都真实产出了假产物文件（不是只有计数对）
    # 只统计本任务块 rel_target 对应的产物（避免共享 cache_dir 的跨测试污染）
    block_targets = [b["rel_target"] for b in task["params"]["blocks"]]
    assert block_targets, "任务应包含已生成的块定义（含 rel_target）"
    missing = []
    for rel in block_targets:
        p = app_state.settings.cache_dir / rel
        if not p.is_file():
            missing.append(rel)
    assert not missing, f"以下块未产出假产物文件: {missing}"

    # 抽查产物内容为 FakeCdsClient 假 NetCDF 文本
    sample = app_state.settings.cache_dir / block_targets[0]
    payload = json.loads(sample.read_text(encoding="utf-8"))
    assert payload.get("fake") is True, f"{sample} 不是 FakeCdsClient 产物"
    assert payload.get("name") == "reanalysis-era5-single-levels"
    assert isinstance(payload.get("request"), dict)
