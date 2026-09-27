# -*- coding: utf-8 -*-
"""QA：流式提交（streaming submit）下"任务暂停即时生效"回归（本次 Bug 修复）。

覆盖三件事：
1. 即时性：run_blocks 在 cancel_event.set() 后（甚至早于第一块完成）快速返回，
   不再被 as_completed 阻塞到下一完成点才感知取消。
2. 取消后 results 中 cancelled 块 ≥ 1（供编排层 cancelled 列表非空 → 转 paused）。
3. 无在跑块结果丢失：取消前后 done + cancelled 计数与块总数一致（remaining + running
   中未 collect 的块均被显式标 cancelled，断点续传依据磁盘 .done 兜底，不重复下载）。

使用 FakeCdsClient 慢速 sleep 模拟长块（mock_delay 较大），构造"长块期间点击暂停"
的真实场景。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from era5tool.acquisition.cds_channel import CdsChannel
from era5tool.config.settings import Settings
from era5tool.core.events import EventBroker, TaskEventBus
from era5tool.core.resumable import ResumableStore
from era5tool.models.task import Task


def _isolated_settings(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.delenv("ERA5_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ERA5_DATA_DIR", raising=False)
    s = Settings.load(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data")
    s.download.mock = True
    s.download.cds_max_workers = 2
    s.download.chunk_granularity = "month"
    return s


def _slow_cfg(delay: float):
    def _make(self):
        d = self.settings.download
        return {
            "mock": True, "mock_delay": delay, "fail_rate": 0.0, "seed": 7,
            "retry_max": d.retry_max, "backoff_base": d.backoff_base,
            "backoff_factor": d.backoff_factor, "backoff_max": d.backoff_max,
            "backoff_jitter": d.backoff_jitter,
        }
    return _make


def _monthly_blocks(n_months: int = 12, var: str = "2m_temperature") -> List[Dict[str, Any]]:
    return [
        {"key": f"{var}/2020/{m:02d}", "dataset": "reanalysis-era5-single-levels",
         "variable": var, "year": 2020, "month": m,
         "request": {"variable": [var], "year": ["2020"], "month": [f"{m:02d}"]},
         "rel_target": f"reanalysis-era5-single-levels/{var}/hourly/2020/{m:02d}.nc"}
        for m in range(1, n_months + 1)
    ]


def test_pause_fix_cancel_returns_quickly_even_before_first_block(tmp_path, monkeypatch):
    """长块（1.2s）× 多块：在 cancel 早于第一块完成时 set → run_blocks 快速返回。

    旧实现一次性提交全部 + as_completed，取消检查只在首个 future 完成后才执行 →
    若单块很慢（真实下载分钟级）会"看起来卡住"。流式模型在等待 future 之前便检查
    cancel，故即便第一块远未完成也能秒级响应。
    """
    settings = _isolated_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(CdsChannel, "worker_cfg", _slow_cfg(1.2))
    channel = CdsChannel(settings)
    task = Task(id="t_pause_early")
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
        time.sleep(0.2)  # 仅 0.2s：远早于第一块完成（1.2s）→ 模拟"长块中点击暂停"
        t0 = time.time()
        cancel_event.set()
        t.join(timeout=10)
        elapsed = time.time() - t0
        assert not t.is_alive(), "cancel 后 run_blocks 10s 内未返回 → 取消挂死"
        # 关键：必须在第一块完成时间（1.2s）之前返回 → 证明取消检查先于等待发生
        assert elapsed < 1.0, \
            f"流式取消应在首块完成前响应，实际 {elapsed:.2f}s（单块 1.2s）"
        results = out.get("results", [])
        cancelled = [r for r in results if r["status"] == "cancelled"]
        # N4：cancelled 列表非空，供编排层转 paused
        assert len(cancelled) >= 1, f"取消时 cancelled 块应 ≥1，实际 {len(cancelled)}"
        # 完整性：所有未 collect 的块都在 results 中（不丢在跑块）
        assert len(results) == 12, \
            f"results 应含全部 12 块（含 cancelled），实际 {len(results)}"
        assert len([r for r in results if r["status"] == "done"]) == 0, \
            "0.2s 取消应无 done 块（单块 1.2s）"
        assert len(cancelled) == 12
    finally:
        bus.stop()


def test_pause_fix_cancelled_count_and_no_lost_blocks(tmp_path, monkeypatch):
    """取消发生在若干块已完成后：done + cancelled 计数 == 块总数；无在跑块结果丢失。

    模拟真实"跑到一半暂停"：先等若干块完成再 cancel，验证剩余（remaining + running
    未 collect）块均被标 cancelled，且被取消时正在跑的块即便稍后 mark_done（磁盘
    .done 存在）也不会导致 resume 重复下载——本测试只验证 results 计数一致性。
    """
    settings = _isolated_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(CdsChannel, "worker_cfg", _slow_cfg(0.4))
    channel = CdsChannel(settings)
    task = Task(id="t_pause_mid")
    task_dir = settings.tasks_dir / task.id
    task_dir.mkdir(parents=True, exist_ok=True)
    store = ResumableStore(task_dir, settings)
    broker = EventBroker()
    bus = TaskEventBus(task.id, task_dir, broker)
    blocks = _monthly_blocks(12)

    cancel_event = threading.Event()
    out: Dict[str, Any] = {}
    done_seen: List[str] = []

    def on_block_done(result, completed, total):
        if result["status"] == "done":
            done_seen.append(result["block"])

    def runner():
        out["results"] = channel.run_blocks(task, blocks, store, bus, cancel_event,
                                            on_block_done=on_block_done)

    t = threading.Thread(target=runner, daemon=True)
    bus.start()
    try:
        t.start()
        time.sleep(0.9)  # ~2 块完成（0.4s × 2 workers）后取消
        cancel_event.set()
        t.join(timeout=10)
        assert not t.is_alive(), "cancel 后 run_blocks 未返回"
        results = out["results"]
        done = [r for r in results if r["status"] == "done"]
        cancelled = [r for r in results if r["status"] == "cancelled"]
        # 计数一致性：done + cancelled == 总数（无遗漏、无重复）
        assert len(done) + len(cancelled) == 12, \
            f"done({len(done)}) + cancelled({len(cancelled)}) 应 == 12"
        # cancelled 列表非空（编排层转 paused 的必要条件）
        assert len(cancelled) >= 1
        # on_block_done 收到的 done 块应与 results 中的 done 块一致
        assert set(done_seen) == {r["block"] for r in done}
        # 已完成的 done 块应带 .done 标记（resume 可跳过，不重复下载）
        for r in done:
            assert store.is_done(r["block"]) is True, f"done 块 {r['block']} 缺 .done"
        # 每个结果 dict 均含编排层依赖字段
        for r in results:
            assert "block" in r and "status" in r
            assert r["status"] in ("done", "failed", "cancelled")
    finally:
        bus.stop()


def test_pause_fix_normal_complete_no_cancel(tmp_path, monkeypatch):
    """无取消：流式提交必须等价旧行为——全部 done、逐块回调、completed 递增 1..N。"""
    settings = _isolated_settings(tmp_path, monkeypatch)
    channel = CdsChannel(settings)
    task = Task(id="t_pause_normal")
    task_dir = settings.tasks_dir / task.id
    task_dir.mkdir(parents=True, exist_ok=True)
    store = ResumableStore(task_dir, settings)
    broker = EventBroker()
    bus = TaskEventBus(task.id, task_dir, broker)
    blocks = _monthly_blocks(4)  # 4 块，2 workers

    calls: List = []

    def on_block_done(result, completed, total):
        calls.append((result["status"], completed, total))

    bus.start()
    try:
        results = channel.run_blocks(task, blocks, store, bus, None,
                                     on_block_done=on_block_done)
    finally:
        bus.stop()

    assert len(results) == 4
    assert all(r["status"] == "done" for r in results)
    assert len(calls) == 4, f"每块应回调一次，实际 {len(calls)}"
    assert [c[1] for c in calls] == [1, 2, 3, 4], "completed 应逐块递增"
    assert all(c[2] == 4 for c in calls)
