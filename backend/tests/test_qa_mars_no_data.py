# -*- coding: utf-8 -*-
"""QA：MarsNoDataError 不可重试判定 + 用户友好中文诊断（T2）。

背景：真实下载中 10米风速 10si 等派生量在月度均值数据集不提供 →
CDS 返回 `MarsNoDataError` / "MARS returned no data"。根因有两层：
1. 代码 Bug（T1）：请求用了已弃用的 "format" 字段（新后端要求 "data_format"）。
2. 用户选择问题（T2）：变量×频率组合不提供数据，代码无法变出数据，
   只能给出清晰的中文诊断，并让该错误明确判为「不可重试」。

本文件覆盖：
- is_retryable_error 对 MarsNoData 类异常（含/不含 400 状态码文本）返回 False。
- _friendly_error 对同名异常返回含「变量名 + 逐小时(hourly) + 10u/10v」建议的中文串。
- _fetch_one_block（不可重试分支）返回失败结果 dict 含 user_message，且
  is_retryable_error 判为不可重试（status=failed）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from era5tool.acquisition import cds_channel
from era5tool.acquisition.cds_channel import _fetch_one_block, _friendly_error, is_retryable_error


# ---------------------------------------------------------------------------
# 假"真实"客户端：按 errors 顺序抛异常，耗尽后正常返回（复用生产签名）
# ---------------------------------------------------------------------------
class FakeRealClient:
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


def _block(key: str = "10si/2007/2007", variable: str = "10si") -> Dict[str, Any]:
    return {
        "key": key,
        "variable": variable,
        "dataset": "reanalysis-era5-single-levels-monthly-means",
        "request": {"product_type": ["reanalysis"], "variable": [variable],
                    "year": ["2007"], "data_format": "netcdf"},
        "rel_target": "reanalysis-era5-single-levels-monthly-means/10si/monthly/2007/2007.nc",
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
# 1. is_retryable_error 对 MarsNoData 类异常
# ---------------------------------------------------------------------------
def test_is_retryable_mars_no_data_with_400():
    """含 400 + 'returned no data' 的真实报错文本 → 不可重试。"""
    exc = Exception(
        "400 Client Error: Bad Request for url: ... "
        "The job has failed MARS returned no data, please check your selection.")
    assert is_retryable_error(exc) is False


def test_is_retryable_mars_no_data_without_status():
    """异常文本无 400 状态码、仅含 'returned no data'/'marsnodata' → 不可重试。"""
    exc = Exception("MarsNoDataError: MARS returned no data, please check your selection")
    assert is_retryable_error(exc) is False


def test_is_retryable_no_data_available_keyword():
    """'no data available' 关键词（无 400）→ 不可重试。"""
    exc = Exception("Your request returned: no data available for this selection")
    assert is_retryable_error(exc) is False


def test_is_retryable_plain_no_data_short_is_not_hit():
    """仅 'no data' 极短词（不含更长关键词）不误判为不可重试（保守按可重试）。"""
    exc = Exception("partial no data in cache, retrying may help")
    # 该文本不含 "returned no data"/"marsnodata"/"no data available" 等关键词，
    # 保守路径返回 True（不误伤瞬时故障）。
    assert is_retryable_error(exc) is True


# ---------------------------------------------------------------------------
# 2. _friendly_error 中文诊断
# ---------------------------------------------------------------------------
def test_friendly_error_contains_variable_and_suggestions():
    exc = Exception("MarsNoDataError: MARS returned no data, please check your selection")
    msg = _friendly_error(exc, {"variable": "10si"})
    assert "10si" in msg
    assert "逐小时" in msg and "hourly" in msg
    assert "10u" in msg and "10v" in msg
    assert "MarsNoDataError" in msg


def test_friendly_error_unknown_variable_falls_back():
    exc = Exception("MARS returned no data")
    msg = _friendly_error(exc, {"variable": "2m_temperature"})
    assert "2m_temperature" in msg


def test_friendly_error_non_no_data_returns_empty():
    """非 no-data 异常 → 返回空串（沿用原 error 文本）。"""
    exc = Exception("Connection reset by peer")
    assert _friendly_error(exc, {"variable": "10si"}) == ""


# ---------------------------------------------------------------------------
# 3. _fetch_one_block 失败结果含 user_message（端到端）
# ---------------------------------------------------------------------------
def test_fetch_one_block_mars_no_data_user_message(monkeypatch, tmp_path):
    """mock client 抛 MarsNoData → 失败结果 dict 含 user_message，
    is_retryable_error 判为不可重试，status=failed、retried=False。"""
    exc = Exception(
        "400 Client Error: Bad Request ... "
        "The job has failed MARS returned no data, please check your selection.")
    fake = FakeRealClient(errors=[exc])
    block = _block()
    result = _run_block(block, _real_cfg(retry_max=3), fake, monkeypatch, tmp_path)

    assert result["status"] == "failed"
    assert result["retried"] is False
    assert "error" in result and result["error"]
    assert "user_message" in result
    assert "10si" in result["user_message"]
    assert "逐小时" in result["user_message"]
    # 不可重试 → 只尝试一次，不 sleep
    assert len(fake.calls) == 1
    assert is_retryable_error(exc) is False


def test_fetch_one_block_user_message_absent_for_unknown_error(monkeypatch, tmp_path):
    """非 no-data 异常 → 失败结果 dict 不应含 user_message 键。"""
    exc = Exception("Connection reset by peer")
    fake = FakeRealClient(errors=[exc])
    block = _block()
    result = _run_block(block, _real_cfg(retry_max=1), fake, monkeypatch, tmp_path)

    assert result["status"] == "failed"
    assert "user_message" not in result
