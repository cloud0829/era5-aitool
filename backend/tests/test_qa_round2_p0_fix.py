# -*- coding: utf-8 -*-
"""QA Round-2 独立回归（fresh eyes）：P0 下载修复独立取证。

刻意不依赖工程师既有套件结论，围绕本次 P0 修复的四个可验证面独立补测：

1. e2e mock：Orchestrator→run_blocks→_fetch_one_block→FakeCdsClient 真实子进程链路，
   任务 SUCCESS；缓存目录下真实产物文件存在且目录层级完整（dataset/var/freq/year/leaf）
   —— 证明 prepare_blocks 的 rel_target 目录被 ensure_dir 创建（用户可见结果）。
2. is_retryable_error 边界：FileNotFoundError("[Errno 2] No such file...") → False
   （不再被误判可重试）；requests 异常带 429/5xx → True（不回归）。
3. 取消路径：run_blocks 运行中 cancel → 快速返回（验证 shutdown(wait=False) 生效，
   不隐式 wait=True 卡数分钟）；编排层任务在合理时间内转 paused。
4. diagnose_download.py：--mock 模式跑通；check_rc 兼容无冒号纯 token（LegacyClient）。

说明：本文件只在进程内构造数据/配置，worker 子进程运行的是生产代码本身。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
import requests

from era5tool.acquisition.cds_channel import CdsChannel, is_retryable_error
from era5tool.acquisition.mock_client import (NonRetryableError, RetryableError)
from era5tool.config.schema import Area, RequestSchema, Timerange
from era5tool.config.settings import Settings
from era5tool.core.events import EventBroker, TaskEventBus
from era5tool.core.orchestrator import Orchestrator
from era5tool.core.resumable import ResumableStore
from era5tool.models.task import Task

DATASET = "reanalysis-era5-single-levels"
BACKEND_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# helpers（与既有套件同构，但独立于其断言）
# ---------------------------------------------------------------------------
def _schema(variables=None, family: str = "era5-single",
            start: str = "2020-01-01", end: str = "2020-12-31") -> RequestSchema:
    return RequestSchema(
        dataset_family=family,
        variables=variables or ["2m_temperature", "total_precipitation"],
        pressure_levels=None,
        timerange=Timerange(start=start, end=end),
        area=Area(west=118, south=29, east=123, north=34),
    )


def _isolated_settings(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.delenv("ERA5_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ERA5_DATA_DIR", raising=False)
    s = Settings.load(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data")
    s.download.mock = True
    s.download.cds_max_workers = 2
    # 显式月粒度：保持 e2e 块数小（2 变量 × 12 月 = 24 块），不触发 day 切块
    s.download.chunk_granularity = "month"
    return s


def _cfg(delay: float, fail_rate: float = 0.0, retry_max: int = 3):
    """按当前 settings 生成 worker cfg（慢/快可调）。"""
    def _make(self):
        d = self.settings.download
        return {
            "mock": True, "mock_delay": delay, "fail_rate": fail_rate, "seed": 7,
            "retry_max": retry_max, "backoff_base": d.backoff_base,
            "backoff_factor": d.backoff_factor, "backoff_max": d.backoff_max,
            "backoff_jitter": d.backoff_jitter,
        }
    return _make


def _read_task_json(settings: Settings, task_id: str) -> Dict[str, Any]:
    p = settings.tasks_dir / task_id / "task.json"
    last: Optional[Exception] = None
    for _ in range(200):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (PermissionError, OSError) as exc:  # Windows os.replace 原子写瞬时锁
            last = exc
            time.sleep(0.02)
    raise AssertionError(f"读取 task.json 持续失败: {p}") from last


def _wait_terminal(settings: Settings, task_id: str, timeout: float = 40.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = _read_task_json(settings, task_id)
        if raw["status"] not in ("pending", "running"):
            return raw
        time.sleep(0.05)
    raise TimeoutError(f"task {task_id} 未在 {timeout}s 内进入终态: {raw['status']}")


def _monthly_blocks(n_months: int = 12, var: str = "2m_temperature") -> List[Dict[str, Any]]:
    return [
        {"key": f"{var}/2020/{m:02d}", "dataset": DATASET, "variable": var,
         "year": 2020, "month": m,
         "request": {"variable": [var], "year": ["2020"], "month": [f"{m:02d}"]},
         "rel_target": f"{DATASET}/{var}/hourly/2020/{m:02d}.nc"}
        for m in range(1, n_months + 1)
    ]


# ---------------------------------------------------------------------------
# 1. e2e mock：SUCCESS + 缓存目录层级完整（prepare_blocks rel_target 被 ensure_dir 创建）
# ---------------------------------------------------------------------------
def test_round2_mock_e2e_success_and_cache_hierarchy(tmp_path, monkeypatch):
    """2 变量 × 1 年 = 24 块 mock 任务 → SUCCESS；每个 rel_target 的产物文件真实存在。

    这直接复现用户场景（24 块）：若 _fetch_one_block 未先 ensure_dir 父目录，
    产物文件不可能存在于 dataset/var/freq/year/leaf 深层路径。
    """
    settings = _isolated_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(CdsChannel, "worker_cfg", _cfg(delay=0.02, fail_rate=0.0))
    orch = Orchestrator(settings, EventBroker())
    task = orch.submit(_schema(
        variables=["2m_temperature", "total_precipitation"],
        start="2020-01-01", end="2020-12-31"))

    raw = _wait_terminal(settings, task.id)
    assert raw["status"] == "success", \
        f"mock 任务应 success，实际 {raw['status']} error={raw.get('error')}"
    assert raw["block_stats"]["total"] == 24
    assert raw["block_stats"]["done"] == 24
    assert raw["block_stats"]["failed"] == 0

    # 缓存目录：每块 rel_target 产物文件存在 + 目录层级完整
    blocks = orch.get(task.id).params["blocks"]
    assert len(blocks) == 24
    cache = settings.cache_dir
    missing: List[str] = []
    for b in blocks:
        rel = b["rel_target"]
        parts = Path(rel).parts
        if len(parts) != 5:  # dataset/var/freq/year/leaf
            missing.append(f"{rel} (层级={len(parts)})")
            continue
        target = cache / rel
        if not target.is_file() or target.stat().st_size == 0:
            missing.append(str(target))
        elif not target.parent.is_dir():
            missing.append(f"{target} (父目录缺失)")
    assert not missing, f"缓存产物缺失/层级异常: {missing[:10]}"

    # task.result.files 均为真实存在的绝对路径（编排层兼容性）
    res = orch.get(task.id).result or {}
    files = res.get("files", [])
    assert len(files) == 24, f"result.files 应有 24 个，实际 {len(files)}"
    absent = [f for f in files if not Path(f).is_file()]
    assert not absent, f"result.files 中不存在的路径: {absent[:5]}"


# ---------------------------------------------------------------------------
# 2. is_retryable_error 边界（P0 关键词不回归 + 状态码不回归）
# ---------------------------------------------------------------------------
def test_round2_is_retryable_file_not_found_false():
    """FileNotFoundError [Errno 2]（真实 cdsapi 缺目录报文）→ 不可重试。"""
    exc = FileNotFoundError("[Errno 2] No such file or directory: 'D:\\x\\y\\01.nc'")
    assert is_retryable_error(exc) is False
    # Windows 原生构造等价形式
    exc2 = FileNotFoundError(2, "No such file or directory",
                             r"D:\Desktop\era5-AItool\data\cache\reanalysis-era5-single-levels\10m_u_component_of_wind\hourly\2025\01.nc")
    assert is_retryable_error(exc2) is False
    assert is_retryable_error(OSError(2, "file does not exist")) is False
    assert is_retryable_error(OSError(2, "No such file or directory")) is False


def test_round2_is_retryable_status_codes_no_regression():
    """429/5xx → True；400/404 → False（分类表不回归）。"""
    def http_err(code: int) -> requests.exceptions.HTTPError:
        return requests.exceptions.HTTPError(f"{code} x", response=SimpleNamespace(status_code=code))

    for code in (429, 500, 502, 503, 504):
        assert is_retryable_error(http_err(code)) is True, f"{code} 应可重试"
    for code in (400, 401, 403, 404):
        assert is_retryable_error(http_err(code)) is False, f"{code} 应不可重试"

    # cdsapi.ClientError 形态（带 .response）同规则
    class _CdsErr(Exception):
        pass
    e = _CdsErr("HTTP 503 Service Unavailable")
    e.response = SimpleNamespace(status_code=503)
    assert is_retryable_error(e) is True
    e2 = _CdsErr("HTTP 404 Not Found")
    e2.response = SimpleNamespace(status_code=404)
    assert is_retryable_error(e2) is False

    # mock 自有异常不回归
    assert is_retryable_error(RetryableError("HTTP 429 (mock)")) is True
    assert is_retryable_error(NonRetryableError("Bad request (mock)")) is False


# ---------------------------------------------------------------------------
# 3. 取消路径：run_blocks 快速返回（shutdown wait=False 生效）
# ---------------------------------------------------------------------------
def test_round2_cancel_run_blocks_returns_quickly(tmp_path, monkeypatch):
    """12 块 × mock_delay=1.2s × 2 workers（完整跑约 7.2s）。

    cancel 后 run_blocks 必须快速返回（<3s）：若旧实现隐式 shutdown(wait=True)
    会等全部 12 块完成（~7s），本测试即可暴露。
    """
    settings = _isolated_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(CdsChannel, "worker_cfg", _cfg(delay=1.2, fail_rate=0.0))
    channel = CdsChannel(settings)

    task = Task(id="t_round2_cancel_fast")
    task_dir = settings.tasks_dir / task.id
    task_dir.mkdir(parents=True, exist_ok=True)
    store = ResumableStore(task_dir, settings)
    broker = EventBroker()
    bus = TaskEventBus(task.id, task_dir, broker)
    blocks = _monthly_blocks(12)

    cancel_event = threading.Event()
    out: Dict[str, Any] = {}

    def runner():
        out["results"] = channel.run_blocks(task, blocks, store, bus, cancel_event)

    t = threading.Thread(target=runner, daemon=True)
    bus.start()
    try:
        t.start()
        time.sleep(1.3)  # 等第一批 future 完成（~1.2s）再 cancel，确保已进入下载
        t0 = time.time()
        cancel_event.set()
        t.join(timeout=10)
        elapsed = time.time() - t0
        assert not t.is_alive(), "cancel 后 run_blocks 10s 内未返回 → 取消挂死"
        assert elapsed < 3.0, \
            f"cancel 后应快速返回（shutdown wait=False），实际 {elapsed:.2f}s（完整跑约 7.2s）"
        results = out.get("results", [])
        cancelled = [r for r in results if r["status"] == "cancelled"]
        # 未启动 future 会被 cancel() 收集为 cancelled（计数≥1 即可证取消路径生效；
        # 精确数量取决于 Windows spawn 时序——已完成的 done 块结果会被处理、正在跑的
        # 块不会被收集，均由 .done 标记在 resume 时兜底，属设计内取消语义）
        assert len(cancelled) >= 1, \
            f"取消时应收集未启动块为 cancelled，实际 {len(cancelled)}"
        assert len(results) <= 12
        # 每个结果 dict 均含 orchestrator 依赖的字段
        for r in results:
            assert "block" in r and "status" in r, f"结果缺 block/status: {r}"
            assert r["status"] in ("done", "failed", "cancelled"), r
        # 已完成的块应有 .done 标记（resume 可跳过，不重复下载）
        done_keys = [r["block"] for r in results if r["status"] == "done"]
        for k in done_keys:
            assert store.is_done(k) is True, f"done 块 {k} 应有 .done 标记"
    finally:
        bus.stop()


def test_round2_orchestrator_cancel_paused_within_timeout(tmp_path, monkeypatch):
    """编排层：48 块慢速任务运行中 cancel → 60s 内转 paused（不挂死）。"""
    settings = _isolated_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(CdsChannel, "worker_cfg", _cfg(delay=0.5, fail_rate=0.0))
    orch = Orchestrator(settings, EventBroker())
    task = orch.submit(_schema(
        variables=["2m_temperature", "total_precipitation"],
        start="2020-01-01", end="2021-12-31"))  # 2 变量 × 2 年 = 48 块

    # 等进入 running 且至少 1 块 done（确保下载线程已真正开工）
    deadline = time.time() + 20
    while time.time() < deadline:
        raw = _read_task_json(settings, task.id)
        if raw["status"] not in ("pending", "running"):
            break
        if raw["block_stats"]["done"] >= 1:
            break
        time.sleep(0.1)
    assert raw["status"] in ("pending", "running"), f"任务过早终态: {raw['status']}"

    t0 = time.time()
    orch.cancel(task.id)
    deadline = time.time() + 60
    while time.time() < deadline:
        raw = _read_task_json(settings, task.id)
        if raw["status"] not in ("pending", "running"):
            break
        time.sleep(0.05)
    elapsed = time.time() - t0
    assert raw["status"] == "paused", f"cancel 后应 paused，实际 {raw['status']} error={raw.get('error')}"
    assert elapsed < 60.0, f"cancel→paused 应在 60s 内完成（旧实现可能卡数分钟），实际 {elapsed:.2f}s"
    print(f"[round2-evidence] orchestrator cancel→paused latency = {elapsed:.2f}s")


# ---------------------------------------------------------------------------
# 4. diagnose_download.py：--mock 跑通 + check_rc 兼容无冒号 token
# ---------------------------------------------------------------------------
def _load_diagnose():
    path = BACKEND_ROOT / "scripts" / "diagnose_download.py"
    spec = importlib.util.spec_from_file_location("diagnose_download_qa", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Python 3.13 的 dataclasses 在解析 @dataclass 时会查
    # sys.modules[cls.__module__]；若模块未先注册进 sys.modules 会抛
    # 'NoneType' object has no attribute '__dict__'。标准 importlib 用法：
    # exec_module 前先把模块注册进 sys.modules（不影响脚本直接运行时 __main__ 路径）。
    sys.modules[mod.__name__] = mod
    spec.loader.exec_module(mod)
    return mod


def test_round2_diagnose_mock_runs():
    """diagnose_download.py --mock 离线跑通（不依赖真实网络）。"""
    script = BACKEND_ROOT / "scripts" / "diagnose_download.py"
    r = subprocess.run([sys.executable, str(script), "--mock"],
                       capture_output=True, text=True, timeout=60,
                       cwd=str(BACKEND_ROOT))
    assert r.returncode == 0, f"退出码 {r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    assert "CDS 下载诊断报告" in r.stdout
    assert "mock 离线验证" in r.stdout
    assert "FakeCdsClient" in r.stdout


def test_round2_diagnose_check_rc_token_and_uid(tmp_path, monkeypatch):
    """check_rc：无冒号纯 token → LegacyClient；含冒号 UID:KEY → 旧式 Client。"""
    diag = _load_diagnose()

    # 新 CDS v2 纯 token（当前真实凭据形态：36 位无冒号）
    rc_token = tmp_path / "rc_token"
    rc_token.write_text(
        "url: https://cds.climate.copernicus.eu/api\nkey: " + "a" * 36 + "\n",
        encoding="utf-8")
    monkeypatch.setattr(diag, "RC_PATH", str(rc_token))
    ok, msg = diag.check_rc()
    assert ok, f"无冒号 token 应判定 OK: {msg}"
    assert "LegacyClient" in msg and "token" in msg.lower(), f"应识别 LegacyClient 模式: {msg}"

    # 旧式 UID:APIKEY
    rc_uid = tmp_path / "rc_uid"
    rc_uid.write_text(
        "url: https://cds.climate.copernicus.eu/api\nkey: 123456:deadbeefcafe\n",
        encoding="utf-8")
    monkeypatch.setattr(diag, "RC_PATH", str(rc_uid))
    ok, msg = diag.check_rc()
    assert ok, f"UID:KEY 应判定 OK: {msg}"
    assert "UID:APIKEY" in msg and "cdsapi.Client" in msg

    # 缺失文件 / 缺 key
    monkeypatch.setattr(diag, "RC_PATH", str(tmp_path / "nope"))
    ok, _ = diag.check_rc()
    assert not ok
    rc_bad = tmp_path / "rc_bad"
    rc_bad.write_text("url: https://cds.climate.copernicus.eu/api\n", encoding="utf-8")
    monkeypatch.setattr(diag, "RC_PATH", str(rc_bad))
    ok, _ = diag.check_rc()
    assert not ok
