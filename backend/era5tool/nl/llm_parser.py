# -*- coding: utf-8 -*-
"""DeepSeek LLM 解析器（design-final.md §3.2，D1 落地）。

- openai SDK 指向 https://api.deepseek.com，模型 deepseek-chat。
- response_format={"type":"json_object"}；SYSTEM_PROMPT 必须含 "json" 字样。
- _clean_json 清洗 Markdown 围栏；解析失败重试 1 次；仍失败抛 ERR_LLM_PARSE。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from era5tool.config.schema import ApiError, ERR_LLM_PARSE, ERR_NO_LLM
from era5tool.config.settings import Settings

SYSTEM_PROMPT = """【角色】
你是 ERA5 / ERA5-Land 气象再分析数据下载助手。用户用中文或英文自然语言描述数据需求，
你必须把需求转成符合 CDS API 规范的结构化 JSON。

【输出硬性约束】
1. 请严格输出一个 JSON 对象，禁止输出解释、Markdown 代码块或多余文字。
2. 字段必须符合下方 JSON Schema；未提到但可推导的字段给合理默认值。
3. 若信息不足无法确定，请输出一个 JSON 对象：
   {"need_info": ["字段名", ...], "questions": ["面向用户的追问问题", ...]}，
   每次最多追问 3 个字段，不要臆造数值。

【数据集与字段转换规则】
- dataset 从枚举中选择，默认 reanalysis-era5-single-levels。
  用户说"陆地/地面/土壤/0.1度"类需求优先选 reanalysis-era5-land；
  说"月均/月度平均"且带 land 时选 reanalysis-era5-land-monthly-means。
- dataset_family（派生，不要用户输入）:
  land → reanalysis-era5-land*（无气压层，禁止输出 pressure_levels）
  era5-single → reanalysis-era5-single-levels*
  era5-pressure → reanalysis-era5-pressure-levels（必须输出 pressure_levels，如 [850,500]）
- variables: 中文变量名必须映射为 ERA5/ERA5-Land 标准英文变量名（见映射表）；
  映射不确定时加 "confidence": <0~1> 并可用 need_info 确认。
- timerange: ISO 8601（YYYY-MM-DD）；"近五年"按当前日期推算；无结束日期默认今天。
- area: bbox = {west, south, east, north}（西经为负、南纬为负）；
  "长三角"等区域词用内置区域词典解析；未提及默认全球。
- aggregation: raw | mean | sum | max | min；提到"平均/月均/年均"才设，否则 raw。
- frequency: hourly | daily | monthly；提到"逐日/逐月"才设，否则 hourly。
  （ERA5-Land 原生为 hour 维度；daily/monthly 由出图层聚合实现，仍可设）

【JSON Schema】
{"type":"object","required":["dataset","dataset_family","variables","timerange"],
 "properties":{"dataset":{"type":"string"},"dataset_family":{"type":"string"},
 "variables":{"type":"array","items":{"type":"string"},"minItems":1},
 "pressure_levels":{"type":"array","items":{"type":"integer"}},
 "timerange":{"type":"object","required":["start","end"],
   "properties":{"start":{"type":"string"},"end":{"type":"string"}}},
 "area":{"type":"object","required":["west","south","east","north"]},
 "frequency":{"type":"string"},"aggregation":{"type":"string"},
 "confidence":{"type":"number"}}}

【变量映射表（摘要）】
temperature/温度 → 2m_temperature；precipitation/降水 → total_precipitation；
wind/风 → 10m_u_component_of_wind + 10m_v_component_of_wind；
地面气压 → surface_pressure；露点温度 → 2m_dewpoint_temperature；
土壤温度 → soil_temperature_level_1；土壤湿度 → volumetric_soil_water_layer_1；
雪水当量 → snow_depth_water_equivalent；净太阳辐射 → surface_net_solar_radiation；
蒸发 → evaporation；潜在蒸发 → potential_evaporation；海温 → sea_surface_temperature。

【示例】
用户: "下载最近五年长江三角洲五六月地表温度"
输出: {"dataset":"reanalysis-era5-single-levels","dataset_family":"era5-single",
      "variables":["2m_temperature"],
      "timerange":{"start":"2020-06-01","end":"2025-06-30"},
      "area":{"west":118,"south":29,"east":123,"north":34},
      "frequency":"hourly","aggregation":"raw","confidence":0.9}"""


class DeepSeekParser:
    """DeepSeek 解析器（openai SDK）。"""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _client(self):
        from openai import OpenAI
        llm = self.settings.llm
        return OpenAI(api_key=llm.deepseek_api_key, base_url=llm.deepseek_base_url)

    def parse(self, text: str) -> Dict[str, Any]:
        """解析并返回结构化 dict；失败抛 ApiError(ERR_LLM_PARSE)。"""
        llm = self.settings.llm
        if not llm.has_key:
            raise ApiError(ERR_NO_LLM, "未配置 DEEPSEEK_API_KEY")
        try:
            client = self._client()
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ]
            for _ in range(2):
                resp = client.chat.completions.create(
                    model=llm.deepseek_model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=0.1,
                    max_tokens=1024,
                )
                raw = resp.choices[0].message.content or ""
                obj = self._clean_json(raw)
                if obj is not None:
                    return obj
                # 重试：追加「请只输出 JSON」
                messages = messages + [
                    {"role": "assistant", "content": raw[:500]},
                    {"role": "user", "content": "请只输出 JSON，不要其他文字。"},
                ]
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(ERR_LLM_PARSE, f"DeepSeek 调用失败: {exc}")
        raise ApiError(ERR_LLM_PARSE, "DeepSeek 连续两次未返回合法 JSON")

    @staticmethod
    def _clean_json(raw: str) -> Optional[Dict[str, Any]]:
        """清洗 Markdown 围栏 / 首尾空白后解析 JSON。"""
        if not raw:
            return None
        s = raw.strip()
        # 剥 ```json ... ``` 围栏
        fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
        if fence:
            s = fence.group(1).strip()
        # 剥散落的围栏标记
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
