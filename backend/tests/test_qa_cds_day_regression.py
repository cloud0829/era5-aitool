# -*- coding: utf-8 -*-
"""QA 独立回归：CDS 请求缺 day 字段导致 400（P0）。

背景（真实下载全部块失败）：
- build_cds_request() 小时级 family 只在传 day_block 时写 day 字段；
- cds_channel.prepare_blocks() 从不传 day_block → 请求永远缺 day；
- CDS v2 判时段不可用 → 400 "None of the data you have requested is
  available yet..."。

本文件独立于工程师新增用例，从真实调用链（Normalizer → CdsChannel.
prepare_blocks）与直接构造两个层面验证修复：
1. hourly 家族（era5-single / era5-pressure / land）不带 day_block 时，
   请求必须含 day=["01".."31"]（31 天缺省整月）；
2. 带 day_block 时用其闭区间范围（含两端）；
3. monthly 家族（era5-monthly / land-monthly）仍无 day 字段。
"""
from __future__ import annotations

from pathlib import Path
from tempfile import mkdtemp

import pytest

from era5tool.acquisition.cds_channel import CdsChannel
from era5tool.acquisition.cds_request import build_cds_request
from era5tool.config.schema import Area, RequestSchema, Timerange
from era5tool.config.settings import Settings
from era5tool.core.normalizer import Normalizer

FULL_MONTH_DAY = [f"{d:02d}" for d in range(1, 32)]
HOURLY_FAMILIES = ["era5-single", "era5-pressure", "land"]
MONTHLY_FAMILIES = ["era5-monthly", "land-monthly"]


def _schema(family: str, variables=None, pressure=None) -> RequestSchema:
    if family == "era5-pressure" and pressure is None:
        pressure = [850, 500]  # era5-pressure 必须带 pressure_levels
    return RequestSchema(
        dataset_family=family,
        variables=variables or ["2m_temperature"],
        pressure_levels=pressure,
        timerange=Timerange(start="2005-01-01", end="2005-12-31"),
        area=Area(west=118, south=29, east=123, north=34),
    )


def _new_settings() -> Settings:
    """独立临时目录（隔离，不触碰真实 config/data）。"""
    return Settings.load(
        config_dir=Path(mkdtemp(prefix="era5_qa_")),
        data_dir=Path(mkdtemp(prefix="era5_qa_")),
    )


# ---------------------------------------------------------------------------
# 1) 直接构造层：Bug 失败形状（不带 day_block）必须已杜绝
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("family", HOURLY_FAMILIES)
def test_hourly_build_without_day_block_has_full_month_day(family):
    """小时级家族 build_cds_request(schema, 2005, 1) 必须含 day 整月。

    这正是本 Bug 的失败形状：旧代码无 day_block → 无 day → CDS v2 400。
    """
    req = build_cds_request(_schema(family), 2005, 1)
    assert "day" in req, f"{family} 请求缺 day（旧 Bug 形状，应已修复）"
    assert req["day"] == FULL_MONTH_DAY
    assert req["day"][0] == "01" and req["day"][-1] == "31"
    assert req["month"] == ["01"]


@pytest.mark.parametrize("family", MONTHLY_FAMILIES)
def test_monthly_build_never_has_day(family):
    """monthly 家族请求不得含 day 字段（monthly-means 无需按日选）。"""
    req = build_cds_request(_schema(family), 2005, 1)
    assert "day" not in req
    assert req["time"] == ["00:00"]


# ---------------------------------------------------------------------------
# 2) day_block 边界（逐日切块路径）
# ---------------------------------------------------------------------------
def test_day_block_full_ten_days():
    req = build_cds_request(_schema("era5-single"), 2005, 1, day_block=(1, 10))
    assert req["day"] == [f"{d:02d}" for d in range(1, 11)]
    assert req["day"][0] == "01" and req["day"][-1] == "10"


def test_day_block_single_day():
    """单日块 (1,1) 与 (31,31) 闭区间含端点。"""
    req1 = build_cds_request(_schema("era5-single"), 2005, 1, day_block=(1, 1))
    assert req1["day"] == ["01"]
    req31 = build_cds_request(_schema("era5-single"), 2005, 1, day_block=(31, 31))
    assert req31["day"] == ["31"]


