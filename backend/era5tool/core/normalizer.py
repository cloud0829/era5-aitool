# -*- coding: utf-8 -*-
"""请求归一化（design-final.md §3.3/§3.4，原 router.py 改造）。

职责：把 RequestSchema 翻译成唯一合法通道 CDS 的请求参数，并处理
ERA5 / ERA5-Land 差异（气压层、时间字段、网格、切块粒度、变量合法性）。

切块粒度（design-speedup-download.md §1）：
- hourly 家族支持 day / month / auto 三种粒度；
- monthly 家族（era5-monthly / land-monthly）固定 monthly，忽略请求粒度；
- day 切块按 timerange 精确到天，日历日由 date 加法保证（绝不 2 月 30 日 → CDS 400）；
- auto：month 块数 ≥ 2×workers 且单块估算体积 ≤ auto_max_block_gb → month，否则 day；
- 熔断：day 块数 > max_blocks_per_task → 自动降级 month（防一次打出上万请求）。
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from era5tool.acquisition.cds_request import FAMILY_TABLE, build_cds_request
from era5tool.config.schema import RequestSchema

ERR_PARAM_MSG = {
    "land_pressure": "ERA5-Land 系列不允许 pressure_levels",
    "pressure_missing": "era5-pressure 必须提供 pressure_levels",
    "family_unknown": "未知 dataset_family",
    "range_invalid": "timerange 非法（start>end）",
    "bad_path_token": "变量/年份含非法路径字符",
}

# 缓存路径里不允许出现的字符（Windows 非法文件名字符 + 路径分隔符 + 通配符 +
# 控制字符）。变量/年份若含这些字符会被拼进 rel_target → 磁盘上出现名为 `*`
# 的诡异目录（用户报告现象）→ 在归一化阶段就拦下，转 ERR_PARAM 给用户。
_ILLEGAL_PATH_CHARS = frozenset('\\/:*?"<>|') | {chr(i) for i in range(32)}


def assert_safe_path_token(value: Any, what: str = "变量名") -> str:
    """校验并返回一个可安全用作缓存路径分量的字符串（非空、非 . / .. 、无非法字符）。"""
    s = str(value)
    if not s or s in (".", ".."):
        raise ValueError(f"{ERR_PARAM_MSG['bad_path_token']}: {value!r}")
    bad = sorted({c for c in s if c in _ILLEGAL_PATH_CHARS})
    if bad:
        raise ValueError(f"{ERR_PARAM_MSG['bad_path_token']}"
                         f"（{' '.join(repr(c) for c in bad)}）: {value!r}")
    return s


def _round_robin_lists(lists: List[List[Any]]) -> List[Any]:
    """把若干等/不等长列表按轮转交错合并（稳定、确定性、保持元素总数）。

    [[a1,a2,a3],[b1,b2]] → [a1,b1,a2,b2,a3]
    """
    out: List[Any] = []
    idx = 0
    while True:
        added = False
        for lst in lists:
            if idx < len(lst):
                out.append(lst[idx])
                added = True
        if not added:
            break
        idx += 1
    return out


def _round_robin(items: List[Any], key_fn) -> List[Any]:
    """按 key_fn 分组（保持组内原序）后轮转交错。"""
    groups: Dict[Any, List[Any]] = {}
    order: List[Any] = []
    for it in items:
        k = key_fn(it)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(it)
    return _round_robin_lists([groups[k] for k in order])


def interleave_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """提交顺序去偏：跨变量 / 跨年轮转交错（bugfix download-gaps）。

    问题：`_split_blocks` 是 `for var: for year: for month:` 的**变量外层循环**，
    于是"变量 A 的全部块排在列表最前面，变量 B 的全排在后面"。配合 run_blocks
    `remaining.pop(0)` 的按序提交，在 CDS 排队/限流（队列墙）场景下后果严重：
    **排在前面的变量把并发额度和 CDS 排队名额全占满，排在后面的变量一块都抢不到**
    ——线上实证：10m_u 拿到 52 个文件，10m_v 一个都没有（用户报告"有的下不上"）。

    交错策略（两级轮转，确定性无随机）：
      1. 每个变量内部先按 **year** 轮转（A/y1m1, A/y2m1, ..., A/y1m2, ...）；
      2. 再跨 **variable** 轮转（A/y1m1, B/y1m1, A/y2m1, B/y2m1, ...）。
    于是任意前 K 个块都均匀覆盖各变量/各年份，任一变量的失败不会导致其他变量
    全灭；同时块集合与数量完全不变（纯排序，不影响断点续传的 key 匹配）。
    """
    if not blocks:
        return []
    if len(blocks) == 1:
        return list(blocks)
    per_var: Dict[Any, List[Dict[str, Any]]] = {}
    order: List[Any] = []
    for b in blocks:
        var = b.get("variable")
        if var not in per_var:
            per_var[var] = []
            order.append(var)
        per_var[var].append(b)
    for var in order:
        per_var[var] = _round_robin(per_var[var], lambda b: b.get("year"))
    return _round_robin_lists([per_var[var] for var in order])


@dataclass
class CdsRequest:
    """归一化后的 CDS 请求（含切块元数据）。"""

    schema: RequestSchema
    dataset: str
    family: str
    grid_step: float
    granularity: str = "month"     # 实际生效粒度：day | month | monthly
    blocks: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset": self.dataset,
            "family": self.family,
            "grid_step": self.grid_step,
            "granularity": self.granularity,
            "blocks": self.blocks,
            "warnings": self.warnings,
        }


class Normalizer:
    """RequestSchema → CdsRequest（含切块）。"""

    def __init__(self, settings=None, granularity: Optional[str] = None) -> None:
        """粒度优先级：显式参数 > settings.download.chunk_granularity > "day"。

        settings 为 None（如单测直接 Normalizer()）时回退 "day"；
        构造时显式 granularity 优先级最高，用于 resume 锁定粒度防漂移。
        """
        self.family_table = FAMILY_TABLE
        self.settings = settings
        if granularity:
            self._granularity = granularity
        elif settings is not None:
            dl = getattr(settings, "download", None)
            self._granularity = getattr(dl, "chunk_granularity", "day") or "day"
        else:
            self._granularity = "day"

    # ------------------------------------------------------------------
    def normalize(self, schema: RequestSchema,
                  granularity: Optional[str] = None) -> CdsRequest:
        family = schema.dataset_family
        if family not in self.family_table:
            raise ValueError(ERR_PARAM_MSG["family_unknown"])
        # 路径安全：变量会被拼进缓存目录名，含 `/ \ : * ? " < > |` 等字符会在磁盘
        # 上生成非法/诡异目录（用户报告出现名为 `*` 的目录）→ 入口即拦下。
        for var in schema.variables:
            assert_safe_path_token(var, "变量名")
        info = self.family_table[family]
        if family in ("land", "land-monthly") and schema.pressure_levels:
            raise ValueError(ERR_PARAM_MSG["land_pressure"])
        if family == "era5-pressure" and not schema.pressure_levels:
            raise ValueError(ERR_PARAM_MSG["pressure_missing"])
        if schema.timerange.start > schema.timerange.end:
            raise ValueError(ERR_PARAM_MSG["range_invalid"])

        warnings: List[str] = []
        if family in ("land", "land-monthly"):
            warnings.append("ERA5-Land 为 0.1° 网格，文件较大；必要时按 10 天块切分")

        requested = granularity or self._granularity
        resolved, gran_warnings = self._resolve_granularity(schema, requested)
        warnings.extend(gran_warnings)

        cds_req = CdsRequest(schema=schema, dataset=info["dataset"], family=family,
                             grid_step=float(info["grid_step"]), granularity=resolved,
                             warnings=warnings)
        cds_req.blocks = self._split_blocks(schema, resolved)
        return cds_req

    # ------------------------------------------------------------------
    def _resolve_granularity(self, schema: RequestSchema,
                             requested: str) -> Tuple[str, List[str]]:
        """解析实际生效粒度（design-speedup-download.md §3.2）。

        返回 (生效粒度, warnings)：
        1) monthly 家族 → 固定 "monthly"（忽略请求值）；
        2) requested == "auto" → month 块数 ≥ 2×workers 且单块估算 ≤ auto_max_block_gb
           → "month"，否则 "day"；
        3) requested == "day" 且 day 块数 > max_blocks_per_task → 熔断降级 "month"。
        其余（"month" / 默认）原样返回。
        """
        warnings: List[str] = []
        monthly_family = schema.dataset_family in ("land-monthly", "era5-monthly")
        if monthly_family:
            return "monthly", warnings

        if requested == "auto":
            workers = 6
            auto_max = 2.0
            if self.settings is not None:
                dl = self.settings.download
                workers = getattr(dl, "cds_max_workers", 6)
                auto_max = getattr(dl, "auto_max_block_gb", 2.0)
            month_count = len(schema.variables) * len(self._iter_months(schema))
            est = self._estimate_block_gb(schema, "month")
            # month 块数足够喂饱并发且单块不大 → 用整月（避免 day 放大固定排队开销）
            if month_count >= 2 * workers and est <= auto_max:
                return "month", warnings
            return "day", warnings

        if requested == "day":
            max_blocks = 2000
            if self.settings is not None:
                max_blocks = getattr(self.settings.download,
                                    "max_blocks_per_task", 2000)
            day_count = len(schema.variables) * len(self._iter_days(schema))
            if day_count > max_blocks:
                # 熔断：一次提交打出上万个 CDS 请求会瞬间打满配额/限速，故降级 month
                warnings.append(
                    f"day 切块数 {day_count} 超过上限 {max_blocks}，"
                    f"已自动降级为 month 粒度（防止过量请求）")
                return "month", warnings

        return requested, warnings

    def _split_blocks(self, schema: RequestSchema,
                      granularity: str) -> List[Dict[str, Any]]:
        """切块（design-speedup-download.md §3.2）。

        块结构新增 day 字段；month / monthly 形状与今天完全一致：
        day     : {"key": f"{var}/{year}/{mm}/{dd}", "variable", "year", "month", "day"}
        month   : {"key": f"{var}/{year}/{mm}",      "variable", "year", "month", "day": None}
        monthly : {"key": f"{var}/{year}",           "variable", "year", "month": None, "day": None}
        """
        blocks: List[Dict[str, Any]] = []
        if granularity == "monthly":
            for var in schema.variables:
                for year in self._years(schema):
                    blocks.append({
                        "key": f"{var}/{year}",
                        "variable": var,
                        "year": year,
                        "month": None,
                        "day": None,
                    })
            return blocks
        if granularity == "month":
            for var in schema.variables:
                for y, m in self._iter_months(schema):
                    blocks.append({
                        "key": f"{var}/{y}/{m:02d}",
                        "variable": var,
                        "year": y,
                        "month": m,
                        "day": None,
                    })
            return blocks
        # day 粒度
        for var in schema.variables:
            for y, m, d in self._iter_days(schema):
                blocks.append({
                    "key": f"{var}/{y}/{m:02d}/{d:02d}",
                    "variable": var,
                    "year": y,
                    "month": m,
                    "day": d,
                })
        return blocks

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_date(s: str) -> Optional[date]:
        """解析 YYYY-MM-DD；失败返回 None（走容错整年）。"""
        try:
            return date.fromisoformat(s)
        except (TypeError, ValueError):
            return None

    def _iter_months(self, schema: RequestSchema) -> List[Tuple[int, int]]:
        """与 timerange 有交集的 (year, month) 列表（新增裁剪）。

        解析失败 → 回退整年 × 12 月（沿用 _years 的容错风格）。
        """
        sd = self._parse_date(schema.timerange.start)
        ed = self._parse_date(schema.timerange.end)
        if sd is None or ed is None:
            out: List[Tuple[int, int]] = []
            for y in self._years(schema):
                for m in range(1, 13):
                    out.append((y, m))
            return out
        out = []
        cur = date(sd.year, sd.month, 1)
        end_month = date(ed.year, ed.month, 1)
        while cur <= end_month:
            out.append((cur.year, cur.month))
            if cur.month == 12:
                cur = date(cur.year + 1, 1, 1)
            else:
                cur = date(cur.year, cur.month + 1, 1)
        return out

    def _iter_days(self, schema: RequestSchema) -> List[Tuple[int, int, int]]:
        """[start, end] 闭区间内每一天 (year, month, day)。

        用 date 逐日迭代，天数由 calendar 加法保证合法（绝不产生 2 月 30 日
        → CDS 400）。解析失败 → 回退整年所有天。
        """
        sd = self._parse_date(schema.timerange.start)
        ed = self._parse_date(schema.timerange.end)
        if sd is None or ed is None:
            out: List[Tuple[int, int, int]] = []
            for y in self._years(schema):
                d = date(y, 1, 1)
                while d.year == y:
                    out.append((d.year, d.month, d.day))
                    d = d + timedelta(days=1)
            return out
        out = []
        d = sd
        while d <= ed:
            out.append((d.year, d.month, d.day))
            d = d + timedelta(days=1)
        return out

    def _estimate_block_gb(self, schema: RequestSchema, granularity: str) -> float:
        """粗略估算单 (变量) 月块体积（GB），仅用于 auto 决策。

        公式：网格点数 × 月内小时数 × 4B(float32) × 变量数 / 1e9。
        月均家族文件很小 → 返回 0（不触发体积熔断）。
        """
        if schema.dataset_family in ("land-monthly", "era5-monthly"):
            return 0.0
        area = schema.area
        grid_step = float(self.family_table[schema.dataset_family]["grid_step"])
        nlon = max(1, int(round((area.east - area.west) / grid_step)) + 1)
        nlat = max(1, int(round((area.north - area.south) / grid_step)) + 1)
        hours_per_month = 24 * 30
        cells = nlon * nlat * hours_per_month * len(schema.variables)
        return cells * 4 / 1e9

    @staticmethod
    def _years(schema: RequestSchema) -> List[int]:
        try:
            y1 = int(schema.timerange.start[:4])
            y2 = int(schema.timerange.end[:4])
        except (TypeError, ValueError):
            return [date.today().year]
        return list(range(y1, y2 + 1))
