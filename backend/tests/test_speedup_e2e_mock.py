# -*- coding: utf-8 -*-
"""T03：编排层适配 e2e（design-speedup-download.md §3.5）。

覆盖（mock CDS，不消耗真实配额）：
1. submit 落盘瘦身：params 含 chunk_granularity / warnings；params["blocks"]
   不含 request 字典（§7-③，避免响应体膨胀）。
2. day 粒度全链路 SUCCESS：result.files 由 manifest + rel_target 全量重建，
   数量 == total，且全部落盘（文件不全 → 出图缺数据）。
3. resume 粒度锁定：提交 day → 中途改配置为 month → resume 仍按 day 重建
   （块 key 不漂移，已完成块被跳过、不重复下载）。
4. 落盘节流：_persist_throttled 写盘次数 ≤ 块数/progress_persist_every + 3。
5. files 重建：_rebuild_result_files 只收集 manifest 中 status==done 的块。
"""
from __future__ import annotations

import os
import time

import pytest

from era5tool.acquisition.cds_channel import CdsChannel
from era5tool.config.schema import RequestSchema
from era5tool.config.settings import DownloadSettings, Settings
from era5tool.core.events import EventBroker
from era5tool.core.orchestrator import Orchestrator
from era5tool.core.resumable import ResumableStore
from era5tool.models.task import BlockStats, TaskStatus, TaskType


def _payload(family: str = "era5-single", variables=None,
            start: str = "2020-01-01", end: str = "2020-12-31") -> dict:
    return {
        "dataset": {
            "era5-single": "reanalysis-era5-single-levels",
            "era5-pressure": "reanalysis-era5-pressure-levels",
            "era5-monthly": "reanalysis-era5-single-levels-monthly-means",
            "land": "reanalysis-era5-land",
            "land-monthly": "reanalysis-era5-land-monthly-means",
        }[family],
        "dataset_family": family,
        "variables": variables or ["2m_temperature"],
        "timerange": {"start": start, "end": end},
        "area": {"west": 118, "south": 29, "east": 123, "north": 34},
        "frequency": "hourly", "aggregation": "raw", "confidence": 0.9,
    }


def _make_orch(tmp_path, **dl_kwargs):
    """构造隔离的 Settings + Orchestrator（不污染 conftest 共享状态）。"""
    settings = Settings(
        download=DownloadSettings(mock=True, cds_max_workers=2, **dl_kwargs),
        config_dir=tmp_path, data_dir=tmp_path)
    return Orchestrator(settings, EventBroker()), settings


