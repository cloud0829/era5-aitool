# -*- coding: utf-8 -*-
"""bugfix download-gaps ①：错误分类 —— CDS 队列限流（HTTP 400）必须可重试。

线上事故（data/tasks/t_20260905_043416_261be78b/events.jsonl，172/172 失败块）：

    400 Client Error: Bad Request for url:
    https://cds.climate.copernicus.eu/api/retrieve/v1/jobs/<uuid>/results
    The job has been rejected
    Number queued requests for this dataset is temporarily limited.
    Please configure your scripts accordingly

这是**瞬时**错误（队列腾出空间即可成功），但状态码是 400。旧 `is_retryable_error`
按状态码把 400 一刀切判为「不可重试」→ 块**一次都不重试**直接永久失败 → 用户看到
「有的下不上、跳着时间下」。

本文件覆盖：
1. 线上原样 400 队列限流 → 可重试（核心回归）；
2. 纯 400（真·请求错误）仍不可重试（不误伤既有判定）；
3. 既有分类表全量回归（429/5xx 可重试、401/403/404/license/marsnodata 不可重试）；
4. classify_error / is_throttle_error 的类别语义；
5. _fetch_one_block 端到端：400 队列限流 → 退避重试后成功（不再一次即死）。
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
import requests

from era5tool.acquisition import cds_channel
from era5tool.acquisition.cds_channel import (CATEGORY_AUTH,
                                             CATEGORY_BAD_REQUEST,
                                             CATEGORY_NETWORK,
                                             CATEGORY_NOT_FOUND,
                                             CATEGORY_NO_DATA,
                                             CATEGORY_QUEUE_LIMITED,
                                             CATEGORY_RATE_LIMIT,
                                             CATEGORY_SERVER,
                                             CATEGORY_TRANSIENT,
                                             CATEGORY_UNKNOWN,
                                             RETRYABLE_CATEGORIES,
                                             THROTTLE_CATEGORIES,
                                             _fetch_one_block, classify_error,
                                             is_retryable_error,
                                             is_throttle_error)
from era5tool.acquisition.mock_client import (NonRetryableError, QueueLimitedError,
                                             RetryableError)

# 线上原文（逐字取自 events.jsonl 的失败事件 message 尾部）
CDS_QUEUE_BODY = ("The job has been rejected\n"
                  "Number queued requests for this dataset is temporarily limited. "
                  "Please configure your scripts accordingly ")
CDS_QUEUE_URL = ("https://cds.climate.copernicus.eu/api/retrieve/v1/jobs/"
                 "da55bb56-adb0-494b-83d4-320f907969f2/results")
CDS_QUEUE_MSG = f"400 Client Error: Bad Request for url: {CDS_QUEUE_URL}\n{CDS_QUEUE_BODY}"


def _http_error(status_code: int, text: str = "",
                body: str = "") -> requests.exceptions.HTTPError:
    """构造带 .response.status_code / .response.text 的 requests.HTTPError。"""
    resp = SimpleNamespace(status_code=status_code, text=body or text)
    return requests.exceptions.HTTPError(f"{status_code} {text}".strip(),
                                         response=resp)


def _cds_queue_error() -> requests.exceptions.HTTPError:
    """线上原样的 CDS 队列限流异常（HTTP 400 + temporarily limited 正文）。"""
    resp = SimpleNamespace(status_code=400, text=CDS_QUEUE_BODY)
    return requests.exceptions.HTTPError(CDS_QUEUE_MSG, response=resp)


# ---------------------------------------------------------------------------
# 1. 核心回归：CDS 队列限流 400 必须可重试
# ---------------------------------------------------------------------------
def test_cds_queue_limited_400_is_retryable():
    """线上根因回归：400 + "temporarily limited" 必须判为可重试（旧代码判 False）。"""
    exc = _cds_queue_error()
    assert exc.response.status_code == 400, "前提：CDS 确实用 400 返回队列限流"
    assert is_retryable_error(exc) is True
    assert classify_error(exc) == CATEGORY_QUEUE_LIMITED


def test_cds_queue_limited_400_is_throttle_error():
    """队列限流属于"撞墙"类别 → 触发全局降速（不只是本块退避）。"""
    exc = _cds_queue_error()
    assert is_throttle_error(exc) is True
    assert classify_error(exc) in THROTTLE_CATEGORIES
    assert classify_error(exc) in RETRYABLE_CATEGORIES


def test_cds_queue_limited_body_only_in_response_text():
    """正文只在 response.text（异常 str 里没有）时也要能识别。"""
    resp = SimpleNamespace(status_code=400, text=CDS_QUEUE_BODY)
    exc = requests.exceptions.HTTPError("400 Client Error: Bad Request",
                                        response=resp)
    assert is_retryable_error(exc) is True
    assert classify_error(exc) == CATEGORY_QUEUE_LIMITED


def test_queue_limited_variants_all_retryable():
    """各类限流/排队措辞（不同 CDS 版本文案）都应归类为瞬时。"""
    variants = [
        "400 Client Error: Bad Request\nThe job has been rejected",
        "400 Client Error: Bad Request\nNumber queued requests for this dataset is temporarily limited.",
        "400 Client Error: Bad Request\nRequest queue is full",
        "400 Client Error: Bad Request\nServer is throttling your requests",
        "429 Client Error: Too Many Requests\nslow down please",
        "400 Client Error: Bad Request\nrate limit exceeded",
    ]
    for text in variants:
        resp = SimpleNamespace(status_code=400, text=text)
        exc = requests.exceptions.HTTPError(text, response=resp)
        assert is_retryable_error(exc) is True, f"限流文案应可重试: {text!r}"
        assert classify_error(exc) in THROTTLE_CATEGORIES, text


# ---------------------------------------------------------------------------
# 2. 不误伤：纯 400（真·请求错误）仍不可重试
# ---------------------------------------------------------------------------
def test_plain_400_bad_request_still_non_retryable():
    """纯 400 且正文无瞬时措辞 → 真·请求错误，仍不可重试（防误伤既有判定）。"""
    exc = _http_error(400, "Bad Request")
    assert is_retryable_error(exc) is False
    assert classify_error(exc) == CATEGORY_BAD_REQUEST
    assert is_throttle_error(exc) is False


def test_plain_400_unknown_variable_non_retryable():
    """参数错误（如未知变量）类 400 不因本次改动被误判为可重试。"""
    resp = SimpleNamespace(status_code=400,
                           text="invalid request: unknown variable 'foo'")
    exc = requests.exceptions.HTTPError("400 Client Error: Bad Request",
                                        response=resp)
    assert is_retryable_error(exc) is False


# ---------------------------------------------------------------------------
# 3. 既有分类表全量回归（与 test_cds_channel_retry 同表，防止本次改动回退）
# ---------------------------------------------------------------------------
def test_existing_classification_table_regression():
    assert is_retryable_error(RetryableError("HTTP 429 Too Many Requests (mock)")) is True
    assert is_retryable_error(NonRetryableError("Bad request (mock)")) is False
    assert is_retryable_error(_http_error(429, "Too Many Requests")) is True
    assert is_retryable_error(_http_error(500, "Internal Server Error")) is True
    assert is_retryable_error(_http_error(502, "Bad Gateway")) is True
    assert is_retryable_error(_http_error(503, "Service Unavailable")) is True
    assert is_retryable_error(_http_error(504, "Gateway Timeout")) is True
    assert is_retryable_error(_http_error(400, "Bad Request")) is False
    assert is_retryable_error(_http_error(401, "Unauthorized")) is False
    assert is_retryable_error(_http_error(403, "Forbidden")) is False
    assert is_retryable_error(_http_error(404, "Not Found")) is False
    assert is_retryable_error(Exception("Connection reset by peer")) is True
    assert is_retryable_error(Exception("request timed out")) is True
    assert is_retryable_error(Exception("CDS API is busy, try later")) is True
    assert is_retryable_error(Exception("Temporary failure in name resolution")) is True
    assert is_retryable_error(Exception("license not accepted for dataset")) is False
    assert is_retryable_error(Exception("dataset era5 not found")) is False
    assert is_retryable_error(Exception("some unknown error")) is True


def test_classify_error_categories():
    """分类函数的类别语义（供编排层 failure_summary 聚合）。"""
    assert classify_error(_http_error(429, "Too Many Requests")) == CATEGORY_RATE_LIMIT
    assert classify_error(_http_error(503, "Service Unavailable")) == CATEGORY_SERVER
    assert classify_error(_http_error(404, "Not Found")) == CATEGORY_NOT_FOUND
    assert classify_error(_http_error(403, "Forbidden")) == CATEGORY_AUTH
    assert classify_error(_http_error(401, "Unauthorized")) == CATEGORY_AUTH
    assert classify_error(Exception("connection reset")) == CATEGORY_NETWORK
    assert classify_error(Exception("CDS is busy")) == CATEGORY_TRANSIENT
    assert classify_error(Exception("some unknown error")) == CATEGORY_UNKNOWN
    assert classify_error(Exception("Mars returned no data")) == CATEGORY_NO_DATA
    assert classify_error(RetryableError("429")) == CATEGORY_RATE_LIMIT
    assert classify_error(NonRetryableError("400")) == CATEGORY_BAD_REQUEST
    # 未知类别按可重试处理（保守策略不变）
    assert CATEGORY_UNKNOWN in RETRYABLE_CATEGORIES


def test_retryable_categories_and_throttle_categories_consistency():
    """THROTTLE_CATEGORIES 必须是 RETRYABLE_CATEGORIES 的子集（撞墙也是可重试）。"""
    assert THROTTLE_CATEGORIES <= RETRYABLE_CATEGORIES
    assert CATEGORY_QUEUE_LIMITED in THROTTLE_CATEGORIES
    assert CATEGORY_RATE_LIMIT in THROTTLE_CATEGORIES


# ---------------------------------------------------------------------------
# 4. _fetch_one_block 端到端：队列限流 → 重试后成功
# ---------------------------------------------------------------------------
class FakeRealClient:
    """模拟真实 cdsapi.Client.retrieve(name, request, target)，按 errors 顺序抛异常。"""

    def __init__(self, errors: Optional[List[BaseException]] = None):
        self.errors = list(errors or [])
        self.calls: List[Dict[str, Any]] = []

    def retrieve(self, name: str, request: Optional[Dict[str, Any]] = None,
                 target: Optional[str] = None) -> Dict[str, Any]:
        self.calls.append({"name": name, "request": request, "target": target})
        if self.errors:
            raise self.errors.pop(0)
        return {"status": "done", "target": target}


def _real_cfg(retry_max: int = 3) -> Dict[str, Any]:
    """真实模式 cfg（退避取极小值，避免测试长时间 sleep）。"""
    return {
        "mock": False, "mock_delay": 0.001, "fail_rate": 0.0, "seed": 7,
        "retry_max": retry_max, "backoff_base": 0.01, "backoff_factor": 2.0,
        "backoff_max": 0.05, "backoff_jitter": 0.0, "submit_stagger_s": 0.0,
        "throttle_enabled": False, "throttle_file": "",
        "mock_error_mode": "retryable_429",
    }


def _block() -> Dict[str, Any]:
    return {
        "key": "10m_u_component_of_wind/2005/01",
        "dataset": "reanalysis-era5-single-levels",
        "request": {"variable": ["10m_u_component_of_wind"], "year": ["2005"],
                    "month": ["01"]},
        "rel_target": ("reanalysis-era5-single-levels/10m_u_component_of_wind/"
                       "hourly/2005/01.nc"),
    }


def _run(block: Dict[str, Any], cfg: Dict[str, Any], fake: FakeRealClient,
         monkeypatch, tmp_path: Path) -> Dict[str, Any]:
    task_dir = tmp_path / "task"
    cache_dir = tmp_path / "cache"
    task_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cds_channel, "_make_client", lambda cfg_: fake)
    return _fetch_one_block((block, cfg, str(task_dir), str(cache_dir), "t_bugfix"))


def test_queue_limited_400_retried_then_success(monkeypatch, tmp_path):
    """核心修复验证：400 队列限流第 1 次失败 → 退避重试 → 第 2 次成功。

    修复前：is_retryable_error 返回 False → attempts==1、只调用 1 次、直接失败。
    """
    fake = FakeRealClient(errors=[_cds_queue_error()])
    result = _run(_block(), _real_cfg(retry_max=3), fake, monkeypatch, tmp_path)

    assert result["status"] == "done", f"应重试后成功，实际 {result}"
    assert result["attempts"] == 2
    assert len(fake.calls) == 2, "必须真的重试了一次（修复前只有 1 次）"


def test_queue_limited_400_exhausted_is_busy_after_retries(monkeypatch, tmp_path):
    """队列限流一直失败 → 重试耗尽：retried=True、类别为 queue_limited、throttled=True。"""
    fake = FakeRealClient(errors=[_cds_queue_error() for _ in range(5)])
    result = _run(_block(), _real_cfg(retry_max=3), fake, monkeypatch, tmp_path)

    assert result["status"] == "failed"
    assert result["attempts"] == 3
    assert result["retried"] is True                  # 真的重试过（旧行为是 False）
    assert result["error_category"] == CATEGORY_QUEUE_LIMITED
    assert result["throttled"] is True
    assert "temporarily limited" in result["error"]


def test_plain_400_still_immediate_fail_no_retry(monkeypatch, tmp_path):
    """纯 400（无瞬时措辞）→ 仍立即失败、不 sleep 不重试（防误伤）。"""
    fake = FakeRealClient(errors=[_http_error(400, "Bad Request") for _ in range(5)])
    cfg = _real_cfg(retry_max=3)
    t0 = time.time()
    result = _run(_block(), cfg, fake, monkeypatch, tmp_path)
    elapsed = time.time() - t0

    assert result["status"] == "failed"
    assert result["attempts"] == 1
    assert result["retried"] is False
    assert result["error_category"] == CATEGORY_BAD_REQUEST
    assert result.get("throttled") is not True
    assert len(fake.calls) == 1
    assert elapsed < 0.5, f"不可重试错误不应 sleep 重试，实际耗时 {elapsed:.3f}s"


def test_non_retryable_marsnodata_immediate_fail(monkeypatch, tmp_path):
    """MarsNoData（变量在数据集中无数据）→ 立即失败并给出中文用户提示。"""
    fake = FakeRealClient(errors=[Exception("Mars returned no data") for _ in range(5)])
    result = _run(_block(), _real_cfg(retry_max=3), fake, monkeypatch, tmp_path)

    assert result["status"] == "failed"
    assert result["attempts"] == 1
    assert result["error_category"] == CATEGORY_NO_DATA
    assert result.get("user_message"), "MarsNoData 应给出用户友好提示"


# ---------------------------------------------------------------------------
# 5. mock 故障注入：QueueLimitedError 与真实形态一致
# ---------------------------------------------------------------------------
def test_mock_queue_limited_error_matches_production_shape():
    """mock 的 QueueLimitedError 必须与线上形态一致（400 + temporarily limited）。"""
    exc = QueueLimitedError()
    assert exc.response.status_code == 400
    assert "temporarily limited" in str(exc).lower()
    assert "has been rejected" in str(exc).lower()
    assert is_retryable_error(exc) is True
    assert classify_error(exc) == CATEGORY_QUEUE_LIMITED
