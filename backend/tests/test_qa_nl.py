# -*- coding: utf-8 -*-
"""QA：NL 层边界/错误路径（DeepSeek 非法 JSON → 规则兜底不崩溃；need_info 流转）。

- DeepSeek 无真实 key：monkeypatch 假 LLM，验证降级与多轮流转。
- _clean_json 清洗 Markdown 围栏/非法 JSON。
"""
from __future__ import annotations

import pytest

from era5tool.config.schema import ApiError, ERR_LLM_PARSE
from era5tool.nl.llm_parser import DeepSeekParser


# ---------------------------------------------------------------------------
# _clean_json
# ---------------------------------------------------------------------------
def test_clean_json_markdown_fence():
    assert DeepSeekParser._clean_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_clean_json_stray_fence():
    assert DeepSeekParser._clean_json('```{"a": 1}```') == {"a": 1}


def test_clean_json_whitespace_only():
    assert DeepSeekParser._clean_json("   \n  ") is None


def test_clean_json_invalid_text():
    assert DeepSeekParser._clean_json("今天天气不错") is None


def test_clean_json_list_not_dict():
    assert DeepSeekParser._clean_json("[1,2,3]") is None


# ---------------------------------------------------------------------------
# LLM 降级路径（有 key 但 LLM 返回非法 JSON → 规则兜底）
# ---------------------------------------------------------------------------
def _enable_fake_key(app_state, monkeypatch):
    monkeypatch.setattr(app_state.settings.llm, "deepseek_api_key", "sk-test-qa")


def test_llm_invalid_json_falls_back_to_rule(app_state, monkeypatch):
    """LLM 抛 ERR_LLM_PARSE → 降级规则，返回 schema 不崩溃。"""
    class FakeLLM:
        def parse(self, text):
            raise ApiError(ERR_LLM_PARSE, "连续两次未返回合法 JSON")
    monkeypatch.setattr(app_state.nl_parser, "llm", FakeLLM())
    _enable_fake_key(app_state, monkeypatch)

    res = app_state.nl_parser.parse("下载2020年6月长三角温度")
    assert res.engine == "rule"
    assert res.request_schema is not None
    assert "2m_temperature" in res.request_schema.variables


def test_llm_garbage_string_falls_back(app_state, monkeypatch):
    """LLM 返回非 dict 垃圾 → 规则兜底。"""
    class FakeLLM:
        def parse(self, text):
            return "not a json at all"
    monkeypatch.setattr(app_state.nl_parser, "llm", FakeLLM())
    _enable_fake_key(app_state, monkeypatch)

    res = app_state.nl_parser.parse("下载2020年6月温度")
    assert res.engine == "rule"
    assert res.request_schema is not None


def test_llm_need_info_flow(app_state, monkeypatch):
    """LLM 返回 need_info JSON → 追问流转，不构造 schema。"""
    class FakeLLM:
        def parse(self, text):
            return {"need_info": ["timerange"],
                    "questions": ["请提供时间范围，例如 2020年6月"]}
    monkeypatch.setattr(app_state.nl_parser, "llm", FakeLLM())
    _enable_fake_key(app_state, monkeypatch)

    res = app_state.nl_parser.parse("下载降水")
    assert res.engine == "deepseek"
    assert res.need_info is not None
    assert "timerange" in res.need_info.missing
    assert res.request_schema is None


def test_clarify_merges_answers(app_state, monkeypatch):
    """clarify 合并表单答案生成完整 schema。"""
    monkeypatch.setattr(app_state.nl_parser, "llm", _RuleOnlyLLM())
    _enable_fake_key(app_state, monkeypatch)

    first = app_state.nl_parser.parse("下载降水")
    assert first.need_info is not None
    res = app_state.nl_parser.clarify(first.session_id, {
        "timerange": {"start": "2020-06-01", "end": "2020-06-30"},
        "variables": ["total_precipitation"],
    })
    assert res.request_schema is not None
    assert "total_precipitation" in res.request_schema.variables


class _RuleOnlyLLM:
    """解析器：恒抛错 → 强制规则路径。"""
    def parse(self, text):
        raise ApiError(ERR_LLM_PARSE, "no llm")
