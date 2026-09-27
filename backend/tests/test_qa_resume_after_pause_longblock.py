# -*- coding: utf-8 -*-
"""QA：真实长块语义下“取消(cancel) 后继续(resume)”回归（本次 Bug 修复）。

Bug 现象：真实 CDS 下载（块耗时分钟级）中点击暂停 → 任务 paused → 点击继续后
无法正常继续下载。mock（0.02s 快块）下旧 resume 测试全绿掩盖了缺陷。

根因（实证复现）：暂停只取消“未开始的块”；在跑块的真实下载（cdsapi.retrieve 阻塞
分钟级，无法被 kill）由旧 worker 继续跑完并写 .done。若 resume 不等待这些旧 worker
真正退出就立即发起新下载，新一轮会话会把旧在跑块重新判为 pending → 新旧两会话
**并发下载同一块**：重复 CDS 请求 + 并发写同一 .nc（Windows 下相互截断/覆盖导致
产物损坏甚至任务失败）。旧 run_blocks 取消路径 shutdown(wait=False, cancel_futures=True)
快速返回，且 ProcessPoolExecutor 二次 shutdown(wait=True) 不会真正等待在跑 worker
（_shutdown_thread 已置位即返回）——这是 mock 掩盖真实缺陷的机制根源。

修复：run_blocks 新增 settle_event —— 取消路径在 shutdown 前捕获 worker 进程句柄，
由守护线程轮询至其全部退出后置位；编排层 resume 的新会话先等上一会话 settle
（期间旧块写完 .done），再重建 pending（跳过旧块）→ 不重复下载、不并发写文件。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List

from era5tool.acquisition.cds_channel import CdsChannel
from era5tool.config.schema import RequestSchema
from era5tool.config.settings import DownloadSettings, Settings
from era5tool.core.events import EventBroker
from era5tool.core.orchestrator import Orchestrator


def _payload(start: str = "2020-01-01", end: str = "2020-06-30") -> dict:
    """6 个 month 块（month 粒度，控制回归测试时长）。"""
    return {
        "dataset": "reanalysis-era5-single-levels", "dataset_family": "era5-single",
        "variables": ["2m_temperature"], "timerange": {"start": start, "end": end},
        "area": {"west": 118, "south": 29, "east": 123, "north": 34},
        "frequency": "hourly", "aggregation": "raw", "confidence": 0.9,
    }


def _slow_cfg(delay: float):
    """慢速长块 worker 配置（真实 cdsapi 分钟级下载的 mock 近似）。"""
    def _cfg(self):
        d = self.settings.download
        return {
            "mock": True, "mock_delay": delay, "fail_rate": 0.0, "seed": 7,
            "retry_max": d.retry_max, "backoff_base": d.backoff_base,
            "backoff_factor": d.backoff_factor, "backoff_max": d.backoff_max,
            "backoff_jitter": d.backoff_jitter,
        }
    return _cfg


def _make_orch(tmp_path, monkeypatch, delay: float, workers: int = 2):
    settings = Settings(
        download=DownloadSettings(mock=True, cds_max_workers=workers,
                                  chunk_granularity="month", aria2_enabled=False,
                                  retry_max=2),
        config_dir=tmp_path / "cfg", data_dir=tmp_path / "data")
    settings.ensure_dirs()
    monkeypatch.setattr(CdsChannel, "worker_cfg", _slow_cfg(delay))
    return Orchestrator(settings, EventBroker()), settings


def _wait_status(orch, task_id, statuses, timeout: float = 90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = orch.get(task_id)
        if t.status.value in statuses:
            return t
        time.sleep(0.03)
    raise TimeoutError(
        f"task {task_id} 未进入 {statuses}，当前 {orch.get(task_id).status.value}")


def _wait_download_starts(task_dir: Path, n: int, timeout: float = 30.0) -> None:
    """等待 events.jsonl 中出现 ≥ n 个块 “开始下载” 事件。

    确保取消前确有 worker 已开始跑长块（避免 spawn 慢导致的“未在跑就取消”假阳性）。
    """
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


# ---------------------------------------------------------------------------
# 1. 核心回归：暂停 → 立即 resume（不等旧 worker 退出）→ 成功且不重复下载
# ---------------------------------------------------------------------------
def test_pause_immediate_resume_no_duplicate_download(tmp_path, monkeypatch):
    """长块下载中暂停 → **立即** resume：最终 success、每块最多下载 1 次。

    修复前：resume 不等旧在跑 worker（真实 retrieve 分钟级不可中断）退出，会把它
    尚未写 .done 的块重新判为 pending → 新旧两会话并发下载同一块（start>1）。
    """
    orch, settings = _make_orch(tmp_path, monkeypatch, delay=1.0, workers=2)
    task = orch.submit(RequestSchema(**_payload()))
    tid = task.id
    task_dir = orch.store.task_dir(tid)
    _wait_status(orch, tid, {"running"})
    _wait_download_starts(task_dir, n=2)   # 2 个 worker 确已在跑长块
    orch.cancel(tid)
    _wait_status(orch, tid, {"paused"})
    # 关键：立即 resume，**不**等待旧 worker 退出（修复前在此处触发并发冲突）
    orch.resume(tid)
    t = _wait_status(orch, tid, {"success", "failed", "paused"})
    assert t.status.value == "success", \
        f"暂停后立即 resume 应 success，实际 {t.status.value} error={t.error}"
    assert t.block_stats.done == t.block_stats.total
    assert t.block_stats.failed == 0
    dup = {k: v for k, v in _download_starts(task_dir).items() if v > 1}
    assert not dup, f"resume 重复下载了在跑块: {dup}"
    # 产物全部落盘且可解析（无并发写坏的文件）
    for rel in [b["rel_target"] for b in t.params["blocks"]]:
        f = settings.cache_dir / rel
        assert f.is_file(), f"产物缺失: {f}"
        with open(f, "r", encoding="utf-8") as fh:
            json.load(fh)


# ---------------------------------------------------------------------------
# 2. 旧 worker 暂停后稍后才 mark_done 的块，resume 后按 .done 跳过（不重复）
# ---------------------------------------------------------------------------
def test_pause_old_worker_late_done_skipped_on_resume(tmp_path, monkeypatch):
    """暂停后在跑块由旧 worker 跑完并补写 .done → resume 据 .done 跳过，不重下。

    语义确认：resume 等待旧会话收尾后，pending_blocks 对旧 worker 补写 .done 的
    块返回跳过（skipped ≥ 在跑块数），不重复下载。
    """
    orch, settings = _make_orch(tmp_path, monkeypatch, delay=1.0, workers=2)
    task = orch.submit(RequestSchema(**_payload()))
    tid = task.id
    task_dir = orch.store.task_dir(tid)
    _wait_status(orch, tid, {"running"})
    _wait_download_starts(task_dir, n=2)
    orch.cancel(tid)
    _wait_status(orch, tid, {"paused"})
    # 等旧 worker 跑完当前块并补写 .done（mock_delay 1.0s，稍等即完成）
    time.sleep(1.6)
    done_markers = 0
    for p in task_dir.rglob("*.done"):
        if p.read_text(encoding="utf-8").strip() == "done":
            done_markers += 1
    assert done_markers >= 2, f"暂停后旧 worker 应补写 ≥2 个 .done，实际 {done_markers}"

    orch.resume(tid)
    t = _wait_status(orch, tid, {"success", "failed", "paused"})
    assert t.status.value == "success", \
        f"resume 应 success，实际 {t.status.value} error={t.error}"
    assert t.block_stats.skipped >= done_markers, \
        f"resume 应按 .done 跳过旧块，skipped={t.block_stats.skipped}"
    dup = {k: v for k, v in _download_starts(task_dir).items() if v > 1}
    assert not dup, f"resume 重复下载: {dup}"


# ---------------------------------------------------------------------------
# 3. resume 等待上一会话收尾期间再次 cancel → paused；再 resume 仍可成功
# ---------------------------------------------------------------------------
def test_cancel_during_resume_drain_wait_goes_paused_then_resume_succeeds(
        tmp_path, monkeypatch):
    """resume 的新会话在等待旧 worker 收尾期间收到新 cancel → 立即转 paused；
    之后再次 resume 最终 success 且不重复下载。
    """
    orch, settings = _make_orch(tmp_path, monkeypatch, delay=1.0, workers=2)
    task = orch.submit(RequestSchema(**_payload()))
    tid = task.id
    task_dir = orch.store.task_dir(tid)
    _wait_status(orch, tid, {"running"})
    _wait_download_starts(task_dir, n=2)
    orch.cancel(tid)
    _wait_status(orch, tid, {"paused"})

    orch.resume(tid)                    # B 会话：进入 running 后等待旧会话收尾
    _wait_status(orch, tid, {"running"})
    time.sleep(0.15)                     # B 仍在等待旧 worker（delay 1.0s）中
    orch.cancel(tid)                     # 等待期间再次取消
    t = _wait_status(orch, tid, {"paused"}, timeout=15)
    assert t.status.value == "paused"

    orch.resume(tid)                     # 再次 resume → 最终 success
    t = _wait_status(orch, tid, {"success", "failed", "paused"})
    assert t.status.value == "success", \
        f"再次 resume 应 success，实际 {t.status.value} error={t.error}"
    assert t.block_stats.done == t.block_stats.total
    dup = {k: v for k, v in _download_starts(task_dir).items() if v > 1}
    assert not dup, f"多轮 pause/resume 重复下载: {dup}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
