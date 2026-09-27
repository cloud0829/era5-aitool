# -*- coding: utf-8 -*-
"""QA 独立回归验证（fresh eyes）：两项修复的防回归独立取证。

本文件刻意独立于 test_qa_progress_and_blocks.py，逐项复核主理人/工程师
声称的修复语义，不依赖既有套件结论：

A. 下载进度实时化
   - 运行期间 task.json 的 progress 单调递增（落盘实时生效）
   - 收到过 {"type":"task"} WS 事件（events.jsonl 取证）
   - failed 块不虚增 progress（progress 只按 done 算）
   - 断点续传 skipped 块计入 done 基数（progress 分子含 skipped）

B. 每块只下载自己的变量
   - Normalizer→prepare_blocks 每块 request["variable"]==[b["variable"]]
   - 无两个块拥有完全相同的请求（不再重复提交）
   - build_cds_request 缺省仍用 schema.variables（默认行为不变）
"""
from __future__ import annotations

import json
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from era5tool.acquisition.cds_channel import CdsChannel, _make_client
from era5tool.acquisition.cds_request import build_cds_request
from era5tool.acquisition.mock_client import (FakeCdsClient, RetryableError)
from era5tool.config.schema import Area, RequestSchema, Timerange
from era5tool.config.settings import Settings
from era5tool.core.events import EventBroker, TaskEventBus
from era5tool.core.normalizer import Normalizer
from era5tool.core.orchestrator import Orchestrator
from era5tool.core.resumable import ResumableStore
from era5tool.models.task import Task

