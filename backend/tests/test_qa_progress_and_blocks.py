# -*- coding: utf-8 -*-
"""QA：多变量按块隔离下载 + 实时进度回调（两个 Bug 的防回归）。

背景：
- Bug A（进度条卡住）：orchestrator 只在 submit/resume 置 progress=0、
  成功置 1.0，运行期从不更新/落盘 → GET 永远返回 0。修复后每个块完成时
  通过 on_block_done 回调实时累加 block_stats、更新 progress、落盘
  task.json 并 emit {"type":"task"} WS 事件。
- Bug B（重复下载）：prepare_blocks 用 schema.variables（全部变量）构造每个
  块的请求 → 每块都下载全部变量（2 变量任务 = 下载量/配额/磁盘翻倍）。修复后
  build_cds_request 支持 variables 参数，prepare_blocks 传
  variables=[b["variable"]] → 每块只下载自己的变量。
"""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from era5tool.acquisition.cds_channel import CdsChannel
from era5tool.acquisition.cds_request import build_cds_request
from era5tool.config.schema import Area, RequestSchema, Timerange
from era5tool.config.settings import Settings
from era5tool.core.events import EventBroker, TaskEventBus
from era5tool.core.normalizer import Normalizer
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
    """独立配置/数据目录（清掉 conftest 注入的 ERA5_* 覆盖，避免共享测试目录）。"""
    monkeypatch.delenv("ERA5_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ERA5_DATA_DIR", raising=False)
    s = Settings.load(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data")
    s.download.mock = True
    s.download.cds_max_workers = 2
    # 显式月粒度：保持 e2e 块数小（2 变量 × 12 月 = 24 块），加速测试且不触发 day 切块
    s.download.chunk_granularity = "month"
    return s


def _block(key: str, var: str, year: int, month: int) -> Dict[str, Any]:
    return {
        "key": key,
        "dataset": DATASET,
        "request": {"variable": [var], "year": [str(year)],
                    "month": [f"{month:02d}"]},
        "rel_target": f"{DATASET}/{var}/hourly/{year}/{month:02d}.nc",
    }


# ---------------------------------------------------------------------------
# Part 2：build_cds_request variables 参数
# ---------------------------------------------------------------------------
def test_build_cds_request_default_uses_schema_variables():
    """默认不带 variables → 仍用 schema.variables（兼容既有调用/测试）。"""
    schema = _schema()
    req = build_cds_request(schema, 2020, 6)
    assert req["variable"] == schema.variables
    assert req["variable"] == ["2m_temperature", "total_precipitation"]


def test_build_cds_request_explicit_variables_override():
    """显式传 variables → 请求只含该变量（每块单变量的基础）。"""
    schema = _schema()
    req = build_cds_request(schema, 2020, 6, variables=["2m_temperature"])
    assert req["variable"] == ["2m_temperature"]


def test_build_cds_request_empty_variables_falls_back_to_schema():
    """空列表按 falsy 处理 → 回退 schema.variables（保持默认语义）。"""
    schema = _schema()
    req = build_cds_request(schema, 2020, 6, variables=[])
    assert req["variable"] == schema.variables


# ---------------------------------------------------------------------------
# Part 2：prepare_blocks 每块只含自己的变量（重复下载 Bug 根因回归）
# ---------------------------------------------------------------------------
def test_prepare_blocks_each_block_only_own_variable(tmp_path, monkeypatch):
    """真实链 Normalizer → prepare_blocks：2 变量 × 12 月 = 24 块，每块单变量。"""
    channel = CdsChannel(_isolated_settings(tmp_path, monkeypatch))
    cds_req = Normalizer(granularity="month").normalize(_schema())
    blocks = channel.prepare_blocks(cds_req)
    assert len(blocks) == 24          # 2 变量 × 1 年 × 12 月
    for b in blocks:
        assert b["request"]["variable"] == [b["variable"]], \
            f"块 {b['key']} 请求应只含自己的变量，实际 {b['request']['variable']}"


def test_prepare_blocks_two_vars_no_duplicate_requests(tmp_path, monkeypatch):
    """修复前同 (年,月) 不同变量的块请求完全相同（都含全部变量）→ 重复下载。

    修复后每块请求唯一：24 个块 = 12 个 (年,月) 组合 × 2 变量，无两个块的
    请求 dict 完全相同的场景。
    """
    channel = CdsChannel(_isolated_settings(tmp_path, monkeypatch))
    cds_req = Normalizer(granularity="month").normalize(_schema())
    blocks = channel.prepare_blocks(cds_req)
    assert len(blocks) == 24

    # 每块请求的唯一标识：(variable, year, month) 全组合无重复
    identities = [(tuple(b["request"]["variable"]), b["year"], b["month"])
                  for b in blocks]
    assert len(set(identities)) == 24, "存在重复的 (variable, year, month) 块"

    # 12 个 (年,月) 组合全覆盖，每个组合恰好 2 块（每变量 1 块）
    ym_cnt = Counter((b["year"], b["month"]) for b in blocks)
    assert len(ym_cnt) == 12
    assert all(v == 2 for v in ym_cnt.values())
    assert (2020, 1) in ym_cnt and (2020, 12) in ym_cnt


def test_prepare_blocks_monthly_two_vars_two_years_four_blocks(tmp_path, monkeypatch):
    """monthly 家族：2 变量 × 2 年 = 4 块，每块请求只含自己的变量。"""
    channel = CdsChannel(_isolated_settings(tmp_path, monkeypatch))
    cds_req = Normalizer(granularity="month").normalize(
        _schema(family="land-monthly", start="2020-01-01", end="2021-12-31"))
    blocks = channel.prepare_blocks(cds_req)
    assert len(blocks) == 4
    for b in blocks:
        assert b["request"]["variable"] == [b["variable"]]


# ---------------------------------------------------------------------------
# Part 1：run_blocks on_block_done 回调
# ---------------------------------------------------------------------------
def test_run_blocks_on_block_done_called_per_block(tmp_path, monkeypatch):
    """mock 模式（fail_rate=0）：每个块完成回调一次，done 计数递增。"""
    settings = _isolated_settings(tmp_path, monkeypatch)
    channel = CdsChannel(settings)
    task = Task(id="t_cb")
    task_dir = settings.tasks_dir / task.id
    task_dir.mkdir(parents=True, exist_ok=True)
    store = ResumableStore(task_dir, settings)
    broker = EventBroker()
    bus = TaskEventBus(task.id, task_dir, broker)
    blocks = [
        _block("2m_temperature/2020/01", "2m_temperature", 2020, 1),
        _block("2m_temperature/2020/02", "2m_temperature", 2020, 2),
        _block("total_precipitation/2020/01", "total_precipitation", 2020, 1),
        _block("total_precipitation/2020/02", "total_precipitation", 2020, 2),
    ]
    calls: List = []

    def on_block_done(result: Dict[str, Any], completed: int, total: int) -> None:
        calls.append((result["status"], completed, total))

    bus.start()
    try:
        results = channel.run_blocks(task, blocks, store, bus, None,
                                     on_block_done=on_block_done)
    finally:
        bus.stop()

    assert len(results) == 4
    assert all(r["status"] == "done" for r in results)
    assert len(calls) == 4, f"每个块应回调一次，实际 {len(calls)} 次"
    assert [c[1] for c in calls] == [1, 2, 3, 4], "completed 应逐块递增"
    assert all(c[2] == 4 for c in calls), "total 应为块总数"
    assert all(c[0] == "done" for c in calls)


def test_run_blocks_without_callback_unchanged(tmp_path, monkeypatch):
    """不传 on_block_done → 行为与历史一致（返回结果结构不变）。"""
    settings = _isolated_settings(tmp_path, monkeypatch)
    channel = CdsChannel(settings)
    task = Task(id="t_nocb")
    task_dir = settings.tasks_dir / task.id
    task_dir.mkdir(parents=True, exist_ok=True)
    store = ResumableStore(task_dir, settings)
    broker = EventBroker()
    bus = TaskEventBus(task.id, task_dir, broker)
    blocks = [_block("2m_temperature/2020/01", "2m_temperature", 2020, 1)]

    bus.start()
    try:
        results = channel.run_blocks(task, blocks, store, bus, None)
    finally:
        bus.stop()

    assert len(results) == 1
    assert results[0]["status"] == "done"
    assert "block" in results[0] and "attempts" in results[0]


# ---------------------------------------------------------------------------
# Part 1：编排器端到端——运行期间 task.json 的 progress 递增
# ---------------------------------------------------------------------------
def _payload(variables=None) -> Dict[str, Any]:
    return {
        "dataset": DATASET,
        "dataset_family": "era5-single",
        "variables": variables or ["2m_temperature"],
        "timerange": {"start": "2005-01-01", "end": "2005-01-28"},
        "area": {"west": 118, "south": 29, "east": 123, "north": 34},
        "frequency": "hourly", "aggregation": "raw", "confidence": 0.9,
    }


def _wait_status(client, task_id: str, status: str, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/download/{task_id}")
        assert r.json()["code"] == 0
        if r.json()["data"]["status"] == status:
            return r.json()["data"]
        time.sleep(0.05)
    raise TimeoutError(f"task {task_id} 未进入 {status}")


def _slow_progress_cfg(self):
    d = self.settings.download
    return {
        "mock": True, "mock_delay": 0.3, "fail_rate": 0.0, "seed": 7,
        "retry_max": d.retry_max, "backoff_base": d.backoff_base,
        "backoff_factor": d.backoff_factor, "backoff_max": d.backoff_max,
        "backoff_jitter": d.backoff_jitter,
    }


def _read_task_json(path: Path) -> Dict[str, Any]:
    """读取 task.json，容忍 Windows 下 os.replace 原子写期间的瞬时文件锁错误。

    编排线程用 tmp+os.replace 落盘；Windows 上读方可能在替换瞬间碰到
    PermissionError/OSError，属时序竞争而非数据错误，重试即可。
    """
    last: Optional[Exception] = None
    for _ in range(50):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (PermissionError, OSError) as exc:
            last = exc
            time.sleep(0.02)
    raise AssertionError(f"读取 task.json 持续失败: {path}") from last


def test_orchestrator_progress_increases_during_run(client, app_state, monkeypatch):
    """编排器端到端：运行期间 task.json 的 progress 递增（进度条卡住 Bug 回归）。

    提交 2 变量 × 12 月 = 24 块的慢速 mock 任务，轮询 task.json：
    - 运行中出现 progress>0 的中间值（修复前恒为 0.0）；
    - 结束 progress==1.0；
    - events.jsonl 中存在 type=task 实时事件（WS 可直接消费）。
    """
    monkeypatch.setattr(CdsChannel, "worker_cfg", _slow_progress_cfg)
    r = client.post("/api/download/submit",
                    json={"request_schema": _payload(
                        variables=["2m_temperature", "total_precipitation"])})
    assert r.json()["code"] == 0
    task_id = r.json()["data"]["task_id"]
    _wait_status(client, task_id, "running")

    task_json = app_state.settings.tasks_dir / task_id / "task.json"
    seen: List[float] = []
    deadline = time.time() + 30
    while time.time() < deadline:
        raw = _read_task_json(task_json)
        seen.append(float(raw["progress"]))
        if raw["status"] not in ("pending", "running"):
            break
        time.sleep(0.1)

    assert any(p > 0 for p in seen), \
        f"运行期间 task.json 的 progress 应出现 >0 中间值，实际 {seen}"
    assert seen[-1] == 1.0, f"最终 progress 应为 1.0，实际 {seen[-1]}"

    # WS 事件（events.jsonl）中应有 type=task 实时事件，带实时 progress/block_stats
    events_path = app_state.settings.tasks_dir / task_id / "events.jsonl"
    task_evs = []
    if events_path.is_file():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ev = json.loads(line)
            if ev.get("type") == "task" and ev.get("task_id") == task_id:
                task_evs.append(ev)
    assert task_evs, "events.jsonl 中应存在 type=task 实时事件"
    assert any(float(ev["progress"]) > 0 for ev in task_evs)
    last = task_evs[-1]
    assert last["block_stats"]["done"] == last["block_stats"]["total"]
    assert last["block_stats"]["failed"] == 0
