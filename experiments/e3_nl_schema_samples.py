# -*- coding: utf-8 -*-
"""E3 · DeepSeek NL→Schema 30 样例（design-final.md §10.4）。

目的：验证 NL 解析管线能稳定产出合规 JSON：合法解析、Schema 校验、
need_info 多轮流转、confidence<0.7 进确认、非法 JSON 降级不崩溃。

用法：
    python e3_nl_schema_samples.py                # mock：预置三类 LLM 响应
    python e3_nl_schema_samples.py --real         # DeepSeek（需 DEEPSEEK_API_KEY）
    python e3_nl_schema_samples.py --samples path

通过标准（mock）：
  ① 合法 JSON 解析率 100%（含清洗 Markdown 围栏）
  ② 完整样例字段正确率 ≥80%
  ③ need_info 样例正确返回 missing/questions 且 ≤3 问
  ④ confidence<0.7 样例进入确认分支
  ⑤ 非法 JSON 触发重试后降级规则/表单，不崩溃
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mocks"))

from exp_common import (  # noqa: E402
    OUTDIR_DEFAULT, ResultCollector, detect_credentials, ensure_dir, pretty_json,
)
from exp_schema import NeedInfo, RequestSchema, parse_llm_json  # noqa: E402
from mocks.fake_llm import FakeDeepSeek, build_cases_from_samples  # noqa: E402

SAMPLES_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples_nl_30.json")


def load_samples(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        samples = json.load(f)
    if not isinstance(samples, list) or len(samples) != 30:
        raise ValueError(f"样例文件应为 30 条列表，实际 {len(samples) if isinstance(samples, list) else '?'} 条")
    return samples


# ---------------------------------------------------------------------------
# mock 解析管线（实验内等价实现 nl/parser.py + llm_parser.py 纯逻辑）
# ---------------------------------------------------------------------------
def rule_fallback(text: str) -> Dict[str, Any]:
    """规则兜底：无法解析时返回需要补参的追问（缺必填字段）。"""
    return {
        "mode": "rule_fallback",
        "need_info": True,
        "missing": ["variables", "timerange", "area"],
        "questions": [
            "请选择需要下载的变量（如 2m 温度、降水、风速）",
            "请提供时间范围（如 2020-01-01 到 2020-12-31）",
            "请提供区域（如长三角，或经纬度范围）",
        ],
        "schema": None,
        "confirm_required": False,
        "parse_attempts": 2,
        "error": "LLM 输出无法解析为合法 JSON，已降级规则兜底",
    }


def parse_with_llm(llm: FakeDeepSeek, text: str, case_id: str) -> Dict[str, Any]:
    """单轮 LLM 解析：清洗 JSON → need_info 检测 → pydantic 校验。"""
    raw = llm.complete(text, case_id=case_id, turn=1)
    try:
        obj = parse_llm_json(raw)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "raw": raw, "obj": None}

    if isinstance(obj, dict) and "need_info" in obj:
        return {"ok": True, "need_info_obj": obj, "raw": raw, "obj": obj}
    try:
        schema = RequestSchema.model_validate(obj)
        return {"ok": True, "schema": schema, "raw": raw, "obj": obj}
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
        return {"ok": False, "error": str(exc), "raw": raw, "obj": obj}


def run_case(llm: FakeDeepSeek, case: Dict[str, Any]) -> Dict[str, Any]:
    """跑单条样例：解析（失败重试 1 次→降级）→ need_info/confirm/字段比对。"""
    case_id = case["id"]
    text = case["text"]
    expect = case.get("expect", {})

    first = parse_with_llm(llm, text, case_id)
    attempts = 1
    result: Dict[str, Any] = {
        "case_id": case_id, "text": text, "mock": case.get("mock"),
        "mode": None, "need_info": False, "missing": [], "questions": [],
        "schema": None, "confirm_required": False, "parse_attempts": attempts,
        "error": None, "matched": {}, "field_ok": None,
    }

    if not first["ok"]:
        # 重试 1 次（追加“请只输出 JSON”）
        attempts = 2
        raw2 = llm.complete(text + "\n请只输出 JSON。", case_id=case_id, turn=2)
        try:
            obj2 = parse_llm_json(raw2)
            if isinstance(obj2, dict) and "need_info" in obj2:
                first = {"ok": True, "need_info_obj": obj2, "raw": raw2, "obj": obj2}
            else:
                schema = RequestSchema.model_validate(obj2)
                first = {"ok": True, "schema": schema, "raw": raw2, "obj": obj2}
        except Exception as exc:  # noqa: BLE001
            first = {"ok": False, "error": str(exc), "raw": raw2, "obj": None}

    result["parse_attempts"] = attempts

    if not first["ok"]:
        # 降级规则兜底
        fb = rule_fallback(text)
        result.update(fb)
        return result

    if "need_info_obj" in first:
        try:
            ni = NeedInfo.model_validate(first["need_info_obj"])
        except Exception as exc:  # noqa: BLE001
            result.update({"mode": "rule_fallback", "need_info": True,
                           "missing": ["variables", "timerange", "area"],
                           "questions": ["请补充必要参数"], "error": str(exc)})
            return result
        result.update({
            "mode": "deepseek", "need_info": True,
            "missing": ni.need_info, "questions": ni.questions,
        })
        return result

    schema = first["schema"]
    result["mode"] = "deepseek"
    result["schema"] = schema.model_dump()
    result["confirm_required"] = schema.confidence < 0.7
    result["matched"] = compare_fields(schema, expect)
    result["field_ok"] = all(result["matched"].values()) if result["matched"] else True
    return result


def compare_fields(schema: RequestSchema, expect: Dict[str, Any]) -> Dict[str, bool]:
    checks: Dict[str, bool] = {}
    if "dataset" in expect:
        checks["dataset"] = schema.dataset == expect["dataset"]
    if "dataset_family" in expect:
        checks["dataset_family"] = schema.dataset_family == expect["dataset_family"]
    if "variables" in expect:
        checks["variables"] = set(schema.variables) == set(expect["variables"])
    if "aggregation" in expect:
        checks["aggregation"] = schema.aggregation == expect["aggregation"]
    if "frequency" in expect:
        checks["frequency"] = schema.frequency == expect["frequency"]
    if "pressure_levels" in expect:
        checks["pressure_levels"] = set(schema.pressure_levels or []) == set(expect["pressure_levels"])
    if "area" in expect:
        exp_area = expect["area"]
        checks["area"] = all(
            abs(getattr(schema.area, k) - v) < 1e-6 for k, v in exp_area.items())
    if "timerange" in expect:
        checks["timerange"] = (
            schema.timerange.start == expect["timerange"]["start"]
            and schema.timerange.end == expect["timerange"]["end"])
    return checks


def run_mock(args: argparse.Namespace) -> Dict[str, Any]:
    samples = load_samples(args.samples)
    cases = build_cases_from_samples(samples)
    llm = FakeDeepSeek(cases, seed=args.seed)

    collector = ResultCollector("E3 · DeepSeek NL→Schema 30 样例 (mock)")
    results: List[Dict[str, Any]] = []

    print(f"[E3] 共 {len(samples)} 条样例，开始 mock 解析…")
    for case in samples:
        r = run_case(llm, case)
        results.append(r)
        tag = "OK " if (r["field_ok"] if r["field_ok"] is not None else True) or r["need_info"] else "??"
        status = "need_info" if r["need_info"] else (
            "confirm" if r["confirm_required"] else
            ("degrade" if r["mode"] == "rule_fallback" else "legal"))
        print(f"  [{case['id']}] mock={case.get('mock'):<8} -> {status:<9} "
              f"attempts={r['parse_attempts']} {r.get('error') or ''}")

    # ---------- 指标 ----------
    legal_cases = [r for r in results if r["mock"] == "legal" and r["mode"] == "deepseek"
                   and not r["need_info"]]
    parse_ok_first = [r for r in results if r["mock"] == "legal"
                      and r["mode"] == "deepseek" and r["parse_attempts"] == 1]
    need_cases = [r for r in results if r["mock"] == "need_info"]
    confirm_cases = [r for r in results if r["mock"] == "legal" and r.get("confirm_required")]
    invalid_cases = [r for r in results if r["mock"] == "invalid"]

    # ① 合法 JSON 解析率 100%（legal mock 首轮即成功）
    collector.check(
        len(legal_cases) == len(parse_ok_first) == len([r for r in results if r["mock"] == "legal"]),
        "① 合法 JSON 首轮解析率 100%（含 Markdown 围栏清洗）",
        f"legal={len([r for r in results if r['mock']=='legal'])}, first_ok={len(parse_ok_first)}")

    # ② 字段正确率 ≥80%
    total_checks = sum(len(r["matched"]) for r in legal_cases)
    ok_checks = sum(sum(1 for v in r["matched"].values() if v) for r in legal_cases)
    field_rate = ok_checks / max(total_checks, 1)
    all_fields_ok = all(r["field_ok"] for r in legal_cases)
    collector.check(field_rate >= 0.8 and all_fields_ok,
                    f"② 完整样例字段正确率 ≥80%（实际 {field_rate:.1%}）",
                    f"matched={ok_checks}/{total_checks}, all_ok={all_fields_ok}")

    # ③ need_info 流转正确且 ≤3 问
    ni_ok = all(r["need_info"] and len(r["questions"]) <= 3 and len(r["missing"]) <= 3
                for r in need_cases)
    collector.check(ni_ok, "③ need_info 样例正确返回 missing/questions 且 ≤3 问",
                    f"need_info_cases={len(need_cases)}")

    # ④ confidence<0.7 → 确认分支
    conf_ok = len(confirm_cases) == 2 and all(r["confirm_required"] for r in confirm_cases)
    collector.check(conf_ok, "④ confidence<0.7 样例进入确认分支",
                    f"confirm_cases={len(confirm_cases)}")

    # ⑤ 非法 JSON → 重试后降级，不崩溃
    invalid_ok = all(r["mode"] == "rule_fallback" and r["error"] for r in invalid_cases)
    collector.check(invalid_ok, "⑤ 非法 JSON 触发重试后降级规则/表单，不崩溃",
                    f"invalid_cases={len(invalid_cases)}, degraded={sum(1 for r in invalid_cases if r['mode']=='rule_fallback')}")

    summary = collector.summary()
    return {
        "mode": "mock",
        "n_samples": len(samples),
        "field_correct_rate": round(field_rate, 4),
        "field_checks": {"ok": ok_checks, "total": total_checks},
        "legal_parse_first_try": len(parse_ok_first),
        "need_info_cases": len(need_cases),
        "confirm_cases": len(confirm_cases),
        "invalid_cases": len(invalid_cases),
        "collector": summary,
    }


# ---------------------------------------------------------------------------
# 真实模式：DeepSeek（openai SDK）
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "【角色】你是 ERA5 / ERA5-Land 气象再分析数据下载助手。用户用中文或英文自然语言描述"
    "数据需求，你必须把需求转成符合 CDS API 规范的结构化 JSON。\n"
    "【输出硬性约束】\n"
    "1. 请严格输出一个 JSON 对象，禁止输出解释、Markdown 代码块或多余文字。\n"
    "2. 字段必须符合下方 JSON Schema；未提到但可推导的字段给合理默认值。\n"
    "3. 若信息不足无法确定，请输出一个 JSON 对象："
    '{"need_info": ["字段名", ...], "questions": ["面向用户的追问问题", ...]}，'
    "每次最多追问 3 个字段，不要臆造数值。\n"
    "【JSON Schema】required: dataset, dataset_family, variables, timerange; "
    "dataset 枚举: reanalysis-era5-single-levels / reanalysis-era5-pressure-levels / "
    "reanalysis-era5-single-levels-monthly-means / reanalysis-era5-land / "
    "reanalysis-era5-land-monthly-means; dataset_family 枚举: era5-single / era5-pressure / "
    "era5-monthly / land / land-monthly; variables 为数组（标准变量名）；pressure_levels "
    "仅 era5-pressure 必填、land 系列禁止；timerange={start,end} YYYY-MM-DD；"
    "area={west,south,east,north}；frequency=hourly|daily|monthly；aggregation=raw|mean|sum|max|min；"
    "confidence 0~1。\n"
    "【变量映射】温度→2m_temperature；降水→total_precipitation；风→10m_u/v_component_of_wind；"
    "地面气压→surface_pressure；海平面气压→mean_sea_level_pressure；露点→2m_dewpoint_temperature；"
    "土壤温度→soil_temperature_level_1；土壤湿度→volumetric_soil_water_layer_1；"
    "雪深水当量→snow_depth_water_equivalent；净太阳辐射→surface_net_solar_radiation；"
    "蒸发→evaporation；潜在蒸发→potential_evaporation；地表温度→2m_temperature。\n"
    "【区域】长三角=[118,29,123,34]；华北平原=[112,32,120,41]；珠三角=[112,21,115,24]；"
    "四川盆地=[102,28,108,33]；青藏高原=[78,27,104,38]；华东=[116,26,123,36]；"
    "中国=[73,18,135,54]。\n"
    "【示例】用户: 下载最近五年长江三角洲五六月地表温度 -> "
    '{"dataset":"reanalysis-era5-single-levels","dataset_family":"era5-single",'
    '"variables":["2m_temperature"],"timerange":{"start":"2020-05-01","end":"2025-06-30"},'
    '"area":{"west":118,"south":29,"east":123,"north":34},"frequency":"hourly",'
    '"aggregation":"raw","confidence":0.9}'
)


def run_real(args: argparse.Namespace) -> Dict[str, Any]:
    creds = detect_credentials()
    if not creds["deepseek"]:
        print("[E3-real] 待凭据：未检测到 DEEPSEEK_API_KEY（环境变量或 config/.env），跳过真实段。")
        return {"mode": "real", "status": "待凭据",
                "message": "配置 DEEPSEEK_API_KEY（参考 config/deepseek.example.env）后重跑 --real"}
    try:
        from openai import OpenAI
    except ImportError as exc:
        print(f"[E3-real] openai 未安装: {exc}")
        return {"mode": "real", "status": "error", "message": str(exc)}

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "config", ".env")
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("DEEPSEEK_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    break

    client = OpenAI(api_key=api_key,
                    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

    samples = load_samples(args.samples)
    print(f"[E3-real] 使用 DeepSeek({model}) 跑 {len(samples)} 条样例…")
    results: List[Dict[str, Any]] = []
    for case in samples:
        text = case["text"]
        expect = case.get("expect", {})
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": text}],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=1024,
        )
        raw = resp.choices[0].message.content or ""
        obj = None
        try:
            obj = parse_llm_json(raw)
        except ValueError as exc:
            results.append({"case_id": case["id"], "text": text, "error": str(exc),
                            "raw": raw[:200]})
            print(f"  [{case['id']}] 解析失败: {exc}")
            continue
        if isinstance(obj, dict) and "need_info" in obj:
            results.append({"case_id": case["id"], "text": text, "need_info": True,
                            "obj": obj})
            print(f"  [{case['id']}] need_info: {obj.get('need_info')}")
            continue
        try:
            schema = RequestSchema.model_validate(obj)
        except Exception as exc:  # noqa: BLE001
            results.append({"case_id": case["id"], "text": text, "error": str(exc),
                            "raw": raw[:200]})
            print(f"  [{case['id']}] Schema 校验失败: {exc}")
            continue
        matched = compare_fields(schema, expect)
        ok = all(matched.values()) if matched else True
        results.append({"case_id": case["id"], "text": text, "schema": schema.model_dump(),
                        "matched": matched, "ok": ok})
        print(f"  [{case['id']}] ok={ok} family={schema.dataset_family} "
              f"vars={schema.variables} conf={schema.confidence}")

    total_checks = sum(len(r.get("matched", {})) for r in results if "matched" in r)
    ok_checks = sum(sum(1 for v in r["matched"].values() if v)
                    for r in results if "matched" in r)
    rate = ok_checks / max(total_checks, 1)
    print(f"\n[E3-real] 字段正确率: {rate:.1%} ({ok_checks}/{total_checks})")
    outfile = os.path.join(ensure_dir(args.outdir), "e3_real_result.json")
    with open(outfile, "w", encoding="utf-8") as f:
        json.dump({"results": results, "field_rate": round(rate, 4)}, f,
                  ensure_ascii=False, indent=2, default=str)
    print(f"[E3-real] 结果已写入 {outfile}")
    return {"mode": "real", "status": "done", "field_rate": round(rate, 4),
            "ok_checks": ok_checks, "total_checks": total_checks}


def main() -> int:
    parser = argparse.ArgumentParser(description="E3 · DeepSeek NL→Schema 30 样例")
    parser.add_argument("--real", action="store_true", help="真实 DeepSeek（需 DEEPSEEK_API_KEY）")
    parser.add_argument("--samples", type=str, default=SAMPLES_DEFAULT, help="样例 JSON 路径")
    parser.add_argument("--outdir", type=str, default=OUTDIR_DEFAULT, help="输出目录")
    parser.add_argument("--seed", type=int, default=42, help="mock 随机种子")
    args = parser.parse_args()

    print("=" * 70)
    print(f"E3 · DeepSeek NL→Schema 30 样例   mode={'real' if args.real else 'mock'}")
    print("=" * 70)

    result = run_real(args) if args.real else run_mock(args)
    outfile = os.path.join(ensure_dir(args.outdir), "e3_result.json")
    with open(outfile, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"[E3] 结果已写入 {outfile}")
    ok = result.get("collector", {}).get("ok", True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
