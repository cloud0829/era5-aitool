# -*- coding: utf-8 -*-
"""任务状态机单测（design-final.md §7.2）。"""
from __future__ import annotations

import pytest

from era5tool.models.task import Task, TaskStatus, TaskType


def test_valid_transitions():
    t = Task(id="t_x", type=TaskType.DOWNLOAD, status=TaskStatus.PENDING)
    t.transition(TaskStatus.RUNNING)
    assert t.status == TaskStatus.RUNNING
    t.transition(TaskStatus.PAUSED)
    t.transition(TaskStatus.RUNNING)
    t.transition(TaskStatus.SUCCESS)
    assert t.status == TaskStatus.SUCCESS


def test_invalid_transition():
    t = Task(id="t_x", type=TaskType.DOWNLOAD, status=TaskStatus.PENDING)
    with pytest.raises(ValueError, match="非法状态转移"):
        t.transition(TaskStatus.SUCCESS)


def test_success_irreversible():
    t = Task(id="t_x", type=TaskType.DOWNLOAD, status=TaskStatus.SUCCESS)
    with pytest.raises(ValueError):
        t.transition(TaskStatus.RUNNING)


def test_to_dict_shape():
    t = Task(id="t_x", type=TaskType.DOWNLOAD, status=TaskStatus.RUNNING)
    d = t.to_dict()
    assert d["status"] == "running"
    assert d["type"] == "download"
    assert d["progress"] == 0.0
    assert d["block_stats"]["total"] == 0
