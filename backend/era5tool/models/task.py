# -*- coding: utf-8 -*-
"""任务模型与状态机（design-final.md §7.2）。"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PAUSED = "paused"


class TaskType(str, Enum):
    DOWNLOAD = "download"
    PLOT = "plot"
    NL_PARSE = "nl_parse"
    TEST_DOWNLOAD = "test_download"


# 状态机合法转移（design-final.md §7.2；pending→paused 为取消未启动任务扩展）
_TRANSITIONS: Dict[TaskStatus, set] = {
    TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.PAUSED},
    TaskStatus.RUNNING: {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.PAUSED},
    TaskStatus.PAUSED: {TaskStatus.RUNNING},
    TaskStatus.FAILED: {TaskStatus.RUNNING},
    TaskStatus.SUCCESS: set(),       # success 不可逆
}


class BlockStats(BaseModel):
    total: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0


class Task(BaseModel):
    """内存/落盘统一的任务模型。"""

    id: str = ""
    type: TaskType = TaskType.DOWNLOAD
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0
    params: Dict[str, Any] = Field(default_factory=dict)
    block_stats: BlockStats = Field(default_factory=BlockStats)
    result: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[Dict[str, Any]] = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)

    def transition(self, new: TaskStatus) -> None:
        if new not in _TRANSITIONS[self.status]:
            raise ValueError(f"非法状态转移: {self.status.value} -> {new.value}")
        self.status = new
        self.updated_at = now_iso()

    def touch(self) -> None:
        self.updated_at = now_iso()

    def to_dict(self) -> Dict[str, Any]:
        d = self.model_dump(mode="json")
        d["status"] = self.status.value
        d["type"] = self.type.value
        return d
