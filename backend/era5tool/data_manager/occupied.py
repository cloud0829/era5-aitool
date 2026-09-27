# -*- coding: utf-8 -*-
"""数据管理：占用集合收集（design-data-manager.md §3.1/§8）。

collect_busy_map(tasks_dir) 遍历 data/tasks/*/task.json：
- 仅取 status ∈ {running, pending} 的任务；
- 收集 params["blocks"][].rel_target → 任务 ID 列表（Key = POSIX 相对路径）。

占用判断**直接读磁盘任务 JSON**（与 orchestrator 的持久化存储一致、不依赖内存），
因此即使 worker 在独立进程写文件、服务重启后依然有效。单文件损坏/JSON 解析失败
一律跳过，绝不抛异常——本函数在任何输入下都必须安全返回字典。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

# 只有这两种状态的任务块会写缓存文件 → 保守保护（design-data-manager.md §8）
_BUSY_STATUSES = ("running", "pending")


def collect_busy_map(tasks_dir: Path) -> Dict[str, List[str]]:
    """读取 running/pending 任务 params.blocks 的 rel_target 占用集合。

    - 返回 {rel_target(如 "reanalysis-.../2m_temperature/hourly/2020/01/05.nc"): [task_id,...]}
    - 目录不存在 / 无 task.json → {}；
    - 单个 task.json 损坏 / JSON 解析失败 / 无 blocks → 跳过该任务，不影响其余任务。
    """
    root = Path(tasks_dir)
    busy: Dict[str, List[str]] = {}
    if not root.is_dir():
        return busy
    for task_path in sorted(root.glob("*/task.json")):
        try:
            data = json.loads(task_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            # 单任务文件损坏/正在被原子替换的瞬时态 → 跳过（绝不阻断整体判断）
            continue
        if not isinstance(data, dict):
            continue
        status = data.get("status")
        if status not in _BUSY_STATUSES:
            continue
        task_id = data.get("id")
        if not task_id or not isinstance(task_id, str):
            task_id = task_path.parent.name  # 兜底：目录名即任务 ID
        blocks = (data.get("params") or {}).get("blocks")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            rel = block.get("rel_target")
            if not isinstance(rel, str) or not rel:
                continue
            ids = busy.setdefault(rel, [])
            if task_id not in ids:  # 同一任务多块引用同一文件时去重
                ids.append(task_id)
    return busy
