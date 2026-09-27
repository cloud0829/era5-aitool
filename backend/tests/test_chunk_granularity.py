# -*- coding: utf-8 -*-
"""切块粒度（day/month/auto）+ 日期裁剪 + 闰年 + 熔断 单测（T01）。

对应 design-speedup-download.md §3.2 / T01 验收点 ①–⑥、⑧ 与 §7 共享约定。
"""
from __future__ import annotations

from pathlib import Path
from tempfile import mkdtemp

import pytest

from era5tool.acquisition.cds_channel import CdsChannel
from era5tool.config.schema import Area, RequestSchema, Timerange
from era5tool.config.settings import Settings
from era5tool.core.normalizer import Normalizer
from era5tool.core.resumable import ResumableStore, ensure_dir

DATASET = "reanalysis-era5-single-levels"


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    """本模块自建临时 config/data 目录，屏蔽 conftest 注入的 ERA5_CONFIG_DIR/DATA_DIR
    （否则 Settings.load 会读取 conftest 的 settings.json，干扰默认字段断言）。"""
    monkeypatch.delenv("ERA5_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ERA5_DATA_DIR", raising=False)


def _schema(family: str = "era5-single", start: str = "2020-01-01",
            end: str = "2020-12-31", area=None, variables=None) -> RequestSchema:
    return RequestSchema(
        dataset_family=family,
        variables=variables or ["2m_temperature"],
        pressure_levels=[850, 500] if family == "era5-pressure" else None,
        timerange=Timerange(start=start, end=end),
        area=area or Area(west=118, south=29, east=123, north=34),
    )


def _settings(granularity: str, **overrides) -> Settings:
    s = Settings.load(config_dir=Path(mkdtemp(prefix="era5_cg_")),
                      data_dir=Path(mkdtemp(prefix="era5_cg_")))
    s.download.mock = True
    s.download.chunk_granularity = granularity
    for k, v in overrides.items():
        setattr(s.download, k, v)
    return s


# ---------------------------------------------------------------------------
# 1. day 切块：闰年 / 合法日历日 / key 形状 / 请求字段
# ---------------------------------------------------------------------------
def test_day_full_year_leap_366():
    """2020（闰年）单变量 day 切块 → 366 块；首块 key=var/2020/01/01。"""
    cds_req = Normalizer(granularity="day").normalize(_schema())
    assert cds_req.granularity == "day"
    assert len(cds_req.blocks) == 366
    assert cds_req.blocks[0]["key"] == "2m_temperature/2020/01/01"
    assert cds_req.blocks[-1]["key"] == "2m_temperature/2020/12/31"


def test_day_full_year_non_leap_365():
    """2019（平年）单变量 day 切块 → 365 块。"""
    cds_req = Normalizer(granularity="day").normalize(
        _schema(start="2019-01-01", end="2019-12-31"))
    assert len(cds_req.blocks) == 365


def test_day_february_legal_days():
    """2 月只生成合法日：2019-02 → 28 块；2020-02 → 29 块（绝不 2 月 30 日）。"""
    feb2019 = Normalizer(granularity="day").normalize(
        _schema(start="2019-02-01", end="2019-02-28"))
    assert len(feb2019.blocks) == 28
    feb2020 = Normalizer(granularity="day").normalize(
        _schema(start="2020-02-01", end="2020-02-29"))
    assert len(feb2020.blocks) == 29
    # 块 day 字段全部合法（1..28 / 1..29）
    assert all(1 <= b["day"] <= 28 for b in feb2019.blocks)
    assert all(1 <= b["day"] <= 29 for b in feb2020.blocks)


def test_day_block_request_fields():
    """每块请求：day 恰 1 天、month 字段保留、time 24 段、变量单块。"""
    cds_req = Normalizer(granularity="day").normalize(_schema())
    blocks = cds_req.blocks
    # 抽样首/末/2 月跳日，覆盖边界
    for b in (blocks[0], blocks[31], blocks[-1]):
        req = (b["request"] if "request" in b else None)
        # 直接用 build_cds_request 验证 day 请求构造一致性
        from era5tool.acquisition.cds_request import build_cds_request
        req = build_cds_request(_schema(), b["year"], b["month"], day=b["day"])
        assert req["day"] == [f"{b['day']:02d}"], b
        assert req["month"] == [f"{b['month']:02d}"]
        assert len(req["time"]) == 24
        assert req["variable"] == ["2m_temperature"]


# ---------------------------------------------------------------------------
# 2. timerange 裁剪（day / month）
# ---------------------------------------------------------------------------
def test_timerange_clip_short_month():
    """2020-06-01..2020-06-05：day 5 块 / month 1 块。"""
    d = Normalizer(granularity="day").normalize(_schema(start="2020-06-01", end="2020-06-05"))
    assert len(d.blocks) == 5
    m = Normalizer(granularity="month").normalize(_schema(start="2020-06-01", end="2020-06-05"))
    assert len(m.blocks) == 1
    assert m.blocks[0]["key"] == "2m_temperature/2020/06"


def test_timerange_clip_quarter():
    """2020-01-01..2020-03-15：day 75 块（31+29+15）/ month 3 块。"""
    d = Normalizer(granularity="day").normalize(_schema(start="2020-01-01", end="2020-03-15"))
    assert len(d.blocks) == 31 + 29 + 15
    m = Normalizer(granularity="month").normalize(_schema(start="2020-01-01", end="2020-03-15"))
    assert len(m.blocks) == 3


