# -*- coding: utf-8 -*-
"""WS 事件广播（design-final.md §5.6 / §7.4）。

- EventBroker：管理 /ws/tasks 连接；broadcast 向订阅了该 task 或订阅全部的前端推送。
- TaskEventBus：下载任务运行期的事件管道——worker 进程把事件放入
  multiprocessing.Queue，主进程 reader 线程负责「写任务 events.jsonl + WS 广播」。
  （Windows 下 Worker 通过 Pool initializer 拿到同一个 Queue，事件行级安全。）
"""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import queue as _queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventBroker:
    """WS 连接管理器：支持按 task_id 订阅或全量订阅。"""

    def __init__(self) -> None:
        self._sockets: Set[WebSocket] = set()
        self._subs: Dict[str, Set[WebSocket]] = {}
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        with self._lock:
            self._sockets.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        with self._lock:
            self._sockets.discard(ws)
            for subs in self._subs.values():
                subs.discard(ws)

    def subscribe(self, ws: WebSocket, task_id: str) -> None:
        with self._lock:
            self._subs.setdefault(task_id, set()).add(ws)

    async def broadcast(self, event: Dict[str, Any]) -> None:
        """向订阅该 task 的 socket（含全量订阅者）推送事件。"""
        task_id = event.get("task_id", "")
        targets: Set[WebSocket] = set()
        with self._lock:
            targets.update(self._sockets)              # 全量订阅者
            if task_id:
                targets.update(self._subs.get(task_id, set()))
        dead: List[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(json.dumps(event, ensure_ascii=False))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def schedule_broadcast(self, event: Dict[str, Any]) -> None:
        """供 reader 线程（非 asyncio）调用：把广播调度到主循环。"""
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self.broadcast(event), self._loop)


class TaskEventBus:
    """单个任务运行期的事件管道（Queue + reader 线程 + 持久 events.jsonl）。"""

    # 注意：必须用可 pickle 的哨兵（multiprocessing.Queue 存取会 pickle/unpickle，
    # `is` 比较会失效），故用字符串并在 reader 中以 == 比较。
    _SENTINEL = "__END_OF_TASK__"

    def __init__(self, task_id: str, task_dir: Path, broker: EventBroker):
        self.task_id = task_id
        self.task_dir = task_dir
        self.broker = broker
        self.queue: "multiprocessing.Queue[Any]" = multiprocessing.Queue(maxsize=10000)
        self.events_path = task_dir / "events.jsonl"
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # events.jsonl 的两个写入方（主进程 emit / reader 线程）须串行，防止行交错损坏
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, daemon=True,
                                        name=f"eventbus-{self.task_id}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # 注入哨兵唤醒 reader；随后等待其退出
        try:
            self.queue.put(self._SENTINEL, timeout=1)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _reader(self) -> None:
        while True:
            try:
                ev = self.queue.get(timeout=0.3)
            except _queue.Empty:
                if self._stop.is_set() and self.queue.empty():
                    break
                continue
            if ev == self._SENTINEL:
                break
            self._on_event(ev)

    def _on_event(self, ev: Dict[str, Any]) -> None:
        if "ts" not in ev:
            ev["ts"] = _now_iso()
        ev.setdefault("task_id", self.task_id)
        # 持久化（锁内单行原子写：主进程 emit 与 reader 线程都写同一文件）
        line = json.dumps(ev, ensure_ascii=False) + "\n"
        with self._write_lock:
            try:
                with open(self.events_path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass
        self.broker.schedule_broadcast(ev)

    # ------------------------------------------------------------------
    def emit(self, event: Dict[str, Any]) -> None:
        """主进程内直接发射事件（写入 events.jsonl + WS 广播）。"""
        self._on_event(dict(event))

    def emit_worker_event(self, event: Dict[str, Any]) -> None:
        """worker 进程内调用：事件入队（由 Pool initializer 注入全局 queue）。"""
        try:
            self.queue.put(event, timeout=2)
        except Exception:
            pass


# 全局 worker queue（由 Pool initializer 注入）
_WORKER_QUEUE: Optional["multiprocessing.Queue[Any]"] = None


def init_worker_queue(q: "multiprocessing.Queue[Any]") -> None:
    global _WORKER_QUEUE
    _WORKER_QUEUE = q


def emit_worker_event(event: Dict[str, Any]) -> None:
    """模块级 worker 事件发射（_fetch_one_block 使用）。"""
    q = _WORKER_QUEUE
    if q is None:
        return
    try:
        q.put(event, timeout=2)
    except Exception:
        pass
