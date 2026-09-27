# -*- coding: utf-8 -*-
"""QA：真实下载 P0 根因回归——retrieve 前必须创建缓存目录（防再犯）。

背景（用户实测铁证）：
- 真实模式任务 24 块：6x `[Errno 2] No such file or directory: 'D:\\...\\cache\\...'`
  + 18x `A process in the process pool was terminated abruptly...`。
- 根因 A（确定，主因）：真实 cdsapi.Client.retrieve 写 target 时【不创建父目录】
  （cdsapi 0.7.7 Result._download → open(target, "wb")），而 mock 的
  FakeCdsClient.retrieve 内部自带 ensure_dir → mock 测试全绿掩盖了该 bug。
- 且 is_retryable_error 对 "no such file" 文本无匹配 → 保守判 True（可重试）
  → 每块退避重试 3 次（30s/60s/120s）→ "重试耗尽"，表现为下载慢且下载不了。

覆盖（与任务清单测试代码 A 对齐）：
1. 模拟真实 cdsapi（open 不建目录）：修复后父目录缺失也能下载成功
   （文件写入、status=done、store.mark_done 生效、结果不含 FileNotFoundError）。
2. is_retryable_error：FileNotFoundError 文本 → False（不再误判可重试）。
3. 分类表不回归：429/5xx 仍 True、400/404 仍 False。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
import requests

from era5tool.acquisition import cds_channel
from era5tool.acquisition.cds_channel import _fetch_one_block, is_retryable_error
from era5tool.core.resumable import ResumableStore


# ---------------------------------------------------------------------------
# 模拟真实 cdsapi：retrieve 直接 open(target, "wb") 写文件，不建父目录
# ---------------------------------------------------------------------------
class RealLikeClient:
    """模拟真实 cdsapi.Client.retrieve(name, request, target)（无网络）。

    与真实 cdsapi 0.7.7 行为一致：Result._download → open(target, "wb")，
    【不 ensure_dir】。因此若调用方（_fetch_one_block）未先建目录，父目录缺失时
    会抛 FileNotFoundError——正是线上 6 块 [Errno 2] No such file 的触发路径。
    类放在模块顶层（可 pickle），兼容 Windows spawn 场景。
    """

    def __init__(self, errors: Optional[List[BaseException]] = None):
        self.errors = list(errors or [])
        self.calls: List[Dict[str, Any]] = []

    def retrieve(self, name: str, request: Optional[Dict[str, Any]] = None,
                 target: Optional[str] = None) -> Dict[str, Any]:
        self.calls.append({"name": name, "request": request, "target": target})
        if self.errors:
            raise self.errors.pop(0)
        if target is None:
            raise TypeError("retrieve(target=...) 必填")
        # 与真实 cdsapi 一致：不创建父目录，直接写文件
        with open(target, "wb") as f:
            f.write(b"fake-netcdf")
        return {"status": "done", "target": target}


def _http_error(status_code: int, text: str) -> requests.exceptions.HTTPError:
    """构造带 .response.status_code 的 requests.HTTPError。"""
    resp = SimpleNamespace(status_code=status_code)
    return requests.exceptions.HTTPError(f"{status_code} {text}", response=resp)


def _cfg(retry_max: int = 3) -> Dict[str, Any]:
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
           key: str = "10m_u_component_of_wind/2025/01") -> Dict[str, Any]:
    var, year, month = key.split("/")
    return {
        "key": key,
        "dataset": dataset,
        "request": {"product_type": ["reanalysis"],
                    "variable": [var], "year": [year], "month": [month],
                    "target": "ignored"},
        "rel_target": f"{dataset}/{var}/hourly/{year}/{month}.nc",
    }


# ---------------------------------------------------------------------------
# 前置：测试夹具确实模拟了真实 cdsapi（open 不建目录 → FileNotFoundError）
# ---------------------------------------------------------------------------
def test_real_like_client_mimics_cdsapi_no_mkdir(tmp_path):
    """前置校验：RealLikeClient 与真实 cdsapi 一致——父目录不存在时 open 抛 FileNotFoundError。

    这证明"修复前必挂"的前提成立；若该夹具行为改变，后续测试断言需要同步审视。
    """
    fake = RealLikeClient()
    target = tmp_path / "a" / "b" / "c" / "01.nc"
    with pytest.raises(FileNotFoundError):
        fake.retrieve("reanalysis-era5-single-levels",
                      {"variable": ["x"]}, str(target))


# ---------------------------------------------------------------------------
# 1. P0 回归：父目录原本不存在 → 修复后下载成功
# ---------------------------------------------------------------------------
def test_fetch_one_block_ensures_cache_dir_before_retrieve(monkeypatch, tmp_path):
    """P0 回归：块 target 父目录原本不存在 → 修复后下载成功。

    断言：文件真实写入、status=done、store.mark_done 生效、结果不含
    FileNotFoundError、fake 只被调用 1 次（目录问题不应触发重试）。
    """
    fake = RealLikeClient()
    monkeypatch.setattr(cds_channel, "_make_client", lambda cfg_: fake)
    block = _block()
    task_dir = tmp_path / "task"
    cache_dir = tmp_path / "cache"
    task_dir.mkdir(exist_ok=True)
    # 关键前提：cache_dir 下嵌套目录【不存在】（prepare_blocks 只生成路径不建目录）
    rel_parent = Path(block["rel_target"]).parent
    assert not (cache_dir / rel_parent).exists(), "测试前提：目标父目录不应预存在"

    result = _fetch_one_block((block, _cfg(), str(task_dir), str(cache_dir), "t_dir"))

    # 下载成功
    assert result["status"] == "done"
    assert result["attempts"] == 1
    # 文件真实写入（不是只有状态计数）
    target = cache_dir / block["rel_target"]
    assert target.is_file()
    assert target.read_bytes() == b"fake-netcdf"
    # ensure_dir 创建了父目录
    assert (cache_dir / rel_parent).is_dir()
    # store.mark_done 生效：.done 标记存在且内容为 "done"（mark_failed 写的不是 done）
    store = ResumableStore(task_dir, None)
    assert store.is_done(block["key"]) is True
    assert store.marker_path(block["key"]).read_text(encoding="utf-8").strip() == "done"
    # 结果（含编排层据此写 manifest 的字段）无 FileNotFoundError
    assert "No such file" not in json.dumps(result, ensure_ascii=False)
    assert "Errno" not in json.dumps(result, ensure_ascii=False)
    # 目录问题不应触发重试（只调用 1 次）
    assert len(fake.calls) == 1
    # 调用参数仍为正确签名 (name=str, request=dict, target=绝对路径)
    call = fake.calls[0]
    assert call["name"] == block["dataset"] == "reanalysis-era5-single-levels"
    assert isinstance(call["request"], dict)
    assert isinstance(call["target"], str) and os.path.isabs(call["target"])
    assert call["target"].endswith(block["rel_target"])


# ---------------------------------------------------------------------------
# 2. FileNotFoundError 文本 → 不可重试（避免 30/60/120s 无效退避）
# ---------------------------------------------------------------------------
def test_is_retryable_error_file_not_found_text_is_false():
    """FileNotFoundError（真实 cdsapi 缺目录时报文）→ 不可重试。"""
    # 真实用户报错文本（Windows 路径，原样复现）
    exc = FileNotFoundError(2, "No such file or directory",
                            r"D:\Desktop\era5-AItool\data\cache\reanalysis-era5-single-levels\10m_u_component_of_wind\hourly\2025\01.nc")
    assert is_retryable_error(exc) is False
    # 各等价写法
    assert is_retryable_error(OSError(2, "No such file or directory")) is False
    assert is_retryable_error(
        FileNotFoundError("[Errno 2] No such file or directory")) is False
    assert is_retryable_error(OSError(2, "file does not exist")) is False
    assert is_retryable_error(
        FileNotFoundError("[Errno 2] No such file or directory: 'D:/x/y/01.nc'")) is False


def test_fetch_one_block_file_not_found_fails_immediately(monkeypatch, tmp_path):
    """即使目录仍缺失（如只读/权限问题）：FileNotFoundError 判不可重试 → 立即失败。

    不 sleep、不重试、只调用 1 次（修复前会退避 30/60/120s 后仍失败）。
    """
    sleeps: List[float] = []

    def spy_sleep(secs: float) -> None:
        sleeps.append(secs)

    monkeypatch.setattr(cds_channel.time, "sleep", spy_sleep)
    fake = RealLikeClient(errors=[
        FileNotFoundError(2, "No such file or directory",
                          r"D:\x\cache\reanalysis-era5-single-levels\10m_u_component_of_wind\hourly\2025\01.nc"),
    ])
    monkeypatch.setattr(cds_channel, "_make_client", lambda cfg_: fake)
    task_dir = tmp_path / "task"
    cache_dir = tmp_path / "cache"
    task_dir.mkdir(exist_ok=True)
    cache_dir.mkdir(exist_ok=True)

    result = _fetch_one_block((_block(), _cfg(retry_max=3), str(task_dir),
                               str(cache_dir), "t_x"))

    assert result["status"] == "failed"
    assert result["attempts"] == 1
    assert result["retried"] is False
    assert sleeps == [], "不可重试不应触发退避 sleep"
    assert len(fake.calls) == 1
    assert "No such file" in result["error"] or "Errno" in result["error"]


# ---------------------------------------------------------------------------
# 3. 分类表不回归：429/5xx 仍 True、400/404 仍 False
# ---------------------------------------------------------------------------
def test_is_retryable_error_classification_table_no_regression():
    # 可重试：429/5xx（瞬时）仍 True
    assert is_retryable_error(_http_error(429, "Too Many Requests")) is True
    assert is_retryable_error(_http_error(500, "Internal Server Error")) is True
    assert is_retryable_error(_http_error(502, "Bad Gateway")) is True
    assert is_retryable_error(_http_error(503, "Service Unavailable")) is True
    assert is_retryable_error(_http_error(504, "Gateway Timeout")) is True
    # 不可重试：400/401/403/404 仍 False
    assert is_retryable_error(_http_error(400, "Bad Request")) is False
    assert is_retryable_error(_http_error(401, "Unauthorized")) is False
    assert is_retryable_error(_http_error(403, "Forbidden")) is False
    assert is_retryable_error(_http_error(404, "Not Found")) is False
    # mock 自有异常不回归
    from era5tool.acquisition.mock_client import NonRetryableError, RetryableError
    assert is_retryable_error(RetryableError("HTTP 429 (mock)")) is True
    assert is_retryable_error(NonRetryableError("Bad request (mock)")) is False
