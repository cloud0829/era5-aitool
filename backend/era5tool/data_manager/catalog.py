# -*- coding: utf-8 -*-
"""数据管理：相对路径反解析 + human_size + 扫描建 DTO（design-data-manager.md §3.1）。

纯函数模块（无 IO 依赖，单测友好）；由 DataManagerService 调用。

- parse_rel_path(rel)    : 反解析缓存相对路径 → ParsedParts；失败返回 None（不阻断列表）。
- human_size(bytes)      : B/KB/MB/GB/TB 展示（1000 进制，>=1KB 保留 1 位小数）。
- scan_cache_files(...)  : rglob("*.nc") 建 CacheFileEntry 行（size 降序默认）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from pydantic import BaseModel, Field

# 反解析规则（design-data-manager.md §8）：
#   {dataset}/{variable}/{freq}/{year}.nc
#   {dataset}/{variable}/{freq}/{year}/{mm}.nc
#   {dataset}/{variable}/{freq}/{year}/{mm}/{dd}.nc
# freq 恒为 hourly | monthly；其后为 1~3 个纯数字段。
_FREQS = ("hourly", "monthly")
_UNKNOWN_DATASET = "未知"
_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")


@dataclass
class ParsedParts:
    """反解析结果（内部结构，见设计 §3.1）。"""

    dataset: str
    variable: str
    freq: str                 # hourly | monthly
    year: int
    month: Optional[int] = None
    day: Optional[int] = None
    period: str = ""          # "2020" | "2020-01" | "2020-01-05"


class CacheFileEntry(BaseModel):
    """文件列表行（响应字段，design-data-manager.md §3.1）。"""

    rel_path: str             # 相对缓存根的 POSIX 路径
    dataset: str              # 反解析段0；失败 = "未知"
    variable: str             # 反解析段1；失败 = 文件名 stem
    freq: str                 # "hourly" | "monthly"（UI 转文案）；失败 = ""
    period: str               # 时间块展示文本；失败 = ""
    size: int                 # 字节
    human_size: str           # "123.4 MB"
    status: str               # "ready" | "busy"
    busy_by: List[str] = Field(default_factory=list)
    mtime: str                # "YYYY-MM-DD HH:MM:SS"（本地时区）
    parsed: bool = False      # 反解析是否成功


def parse_rel_path(rel: str) -> Optional[ParsedParts]:
    """按共享约定反解析缓存相对路径（design-data-manager.md §8）。

    规则：段 = [dataset, variable, freq, year, (mm), (dd)]；freq 恒为 hourly/monthly，
    freq 之后为 1~3 个纯数字段（year / year+mm / year+mm+dd）。
    任何不满足形态（非 .nc / 段数不对 / freq 非法 / 非纯数字 / 数值越界）→ None。

    注意：本函数只负责"形态反解析"，不做路径穿越校验（安全由 service._resolve 把关）。
    """
    if not isinstance(rel, str) or not rel:
        return None
    s = rel.strip().replace("\\", "/")
    if not s.endswith(".nc"):
        return None
    parts = s[:-3].split("/")
    # 期望 4~6 段：dataset/variable/freq + 1~3 个数字时间段
    if len(parts) < 4 or len(parts) > 6:
        return None
    dataset, variable, freq = parts[0], parts[1], parts[2]
    time_parts = parts[3:]
    if freq not in _FREQS:
        return None
    if not time_parts or not all(p.isdigit() for p in time_parts):
        return None
    try:
        values = [int(p) for p in time_parts]
    except ValueError:
        return None
    year = values[0]
    month = values[1] if len(values) >= 2 else None
    day = values[2] if len(values) >= 3 else None
    if not (1 <= year <= 9999):
        return None
    if month is not None and not (1 <= month <= 12):
        return None
    if day is not None and not (1 <= day <= 31):
        return None
    # period 文本 = 数字段按 YYYY[-MM[-DD]] 组装（mm/dd 两位零填充）
    period = f"{year:04d}"
    if month is not None:
        period += f"-{month:02d}"
    if day is not None:
        period += f"-{day:02d}"
    return ParsedParts(dataset=dataset, variable=variable, freq=freq,
                       year=year, month=month, day=day, period=period)


def human_size(num_bytes: int) -> str:
    """B/KB/MB/GB/TB 展示（design-data-manager.md §8：1000 进制）。

    - < 1KB → 整数 B（如 "512 B"）；
    - >= 1KB → 保留 1 位小数（如 "1.2 KB"、"123.4 MB"）。
    """
    try:
        n = max(int(num_bytes), 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1000:
        return f"{n} B"
    value = float(n)
    idx = 0
    # 1000 进制逐级换算：直到 < 1000 或已到最大单位 TB
    while value >= 1000.0 and idx < len(_SIZE_UNITS) - 1:
        value /= 1000.0
        idx += 1
    return f"{value:.1f} {_SIZE_UNITS[idx]}"


def scan_cache_files(cache_dir: Path,
                     busy: Mapping[str, Sequence[str]]) -> List[CacheFileEntry]:
    """递归扫描缓存根下的 .nc 文件并建行（design-data-manager.md §4.1）。

    - 目录不存在/为空 → []（无异常）；
    - 反解析失败文件不阻断：dataset="未知"、variable=文件名 stem、parsed=False；
    - 单文件 stat 失败跳过（不阻断整体扫描）；
    - 返回按 size 降序（默认），同大小按 rel_path 字典序保证稳定。
    """
    root = Path(cache_dir)
    entries: List[CacheFileEntry] = []
    if not root.is_dir():
        return entries
    for path in root.rglob("*.nc"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root).as_posix()
            st = path.stat()
            size = int(st.st_size)
            mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, ValueError):
            # 单文件扫描失败（正在写/被外部删除/路径异常）→ 跳过，不阻断整体列表
            continue
        parsed = parse_rel_path(rel)
        if parsed is not None:
            dataset = parsed.dataset
            variable = parsed.variable
            freq = parsed.freq
            period = parsed.period
            parsed_ok = True
        else:
            dataset = _UNKNOWN_DATASET
            variable = path.stem
            freq = ""
            period = ""
            parsed_ok = False
        owners = list(busy.get(rel, []) or [])
        entries.append(CacheFileEntry(
            rel_path=rel,
            dataset=dataset,
            variable=variable,
            freq=freq,
            period=period,
            size=size,
            human_size=human_size(size),
            status="busy" if owners else "ready",
            busy_by=owners,
            mtime=mtime,
            parsed=parsed_ok,
        ))
    # 默认 size 降序；同大小以 rel_path 升序保证稳定输出
    entries.sort(key=lambda e: (-e.size, e.rel_path))
    return entries