DATASET = "reanalysis-era5-single-levels"


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
    """独立配置/数据目录（与 conftest 隔离，避免共享测试目录）。"""
    monkeypatch.delenv("ERA5_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ERA5_DATA_DIR", raising=False)
    s = Settings.load(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data")
    s.download.mock = True
    s.download.cds_max_workers = 2
    # 显式月粒度：保持 e2e 块数小（2 变量 × 12 月 = 24 块），不触发 day 切块
    s.download.chunk_granularity = "month"
    return s


def _slow_cfg(self) -> Dict[str, Any]:
    """慢速 mock：保证运行期间有可轮询的时间窗。"""
    d = self.settings.download
    return {
        "mock": True, "mock_delay": 0.35, "fail_rate": 0.0, "seed": 7,
        "retry_max": d.retry_max, "backoff_base": d.backoff_base,
        "backoff_factor": d.backoff_factor, "backoff_max": d.backoff_max,
        "backoff_jitter": d.backoff_jitter,
    }


# ---------------------------------------------------------------------------
# B. 变量作用域（独立取证）
# ---------------------------------------------------------------------------
def test_independent_each_block_own_variable(tmp_path, monkeypatch):
    """Normalizer→prepare_blocks：2 变量 × 12 月 = 24 块，每块 request 仅含自己的变量。"""
    channel = CdsChannel(_isolated_settings(tmp_path, monkeypatch))
    cds_req = Normalizer(granularity="month").normalize(_schema())
    blocks = channel.prepare_blocks(cds_req)
    assert len(blocks) == 24
    for b in blocks:
        assert b["request"]["variable"] == [b["variable"]], \
            f"块 {b['key']} 请求应只含 {b['variable']}，实际 {b['request']['variable']}"


def test_independent_no_identical_requests(tmp_path, monkeypatch):
    """修复前同 (year,month) 不同变量的块请求完全相同 → 重复提交/下载翻倍。

    修复后：(variable, year, month) 标识全唯一，且没有任何两个块的请求 dict 相同。
    """
    channel = CdsChannel(_isolated_settings(tmp_path, monkeypatch))
    cds_req = Normalizer(granularity="month").normalize(_schema())  # 2 变量 × 1 年 × 12 月
    blocks = channel.prepare_blocks(cds_req)

    # 1) 每个 (variable, year, month) 组合唯一
    ids = [(b["variable"], b["year"], b["month"]) for b in blocks]
    assert len(set(ids)) == 24

    # 2) 请求 dict 全唯一（修复前 12 对同 (year,month) 块请求完全相同）
    reqs = [json.dumps(b["request"], sort_keys=True) for b in blocks]
    dup = [r for r, c in Counter(reqs).items() if c > 1]
    assert not dup, f"存在请求完全相同的重复块: {dup}"

    # 3) 12 个 (year,month) 组合，每个恰 2 块（每变量 1 块）→ 无整月重复
    ym = Counter((b["year"], b["month"]) for b in blocks)
    assert len(ym) == 12 and all(v == 2 for v in ym.values())


def test_independent_build_cds_request_default_variables():
    """缺省 variables → 仍用 schema.variables（兼容性：历史调用/测试不变）。"""
    schema = _schema()
    req = build_cds_request(schema, 2020, 6)
    assert req["variable"] == schema.variables
    assert req["variable"] == ["2m_temperature", "total_precipitation"]


def test_independent_build_cds_request_explicit_variables():
    """显式 variables → 请求只含该变量。"""
    schema = _schema()
    req = build_cds_request(schema, 2020, 6, variables=["2m_temperature"])
    assert req["variable"] == ["2m_temperature"]


def test_independent_build_cds_request_empty_variables_falls_back():
    """空列表 → 回退 schema.variables（保持默认语义）。"""
    schema = _schema()
    req = build_cds_request(schema, 2020, 6, variables=[])
    assert req["variable"] == schema.variables


# ---------------------------------------------------------------------------
# A. 实时进度（独立取证：落盘 + WS 事件 + 单调递增）
# ---------------------------------------------------------------------------
def _read_task_json(settings: Settings, task_id: str) -> Dict[str, Any]:
    """读取 task.json，容忍 Windows 下 os.replace 原子写期间的瞬时文件锁错误。

    编排线程用 tmp+os.replace 落盘；Windows 上读方可能在替换瞬间碰到
    PermissionError/OSError，属时序竞争而非数据错误，重试即可。
    """
    p = settings.tasks_dir / task_id / "task.json"
    last: Optional[Exception] = None
    for _ in range(100):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (PermissionError, OSError) as exc:
            last = exc
            time.sleep(0.02)
    raise AssertionError(f"读取 task.json 持续失败: {p}") from last


def test_independent_progress_monotonic_and_ws_event(tmp_path, monkeypatch):
    """mock 提交 2 变量 × 12 月任务，运行期间轮询 task.json：

    - progress 出现 >0 的中间值且单调不减（修复前恒为 0.0）；
    - events.jsonl 收到过 {"type":"task"} 事件且带实时 progress。
    """
    settings = _isolated_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(CdsChannel, "worker_cfg", _slow_cfg)
    orch = Orchestrator(settings, EventBroker())
    task = orch.submit(_schema(variables=["2m_temperature", "total_precipitation"]))

    seen: List[float] = []
    deadline = time.time() + 30
    while time.time() < deadline:
        raw = _read_task_json(settings, task.id)
        seen.append(float(raw["progress"]))
        if raw["status"] not in ("pending", "running"):
            break
        time.sleep(0.08)

    assert any(p > 0 for p in seen), \
        f"运行期间 progress 应出现 >0 中间值（落盘实时生效），实际 {seen}"
    assert seen[-1] == 1.0, f"最终 progress 应为 1.0，实际 {seen[-1]}"

    # 单调不减（运行中可能有重复值，但不允许回退）
    assert all(b >= a for a, b in zip(seen, seen[1:])), \
        f"progress 应单调不减，实际序列 {seen}"

    # WS 事件：events.jsonl 应有 type=task 实时事件
    events_path = settings.tasks_dir / task.id / "events.jsonl"
    task_evs: List[Dict[str, Any]] = []
    if events_path.is_file():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ev = json.loads(line)
            if ev.get("type") == "task" and ev.get("task_id") == task.id:
                task_evs.append(ev)
    assert task_evs, "events.jsonl 中应存在 type=task 实时事件"
    assert any(float(ev["progress"]) > 0 for ev in task_evs), \
        "type=task 事件应携带实时 progress>0"
    last = task_evs[-1]
    assert last["block_stats"]["done"] == last["block_stats"]["total"]
    assert last["block_stats"]["failed"] == 0


# ---------------------------------------------------------------------------
# A. 进度语义边界：failed 不虚增 progress（progress 只按 done 算）
# ---------------------------------------------------------------------------
# 说明：hourly 家族 normalize 固定按 变量×年×12 月 切块（timerange 只决定年份），
# 因此 2 变量 × 1 年 = 24 块；用全失败 fail_rate=1.0 验证失败块不计入 progress，
# 用桩 run_blocks（进程内直调 on_block_done 闭包）验证 done/failed 混合语义。


def _all_fail_cfg(self) -> Dict[str, Any]:
    d = self.settings.download
    return {
        "mock": True, "mock_delay": 0.01, "fail_rate": 1.0, "seed": 7,
        "retry_max": 1, "backoff_base": d.backoff_base,
        "backoff_factor": d.backoff_factor, "backoff_max": d.backoff_max,
        "backoff_jitter": d.backoff_jitter,
    }


def test_independent_failed_blocks_do_not_inflate_progress(tmp_path, monkeypatch):
    """全部块失败（fail_rate=1.0，真实子进程）：failed 计入 failed 但不计入 progress。"""
    settings = _isolated_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(CdsChannel, "worker_cfg", _all_fail_cfg)
    orch = Orchestrator(settings, EventBroker())
    task = orch.submit(_schema(variables=["2m_temperature"]))  # 1 变量 × 12 月 = 12 块

    deadline = time.time() + 20
    while time.time() < deadline:
        raw = _read_task_json(settings, task.id)
        if raw["status"] not in ("pending", "running"):
            break
        time.sleep(0.05)
    assert raw["status"] == "failed"
    assert raw["block_stats"]["failed"] == 12
    assert raw["block_stats"]["done"] == 0
    assert raw["progress"] == 0.0, \
        f"失败块不应计入 progress 分子，实际 progress={raw['progress']}"


def test_independent_failed_and_done_mixed_progress(tmp_path, monkeypatch):
    """桩 run_blocks（进程内直调真实 on_block_done 闭包）：2 done + 2 failed。

    progress == done/total == 0.5；failed 不虚增分子。
    """
    settings = _isolated_settings(tmp_path, monkeypatch)
    orch = Orchestrator(settings, EventBroker())

    def fake_run_blocks(task, blocks, store, bus, cancel_event=None,
                        on_block_done=None, settle_event=None):
        total = len(blocks)
        results: List[Dict[str, Any]] = []
        for i, b in enumerate(blocks, 1):
            status = "done" if i <= total // 2 else "failed"
            r = {"block": b["key"], "status": status, "attempts": 1,
                 "error": None if status == "done" else "HTTP 400 (mock)"}
            results.append(r)
            if on_block_done is not None:
                on_block_done(r, i, total)
        # 桩无真实进程池：同步置位 settle（与真实 run_blocks“正常路径结束后置位”对齐）
        if settle_event is not None:
            settle_event.set()
        return results

    monkeypatch.setattr(orch.channel, "run_blocks", fake_run_blocks)
    task = orch.submit(_schema(variables=["2m_temperature", "total_precipitation"],
                               start="2020-01-01", end="2020-12-31"))  # 24 块

    deadline = time.time() + 20
    while time.time() < deadline:
        raw = _read_task_json(settings, task.id)
        if raw["status"] not in ("pending", "running"):
            break
        time.sleep(0.05)
    total = raw["block_stats"]["total"]
    assert total == 24
    assert raw["status"] == "failed"
    assert raw["block_stats"]["failed"] == total // 2
    assert raw["block_stats"]["done"] == total // 2
    assert abs(raw["progress"] - 0.5) < 1e-6, \
        f"progress 应=done/total=0.5，实际 {raw['progress']}"


# ---------------------------------------------------------------------------
# A. 断点续传：skipped 计入 done 基数
# ---------------------------------------------------------------------------
def test_independent_resume_skipped_counts_in_progress_base(tmp_path, monkeypatch):
    """paused 任务 resume：done 块被跳过，progress 从 skipped/total 起步。

    断点续传的进度分子必须包含 skipped（done 基数），否则会出现
    progress 回退/从 0 重算。
    """
    settings = _isolated_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(CdsChannel, "worker_cfg", _slow_cfg)
    orch = Orchestrator(settings, EventBroker())

    # 提交慢速任务（2 变量 × 2 年 = 48 块），等若干块 done 后取消 → paused
    task = orch.submit(_schema(variables=["2m_temperature", "total_precipitation"],
                               start="2020-01-01", end="2021-12-31"))
    deadline = time.time() + 20
    done_observed = False
    while time.time() < deadline:
        raw = _read_task_json(settings, task.id)
        if raw["block_stats"]["done"] > 0:
            done_observed = True
            break
        time.sleep(0.1)
    assert done_observed, "预期运行期至少完成 1 块（慢速 mock + 轮询）"
    orch.cancel(task.id)
    deadline = time.time() + 15
    while time.time() < deadline:
        raw = _read_task_json(settings, task.id)
        if raw["status"] == "paused":
            break
        time.sleep(0.05)
    assert raw["status"] == "paused"
    skipped = raw["block_stats"]["done"]
    total = raw["block_stats"]["total"]
    assert 0 < skipped < total, f"预期部分块 done（skipped={skipped}, total={total}）"

    # resume：慢速 → 运行期间能观察到 progress >= skipped/total（含基数）
    orch.resume(task.id)
    seen: List[float] = []
    deadline = time.time() + 25
    while time.time() < deadline:
        raw = _read_task_json(settings, task.id)
        seen.append(float(raw["progress"]))
        if raw["status"] not in ("pending", "running"):
            break
        time.sleep(0.06)
    base = skipped / total
    assert raw["status"] == "success"
    assert raw["progress"] == 1.0
    assert min(seen) >= base - 1e-6, \
        f"resume 期间 progress 应从 skipped 基数起步（>= {base}），实际 {seen}"


# ---------------------------------------------------------------------------
# A. on_block_done 回调（独立取证）
# ---------------------------------------------------------------------------
def test_independent_on_block_done_called_per_completion(tmp_path, monkeypatch):
    """run_blocks 带回调：每完成一块调用一次，completed 递增，全成功。"""
    settings = _isolated_settings(tmp_path, monkeypatch)
    channel = CdsChannel(settings)
    task = Task(id="t_ind_cb")
    task_dir = settings.tasks_dir / task.id
    task_dir.mkdir(parents=True, exist_ok=True)
    store = ResumableStore(task_dir, settings)
    broker = EventBroker()
    bus = TaskEventBus(task.id, task_dir, broker)
    blocks = [
        {"key": "2m_temperature/2020/01", "dataset": DATASET, "variable": "2m_temperature",
         "year": 2020, "month": 1,
         "request": {"variable": ["2m_temperature"], "year": ["2020"], "month": ["01"]},
         "rel_target": f"{DATASET}/2m_temperature/hourly/2020/01.nc"},
        {"key": "2m_temperature/2020/02", "dataset": DATASET, "variable": "2m_temperature",
         "year": 2020, "month": 2,
         "request": {"variable": ["2m_temperature"], "year": ["2020"], "month": ["02"]},
         "rel_target": f"{DATASET}/2m_temperature/hourly/2020/02.nc"},
        {"key": "total_precipitation/2020/01", "dataset": DATASET,
         "variable": "total_precipitation", "year": 2020, "month": 1,
         "request": {"variable": ["total_precipitation"], "year": ["2020"], "month": ["01"]},
         "rel_target": f"{DATASET}/total_precipitation/hourly/2020/01.nc"},
        {"key": "total_precipitation/2020/02", "dataset": DATASET,
         "variable": "total_precipitation", "year": 2020, "month": 2,
         "request": {"variable": ["total_precipitation"], "year": ["2020"], "month": ["02"]},
         "rel_target": f"{DATASET}/total_precipitation/hourly/2020/02.nc"},
    ]
    calls: List[tuple] = []

    def cb(result: Dict[str, Any], completed: int, total: int) -> None:
        calls.append((result["status"], completed, total))

    bus.start()
    try:
        results = channel.run_blocks(task, blocks, store, bus, None, on_block_done=cb)
    finally:
        bus.stop()

    assert len(results) == 4
    assert all(r["status"] == "done" for r in results)
    assert len(calls) == 4, f"每块应回调一次，实际 {len(calls)}"
    assert [c[1] for c in calls] == [1, 2, 3, 4]
    assert all(c[2] == 4 for c in calls)
    assert all(c[0] == "done" for c in calls)
