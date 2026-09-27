# -*- coding: utf-8 -*-
"""断点续传单测（.done + manifest，E5 验证模式落地）。"""
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


def test_done_marker_nested_dir(tmp_path, app_state):
    """块 key 含子路径：mark_done 必须创建父目录（E5 真实 bug 回归）。"""
    settings = Settings.load()
    store = ResumableStore(tmp_path / "task", settings)
    store.mark_done("2m_temperature/2020/01")
    assert store.is_done("2m_temperature/2020/01")
    assert (tmp_path / "task" / "2m_temperature" / "2020" / "01.done").is_file()


def test_pending_blocks_skip_done(tmp_path, app_state):
    settings = Settings.load()
    store = ResumableStore(tmp_path / "task", settings)
    store.mark_done("2m_temperature/2020/01")
    pending = store.pending_blocks(BLOCKS)
    keys = [b["key"] for b in pending]
    assert "2m_temperature/2020/01" not in keys
    assert len(keys) == 2


def test_manifest_persist(tmp_path, app_state):
    """mark_done 落 .done 标记；pending_blocks 同步 manifest。"""
    settings = Settings.load()
    store = ResumableStore(tmp_path / "task", settings)
    store.mark_done("total_precipitation/2020/01")
    assert store.is_done("total_precipitation/2020/01")
    store.pending_blocks(BLOCKS)
    manifest = store.load()
    assert manifest.get("total_precipitation/2020/01", {}).get("status") == "done"
