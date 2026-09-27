# -*- coding: utf-8 -*-
"""bugfix download-gaps ②：全局限流闸 + 一键补漏（retry_failed）链路回归。

事故复盘（data/tasks/t_20260905_043416_261be78b/events.jsonl，172/172 失败块）：
CDS 用 HTTP 400 返回"排队请求数超限"（The job has been rejected / Number queued
requests ... temporarily limited），旧代码按状态码 400 判不可重试 → 块一次不重试
即永久失败；且只要有 1 块失败整任务就 FAILED，用户 resume 又是全量重跑 → 缺口
永远补不齐。

本文件覆盖（classify/interleave 的专项已在其各自文件）：
1. ThrottleGate 单元：指数冷却 / 上闸取 max / wait 阻塞与抖动 / clear 放行 /
   跨实例文件共享（等价跨进程）；读写异常降级（绝不拖垮下载）。
2. ResumableStore.clear_failed：只清 failed 标记、绝不动 done 标记。
3. orchestrator 汇总：部分失败 → FAILED + outcome=partial_success +
   failure_summary 分类（queue_limited 归 retryable）+ result.files 只含已成功块
   + hint 引导补漏。
4. retry_failed 状态守卫：非 PAUSED/FAILED 一律拒绝。
5. retry_failed 端到端：FAILED(partial) → 补漏 → 只重下失败块（已 done 块
   0 次重复下载）→ SUCCESS。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from era5tool.acquisition.cds_channel import CdsChannel, CATEGORY_QUEUE_LIMITED
from era5tool.config.schema import ApiError, RequestSchema
from era5tool.config.settings import DownloadSettings, Settings
from era5tool.core.events import EventBroker
from era5tool.core.orchestrator import Orchestrator
from era5tool.core.resumable import ResumableStore
from era5tool.core.task_store import TaskStore
from era5tool.core.throttle import ThrottleGate, build_gate
from era5tool.models.task import TaskStatus, TaskType


# ---------------------------------------------------------------------------
# 1. ThrottleGate 单元测试
# ---------------------------------------------------------------------------
class _FakeSleep:
    """记录睡眠时长的替身（不真的睡），供 wait() 行为断言。"""

    def __init__(self) -> None:
        self.total = 0.0

    def __call__(self, s: float) -> None:
        self.total += float(s)


def test_throttle_cooldown_exponential_with_cap(tmp_path):
    gate = build_gate(tmp_path / "g.json", base_s=30.0, factor=2.0,
                      max_s=120.0, jitter_s=0.0)
    assert gate.cooldown_for(1) == 30.0
    assert gate.cooldown_for(2) == 60.0
    assert gate.cooldown_for(3) == 120.0
    assert gate.cooldown_for(9) == 120.0, "冷却必须封顶于 max_s"


def test_throttle_arm_takes_max_and_wait_blocks(tmp_path):
    """arm 的释放时刻取 max(已有, now+cooldown)；wait 在真实时钟下会阻塞到放行。"""
    gate = build_gate(tmp_path / "g.json", base_s=10.0, factor=2.0,
                      max_s=600.0, jitter_s=0.0)
    now = 1000.0
    gate.arm(attempt=1, reason="queue_limited", now=now)
    gate.arm(attempt=1, reason="queue_limited", now=now + 5)
    st = gate.state()
    # 第二次 arm 取 max(已有 until, now+cooldown)
    assert st["until"] == pytest.approx(now + 5 + 10.0)
    assert st["hits"] == 2

    # wait 用真实时钟：小冷却 + 注入睡眠替身 → 阻塞约一个冷却周期
    g2 = build_gate(tmp_path / "g2.json", base_s=0.05, factor=1.0,
                    max_s=60.0, jitter_s=0.0)
    g2.arm(attempt=1, reason="queue_limited")
    sleeper = _FakeSleep()
    waited = g2.wait(sleep=sleeper, rng=__import__("random").Random(0))
    assert 0.0 < waited <= 0.15, f"应阻塞约一个冷却周期，实际 {waited}"
    assert sleeper.total == pytest.approx(waited, abs=0.03)


def test_throttle_clear_releases_and_no_gate_returns_immediately(tmp_path):
    gate = build_gate(tmp_path / "g.json", base_s=30.0, jitter_s=0.0)
    sleeper = _FakeSleep()
    assert gate.wait(sleep=sleeper) == 0.0, "未上闸 → 0 等待"
    gate.arm(attempt=1, reason="rate_limit")
    assert gate.remaining() > 0
    gate.clear()
    assert gate.remaining() == 0.0
    assert gate.wait(sleep=sleeper) == 0.0


def test_throttle_cross_instance_shares_file_like_cross_process(tmp_path):
    """两个实例指向同一文件（等价两个 worker 进程）→ 一个 arm 另一个必须等。"""
    p = tmp_path / "shared.json"
    a = build_gate(p, base_s=8.0, factor=1.0, max_s=60.0, jitter_s=0.0)
    b = build_gate(p, base_s=8.0, factor=1.0, max_s=60.0, jitter_s=0.0)
    a.arm(attempt=1, reason="queue_limited")
    assert b.remaining() > 0, "B 实例必须读到 A 的上闸状态（文件共享）"

    sleeper = _FakeSleep()
    waited = b.wait(sleep=sleeper, rng=__import__("random").Random(1))
    assert waited == pytest.approx(8.0, abs=0.5)
    assert sleeper.total == pytest.approx(8.0, abs=0.5)


def test_throttle_gate_never_raises_on_corrupt_file(tmp_path):
    p = tmp_path / "g.json"
    p.write_text("{corrupt json!!", encoding="utf-8")
    gate = build_gate(p)
    assert gate.remaining() == 0.0, "损坏文件 → 空闸（尽力而为，不拖垮下载）"
    gate.arm(attempt=1, reason="x")          # 覆盖写应原子恢复
    assert gate.remaining() > 0
    # 深层父目录不存在：arm 自动 mkdir(parents) 原子落盘，不抛异常
    deep = tmp_path / "no_such_dir" / "deep" / "g.json"
    deep_gate = build_gate(deep)
    deep_gate.arm(attempt=1, reason="x")     # 不应抛
    assert deep_gate.remaining() > 0, "arm 应自动创建父目录并成功落盘"


# ---------------------------------------------------------------------------
# 2. ResumableStore.clear_failed
# ---------------------------------------------------------------------------
def test_clear_failed_removes_only_failed_marks(tmp_path):
    store = ResumableStore(tmp_path, None)
    store.mark_done("var/2020/01")
    store.mark_failed("var/2020/02", "boom")
    store.mark_failed("var/2020/03", "BUSY_AFTER_RETRIES")

    cleared = store.clear_failed()

    assert cleared == 2
    assert store.is_done("var/2020/01") is True, "done 标记绝不能被清"
    assert store.is_done("var/2020/02") is False
    assert store.is_done("var/2020/03") is False


# ---------------------------------------------------------------------------
# orchestrator / retry_failed 集成（mock 下载 + 可控 run_blocks）
# ---------------------------------------------------------------------------
def _payload() -> dict:
    """6 个 month 块（单变量，2020 上半年）。"""
    return {
        "dataset": "reanalysis-era5-single-levels", "dataset_family": "era5-single",
        "variables": ["2m_temperature"], "timerange": {"start": "2020-01-01",
                                                       "end": "2020-06-30"},
        "area": {"west": 118, "south": 29, "east": 123, "north": 34},
        "frequency": "hourly", "aggregation": "raw", "confidence": 0.9,
    }


def _make_orch(tmp_path, monkeypatch):
    settings = Settings(
        download=DownloadSettings(mock=True, cds_max_workers=2,
                                  chunk_granularity="month", aria2_enabled=False,
                                  retry_max=2),
        config_dir=tmp_path / "cfg", data_dir=tmp_path / "data")
    settings.ensure_dirs()
    return Orchestrator(settings, EventBroker())


def _wait_status(orch, task_id, statuses, timeout: float = 60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = orch.get(task_id)
        if t.status.value in statuses:
            return t
        time.sleep(0.03)
    raise TimeoutError(
        f"task 未进入 {statuses}，当前 {orch.get(task_id).status.value}")


def _start_counts(task_dir: Path) -> Dict[str, int]:
    """统计每块真实 '开始下载' 次数（>1 即重复下载证据）。"""
    counts: Dict[str, int] = {}
    p = task_dir / "events.jsonl"
    if not p.is_file():
        return counts
    import json
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "start" and ev.get("phase") == "downloading":
            key = ev.get("block_key")
            counts[key] = counts.get(key, 0) + 1
    return counts


def test_partial_failure_marks_partial_success_with_summary(tmp_path, monkeypatch):
    """半块失败 → 任务 FAILED 但 outcome=partial_success、files 可用、可补漏。"""
    orch = _make_orch(tmp_path, monkeypatch)
    task = orch.submit(RequestSchema(**_payload()))

    real_run_blocks = CdsChannel.run_blocks

    def _half_fail_run_blocks(self, task, blocks, store, bus, cancel_event=None,
                              on_block_done=None, settle_event=None,
                              fail_rate: float = 0.0) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for i, b in enumerate(blocks):
            key = b["key"]
            # 前一半成功（写真实 .done + 回调），后一半失败（不写标记）
            if i % 2 == 0:
                store.mark_done(key)
                r = {"block": key, "status": "done", "attempts": 1,
                     "target": f"{key}.nc", "transport": "mock"}
            else:
                r = {"block": key, "status": "failed", "attempts": 3,
                     "retried": True,
                     "error": ("400 Client Error: Bad Request for url: ...\n"
                               "The job has been rejected\nNumber queued requests "
                               "for this dataset is temporarily limited."),
                     "error_category": CATEGORY_QUEUE_LIMITED,
                     "throttled": True}
            results.append(r)
            if on_block_done is not None:
                on_block_done(r, i + 1, len(blocks))
        # 遵循 run_blocks 契约：正常路径同步置位 settle（否则 resume 会空等）
        if settle_event is not None:
            settle_event.set()
        return results

    monkeypatch.setattr(CdsChannel, "run_blocks", _half_fail_run_blocks)
    t = _wait_status(orch, task.id, ("failed", "success"))

    assert t.status == TaskStatus.FAILED
    assert t.error["outcome"] == "partial_success"
    assert t.error["done_blocks"] == 3
    assert t.error["failed_blocks_total"] == 3
    # failure_summary：queue_limited 应归入 retryable 计数
    assert t.error["failure_summary"]["retryable"] == 3
    assert "queue_limited" in t.error["failure_summary"]["categories"]
    assert "补漏" in t.error["hint"]
    # 部分成功：result.files 应包含已成功的 3 个块（可立即出图，不必等补漏）
    assert t.result is not None and t.result.get("partial") is True
    assert len(t.result.get("files", [])) == 3
    monkeypatch.setattr(CdsChannel, "run_blocks", real_run_blocks)


def test_retry_failed_state_guard(tmp_path, monkeypatch):
    """只有 PAUSED/FAILED 允许补漏；PENDING/RUNNING 一律拒绝（ApiError）。"""
    orch = _make_orch(tmp_path, monkeypatch)
    params = {"request_schema": _payload(), "chunk_granularity": "month"}
    # PENDING → 拒绝
    p = orch.store.create(TaskType.DOWNLOAD, params)
    with pytest.raises(ApiError):
        orch.retry_failed(p.id)
    # RUNNING（无后台线程，等价重启残留）→ 拒绝
    r = orch.store.create(TaskType.DOWNLOAD, params)
    rt = orch.get(r.id)
    rt.transition(TaskStatus.RUNNING)
    orch.store.save(rt)
    with pytest.raises(ApiError):
        orch.retry_failed(r.id)


def test_retry_failed_only_redownloads_failed_blocks(tmp_path, monkeypatch):
    """端到端：部分失败 → 补漏 → 只重下失败块，已成功块 0 重复下载 → SUCCESS。"""
    orch = _make_orch(tmp_path, monkeypatch)
    real_run_blocks = CdsChannel.run_blocks

    def _half_fail_run_blocks(self, task, blocks, store, bus, cancel_event=None,
                              on_block_done=None, settle_event=None,
                              fail_rate: float = 0.0) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for i, b in enumerate(blocks):
            key = b["key"]
            if i % 2 == 0:
                store.mark_done(key)
                r = {"block": key, "status": "done", "attempts": 1,
                     "target": f"{key}.nc", "transport": "mock"}
            else:
                r = {"block": key, "status": "failed", "attempts": 3,
                     "retried": True, "error": "temporarily limited",
                     "error_category": CATEGORY_QUEUE_LIMITED,
                     "throttled": True}
            results.append(r)
            if on_block_done is not None:
                on_block_done(r, i + 1, len(blocks))
        if settle_event is not None:
            settle_event.set()
        return results

    # 第 1 轮：注入一半失败 → FAILED(partial_success)
    monkeypatch.setattr(CdsChannel, "run_blocks", _half_fail_run_blocks)
    t1 = orch.submit(RequestSchema(**_payload()))
    task1 = _wait_status(orch, t1.id, ("failed",))
    assert task1.error["outcome"] == "partial_success"

    # 第 2 轮：恢复真实 run_blocks（mock 下载 fail_rate=0 → 全成功），一键补漏
    monkeypatch.setattr(CdsChannel, "run_blocks", real_run_blocks)
    orch.retry_failed(t1.id)
    task2 = _wait_status(orch, t1.id, ("success", "failed"))

    assert task2.status == TaskStatus.SUCCESS, f"补漏后应全部成功，实际 {task2.status}"
    assert task2.progress == 1.0
    assert task2.block_stats.done == 6
    assert task2.block_stats.failed == 0
    # 核心承诺：没有任何块被重复下载（真实 start 事件每块 ≤1 次）
    counts = _start_counts(orch.store.task_dir(t1.id))
    assert counts, "应有下载开始事件"
    assert max(counts.values()) == 1, f"出现重复下载: {counts}"
    # 且只有失败的那一半（3 块）真的下过
    assert len(counts) == 3, f"补漏应只重下 3 个失败块，实际 {sorted(counts)}"
