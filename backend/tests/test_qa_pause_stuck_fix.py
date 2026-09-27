# -*- coding: utf-8 -*-
"""QA：运行中任务「暂停无效 / 卡在最后一块不结束」缺陷回归（本次修复）。

三个根因面各自独立取证 + 落盘健壮性回归：

A. cancel() 对「无活动会话的 RUNNING 任务」只写 cancel.flag → 无人感知 →
   永久卡 running（暂停无效、任务不结束）。修复：RUNNING 且本进程无
   cancel_event/settle_event（后端重启残留、已退出进程遗留）→ 立即转 paused。
B. resume 的 _wait_previous_drain 无超时上限 → 旧 worker 永不退出时无限空等 →
   running 无进度。修复：drain_wait_timeout_s 超时转 paused(DRAIN_TIMEOUT)，
   并清掉陈旧 settle（不砖化、可重试）。
C. 流式取消在「仅剩 1 个在跑块」时的行为：cancel 应快速 collect → 转 paused，
   随后 resume 据 .done 跳过并 success（不回归）。

落盘健壮性：TaskStore.save 的 os.replace 在 Windows 下可能因读方瞬时锁而抛
PermissionError，若一次性失败则终态（success）落盘丢失、task.json 永久 running
（实证：事件流已发 success、磁盘仍是 running、.tmp 残留）。修复：写锁串行化 +
有界重试替换 + 清陈旧 .tmp。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from era5tool.acquisition.cds_channel import CdsChannel
from era5tool.config.schema import RequestSchema
from era5tool.config.settings import DownloadSettings, Settings
from era5tool.core import task_store as task_store_mod
from era5tool.core.events import EventBroker
from era5tool.core.orchestrator import Orchestrator
from era5tool.core.task_store import TaskStore
from era5tool.models.task import Task, TaskStatus, TaskType


def _payload(start: str = "2020-01-01", end: str = "2020-06-30") -> dict:
    """6 个 month 块（month 粒度；可收窄为 1 块用于单块场景）。"""
    return {
        "dataset": "reanalysis-era5-single-levels", "dataset_family": "era5-single",
        "variables": ["2m_temperature"], "timerange": {"start": start, "end": end},
        "area": {"west": 118, "south": 29, "east": 123, "north": 34},
        "frequency": "hourly", "aggregation": "raw", "confidence": 0.9,
    }


def _cfg(delay: float):
    """慢速长块 worker 配置（真实 cdsapi 分钟级下载的 mock 近似）。"""
    def _make(self):
        d = self.settings.download
        return {
            "mock": True, "mock_delay": delay, "fail_rate": 0.0, "seed": 7,
            "retry_max": d.retry_max, "backoff_base": d.backoff_base,
            "backoff_factor": d.backoff_factor, "backoff_max": d.backoff_max,
            "backoff_jitter": d.backoff_jitter,
        }
    return _make


def _make_orch(tmp_path, monkeypatch, delay: float, workers: int = 2,
               drain_wait_timeout_s: float = 600.0):
    settings = Settings(
        download=DownloadSettings(mock=True, cds_max_workers=workers,
                                  chunk_granularity="month", aria2_enabled=False,
                                  retry_max=2,
                                  drain_wait_timeout_s=drain_wait_timeout_s),
        config_dir=tmp_path / "cfg", data_dir=tmp_path / "data")
    settings.ensure_dirs()
    monkeypatch.setattr(CdsChannel, "worker_cfg", _cfg(delay))
    return Orchestrator(settings, EventBroker()), settings


def _wait_status(orch, task_id, statuses, timeout: float = 30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = orch.get(task_id)
        if t.status.value in statuses:
            return t
        time.sleep(0.03)
    raise TimeoutError(
        f"task {task_id} 未进入 {statuses}，当前 {orch.get(task_id).status.value}")


def _wait_download_starts(task_dir: Path, n: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sum(_download_starts(task_dir).values()) >= n:
            return
        time.sleep(0.05)
    raise TimeoutError(f"task 未出现 {n} 个块开始下载事件")


def _download_starts(task_dir: Path) -> Dict[str, int]:
    """统计 events.jsonl 中每块 “开始下载” 次数（>1 即重复下载证据）。"""
    starts: Dict[str, int] = {}
    p = task_dir / "events.jsonl"
    if not p.is_file():
        return starts
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "start" and ev.get("phase") == "downloading":
            key = ev.get("block_key")
            starts[key] = starts.get(key, 0) + 1
    return starts


def _write_running_task(orch: Orchestrator, payload: dict) -> str:
    """直接写盘构造 RUNNING 任务（无任何后台线程/活动会话，等价重启残留）。"""
    task = orch.store.create(
        TaskType.DOWNLOAD,
        {"request_schema": payload, "chunk_granularity": "month"})
    t = orch.get(task.id)
    t.transition(TaskStatus.RUNNING)
    orch.store.save(t)
    return task.id


# ---------------------------------------------------------------------------
# A. RUNNING + 无活动会话 → cancel 必须生效（转 paused），绝不静默只写 flag
# ---------------------------------------------------------------------------
def test_cancel_running_without_live_session_goes_paused_and_recoverable(
        tmp_path, monkeypatch):
    """磁盘 running 但本进程无 cancel_event/settle_event（如后端重启残留）→
    cancel() 立即转 paused；任务不砖化，可 resume→success / delete。
    """
    orch, settings = _make_orch(tmp_path, monkeypatch, delay=0.01)
    tid = _write_running_task(orch, _payload())

    # 前提：确无活动会话（复现“无人会感知 cancel.flag”的场景）
    with orch._lock:
        assert orch._cancel_events.get(tid) is None
        assert orch._settle_events.get(tid) is None

    t = orch.cancel(tid)
    assert t.status.value == "paused", \
        f"无活动会话的 RUNNING 任务 cancel 应转 paused，实际 {t.status.value}"
    persisted = orch.get(tid)
    assert persisted.status.value == "paused"
    assert (persisted.error or {}).get("code") == "CANCELLED"

    # 不砖化：可再次 resume → success（取消未启动任何下载，resume 从零跑完）
    orch.resume(tid)
    done = _wait_status(orch, tid, {"success", "failed", "paused"})
    assert done.status.value == "success", \
        f"resume 应 success，实际 {done.status.value} error={done.error}"
    assert done.block_stats.done == done.block_stats.total
    assert orch.delete(tid) is True


# ---------------------------------------------------------------------------
# B. resume drain-wait 超时上限：旧 worker 永不收尾 → 转 paused，不无限卡 running
# ---------------------------------------------------------------------------
def test_resume_drain_wait_timeout_goes_paused_not_stuck(tmp_path, monkeypatch):
    """_settle_events 里有一个永不置位的旧 settle（旧 worker 挂起）→ resume 的
    等待在 drain_wait_timeout_s 后超时转 paused(DRAIN_TIMEOUT)，并清理陈旧 settle；
    任务不无限卡 running（修复前永久 running 无进度、不结束）。
    """
    orch, settings = _make_orch(tmp_path, monkeypatch, delay=0.01,
                                drain_wait_timeout_s=0.4)
    payload = _payload()
    task = orch.store.create(
        TaskType.DOWNLOAD,
        {"request_schema": payload, "chunk_granularity": "month"})
    tid = task.id
    t = orch.get(tid)
    t.transition(TaskStatus.PAUSED)   # resume 合法起点
    orch.store.save(t)

    # 模拟「上一会话的 settle 永不置位」（旧 worker 进程挂死/看门狗丢失）
    phantom = threading.Event()
    with orch._lock:
        orch._settle_events[tid] = phantom

    t0 = time.time()
    orch.resume(tid)                   # 进入 running 后会在 drain-wait 卡到超时
    paused = _wait_status(orch, tid, {"paused"}, timeout=10)
    elapsed = time.time() - t0
    assert paused.status.value == "paused"
    assert (paused.error or {}).get("code") == "DRAIN_TIMEOUT", \
        f"超时暂停应标记 DRAIN_TIMEOUT，实际 {paused.error}"
    assert elapsed < 5.0, f"drain 超时应秒级转 paused，实际 {elapsed:.2f}s"
    with orch._lock:
        assert orch._settle_events.get(tid) is None, \
            "超时后应清理陈旧 settle，避免下次 resume 再次空等"

    # 不砖化：干净后再次 resume → success
    orch.resume(tid)
    done = _wait_status(orch, tid, {"success", "failed", "paused"})
    assert done.status.value == "success", \
        f"清理后 resume 应 success，实际 {done.status.value} error={done.error}"
    assert done.block_stats.done == done.block_stats.total


# ---------------------------------------------------------------------------
# C. 仅剩 1 个在跑块时 cancel：快速转 paused；resume 据 .done 跳过 → success
# ---------------------------------------------------------------------------
def test_cancel_single_inflight_block_pauses_and_resume_succeeds(
        tmp_path, monkeypatch):
    """单块慢速（delay 1.0s）任务运行中 cancel → 快速转 paused（非卡死），
    之后 resume 最终 success 且不重复下载。
    """
    orch, settings = _make_orch(tmp_path, monkeypatch, delay=1.0, workers=2)
    task = orch.submit(RequestSchema(**_payload(start="2020-01-01",
                                                end="2020-01-31")))
    tid = task.id
    task_dir = orch.store.task_dir(tid)
    _wait_status(orch, tid, {"running"})
    _wait_download_starts(task_dir, n=1)   # 唯一一块确已在跑

    orch.cancel(tid)
    t = _wait_status(orch, tid, {"paused"}, timeout=10)
    assert t.status.value == "paused", \
        f"单在跑块 cancel 应 paused，实际 {t.status.value} error={t.error}"

    orch.resume(tid)
    done = _wait_status(orch, tid, {"success", "failed", "paused"})
    assert done.status.value == "success", \
        f"resume 应 success，实际 {done.status.value} error={done.error}"
    assert done.block_stats.done == done.block_stats.total
    dup = {k: v for k, v in _download_starts(task_dir).items() if v > 1}
    assert not dup, f"resume 重复下载了在跑块: {dup}"


# ---------------------------------------------------------------------------
# 落盘健壮性：save 的 os.replace 瞬时锁 → 有界重试，终态不丢
# ---------------------------------------------------------------------------
def test_store_save_retries_transient_replace_failure(tmp_path, monkeypatch):
    """os.replace 前若干次抛 PermissionError（Windows 读方瞬时锁）→ save 有界
    重试后仍成功落盘，终态不丢（修复前一次失败即丢终态 → task.json 永久 running）。
    """
    settings = Settings(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data")
    settings.ensure_dirs()
    store = TaskStore(settings)
    task = Task(id="t_save_retry", status=TaskStatus.RUNNING, params={})

    real_replace = os.replace
    state = {"attempts": 0}

    def flaky_replace(src, dst):
        state["attempts"] += 1
        if state["attempts"] <= 3:
            raise PermissionError(13, "Permission denied", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(task_store_mod.os, "replace", flaky_replace)
    store.save(task)   # 不应抛异常

    assert state["attempts"] >= 4, f"应发生重试，实际 {state['attempts']}"
    path = settings.tasks_dir / task.id / "task.json"
    got = Task(**json.loads(path.read_text(encoding="utf-8")))
    assert got.id == task.id
    assert got.status.value == "running"
    assert not path.with_suffix(".json.tmp").exists(), "不应残留孤儿 .tmp"


def test_store_save_concurrent_writers_no_corruption(tmp_path):
    """多会话线程并发 save 同一任务 → 写锁串行化：最终文件是完整一致 JSON
    （不损坏/不丢终态），无孤儿 .tmp（修复前并发写同一 .tmp 可能互相覆盖+替换失败）。
    """
    settings = Settings(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data")
    settings.ensure_dirs()
    store = TaskStore(settings)
    tid = "t_save_concurrent"
    a = Task(id=tid, status=TaskStatus.RUNNING, params={"who": "a"})
    b = Task(id=tid, status=TaskStatus.PAUSED, params={"who": "b"})

    def worker(task: Task):
        for _ in range(60):
            store.save(task)

    ta = threading.Thread(target=worker, args=(a,), daemon=True)
    tb = threading.Thread(target=worker, args=(b,), daemon=True)
    ta.start()
    tb.start()
    ta.join(timeout=20)
    tb.join(timeout=20)
    assert not ta.is_alive() and not tb.is_alive(), "并发 save 线程应正常结束"

    path = settings.tasks_dir / tid / "task.json"
    got = Task(**json.loads(path.read_text(encoding="utf-8")))
    assert got.id == tid
    assert got.status.value in ("running", "paused")
    assert not path.with_suffix(".json.tmp").exists(), "不应残留孤儿 .tmp"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
