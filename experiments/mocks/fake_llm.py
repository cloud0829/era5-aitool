# -*- coding: utf-8 -*-
"""假 DeepSeek（供 E3 mock 复用）。

design-final.md §3.2：LLM 只负责产出结构化 JSON；业务校验一律在 pydantic Schema
层完成。fake_llm 按预置响应表返回三类结果：
  - legal      ：合法 JSON（可含 Markdown 围栏，测试 _clean_json）
  - need_info  ：{"need_info": [...], "questions": [...]}
  - invalid    ：非法 JSON（Markdown 围栏 + 非 JSON 文本 / 缺字段），触发重试→降级

真实模式（--real）使用 openai SDK 指向 DeepSeek，见 e3_nl_schema_samples.py。
"""
from __future__ import annotations

import json
import random
from typing import Any, Dict, List

from exp_common import pretty_json


class FakeDeepSeek:
    """预置响应表的假 LLM。

    cases 为 dict: case_id -> {"mock": "legal"|"need_info"|"invalid", "expect": {...}}
    legal 响应由 expect + 默认值程序化生成，保证字段确定性。
    """

    def __init__(self, cases: Dict[str, Dict[str, Any]], seed: int = 42):
        self.cases = cases
        self._rng = random.Random(seed)
        self.call_count = 0
        # 记录每次 complete 的输入（供断言重试逻辑）
        self.call_log: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    def complete(self, user_text: str, case_id: str = "case_00",
                 system_prompt: str = "", turn: int = 1) -> str:
        """返回预置的原始 LLM 文本（未清洗）。"""
        self.call_count += 1
        self.call_log.append({"case_id": case_id, "turn": turn, "len": len(user_text)})
        case = self.cases.get(case_id, {"mock": "legal", "expect": {}})
        mock = case.get("mock", "legal")
        if mock == "need_info":
            return self._need_info_response(case.get("expect", {}))
        if mock == "invalid":
            return self._invalid_response()
        return self._legal_response(case.get("expect", {}))

    # ------------------------------------------------------------------
    def _legal_response(self, expect: Dict[str, Any]) -> str:
        """由 expect 生成合法 JSON（带 Markdown 围栏，验证 _clean_json）。"""
        schema = self._schema_from_expect(expect)
        return "```json\n" + json.dumps(schema, ensure_ascii=False) + "\n```"

    def _need_info_response(self, expect: Dict[str, Any]) -> str:
        missing = expect.get("missing", ["timerange", "variables"])
        questions = expect.get("questions", [
            "请提供需要下载的时间范围（如 2020-01-01 到 2020-12-31）",
            "请提供需要下载的变量（如 2m 温度、降水）",
        ])[:3]
        obj = {"need_info": missing[:3], "questions": questions[:3]}
        return json.dumps(obj, ensure_ascii=False)

    def _invalid_response(self) -> str:
        style = self._rng.randint(0, 2)
        if style == 0:      # 纯 Markdown 围栏但内容非 JSON
            return "```json\n这不是 JSON，只是说明文字。\n```"
        if style == 1:      # 缺关键字段
            return json.dumps({"variables": ["2m_temperature"]}, ensure_ascii=False)
        # 完全非 JSON 文本
        return "好的，我理解你的需求了，请稍等。"

    # ------------------------------------------------------------------
    @staticmethod
    def _schema_from_expect(expect: Dict[str, Any]) -> Dict[str, Any]:
        """把 expect 字段转成完整 RequestSchema JSON（缺省用合理默认值）。

        注意：need_info/invalid 不走到这里。
        """
        schema: Dict[str, Any] = {
            "dataset": expect.get("dataset", "reanalysis-era5-single-levels"),
            "dataset_family": expect.get("dataset_family", "era5-single"),
            "variables": expect.get("variables", ["2m_temperature"]),
            "timerange": expect.get("timerange", {"start": "2020-01-01", "end": "2025-12-31"}),
            "area": expect.get("area", {"west": -180, "south": -90, "east": 180, "north": 90}),
            "frequency": expect.get("frequency", "hourly"),
            "aggregation": expect.get("aggregation", "raw"),
            "confidence": expect.get("confidence", 0.9),
        }
        if expect.get("pressure_levels") is not None:
            schema["pressure_levels"] = expect["pressure_levels"]
        # 与 §7.3 一致：land 系列强制无 pressure_levels
        if schema["dataset_family"].startswith("land"):
            schema.pop("pressure_levels", None)
        return schema

    # ------------------------------------------------------------------
    @staticmethod
    def format_legal(schema: Dict[str, Any]) -> str:
        """供外部直接构造合法响应文本。"""
        return "```json\n" + json.dumps(schema, ensure_ascii=False) + "\n```"


def build_cases_from_samples(samples: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """把 samples_nl_30.json 的 case 列表转成 FakeDeepSeek 需要的 dict。"""
    return {s["id"]: s for s in samples}
