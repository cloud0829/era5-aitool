# -*- coding: utf-8 -*-
"""NL 多轮会话状态机（design-final.md §3.1/§3.2：need_info 多轮澄清，max_turns=5）。"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from era5tool.config.schema import RequestSchema


class NLSession:
    """一次 NL 对话的状态。"""

    def __init__(self, session_id: str, engine: str = "rule"):
        self.id = session_id
        self.engine = engine
        self.schema: Optional[RequestSchema] = None
        self.partial: Dict[str, Any] = {}
        self.missing: List[str] = []
        self.questions: List[str] = []
        self.turns = 0
        self.max_turns = 5
        self.updated_at = time.time()

    def touch(self) -> None:
        self.updated_at = time.time()
        self.turns += 1

    @property
    def exhausted(self) -> bool:
        return self.turns >= self.max_turns


class SessionManager:
    """内存会话表（本地单用户，TTL 过期清理）。"""

    def __init__(self, ttl: float = 1800.0):
        self._sessions: Dict[str, NLSession] = {}
        self.ttl = ttl

    def get_or_create(self, session_id: Optional[str],
                      engine: str = "rule") -> NLSession:
        sid = (session_id or "").strip()
        if not sid or sid not in self._sessions:
            sid = f"s_{uuid.uuid4().hex[:12]}"
            self._sessions[sid] = NLSession(sid, engine)
        s = self._sessions[sid]
        s.touch()
        return s

    def get(self, session_id: str) -> Optional[NLSession]:
        s = self._sessions.get(session_id)
        if s is None:
            return None
        if time.time() - s.updated_at > self.ttl:
            self._sessions.pop(session_id, None)
            return None
        return s

    def update(self, s: NLSession) -> None:
        s.touch()
        self._sessions[s.id] = s

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def cleanup(self) -> None:
        now = time.time()
        for sid in [k for k, v in self._sessions.items()
                    if now - v.updated_at > self.ttl]:
            self._sessions.pop(sid, None)
