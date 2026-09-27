# -*- coding: utf-8 -*-
"""NL 解析器调度（design-final.md §3.1：DeepSeek → 规则 → need_info/表单）。

- 有 Key 且在线 → DeepSeek；失败重试 1 次后降级规则。
- 无 Key/离线 → 规则兜底。
- 缺必填 / confidence<0.7 → need_info 或 confirm 分支。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from era5tool.config.schema import (NeedInfo, NlParseResult, RequestSchema)
from era5tool.config.settings import Settings
from era5tool.nl.llm_parser import DeepSeekParser
from era5tool.nl.rule_parser import RuleParser
from era5tool.nl.session import NLSession, SessionManager
from era5tool.nl.variable_map import VariableMap

REQUIRED_FIELDS = ("dataset", "dataset_family", "variables", "timerange")


class NLParser:
    """NL 解析统一入口。"""

    def __init__(self, settings: Settings, var_map: VariableMap):
        self.settings = settings
        self.var_map = var_map
        self.rule = RuleParser(var_map)
        self.llm = DeepSeekParser(settings)
        self.sessions = SessionManager()

    # ------------------------------------------------------------------
    def parse(self, text: str, session_id: Optional[str] = None) -> NlParseResult:
        session = self.sessions.get_or_create(session_id, engine="rule")
        engine = "rule"
        schema: Optional[RequestSchema] = None

        if self.settings.llm.has_key:
            try:
                raw = self.llm.parse(text)
                engine = "deepseek"
                if isinstance(raw, dict) and ("need_info" in raw):
                    session.missing = list(raw.get("need_info", []))
                    session.questions = list(raw.get("questions", []))
                    self.sessions.update(session)
                    return NlParseResult(
                        need_info=NeedInfo(missing=session.missing,
                                           questions=session.questions),
                        session_id=session.id, engine=engine)
                schema = self._to_schema(raw)
            except Exception:
                schema = None

        if schema is None:
            outcome = self.rule.parse(text)
            if outcome.schema is not None:
                schema = outcome.schema
                engine = "rule"
            else:
                session.missing = outcome.missing
                session.questions = outcome.questions
                self.sessions.update(session)
                return NlParseResult(
                    need_info=NeedInfo(missing=outcome.missing,
                                       questions=outcome.questions),
                    session_id=session.id, engine=engine)

        session.schema = schema
        session.engine = engine
        session.missing = []
        session.questions = []
        self.sessions.update(session)
        confirm = (schema.confidence or 0.0) < 0.7
        return NlParseResult(request_schema=schema, session_id=session.id,
                             engine=engine, confirm=confirm)

    # ------------------------------------------------------------------
    def clarify(self, session_id: str, answers: Dict[str, Any]) -> NlParseResult:
        """多轮补参/确认：把用户表单答案合并进会话并生成最终 schema。"""
        session = self.sessions.get(session_id)
        if session is None:
            session = self.sessions.get_or_create(None, engine="rule")
        session.touch()

        merged: Dict[str, Any] = dict(session.partial)
        if session.schema is not None:
            merged.update(session.schema.model_dump(mode="json"))
        merged.update(answers or {})

        try:
            schema = self._to_schema(merged)
            session.schema = schema
            session.missing = []
            session.questions = []
            self.sessions.update(session)
            return NlParseResult(request_schema=schema, session_id=session.id,
                                 engine=session.engine,
                                 confirm=(schema.confidence or 0.0) < 0.7)
        except ValueError as exc:
            session.missing = ["schema"]
            session.questions = [f"参数仍不完整或非法：{exc}"]
            self.sessions.update(session)
            return NlParseResult(
                need_info=NeedInfo(missing=session.missing,
                                   questions=session.questions),
                session_id=session.id, engine=session.engine)

    # ------------------------------------------------------------------
    def _to_schema(self, data: Dict[str, Any]) -> RequestSchema:
        """把 LLM/表单 dict 构造为合法 RequestSchema（pydantic 校验）。"""
        d = dict(data or {})
        # 兼容 LLM 返回 area/timerange 为 dict
        area = d.get("area")
        if isinstance(area, dict):
            d["area"] = area
        timerange = d.get("timerange")
        if isinstance(timerange, dict):
            if not timerange.get("start") or not timerange.get("end"):
                raise ValueError("timerange 需要 start 与 end")
        return RequestSchema(**d)
