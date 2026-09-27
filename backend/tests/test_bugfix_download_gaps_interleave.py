# -*- coding: utf-8 -*-
"""bugfix download-gaps ②：提交顺序去偏 —— 跨变量 / 跨年轮转交错。

问题（线上实证 `data/tasks/t_20260905_043416_261be78b`）：
`Normalizer._split_blocks` 是 `for var: for year: for month:` 的**变量外层循环**
→ 变量 A 的全部块排在列表最前面，变量 B 的全排在后面。配合 run_blocks 的
`remaining.pop(0)` 按序提交，在 CDS 排队/限流墙下后果是**排在后面的变量被饿死**：

    10m_u_component_of_wind : done=29  failed=103
    10m_v_component_of_wind : done=0   failed=69     ← 一个都没抢到

修复：`interleave_blocks()` 做两级轮转（变量内按 year 轮转 → 再跨 variable 轮转），
纯排序、不改变块集合与数量，故不影响断点续传的 key 匹配。

覆盖：
1. interleave_blocks 的交错形态与不变量（集合/数量不变、前缀均匀）；
2. 端到端：高失败率下两个变量都能拿到块（修复前第二变量全灭）；
3. 编排层默认开启交错；
4. 交错不影响断点续传（已 done 的块不被重下）。
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

import pytest

from era5tool.config.schema import Area, RequestSchema, Timerange
from era5tool.config.settings import Settings
from era5tool.core.events import EventBroker, TaskEventBus
from era5tool.core.normalizer import Normalizer, interleave_blocks
from era5tool.core.resumable import ResumableStore
from era5tool.models.task import Task

DATASET = "reanalysis-era5-single-levels"
VAR_U = "10m_u_component_of_wind"
VAR_V = "10m_v_component_of_wind"


def _mk_blocks(var: str, years: List[int], months: range) -> List[Dict[str, Any]]:
    return [{"key": f"{var}/{y}/{m:02d}", "variable": var, "year": y,
             "month": m, "day": None} for y in years for m in months]


def _two_var_blocks() -> List[Dict[str, Any]]:
    """变量外层循环（= _split_blocks 现状）：U 全在前，V 全在后。"""
    return _mk_blocks(VAR_U, [2020], range(1, 13)) + \
        _mk_blocks(VAR_V, [2020], range(1, 13))


# ---------------------------------------------------------------------------
# 1. interleave_blocks 形态与不变量
# ---------------------------------------------------------------------------
def test_interleave_alternates_variables():
    """两个变量严格交替：U/01, V/01, U/02, V/02, ..."""
    out = interleave_blocks(_two_var_blocks())
    assert [b["key"] for b in out[:6]] == [
        f"{VAR_U}/2020/01", f"{VAR_V}/2020/01",
        f"{VAR_U}/2020/02", f"{VAR_V}/2020/02",
        f"{VAR_U}/2020/03", f"{VAR_V}/2020/03",
    ]


def test_interleave_preserves_set_and_length():
    """交错是纯排序：块集合、数量、key 完全不变（不影响断点续传匹配）。"""
    src = _two_var_blocks()
    out = interleave_blocks(src)
    assert len(out) == len(src)
    assert {b["key"] for b in out} == {b["key"] for b in src}
    assert sorted(b["key"] for b in out) == sorted(b["key"] for b in src)


def test_interleave_is_deterministic():
    """确定性（无随机）：同一输入多次调用结果一致 → 测试可复现、行为可预测。"""
    src = _two_var_blocks()
    assert [b["key"] for b in interleave_blocks(src)] == \
           [b["key"] for b in interleave_blocks(src)]


def test_interleave_prefix_is_balanced_across_variables():
    """任意前缀都均匀覆盖各变量（这是"不被饿死"的关键性质）。"""
    out = interleave_blocks(_two_var_blocks())
    for k in (2, 4, 6, 12, 24):
        cnt = Counter(b["variable"] for b in out[:k])
        assert max(cnt.values()) - min(cnt.values()) <= 1, f"前缀 {k} 不均衡: {cnt}"


def test_interleave_also_spreads_years():
    """变量内部按 year 轮转：前缀不会全部落在同一年。"""
    blocks = _mk_blocks(VAR_U, [2005, 2006, 2007], range(1, 4))
    out = interleave_blocks(blocks)
    first_year_months = [(b["year"], b["month"]) for b in out[:6]]
    assert first_year_months == [(2005, 1), (2006, 1), (2007, 1),
                                 (2005, 2), (2006, 2), (2007, 2)]


def test_interleave_edge_cases():
    """空列表 / 单块 / 单变量 都不改变语义。"""
    assert interleave_blocks([]) == []
    one = _mk_blocks(VAR_U, [2020], range(1, 2))
    assert [b["key"] for b in interleave_blocks(one)] == [b["key"] for b in one]
    single_var = _mk_blocks(VAR_U, [2020], range(1, 4))
    assert len(interleave_blocks(single_var)) == 3


def test_interleave_handles_uneven_groups():
    """变量数量不等长（A 12 块、B 3 块）时不丢块、不重复。"""
    src = _mk_blocks(VAR_U, [2020], range(1, 13)) + _mk_blocks(VAR_V, [2020], range(1, 4))
    out = interleave_blocks(src)
    assert len(out) == 15
    assert len({b["key"] for b in out}) == 15


# ---------------------------------------------------------------------------
# 2. 与真实切块链路对接
# ---------------------------------------------------------------------------
def _schema(variables: List[str], start: str = "2020-01-01",
            end: str = "2020-12-31") -> RequestSchema:
    return RequestSchema(
        dataset_family="era5-single", variables=variables, pressure_levels=None,
        timerange=Timerange(start=start, end=end),
        area=Area(west=118, south=29, east=123, north=34),
    )


def test_split_blocks_still_var_major_interleave_fixes_order():
    """_split_blocks 保持变量外层（既有断言依赖），交错在提交前做。"""
    cds = Normalizer(granularity="month").normalize(_schema([VAR_U, VAR_V]))
    # 既有行为：变量外层循环
    assert cds.blocks[0]["key"].startswith(VAR_U)
    assert cds.blocks[-1]["key"].startswith(VAR_V)
    # 交错后：首块 U、次块 V
    out = interleave_blocks(cds.blocks)
    assert out[0]["key"].startswith(VAR_U)
    assert out[1]["key"].startswith(VAR_V)


# ---------------------------------------------------------------------------
# 3. 端到端：高失败率下两个变量都不会被饿死
# ---------------------------------------------------------------------------
def _isolated_settings(tmp_path: Path, monkeypatch, **overrides) -> Settings:
    monkeypatch.delenv("ERA5_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ERA5_DATA_DIR", raising=False)
    s = Settings.load(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data")
    s.download.mock = True
    s.download.chunk_granularity = "month"
    s.download.retry_max = 2
    s.download.backoff_base = 0.01
    s.download.backoff_factor = 2.0
    s.download.backoff_max = 0.05
    s.download.backoff_jitter = 0.0
    s.download.adaptive_concurrency = False      # 隔离变量：本用例只看顺序效应
    for k, v in overrides.items():
        setattr(s.download, k, v)
    return s


def _prepare_blocks(settings: Settings, variables: List[str],
                    start: str, end: str) -> List[Dict[str, Any]]:
    from era5tool.acquisition.cds_channel import CdsChannel
    cds = Normalizer(granularity="month").normalize(_schema(variables, start, end))
    return CdsChannel(settings).prepare_blocks(cds)


def test_e2e_interleaved_order_gives_every_variable_a_share(tmp_path, monkeypatch):
    """端到端（mock + 高失败率）：交错后**每个变量**都能拿到成功块。

    修复前（变量外层 + 高失败率）：前面的变量吃掉几乎所有并发额度，后面的变量
    往往一个都拿不到（线上 10m_v = 0）。
    """
    settings = _isolated_settings(tmp_path, monkeypatch, cds_max_workers=4)
    monkeypatch.setenv("ERA5_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ERA5_DATA_DIR", str(tmp_path / "data"))
    from era5tool.acquisition.cds_channel import CdsChannel

    # 关键：mock 的 FakeCdsClient 用固定 seed=7 时，**每个块共享同一随机序列**
    # （worker_cfg 每会话只下发一次 seed，所有块的首次 random() 相同）→ 高失败率下
    # 全块同进退（全灭或全成），无法体现"交错公平性"。这里把 seed 置 None（系统
    # 随机），让每个 worker 的 client 有独立随机起点 → 各块命运真正独立。
    def _worker_cfg(self, fail_rate: float = 0.0) -> Dict[str, Any]:
        d = self.settings.download
        return {
            "mock": True, "mock_delay": 0.01, "fail_rate": 0.55, "seed": None,
            "retry_max": d.retry_max, "backoff_base": 0.01, "backoff_factor": 2.0,
            "backoff_max": 0.05, "backoff_jitter": 0.0,
            "aria2_enabled": False, "aria2_bin": "", "aria2_connections": 8,
            "aria2_timeout_s": 1800, "submit_stagger_s": 0.0,
            "mock_error_mode": "retryable_429", "throttle_enabled": False,
        }

    monkeypatch.setattr(CdsChannel, "worker_cfg", _worker_cfg)

    blocks = _prepare_blocks(settings, [VAR_U, VAR_V], "2020-01-01", "2021-12-31")
    assert len(blocks) == 48                     # 2 变量 × 2 年 × 12 月
    ordered = interleave_blocks(blocks)          # 编排层默认做的交错

    task = Task(id="t_interleave")
    task_dir = settings.tasks_dir / task.id
    task_dir.mkdir(parents=True, exist_ok=True)
    store = ResumableStore(task_dir, settings)
    bus = TaskEventBus(task.id, task_dir, EventBroker())
    channel = CdsChannel(settings)

    bus.start()
    try:
        results = channel.run_blocks(task, ordered, store, bus, None,
                                     fail_rate=0.55)
    finally:
        bus.stop()

    per_var = Counter()
    for r in results:
        if r["status"] == "done":
            per_var[r["block"].split("/")[0]] += 1
    # 交错后每个变量至少应拿到 1 个成功块（两个变量"公平共享"并发额度）；
    # 48 块独立随机（成功概率 ≥1-0.45³≈0.91）→ 单变量 24 块全失败概率 ≈ 0，非 flaky。
    assert per_var[VAR_U] > 0, f"U 变量应有成功块，实际 {dict(per_var)}"
    assert per_var[VAR_V] > 0, f"V 变量被饿死（交错失效），实际 {dict(per_var)}"


def test_e2e_var_major_order_can_starve_second_variable(tmp_path, monkeypatch):
    """对照实验（复现原 bug 的机理）：变量外层顺序 + 只提交前 N 个块时，
    第二个变量一个块都排不上——这正是线上 10m_v 全灭的原因。

    用例不依赖随机（不发起真实下载），只证明"顺序决定谁能先拿到并发额度"。
    """
    settings = _isolated_settings(tmp_path, monkeypatch, cds_max_workers=4)
    from era5tool.acquisition.cds_channel import CdsChannel
    blocks = _prepare_blocks(settings, [VAR_U, VAR_V], "2020-01-01", "2021-12-31")
    assert len(blocks) == 48

    # 变量外层：前 24 个块全是 U（V 一个都排不上）
    var_major_first_24 = blocks[:24]
    assert all(b["variable"] == VAR_U for b in var_major_first_24)

    # 交错后：前 24 个块 U/V 各 12 个
    interleaved_first_24 = interleave_blocks(blocks)[:24]
    cnt = Counter(b["variable"] for b in interleaved_first_24)
    assert cnt[VAR_U] == 12 and cnt[VAR_V] == 12


# ---------------------------------------------------------------------------
# 4. 交错不影响断点续传
# ---------------------------------------------------------------------------
def test_interleave_does_not_affect_resume_skipping(tmp_path, monkeypatch):
    """交错只改变提交顺序：已 done 的块仍被 pending_blocks 跳过，不会重下。"""
    settings = _isolated_settings(tmp_path, monkeypatch, cds_max_workers=2)
    blocks = _prepare_blocks(settings, [VAR_U, VAR_V], "2020-01-01", "2020-12-31")
    task_dir = settings.tasks_dir / "t_resume_order"
    task_dir.mkdir(parents=True, exist_ok=True)
    store = ResumableStore(task_dir, settings)

    # 标记若干块为已完成（含两个变量各一些）
    for b in blocks[::4]:
        store.mark_done(b["key"])

    ordered = interleave_blocks(blocks)
    pending = store.pending_blocks(ordered)
    done_keys = {b["key"] for b in blocks[::4]}
    assert done_keys & {b["key"] for b in pending} == set()
    assert len(pending) == len(blocks) - len(done_keys)
