# -*- coding: utf-8 -*-
"""任务持久化（data/tasks/*.json）。"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from era5tool.config.settings import Settings
from era5tool.core.resumable import ensure_dir
from era5tool.models.task import Task, TaskStatus, TaskType


def new_task_id() -> str:
    """生成唯一任务 ID：秒级时间戳 + 8 位随机后缀。

    仅用秒级时间戳会在同秒连续提交时碰撞（后建任务覆盖前者目录，导致
    cancel.flag/.done/events.jsonl 交叉污染）；UUID 后缀保证唯一性。
    """
    import uuid
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"t_{ts}_{uuid.uuid4().hex[:8]}"


class TaskStore:
    """data/tasks/{task_id}/task.json 的读写与列表查询。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.tasks_dir = settings.tasks_dir
        ensure_dir(self.tasks_dir)
        # 写锁：串行化同一 store 上的 save（多下载会话线程可能并发落盘同一任务，
        # 无锁会互相覆盖同一 .tmp 并在 os.replace 阶段丢最终状态）。
        self._write_lock = threading.RLock()

    def task_dir(self, task_id: str) -> Path:
        return self.tasks_dir / task_id

    def task_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "task.json"

    # ------------------------------------------------------------------
    def create(self, task_type: TaskType, params: Dict) -> Task:
        task = Task(id=new_task_id(), type=task_type, params=params)
        self.save(task)
        return task

    def save(self, task: Task) -> None:
        ensure_dir(self.task_dir(task.id))
        path = self.task_path(task.id)
        tmp = path.with_suffix(".json.tmp")
        with self._write_lock:
            # 清掉上次中断/并发写遗留的 .tmp，保证 os.replace 目标唯一
            try:
                if tmp.is_file():
                    tmp.unlink()
            except OSError:
                pass
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(task.to_dict(), f, ensure_ascii=False, indent=2)
            self._replace_retry(tmp, path)

    @staticmethod
    def _replace_retry(tmp: Path, path: Path, attempts: int = 200) -> None:
        """Windows 原子替换重试：读方恰在替换瞬间 open（未开 FILE_SHARE_DELETE）
        会让 os.replace 抛 PermissionError/OSError。若无界重试地一次失败，最终状态
        （如 SUCCESS）可能落盘丢失 → task.json 永久停在 running（实测证据：事件流
        已发出 success、磁盘仍是 running、.tmp 残留）。这里做有界短重试（200×5ms
        ≈1s，读方打开窗口仅微秒级，足以收敛）；耗尽仍失败则清理 tmp 并上抛，由
        调用方兜底，不残留孤儿 .tmp。
        """
        last: Optional[OSError] = None
        for _ in range(attempts):
            try:
                os.replace(tmp, path)
                return
            except (PermissionError, OSError) as exc:  # Windows 替换瞬时锁
                last = exc
                time.sleep(0.005)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        if last is not None:
            raise last

    def get(self, task_id: str) -> Optional[Task]:
        path = self.task_path(task_id)
        if not path.is_file():
            return None
        # Windows：store.save 以 tmp + os.replace 原子落盘。读方恰在替换瞬间 open
        # 可能命中瞬时 PermissionError/OSError（文件锁时序竞争，见 test_qa_independent_
        # verify._read_task_json 同款注释），并非任务真丢失。做有界短重试，避免 API/
        # 轮询把瞬时读失败误报为“任务不存在”（2001）；真缺失/目录已删立即返回 None。
        for _ in range(40):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return Task(**json.load(f))
            except FileNotFoundError:
                return None
            except (json.JSONDecodeError, OSError, ValueError):
                if not path.is_file():
                    return None
                time.sleep(0.005)
        # 重试耗尽仍不可读（持续 IO/解析失败）→ 视为不存在/不可读
        return None

    def list(self, status: Optional[str] = None,
             page: int = 1, size: int = 20) -> Dict:
        tasks: List[Task] = []
        if self.tasks_dir.is_dir():
            for p in sorted(self.tasks_dir.glob("*/task.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        t = Task(**json.load(f))
                except (json.JSONDecodeError, OSError, ValueError):
                    continue
                if status and t.status.value != status:
                    continue
                tasks.append(t)
        start = (page - 1) * size
        return {"tasks": [t.to_dict() for t in tasks[start:start + size]],
                "total": len(tasks)}

    def list_all(self) -> List[Task]:
        """全量枚举所有任务（不分页），供批量删除等场景使用。

        list() 的 size 有分页截断（单页上限由路由层 le=100 约束），批量操作
        需要拿到任务存储中的**全部**任务，故提供本方法：按 task.json 修改时间
        倒序返回全部可解析 Task（解析失败/瞬时 IO 异常的任务跳过，与 list() 一致）。
        """
        tasks: List[Task] = []
        if self.tasks_dir.is_dir():
            for p in sorted(self.tasks_dir.glob("*/task.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        t = Task(**json.load(f))
                except (json.JSONDecodeError, OSError, ValueError):
                    continue
                tasks.append(t)
        return tasks

    def delete(self, task_id: str, delete_files: bool = False) -> bool:
        tdir = self.task_dir(task_id)
        if not tdir.is_dir():
            return False
        # N5：先读取 task（随后 rmtree 会删除 task.json，无法再 get）
        task = self.get(task_id)
        if delete_files and task is not None:
            self._delete_cache_files(task)
        shutil.rmtree(tdir, ignore_errors=True)
        return True

    def _delete_cache_files(self, task: Task) -> None:
        req = (task.params or {}).get("request_schema") or {}
        dataset = req.get("dataset", "")
        family = req.get("dataset_family", "era5-single")
        freq = "monthly" if family in ("land-monthly", "era5-monthly") else "hourly"
        for var in req.get("variables", []):
            for year in TaskStore._years_in(req):
                rel = Path(dataset) / var / freq / str(year)
                # 产物实际存放于 data/cache 与 data/products
                for root in (self.settings.cache_dir, self.settings.products_dir):
                    p = root / rel
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)

    @staticmethod
    def _years_in(req: Dict) -> List[str]:
        tr = req.get("timerange") or {}
        start = tr.get("start", "")
        end = tr.get("end", "")
        years: List[str] = []
        try:
            y1, y2 = int(start[:4]), int(end[:4])
            years = [str(y) for y in range(y1, y2 + 1)]
        except (TypeError, ValueError):
            pass
        return years
