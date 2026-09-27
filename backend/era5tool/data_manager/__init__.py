# -*- coding: utf-8 -*-
"""数据管理领域包（design-data-manager.md §1.2）。

只读本地缓存 NetCDF 列表 + 元数据详情 + 安全删除（占用保护），
独立于下载任务链路（orchestrator/cds_channel 一行不改）。

模块：
- catalog   : 相对路径反解析 / human_size / 扫描建 DTO（纯函数，易测）
- occupied  : 读取 tasks_dir 收集 running/pending 任务 blocks 的 rel_target 占用集合（纯函数）
- nc_meta   : netCDF4 头部元数据读取（永不抛异常，异常返回 {ok:false,error}）
- service   : DataManagerService 门面（扫描/元数据/删除编排 + 防穿越 + 删除兜底）
"""
from __future__ import annotations

from era5tool.data_manager.catalog import (CacheFileEntry, ParsedParts,
                                           human_size, parse_rel_path,
                                           scan_cache_files)
from era5tool.data_manager.occupied import collect_busy_map
from era5tool.data_manager.service import DataManagerService

__all__ = [
    "CacheFileEntry",
    "ParsedParts",
    "human_size",
    "parse_rel_path",
    "scan_cache_files",
    "collect_busy_map",
    "DataManagerService",
]
