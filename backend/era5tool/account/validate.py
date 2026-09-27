# -*- coding: utf-8 -*-
"""CDS 凭据校验（design-final.md §3.5：cdsapi.info() 不消耗配额）。"""
from __future__ import annotations

from typing import Tuple

from era5tool.config.settings import Settings


def validate_cds(api_key: str, settings: Settings) -> Tuple[bool, str]:
    """校验 API Key；成功返回 (True, "")，失败返回 (False, 原因)。

    api_key 为用户在 CDS 个人页复制的完整凭据（可为 ``数字:UUID`` 旧格式，
    也可为单 token），全程原样透传给 cdsapi.Client，不再拆分 UID。
    """
    if not api_key:
        return False, "API Key 不能为空"
    if settings.download.mock:
        return True, ""   # mock 模式：不做在线校验
    try:
        import cdsapi
        client = cdsapi.Client(url="https://cds.climate.copernicus.eu/api",
                               key=api_key)
        client.info("reanalysis-era5-single-levels")
        return True, ""
    except Exception as exc:
        return False, f"校验失败：{exc}"