def test_day_block_mid_month():
    req = build_cds_request(_schema("land"), 2005, 6, day_block=(10, 20))
    assert req["day"] == [f"{d:02d}" for d in range(10, 21)]
    assert req["day"][0] == "10" and req["day"][-1] == "20"


def test_day_block_ignored_for_monthly():
    """monthly 家族即便传 day_block 也不得出现 day。"""
    req = build_cds_request(_schema("era5-monthly"), 2005, 1, day_block=(1, 10))
    assert "day" not in req


# ---------------------------------------------------------------------------
# 3) 真实调用链：Normalizer → CdsChannel.prepare_blocks（Bug 真正发生处）
# ---------------------------------------------------------------------------
def _prepared_requests(family: str, granularity: str = "month"):
    """走 Normalizer.normalize + CdsChannel.prepare_blocks 全链构造请求。"""
    channel = CdsChannel(_new_settings())
    cds_req = Normalizer(granularity=granularity).normalize(_schema(family))
    return channel.prepare_blocks(cds_req), cds_req


@pytest.mark.parametrize("family", ["era5-single", "era5-pressure", "land"])
def test_prepare_blocks_hourly_every_block_has_full_month_day(family):
    """真实链：hourly 家族每个块请求都必须含 day 整月（本 Bug 场景）。

    prepare_blocks 只传 year/month（从不传 day_block），修复前这里生成
    的请求全部缺 day → 真实下载 400。现在必须每块都有 31 天 day。
    """
    blocks, cds_req = _prepared_requests(family)
    assert blocks, f"{family} 应生成块"
    # hourly 家族 = 变量×年×月；本 schema 1 变量 × 1 年 → 12 块
    assert len(blocks) == 12
    for b in blocks:
        req = b["request"]
        assert "day" in req, f"{family} 块 {b['key']} 缺 day（旧 Bug 形状）"
        assert req["day"] == FULL_MONTH_DAY
        assert req["month"] == [f"{b['month']:02d}"]
        assert len(req["time"]) == 24
        if family == "era5-pressure":
            assert req["pressure_level"] == ["850", "500"]


@pytest.mark.parametrize("family", MONTHLY_FAMILIES)
def test_prepare_blocks_monthly_no_day(family):
    """真实链：monthly 家族块请求无 day（monthly-means 语义保持）。"""
    blocks, cds_req = _prepared_requests(family)
    # monthly = 变量×年 → 1 块，month=None → build 补全 12 个月
    assert len(blocks) == 1
    req = blocks[0]["request"]
    assert "day" not in req
    assert req["time"] == ["00:00"]
    assert req["month"] == [f"{m:02d}" for m in range(1, 13)]


@pytest.mark.parametrize("family", ["era5-single", "era5-pressure", "land"])
def test_prepare_blocks_day_each_block_single_day(family):
    """真实链：day 粒度每个块请求 day 恰 1 天，month 字段保留，rel 含 /{dd}。

    day 切块是加速主杠杆：每块只请求单日 → 失败隔离到天、负载均衡。
    """
    blocks, cds_req = _prepared_requests(family, granularity="day")
    assert blocks, f"{family} 应生成 day 块"
    # 2005 非闰年 → 365 块
    assert len(blocks) == 365
    for b in blocks:
        req = b["request"]
        # 每块 day 恰 1 天（与块自身 day 字段一致）
        assert req["day"] == [f"{b['day']:02d}"], \
            f"{family} day 块 {b['key']} 的 day 应恰 1 天，实际 {req['day']}"
        assert len(req["day"]) == 1
        assert req["month"] == [f"{b['month']:02d}"]
        assert len(req["time"]) == 24
        # rel_target 须含 /{dd}.nc（day 粒度缓存路径）
        assert b["rel_target"].endswith(f"/{b['month']:02d}/{b['day']:02d}.nc"), \
            f"{family} day 块 rel_target 应为 .../{{mm}}/{{dd}}.nc，实际 {b['rel_target']}"