# ---------------------------------------------------------------------------
# 3. monthly 家族：固定 monthly，不受粒度请求影响
# ---------------------------------------------------------------------------
def test_monthly_family_fixed():
    """era5-monthly / land-monthly：key 无 /mm，request 无 day。"""
    for fam in ("era5-monthly", "land-monthly"):
        cds_req = Normalizer(granularity="day").normalize(
            _schema(family=fam, start="2020-01-01", end="2021-12-31"))
        assert cds_req.granularity == "monthly"
        assert len(cds_req.blocks) == 2          # 1 变量 × 2 年
        assert cds_req.blocks[0]["key"] == "2m_temperature/2020"
        from era5tool.acquisition.cds_request import build_cds_request
        req = build_cds_request(_schema(family=fam), 2020, None)
        assert "day" not in req


# ---------------------------------------------------------------------------
# 4. 熔断：day 块数 > max_blocks_per_task → 自动降级 month
# ---------------------------------------------------------------------------
def test_fuse_day_over_limit_downgrade_month():
    """day 块数超过上限 → 降级 month 粒度 + 中文 warning。"""
    s = _settings("day", max_blocks_per_task=10)
    cds_req = Normalizer(settings=s, granularity="day").normalize(
        _schema(start="2020-01-01", end="2020-02-15"))      # 46 天 > 10
    assert cds_req.granularity == "month"
    assert any("熔断" in w or "降级" in w for w in cds_req.warnings), cds_req.warnings


def test_fuse_day_under_limit_keeps_day():
    """day 块数未超上限 → 保持 day。"""
    s = _settings("day", max_blocks_per_task=10)
    cds_req = Normalizer(settings=s, granularity="day").normalize(
        _schema(start="2020-01-01", end="2020-01-05"))      # 5 天 < 10
    assert cds_req.granularity == "day"


# ---------------------------------------------------------------------------
# 5. auto 模式
# ---------------------------------------------------------------------------
def test_auto_single_month_uses_day():
    """auto + 1 变量 × 1 月（小区域）→ day。"""
    s = _settings("auto", cds_max_workers=6)
    cds_req = Normalizer(settings=s, granularity="auto").normalize(
        _schema(start="2020-06-01", end="2020-06-30",
                area=Area(west=118, south=29, east=123, north=34)))
    assert cds_req.granularity == "day"


def test_auto_single_year_small_area_uses_month():
    """auto + 1 变量 × 1 年（小区域，month 块数 12 ≥ 2×6）→ month。"""
    s = _settings("auto", cds_max_workers=6)
    cds_req = Normalizer(settings=s, granularity="auto").normalize(
        _schema(start="2020-01-01", end="2020-12-31",
                area=Area(west=118, south=29, east=123, north=34)))
    assert cds_req.granularity == "month"


# ---------------------------------------------------------------------------
# 6. prepare_blocks 缓存路径（day 加 /{dd}）
# ---------------------------------------------------------------------------
def test_prepare_blocks_day_rel_target():
    """day 块 rel_target = dataset/var/hourly/{year}/{mm}/{dd}.nc。"""
    channel = CdsChannel(_settings("day"))
    cds_req = Normalizer(granularity="day").normalize(_schema())
    blocks = channel.prepare_blocks(cds_req)
    assert blocks[0]["rel_target"] == \
        f"{DATASET}/2m_temperature/hourly/2020/01/01.nc"
    # 末块
    assert blocks[-1]["rel_target"] == \
        f"{DATASET}/2m_temperature/hourly/2020/12/31.nc"


# ---------------------------------------------------------------------------
# 7. ResumableStore 对 day key 正常（父目录自动创建）
# ---------------------------------------------------------------------------
def test_resumable_day_key(tmp_path):
    """day key 的 .done 标记落点含多级目录，mark/is 正常。"""
    store = ResumableStore(tmp_path / "t1", None)
    key = "2m_temperature/2020/01/05"
    assert not store.is_done(key)
    store.mark_done(key)
    assert store.is_done(key)
    assert (tmp_path / "t1" / "2m_temperature" / "2020" / "01" / "05.done").is_file()


# ---------------------------------------------------------------------------
# 8. 老 settings.json（无新字段）兼容 + cds_max_workers 默认 6
# ---------------------------------------------------------------------------
def test_legacy_settings_json_load():
    """老 config（无 chunk_granularity/aria2_*）可正常加载，cds_max_workers 读到默认 6。"""
    import json
    cfg = Path(mkdtemp(prefix="era5_legacy_"))
    (cfg / "settings.json").write_text(
        json.dumps({"download": {"mock": True, "cds_max_workers": 2}}),
        encoding="utf-8")
    s = Settings.load(config_dir=cfg,
                      data_dir=Path(mkdtemp(prefix="era5_legacy_")))
    assert s.download.cds_max_workers == 2          # 文件值优先
    assert s.download.chunk_granularity == "day"   # 新字段用默认
    assert s.download.aria2_enabled is False


def test_default_cds_max_workers_six():
    """无文件时 cds_max_workers 默认读到 6（T01 硬改动）。"""
    s = Settings.load(config_dir=Path(mkdtemp(prefix="era5_def_")),
                      data_dir=Path(mkdtemp(prefix="era5_def_")))
    assert s.download.cds_max_workers == 6


# ---------------------------------------------------------------------------
# 9. 非法粒度兜底为 day
# ---------------------------------------------------------------------------
def test_invalid_granularity_falls_back_to_day():
    """settings.json 写非法粒度（"week"）→ 校验兜底为 "day"。"""
    import json
    cfg = Path(mkdtemp(prefix="era5_inv_"))
    (cfg / "settings.json").write_text(
        json.dumps({"download": {"chunk_granularity": "week"}}),
        encoding="utf-8")
    s = Settings.load(config_dir=cfg,
                      data_dir=Path(mkdtemp(prefix="era5_inv_")))
    assert s.download.chunk_granularity == "day"
