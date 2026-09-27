# -*- coding: utf-8 -*-
"""QA Round 1 边缘缺陷回归（drain-wait/settle 修复域）。

缺陷1（必修，确定性复现）：
  resume() 在锁内同步把任务置 RUNNING 落盘后才起新会话线程（orchestrator.resume
  L162-166）。若 cancel 恰落在「resume 已置 RUNNING、新会话线程尚未执行到
  _run_download 入口 cancel.flag 检查」的窗口：cancel() 见 status==RUNNING 只写
  cancel.flag + 广播“取消已请求”，不转 paused；新会话线程命中 flag 时 status==
  RUNNING，而旧分支只处理 PENDING→PAUSED，RUNNING 直接 save+return → 任务永久卡
  running（无 worker/无 pool）→ resume 被拒（需 PAUSED/FAILED）、delete 被拒
  （需非 RUNNING）→ 彻底砖化。修复：早退分支对 RUNNING 同样 transition(PAUSED)
  （RUNNING→PAUSED 状态机合法；能走到入口且 flag 存在 ⇒ 本会话尚未启动任何下载，
  转 paused 安全），emit 固定发 paused。

缺陷2（先验证，结论=已被外层兜底，不改代码）：
  QA 怀疑 run_blocks 在自身 finally 之前抛异常（如 cds_channel.run_blocks 内 pool
  构造在 try/finally 之前失败，L404-407）时不会 set settle → 下次 resume 的
  _wait_previous_drain 永久空等。实证：run_blocks 抛异常时 _run_download 的
  settle_handed 仍为 False（L364 只在 run_blocks 正常返回后置 True），外层 finally
  （L433-434：settle_event is not None and not settle_handed → set()）必然兜底置位
  并清理 → _settle_events 无残留 → 下一次 resume/_wait_previous_drain 立即放行。
  下方第 2 条测试对此路径做真实取证（monkeypatch pool 构造抛异常）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from era5tool.acquisition.cds_channel import CdsChannel
from era5tool.config.schema import RequestSchema
from era5tool.config.settings import DownloadSettings, Settings
from era5tool.core.events import EventBroker
from era5tool.core.orchestrator import Orchestrator
from era5tool.models.task import TaskStatus, TaskType


def _payload(start: str = "2020-01-01", end: str = "2020-06-30") -> dict:
    """6 个 month 块（month 粒度，控制回归测试时长）。"""
    return {
        "dataset": "reanalysis-era5-single-levels", "dataset_family": "era5-single",
        "variables": ["2m_temperature"], "timerange": {"start": start, "end": end},
        "area": {"west": 118, "south": 29, "east": 123, "north": 34},
        "frequency": "hourly", "aggregation": "raw", "confidence": 0.9,
    }


def _fast_cfg(self) -> Dict[str, Any]:
    """快块 mock 配置（确定性、不探测 aria2）。"""
    d = self.settings.download
    return {
        "mock": True, "mock_delay": 0.01, "fail_rate": 0.0, "seed": 7,
        "retry_max": d.retry_max, "backoff_base": d.backoff_base,
        "backoff_factor": d.backoff_factor, "backoff_max": d.backoff_max,
        "backoff_jitter": d.backoff_jitter,
    }


def _make_orch(tmp_path, monkeypatch, workers: int = 2):
    settings = Settings(
        download=DownloadSettings(mock=True, cds_max_workers=workers,
                                  chunk_granularity="month", aria2_enabled=False,
                                  retry_max=2),
        config_dir=tmp_path / "cfg", data_dir=tmp_path / "data")
    settings.ensure_dirs()
    monkeypatch.setattr(CdsChannel, "worker_cfg", _fast_cfg)
    return Orchestrator(settings, EventBroker()), settings


def _prepare_blocks(orch: Orchestrator) -> List[Dict[str, Any]]:
    schema = RequestSchema(**_payload())
    cds_req = orch.normalizer.normalize(schema, granularity="month")
    return orch.channel.prepare_blocks(cds_req)


def _wait_status(orch, task_id, statuses, timeout: float = 60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = orch.get(task_id)
        if t.status.value in statuses:
            return t
        time.sleep(0.03)
    raise TimeoutError(
        f"task {task_id} 未进入 {statuses}，当前 {orch.get(task_id).status.value}")


# ---------------------------------------------------------------------------
# 缺陷1 回归：RUNNING + cancel.flag 早退 → paused（不砖化），可 resume/delete
# ---------------------------------------------------------------------------
def test_running_cancel_flag_early_exit_goes_paused_not_bricked(tmp_path,
                                                                monkeypatch):
    """构造「resume 已置 RUNNING + cancel.flag 存在」→ 同步执行 _run_download →
    最终 paused（不砖化），且能正常 resume→success / delete。

    修复前：早退分支只放行 PENDING→PAUSED，RUNNING 直接 save+return → 永久卡
    running（resume 与 delete 均被拒）。
    """
    orch, settings = _make_orch(tmp_path, monkeypatch)
    blocks = _prepare_blocks(orch)
    assert blocks, "应有块用于后续 resume 真实下载"

    # 构造任务并持久化为 RUNNING（等价 resume() 锁内同步置位后的落盘状态）
    task = orch.store.create(TaskType.DOWNLOAD,
                             {"request_schema": _payload(),
                              "chunk_granularity": "month"})
    tid = task.id
    t = orch.get(tid)
    t.transition(TaskStatus.RUNNING)
    orch.store.save(t)
    # cancel 恰落在「resume 已置 RUNNING、新会话线程尚未执行到 flag 检查」的窗口
    (orch.store.task_dir(tid) / "cancel.flag").write_text("cancel", encoding="utf-8")

    # 同步执行会话线程体（等价新会话线程执行 _run_download）
    orch._run_download(tid, blocks)

    paused = orch.get(tid)
    assert paused.status.value == "paused", \
        f"RUNNING+cancel.flag 早退应转 paused（不砖化），实际 {paused.status.value}"

    # 不砖化：可再次 resume → success；可 delete
    orch.resume(tid)
    done = _wait_status(orch, tid, {"success", "failed", "paused"})
    assert done.status.value == "success", \
        f"砖化解除后 resume 应 success，实际 {done.status.value} error={done.error}"
    assert done.block_stats.done == done.block_stats.total
    assert orch.delete(tid) is True


# ---------------------------------------------------------------------------
# 缺陷2 实证：run_blocks 在自身 finally 之前抛异常 → 外层 finally 兜底 set settle
# ---------------------------------------------------------------------------
def test_run_blocks_pool_ctor_failure_settle_covered_no_hang(tmp_path,
                                                             monkeypatch):
    """pool 构造在 try/finally 之前抛异常（run_blocks 不 set settle）→
    _run_download 外层 finally（settle_handed 仍 False）兜底 set 并清理 settle →
    _settle_events 无残留 → 下一次 resume/_wait_previous_drain 不空等。

    结论：该路径已被外层 finally 兜底，无需改动代码（仅保留本取证回归）。
    """
    import era5tool.acquisition.cds_channel as cds_mod
    orch, settings = _make_orch(tmp_path, monkeypatch)
    blocks = _prepare_blocks(orch)
    assert blocks, "需有块才能走到 pool 构造"

    def boom(*args, **kwargs):
        raise RuntimeError("pool 构造失败（模拟）")
    monkeypatch.setattr(cds_mod, "ProcessPoolExecutor", boom)

    task = orch.store.create(TaskType.DOWNLOAD,
                             {"request_schema": _payload(),
                              "chunk_granularity": "month"})
    tid = task.id
    # 同步执行会话线程体（真实会走到 run_blocks 的 ProcessPoolExecutor 构造）
    orch._run_download(tid, blocks)

    assert orch.get(tid).status.value == "failed", \
        f"pool 构造失败应使任务 failed，实际 {orch.get(tid).status.value}"
    with orch._lock:
        assert orch._settle_events.get(tid) is None, \
            "外层 finally 应已 set 并清理 settle，_settle_events 不应残留未置位事件"
    # 下一次 resume 的等待入口立即放行（无未置位 settle → 不空等）
    assert orch._wait_previous_drain(tid, threading.Event()) is True

    # 可再次 resume（FAILED→RUNNING 合法）；pool 仍抛异常 → 快速 failed（不砖化、不挂起）
    orch.resume(tid)
    t = _wait_status(orch, tid, {"success", "failed", "paused"})
    assert t.status.value == "failed", \
        f"再次 resume 后 pool 构造失败应快速 failed，实际 {t.status.value}"
    assert orch.delete(tid) is True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