def _wait(orch: Orchestrator, task_id: str, timeout: float = 40.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = orch.get(task_id)
        if t.status.value not in ("pending", "running"):
            return t
        time.sleep(0.05)
    raise TimeoutError(f"task {task_id} 未在 {timeout}s 内终止")


def _slow_cfg(self):
    d = self.settings.download
    return {"mock": True, "mock_delay": 0.1, "fail_rate": 0.0, "seed": 7,
            "retry_max": d.retry_max, "backoff_base": d.backoff_base,
            "backoff_factor": d.backoff_factor, "backoff_max": d.backoff_max,
            "backoff_jitter": d.backoff_jitter}


# ---------------------------------------------------------------------------
# 1. submit 落盘瘦身
# ---------------------------------------------------------------------------
def test_submit_params_slim_and_granularity(tmp_path):
    """submit：params 含 chunk_granularity/warnings；params["blocks"] 不含 request
    （§3.5 / §7-③，验收点①）。"""
    orch, _ = _make_orch(tmp_path, chunk_granularity="day")
    task = orch.submit(RequestSchema(**_payload(start="2020-01-01",
                                                end="2020-01-31")))
    # 2020-01 全月 day 切块 → 31 块
    assert task.params["chunk_granularity"] == "day"
    assert isinstance(task.params["warnings"], list)
    assert "blocks" in task.params
    assert len(task.params["blocks"]) == 31
    # 瘦身：不含 request 字典（体积敏感，验收点①）
    assert "request" not in task.params["blocks"][0]
    # 可重建字段齐全
    b0 = task.params["blocks"][0]
    for k in ("key", "variable", "year", "month", "day", "dataset",
              "freq", "rel_target"):
        assert k in b0
    assert b0["key"] == "2m_temperature/2020/01/01"


# ---------------------------------------------------------------------------
# 2. day 粒度全链路 SUCCESS + files 全量重建
# ---------------------------------------------------------------------------
def test_day_granularity_full_success_and_files_rebuild(tmp_path):
    """day 粒度全链路 SUCCESS；result.files 全量重建（== total 且全部落盘）。"""
    orch, _ = _make_orch(tmp_path, chunk_granularity="day")
    task = orch.submit(RequestSchema(**_payload(start="2020-01-01",
                                                end="2020-01-31")))
    task = _wait(orch, task.id)
    assert task.status.value == "success"
    assert task.block_stats.done == 31
    assert task.block_stats.total == 31
    # result.files 由 manifest + rel_target 全量重建
    files = task.result["files"]
    assert len(files) == 31, f"files 应全量重建为 31，实际 {len(files)}"
    # 全部文件确实落盘
    for f in files:
        assert os.path.isfile(f), f"文件未落盘: {f}"


# ---------------------------------------------------------------------------
# 3. resume 粒度锁定（配置变动不漂移）
# ---------------------------------------------------------------------------
def test_resume_granularity_lock_no_drift(tmp_path, monkeypatch):
    """提交 day（全月 31 块，全成功）→ 中途改配置为 month → resume 仍按 day 重建
    （块 key 不漂移，已完成块被跳过、不重复下载，验收点②）。"""
    orch, settings = _make_orch(tmp_path, chunk_granularity="day")
    task = orch.submit(RequestSchema(**_payload(start="2020-01-01",
                                                end="2020-01-31")))
    task = _wait(orch, task.id)
    assert task.status.value == "success"
    assert task.block_stats.done == 31

    # 模拟任务需续传：手动置为 paused（不重新下载），保持 31 个 .done 标记
    task = orch.get(task.id)
    task.status = TaskStatus.PAUSED
    orch.store.save(task)

    # 模拟用户中途把配置改成 month
    settings.download.chunk_granularity = "month"
    orch.resume(task.id)
    t = _wait(orch, task.id)
    assert t.status.value == "success"
    # 全部已完成块被跳过，不重复下载（pending 为空 → skipped == total）
    assert t.block_stats.skipped == 31
    assert t.block_stats.done == 31
    # 粒度锁定：resume 仍按 day 重建（31 块、key 含 3 级斜杠），而非 month（12 块）
    blocks = t.params["blocks"]
    assert len(blocks) == 31, f"应锁定 day 重建 31 块，实际 {len(blocks)}"
    assert all(b["key"].count("/") == 3 for b in blocks), "块 key 应为 day 格式"


# ---------------------------------------------------------------------------
# 4. 落盘节流：_persist_throttled 写盘次数有界
# ---------------------------------------------------------------------------
def test_persist_throttled_save_count(tmp_path, monkeypatch):
    """_persist_throttled 写盘次数 ≤ 块数/progress_persist_every + 3（验收点④）。"""
    every = 20
    orch, _ = _make_orch(tmp_path, progress_persist_every=every,
                         progress_persist_interval_s=10.0)
    saves = {"n": 0}
    orig = orch.store.save

    def spy(t):
        saves["n"] += 1
        return orig(t)

    monkeypatch.setattr(orch.store, "save", spy)
    task = orch.store.create(TaskType.DOWNLOAD,
                              {"blocks": [], "dataset": "x", "family": "land"})
    task.block_stats = BlockStats(total=120)
    for i in range(1, 121):
        task.block_stats.done = i
        orch._persist_throttled(task)
    # 120 块 / every(20) + 3 = 9 上限
    assert saves["n"] <= 120 // every + 3, \
        f"写盘次数 {saves['n']} 超过上限 {120 // every + 3}"
    assert saves["n"] >= 2, "至少首块 + 一次周期应落盘"
    # force 必定落盘
    n0 = saves["n"]
    orch._persist_throttled(task, force=True)
    assert saves["n"] == n0 + 1


# ---------------------------------------------------------------------------
# 5. files 重建：只收集 manifest 中 done 的块
# ---------------------------------------------------------------------------
def test_rebuild_result_files_only_done(tmp_path):
    """_rebuild_result_files 仅收集 manifest status==done 的块（验收点⑥）。"""
    orch, _ = _make_orch(tmp_path)
    task_dir = tmp_path / "taskX"
    task_dir.mkdir(parents=True, exist_ok=True)
    store = ResumableStore(str(task_dir), None)
    # manifest 由编排层维护（mark_done 只写 .done 硬标记，不写 manifest）
    store.save({
        "2m_temperature/2020/01/01": {"status": "done"},
        "2m_temperature/2020/01/02": {"status": "done"},
    })
    blocks = [
        {"key": "2m_temperature/2020/01/01",
         "rel_target": "ds/2m_temperature/hourly/2020/01/01.nc"},
        {"key": "2m_temperature/2020/01/02",
         "rel_target": "ds/2m_temperature/hourly/2020/01/02.nc"},
        {"key": "2m_temperature/2020/01/03",
         "rel_target": "ds/2m_temperature/hourly/2020/01/03.nc"},  # 未完成
    ]
    files = orch._rebuild_result_files(blocks, store)
    assert len(files) == 2, f"应只重建 2 个 done 文件，实际 {len(files)}"
    assert all(os.path.isabs(f) for f in files)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
