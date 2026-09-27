# -*- coding: utf-8 -*-
"""QA：断点续传失败块语义（design-final.md §8.2：resume 跳过 done，重下 failed/missing）。

重点验证：mark_failed 落盘的标记不能被当作 done 跳过。
"""
from __future__ import annotations

from era5tool.config.settings import Settings
from era5tool.core.resumable import ResumableStore

BLOCKS = [
    {"key": "2m_temperature/2020/01", "variable": "2m_temperature",
     "year": 2020, "month": 1},
    {"key": "2m_temperature/2020/02", "variable": "2m_temperature",
     "year": 2020, "month": 2},
    {"key": "total_precipitation/2020/01", "variable": "total_precipitation",
     "year": 2020, "month": 1},
]


def test_failed_marker_should_not_be_skipped_on_resume(tmp_path, app_state):
    """失败块必须被重下：mark_failed 后 pending_blocks 应仍包含该块。"""
    settings = Settings.load()
    store = ResumableStore(tmp_path / "task", settings)
    store.mark_failed("2m_temperature/2020/01", "BUSY_AFTER_RETRIES")
    pending = store.pending_blocks(BLOCKS)
    keys = [b["key"] for b in pending]
    assert "2m_temperature/2020/01" in keys, \
        "失败块被当作 done 跳过，resume 将永不重下（数据完整性缺陷）"


def test_done_and_failed_markers_distinguishable(tmp_path, app_state):
    """done 标记与 failed 标记必须可区分（内容不同）。"""
    settings = Settings.load()
    store = ResumableStore(tmp_path / "task", settings)
    store.mark_done("2m_temperature/2020/01")
    store.mark_failed("total_precipitation/2020/01", "BUSY_AFTER_RETRIES")
    m1 = (tmp_path / "task" / "2m_temperature" / "2020" / "01.done").read_text(encoding="utf-8")
    m2 = (tmp_path / "task" / "total_precipitation" / "2020" / "01.done").read_text(encoding="utf-8")
    assert m1 == "done"
    assert m2.startswith("failed:")
