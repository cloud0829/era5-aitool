# -*- coding: utf-8 -*-
"""数据管理：catalog 纯函数测试（design-data-manager.md T01/T05）。

覆盖：反解析三类路径 + 失败降级；human_size 边界；scan_cache_files
（空目录/降级文件/busy 标记/size 降序）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from era5tool.data_manager.catalog import (human_size, parse_rel_path,
                                           scan_cache_files)


# ---------------------------------------------------------------------------
# parse_rel_path：三类合法路径
# ---------------------------------------------------------------------------
def test_parse_year_level():
    p = parse_rel_path("reanalysis-era5-single-levels/2m_temperature/monthly/2020.nc")
    assert p is not None
    assert p.dataset == "reanalysis-era5-single-levels"
    assert p.variable == "2m_temperature"
    assert p.freq == "monthly"
    assert p.year == 2020
    assert p.month is None
    assert p.day is None
    assert p.period == "2020"


def test_parse_month_level():
    p = parse_rel_path("reanalysis-era5-single-levels/2m_temperature/hourly/2020/01.nc")
    assert p is not None
    assert p.freq == "hourly"
    assert p.year == 2020 and p.month == 1 and p.day is None
    assert p.period == "2020-01"


def test_parse_day_level():
    p = parse_rel_path("reanalysis-era5-land/2m_temperature/hourly/2020/01/05.nc")
    assert p is not None
    assert p.freq == "hourly"
    assert (p.year, p.month, p.day) == (2020, 1, 5)
    assert p.period == "2020-01-05"


def test_parse_zero_padded_day():
    p = parse_rel_path("ds/var/hourly/2020/12/31.nc")
    assert p is not None
    assert p.period == "2020-12-31"


# ---------------------------------------------------------------------------
# parse_rel_path：失败形态返回 None（不阻断、降级）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rel", [
    "foo.nc",                          # 段数不足
    "cache/foo.nc",                    # 非 dataset/var/freq/... 形态
    "ds/var/daily/2020.nc",            # freq 非法（非 hourly/monthly）
    "ds/var/hourly/2020/13.nc",        # 月份越界
    "ds/var/hourly/2020/01/00.nc",     # 日 0 越界
    "ds/var/hourly/2020/1/2/3/4.nc",   # 数字段 4 个（超 3）
    "ds/var/hourly/2020/abc.nc",       # 非纯数字
    "ds/var/hourly.txt",               # 非 .nc
    "", None, 123,
])
def test_parse_invalid_returns_none(rel):
    assert parse_rel_path(rel) is None


# ---------------------------------------------------------------------------
# human_size（1000 进制，>=1KB 保留 1 位小数）
# ---------------------------------------------------------------------------
def test_human_size_b():
    assert human_size(0) == "0 B"
    assert human_size(512) == "512 B"
    assert human_size(999) == "999 B"


def test_human_size_kb():
    assert human_size(1000) == "1.0 KB"
    assert human_size(1234) == "1.2 KB"
    assert human_size(999_999) == "1000.0 KB"


def test_human_size_large():
    assert human_size(1_234_567) == "1.2 MB"
    assert human_size(1_234_567_890) == "1.2 GB"
    assert human_size(1_234_567_890_123) == "1.2 TB"


def test_human_size_bad_input():
    assert human_size(None) == "0 B"
    assert human_size(-5) == "0 B"


# ---------------------------------------------------------------------------
# scan_cache_files
# ---------------------------------------------------------------------------
def _write(root: Path, rel: str, size: int) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


def test_scan_empty_dir(tmp_path):
    assert scan_cache_files(tmp_path / "no_such_dir", {}) == []
    empty = tmp_path / "empty"
    empty.mkdir()
    assert scan_cache_files(empty, {}) == []


def test_scan_parses_and_sorted_by_size(tmp_path):
    root = tmp_path / "cache"
    _write(root, "ds/2m_temperature/monthly/2020.nc", 300)
    _write(root, "ds/2m_temperature/hourly/2020/01.nc", 100)
    _write(root, "ds/2m_temperature/hourly/2020/01/05.nc", 200)
    entries = scan_cache_files(root, {})
    # size 降序默认
    assert [e.size for e in entries] == [300, 200, 100]
    by_rel = {e.rel_path: e for e in entries}
    assert by_rel["ds/2m_temperature/monthly/2020.nc"].period == "2020"
    assert by_rel["ds/2m_temperature/hourly/2020/01.nc"].period == "2020-01"
    assert by_rel["ds/2m_temperature/hourly/2020/01/05.nc"].period == "2020-01-05"
    e = by_rel["ds/2m_temperature/hourly/2020/01/05.nc"]
    assert e.status == "ready"
    assert e.busy_by == []
    assert e.parsed is True
    assert e.human_size == "200 B"


def test_scan_unparsed_degrades_but_not_block(tmp_path):
    root = tmp_path / "cache"
    _write(root, "foo.nc", 100)                       # 降级
    _write(root, "ds/var/hourly/2020/01/05.nc", 500)  # 合法
    entries = scan_cache_files(root, {})
    assert len(entries) == 2
    foo = next(e for e in entries if e.rel_path == "foo.nc")
    assert foo.parsed is False
    assert foo.dataset == "未知"
    assert foo.variable == "foo"
    assert foo.freq == "" and foo.period == ""
    assert foo.status == "ready"


def test_scan_busy_flags(tmp_path):
    root = tmp_path / "cache"
    _write(root, "ds/var/hourly/2020/01/05.nc", 100)
    busy = {"ds/var/hourly/2020/01/05.nc": ["t_running_1"]}
    entries = scan_cache_files(root, busy)
    assert len(entries) == 1
    e = entries[0]
    assert e.status == "busy"
    assert e.busy_by == ["t_running_1"]
