# -*- coding: utf-8 -*-
"""QA：真实 cdsapi 签名对齐 + 重试分类（P0 修复防回归）。

背景：cds_channel 旧代码调用 client.retrieve(block["request"], target)，
与真实 cdsapi 签名 (name, request, target=None) 错位，导致整个请求字典被
序列化进 URL 的 process 路径 → 404。mock 的 FakeCdsClient.retrieve(request,
target) 恰好与错位调用匹配，故测试全绿、真实模式必挂。

覆盖：
1. 真实签名回归：name=数据集字符串、request=dict、target=路径字符串。
2. 可重试（429）→ 退避重试后成功（attempts>1）。
3. 不可重试（404）→ 立即失败（attempts==1、retried=False、不 sleep 不重试）。
4. is_retryable_error 分类表。
5. 编排器文案：retried=False → "下载失败"；retried=True → "重试耗尽"。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import requests

from era5tool.acquisition import cds_channel
from era5tool.acquisition.cds_channel import _fetch_one_block, is_retryable_error
from era5tool.acquisition.mock_client import NonRetryableError, RetryableError
from era5tool.core.orchestrator import failed_task_error


# ---------------------------------------------------------------------------
# 假"真实"客户端：记录调用参数，按 errors 顺序抛异常，耗尽后正常返回
# ---------------------------------------------------------------------------
class FakeRealClient:
    """模拟真实 cdsapi.Client.retrieve(name, request, target) 调用（无网络）。"""

    def __init__(self, errors: Optional[List[BaseException]] = None):
        self.errors = list(errors or [])
        self.calls: List[Dict[str, Any]] = []

    def retrieve(self, name: str, request: Optional[Dict[str, Any]] = None,
                 target: Optional[str] = None) -> Dict[str, Any]:
        self.calls.append({"name": name, "request": request, "target": target})
        if self.errors:
            raise self.errors.pop(0)
        return {"status": "done", "target": target}


def _http_error(status_code: int, text: str) -> requests.exceptions.HTTPError:
    """构造带 .response.status_code 的 requests.HTTPError（真实 raise_for_status 同类）。"""
    resp = SimpleNamespace(status_code=status_code)
    return requests.exceptions.HTTPError(f"{status_code} {text}", response=resp)


def _real_cfg(retry_max: int = 3) -> Dict[str, Any]:
    """真实模式 worker cfg；退避参数取极小值避免测试长时间 sleep。"""
    return {
        "mock": False,
        "mock_delay": 0.001,
        "fail_rate": 0.0,
        "seed": 7,
        "retry_max": retry_max,
        "backoff_base": 0.01,
        "backoff_factor": 2.0,
        "backoff_max": 0.1,
        "backoff_jitter": 0.0,
    }


def _block(dataset: str = "reanalysis-era5-single-levels",
           key: str = "2m_temperature/2020/01") -> Dict[str, Any]:
    return {
        "key": key,
        "dataset": dataset,
        "request": {"product_type": ["reanalysis"],
                    "variable": ["2m_temperature"], "year": ["2020"],
                    "month": ["01"], "target": "ignored"},
        "rel_target": "reanalysis-era5-single-levels/2m_temperature/hourly/2020/01.nc",
    }


def _run_block(block: Dict[str, Any], cfg: Dict[str, Any],
               fake: FakeRealClient, monkeypatch, tmp_path: Path) -> Dict[str, Any]:
    task_dir = tmp_path / "task"
    cache_dir = tmp_path / "cache"
    task_dir.mkdir(exist_ok=True)
    cache_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(cds_channel, "_make_client", lambda cfg_: fake)
    return _fetch_one_block((block, cfg, str(task_dir), str(cache_dir), "t_test"))


# ---------------------------------------------------------------------------
# 1. 真实签名回归
# ---------------------------------------------------------------------------
def test_real_signature_name_request_target(monkeypatch, tmp_path):
    """retrieve(name, request, target)：name 必须是数据集字符串、request 是 dict、
    target 是路径字符串（P0 根因回归）。"""
    fake = FakeRealClient()
    block = _block()
    result = _run_block(block, _real_cfg(), fake, monkeypatch, tmp_path)

    assert result["status"] == "done"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["name"] == block["dataset"] == "reanalysis-era5-single-levels"
    assert isinstance(call["name"], str) and not isinstance(call["name"], dict)
    assert isinstance(call["request"], dict)
    assert call["request"] == block["request"]
    assert isinstance(call["target"], str)
    assert os.path.isabs(call["target"])
    assert call["target"].endswith("reanalysis-era5-single-levels/2m_temperature/"
                                   "hourly/2020/01.nc")


# ---------------------------------------------------------------------------
# 2. 可重试 → 退避重试后成功
# ---------------------------------------------------------------------------
def test_retryable_429_then_success(monkeypatch, tmp_path):
    """429（可重试）第一次失败、第二次成功 → done 且 attempts>1。"""
    fake = FakeRealClient(errors=[_http_error(429, "Too Many Requests")])
    block = _block()
    result = _run_block(block, _real_cfg(retry_max=3), fake, monkeypatch, tmp_path)

    assert result["status"] == "done"
    assert result["attempts"] == 2
    assert len(fake.calls) == 2
    # 重试调用参数依然正确（name 是数据集字符串而非 dict）
    assert fake.calls[1]["name"] == block["dataset"]
    assert isinstance(fake.calls[1]["request"], dict)


def test_retryable_503_then_success(monkeypatch, tmp_path):
    """503 Service Unavailable（可重试）→ 第二次成功。"""
    fake = FakeRealClient(errors=[_http_error(503, "Service Unavailable")])
    block = _block()
    result = _run_block(block, _real_cfg(retry_max=3), fake, monkeypatch, tmp_path)

    assert result["status"] == "done"
    assert result["attempts"] == 2
    assert len(fake.calls) == 2


# ---------------------------------------------------------------------------
# 3. 不可重试 → 立即失败（不 sleep 不重试）
# ---------------------------------------------------------------------------
def test_non_retryable_404_immediate_fail(monkeypatch, tmp_path):
    """404（不可重试）→ 立即失败：attempts==1、retried=False、只调用一次、不 sleep。"""
    fake = FakeRealClient(errors=[
        _http_error(404, "Not Found for url: https://cds.climate.copernicus.eu/"
                         "api/retrieve/v1/processes/reanalysis-era5-single-levels"),
    ])
    block = _block()
    cfg = _real_cfg(retry_max=3)
    t0 = time.time()
    result = _run_block(block, cfg, fake, monkeypatch, tmp_path)
    elapsed = time.time() - t0

    assert result["status"] == "failed"
    assert result["attempts"] == 1
    assert result["retried"] is False
    assert "404" in result["error"] or "Not Found" in result["error"]
    assert len(fake.calls) == 1
    assert elapsed < 0.5, f"不可重试错误不应 sleep 重试，实际耗时 {elapsed:.3f}s"


def test_non_retryable_403_immediate_fail(monkeypatch, tmp_path):
    """403 Forbidden（不可重试）→ 立即失败。"""
    fake = FakeRealClient(errors=[_http_error(403, "Forbidden")])
    block = _block()
    result = _run_block(block, _real_cfg(retry_max=3), fake, monkeypatch, tmp_path)

    assert result["status"] == "failed"
    assert result["attempts"] == 1
    assert result["retried"] is False
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# 4. is_retryable_error 分类表
# ---------------------------------------------------------------------------
def test_is_retryable_error_classification():
    # mock 自有异常
    assert is_retryable_error(RetryableError("HTTP 429 Too Many Requests (mock)")) is True
    assert is_retryable_error(NonRetryableError("Bad request (mock)")) is False
    # 带状态码：429/5xx 可重试
    assert is_retryable_error(_http_error(429, "Too Many Requests")) is True
    assert is_retryable_error(_http_error(500, "Internal Server Error")) is True
    assert is_retryable_error(_http_error(502, "Bad Gateway")) is True
    assert is_retryable_error(_http_error(503, "Service Unavailable")) is True
    assert is_retryable_error(_http_error(504, "Gateway Timeout")) is True
    # 带状态码：400/401/403/404 不可重试
    assert is_retryable_error(_http_error(400, "Bad Request")) is False
    assert is_retryable_error(_http_error(401, "Unauthorized")) is False
    assert is_retryable_error(_http_error(403, "Forbidden")) is False
    assert is_retryable_error(_http_error(404, "Not Found")) is False
    # 无 .response：按文本/保守兜底
    assert is_retryable_error(Exception("Connection reset by peer")) is True
    assert is_retryable_error(Exception("request timed out")) is True
    assert is_retryable_error(Exception("CDS API is busy, try later")) is True
    assert is_retryable_error(Exception("Temporary failure in name resolution")) is True
    assert is_retryable_error(Exception("license not accepted for dataset")) is False
    assert is_retryable_error(Exception("dataset reanalysis-era5-single-levels not found")) is False
    assert is_retryable_error(Exception("some unknown error")) is True  # 保守


# ---------------------------------------------------------------------------
# 5. 编排器文案
# ---------------------------------------------------------------------------
def test_orchestrator_message_non_retryable():
    """全部失败块 retried=False → DOWNLOAD_FAILED，含"下载失败"而非"重试耗尽"。"""
    err = failed_task_error([
        {"block": "a", "status": "failed", "retried": False,
         "error": "404 Not Found for url: https://cds.climate.copernicus.eu/"},
        {"block": "b", "status": "failed", "retried": False,
         "error": "403 Forbidden"},
    ])
    assert err["code"] == "DOWNLOAD_FAILED"
    assert "下载失败" in err["message"]
    assert "重试耗尽" not in err["message"]
    assert "404 Not Found" in err["message"]          # 首个失败原因
    assert "下载失败" in err["event_message"]


def test_orchestrator_message_retry_exhausted():
    """存在 retried=True 失败块 → BUSY_AFTER_RETRIES，含"重试耗尽"。"""
    err = failed_task_error([
        {"block": "a", "status": "failed", "retried": True,
         "error": "429 Too Many Requests"},
    ])
    assert err["code"] == "BUSY_AFTER_RETRIES"
    assert "重试耗尽" in err["message"]
    assert "下载失败" not in err["message"]
    assert "重试耗尽" in err["event_message"]


def test_orchestrator_message_mixed_prefers_retried():
    """混合场景：任一 retried=True → 整体归为重试耗尽（不掩盖重试耗尽块）。"""
    err = failed_task_error([
        {"block": "a", "status": "failed", "retried": False,
         "error": "404 Not Found"},
        {"block": "b", "status": "failed", "retried": True,
         "error": "429 Too Many Requests"},
    ])
    assert err["code"] == "BUSY_AFTER_RETRIES"
    assert "重试耗尽" in err["message"]
