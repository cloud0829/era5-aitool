# -*- coding: utf-8 -*-
"""E2 · ERA5-Land 数据集探测（design-final.md §10.3）。

目的：确认 CDS 上 `reanalysis-era5-land`（hourly）与
`reanalysis-era5-land-monthly-means` 的请求参数，固化 §3.4 family 表与 variable_map。

用法：
    python e2_era5land_probe.py            # mock：内置核对表 + build_cds_request 一致性
    python e2_era5land_probe.py --real     # 在线 cdsapi.Client().info() 比对（需 ~/.cdsapirc）

通过标准：
  ① 输出参数核对表（数据集/变量/时间字段/网格/有无气压层/许可）
  ② land 系列无 pressure_levels
  ③ hourly 时间字段为 time 且 monthly-means 无 day 维度
  ④ build_cds_request 与核对表一致
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
from mocks.fake_cdsapi import INFO_TABLE, FakeCdsClient  # noqa: E402

# 内置「已知参数核对表」（数据来源：§3.4 / §10.3 官方已知信息）
CHECKLIST: List[Dict[str, Any]] = [
    {
        "dataset": "reanalysis-era5-land",
        "sample_variables": ["2m_temperature", "total_precipitation",
                             "volumetric_soil_water_layer_1", "snow_depth_water_equivalent"],
        "time_field": "time",
        "time_values": [f"{h:02d}:00" for h in range(24)],
        "day_field": "day",
        "day_values": [f"{d:02d}" for d in range(1, 32)],
        "grid": 0.1,
        "has_pressure_levels": False,
        "license": "需勾选 Land 许可（CDS 数据集许可页）",
        "typical_chunk": "变量×年×月（必要时按 10 天块）",
    },
    {
        "dataset": "reanalysis-era5-land-monthly-means",
        "sample_variables": ["2m_temperature", "total_precipitation",
                             "soil_temperature_level_1"],
        "time_field": "time",
        "time_values": ["00:00"],
        "day_field": None,          # 月均无 day 维度
        "day_values": None,
        "grid": 0.1,
        "has_pressure_levels": False,
        "license": "需勾选 Land 许可（CDS 数据集许可页）",
        "typical_chunk": "变量×年",
    },
]


def _fmt_month(month) -> str:
    """把 1 / '1' / '01' 统一格式化为 '01'。"""
    if isinstance(month, str):
        return month if len(month) == 2 else f"{int(month):02d}"
    return f"{int(month):02d}"


def build_cds_request(dataset: str, family: str, variables: List[str],
                      year: str, month: Optional[str] = None,
                      day_block: Optional[List[str]] = None,
                      pressure_levels: Optional[List[int]] = None,
                      area: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """实验内等价实现 normalizer.build_cds_request（§3.4 伪代码）。"""
    req: Dict[str, Any] = {
        "product_type": ["reanalysis"],
        "variable": variables,
        "year": [str(year)],
        "format": "netcdf",
    }
    if family in ("era5-single", "era5-pressure", "land"):
        req["month"] = [_fmt_month(month)] if month else [f"{m:02d}" for m in range(1, 13)]
        req["day"] = list(day_block) if day_block else [f"{d:02d}" for d in range(1, 32)]
        req["time"] = [f"{h:02d}:00" for h in range(24)]
    elif family in ("land-monthly", "era5-monthly"):
        req["month"] = [_fmt_month(month)] if month else [f"{m:02d}" for m in range(1, 13)]
        req["time"] = ["00:00"]
    if family == "era5-pressure":
        req["pressure_level"] = [str(p) for p in (pressure_levels or [])]
    if area is not None:
        # ⚠️ CDS area 顺序: [lat_max(北), lon_min(西), lat_min(南), lon_max(东)]
        req["area"] = [area["north"], area["west"], area["south"], area["east"]]
    return req


def run_mock(args: argparse.Namespace) -> Dict[str, Any]:
    collector = ResultCollector("E2 · ERA5-Land 数据集探测 (mock)")
    print("[E2] 内置参数核对表：")
    print(f"{'数据集':<42} {'网格':<6} {'time':<22} {'day':<6} {'pressure_levels':<8} {'许可'}")
    for row in CHECKLIST:
        print(f"{row['dataset']:<42} {row['grid']:<6} "
              f"{str(row['time_values'][:2]) + '…' if len(row['time_values']) > 2 else str(row['time_values']):<22} "
              f"{('有' if row['day_field'] else '无'):<6} "
              f"{('无' if not row['has_pressure_levels'] else '有'):<8} {row['license']}")

    # 3 组变量样例 × 2 数据集，验证 build_cds_request 与核对表一致
    sample_sets = [
        (["2m_temperature"], "era5-single"),
        (["total_precipitation", "10m_u_component_of_wind", "10m_v_component_of_wind"], "era5-single"),
        (["volumetric_soil_water_layer_1"], "land"),
    ]
    area = {"north": 34.0, "west": 118.0, "south": 29.0, "east": 123.0}

    collector.check(
        all(not r["has_pressure_levels"] for r in CHECKLIST),
        "① 核对表：land 系列均无气压层")
    land_hourly = CHECKLIST[0]
    land_monthly = CHECKLIST[1]
    collector.check(
        land_hourly["time_field"] == "time" and len(land_hourly["time_values"]) == 24,
        "② 核对表：land hourly 时间字段为 time（24 个时次）")
    collector.check(
        land_monthly["day_field"] is None and land_monthly["time_values"] == ["00:00"],
        "③ 核对表：land monthly-means 无 day 维度、time=[00:00]")

    # build_cds_request 校验
    req_hourly = build_cds_request("reanalysis-era5-land", "land",
                                   ["2m_temperature"], "2020", "05", area=area)
    collector.check("pressure_level" not in req_hourly and "pressure_levels" not in req_hourly,
                    "④a land hourly 请求无 pressure_levels",
                    f"keys={sorted(req_hourly.keys())}")
    collector.check(len(req_hourly.get("time", [])) == 24,
                    "④b land hourly 请求 time 为 24 时次")
    collector.check(req_hourly.get("day") == [f"{d:02d}" for d in range(1, 32)],
                    "④c land hourly 请求含 day=[01..31]")
    collector.check(req_hourly.get("area") == [34.0, 118.0, 29.0, 123.0],
                    "④d CDS area 顺序 [north, west, south, east]",
                    f"area={req_hourly.get('area')}")

    req_monthly = build_cds_request("reanalysis-era5-land-monthly-means", "land-monthly",
                                    ["soil_temperature_level_1"], "2020", area=area)
    collector.check("day" not in req_monthly,
                    "④e land-monthly 请求无 day 维度")
    collector.check(req_monthly.get("time") == ["00:00"],
                    "④f land-monthly 请求 time=[00:00]")

    # mock info() 与核对表一致性（离线也可跑 info 路径）
    fake = FakeCdsClient()
    info = fake.info("reanalysis-era5-land")
    collector.check(info["grid"] == 0.1 and not info["has_pressure_levels"],
                    "⑤ mock info() 返回 0.1° 且无气压层",
                    f"grid={info['grid']}")

    summary = collector.summary()
    return {"mode": "mock", "checklist": CHECKLIST, "collector": summary}


def run_real(args: argparse.Namespace) -> Dict[str, Any]:
    creds = detect_credentials()
    if not creds["cds"]:
        print("[E2-real] 待凭据：需配置 ~/.cdsapirc 才能调用 cdsapi.Client().info()，跳过在线段。")
        return {"mode": "real", "status": "待凭据",
                "message": "需配置 ~/.cdsapirc 后重跑 --real"}
    try:
        import cdsapi
    except ImportError as exc:
        print(f"[E2-real] cdsapi 未安装: {exc}")
        return {"mode": "real", "status": "error", "message": str(exc)}

    print("[E2-real] 检测到 ~/.cdsapirc，在线拉取数据集元数据比对…")
    client = cdsapi.Client()
    comparison = []
    for row in CHECKLIST:
        ds = row["dataset"]
        try:
            meta = client.info(ds)
            online_vars = sorted(meta.get("variables", {}).keys()) if isinstance(
                meta.get("variables"), dict) else []
            comparison.append({
                "dataset": ds,
                "online_variables_count": len(online_vars),
                "online_variables_sample": online_vars[:10],
                "checklist_variables_sample": row["sample_variables"],
            })
            print(f"[E2-real] {ds}: 在线变量数={len(online_vars)}")
        except Exception as exc:  # noqa: BLE001 - 网络/权限错误统一捕获
            comparison.append({"dataset": ds, "error": str(exc)})
            print(f"[E2-real] {ds}: info() 失败 -> {exc}")
    outfile = os.path.join(ensure_dir(args.outdir), "e2_real_comparison.json")
    with open(outfile, "w", encoding="utf-8") as f:
        json.dump(comparison, f, ensure_ascii=False, indent=2)
    print(f"[E2-real] 比对结果已写入 {outfile}")
    return {"mode": "real", "status": "done", "comparison": comparison}


def main() -> int:
    parser = argparse.ArgumentParser(description="E2 · ERA5-Land 数据集探测")
    parser.add_argument("--real", action="store_true", help="在线 cdsapi info()（需 ~/.cdsapirc）")
    parser.add_argument("--outdir", type=str, default=OUTDIR_DEFAULT, help="输出目录")
    args = parser.parse_args()

    print("=" * 70)
    print(f"E2 · ERA5-Land 数据集探测   mode={'real' if args.real else 'mock'}")
    print("=" * 70)

    result = run_real(args) if args.real else run_mock(args)
    outfile = os.path.join(ensure_dir(args.outdir), "e2_result.json")
    with open(outfile, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"[E2] 结果已写入 {outfile}")
    ok = result.get("collector", {}).get("ok", True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
