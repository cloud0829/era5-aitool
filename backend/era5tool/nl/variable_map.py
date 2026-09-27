# -*- coding: utf-8 -*-
"""变量映射词典（design-final.md §7.3 / config/variable_map.json）。

- 词条含 names（中英同义词）与 datasets（适用 family）。
- 按 family 过滤保证 ERA5-Land 特有变量/ERA5 特有变量不串用。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from era5tool.config.settings import Settings

# 内置兜底（config/variable_map.json 缺失时使用；保持与 §7.3 一致）
_FALLBACK_SYNONYMS: Dict[str, Dict[str, Any]] = {
    "2m_temperature": {"names": ["温度", "气温", "地表气温", "temperature", "temp", "t2m"],
                       "datasets": ["era5-single", "land"]},
    "total_precipitation": {"names": ["降水", "降雨", "降水量", "precipitation", "precip", "tp"],
                            "datasets": ["era5-single", "land"]},
    "surface_pressure": {"names": ["地面气压", "surface pressure", "sp"],
                         "datasets": ["era5-single", "land"]},
    "2m_dewpoint_temperature": {"names": ["露点温度", "dewpoint", "d2m"],
                                "datasets": ["era5-single", "land"]},
    "10m_u_component_of_wind": {"names": ["纬向风", "u风", "u-wind", "10u"],
                                "datasets": ["era5-single", "land"]},
    "10m_v_component_of_wind": {"names": ["经向风", "v风", "v-wind", "10v"],
                                "datasets": ["era5-single", "land"]},
    "10m_wind_speed": {"names": ["风速", "wind speed", "si10"],
                       "datasets": ["era5-single", "land"]},
    "skin_temperature": {"names": ["地表温度", "skin temperature", "skt"],
                         "datasets": ["era5-single", "land"]},
    "soil_temperature_level_1": {"names": ["土壤温度", "soil temperature", "stl1"],
                                 "datasets": ["era5-single", "land"]},
    "volumetric_soil_water_layer_1": {"names": ["土壤湿度", "土壤含水量", "soil moisture", "swvl1"],
                                      "datasets": ["era5-single", "land"]},
    "snow_depth_water_equivalent": {"names": ["雪深水当量", "雪水当量", "snow depth water equivalent", "sde"],
                                    "datasets": ["land"]},
    "surface_net_solar_radiation": {"names": ["净太阳辐射", "net solar radiation", "ssr"],
                                    "datasets": ["era5-single", "land"]},
    "evaporation": {"names": ["蒸发", "蒸发量", "evaporation", "e"],
                    "datasets": ["era5-single", "land"]},
    "potential_evaporation": {"names": ["潜在蒸发", "蒸散发", "potential evaporation", "pev"],
                              "datasets": ["land"]},
    "mean_sea_level_pressure": {"names": ["海平面气压", "气压", "mslp", "msl"],
                                "datasets": ["era5-single"]},
    "relative_humidity": {"names": ["相对湿度", "湿度", "relative humidity", "r"],
                          "datasets": ["era5-single"]},
    "total_cloud_cover": {"names": ["云量", "总云量", "cloud cover", "tcc"],
                          "datasets": ["era5-single"]},
    "surface_solar_radiation_downwards": {"names": ["太阳辐射", "辐射", "ssrd"],
                                          "datasets": ["era5-single"]},
    "snow_depth": {"names": ["雪深", "积雪", "snow depth", "sd"],
                   "datasets": ["era5-single"]},
    "sea_surface_temperature": {"names": ["海温", "sea surface temperature", "sst"],
                                "datasets": ["era5-single"]},
    "visibility": {"names": ["能见度", "visibility"], "datasets": ["era5-single"]},
    "specific_humidity": {"names": ["比湿", "水汽", "specific humidity", "q"],
                          "datasets": ["era5-single"]},
    "2m_temperature_max": {"names": ["最高气温", "max temperature"],
                           "datasets": ["era5-single", "land"]},
    "2m_temperature_min": {"names": ["最低气温", "min temperature"],
                           "datasets": ["era5-single", "land"]},
    "10m_wind_direction": {"names": ["风向", "wind direction"],
                           "datasets": ["era5-single", "land"]},
}


class VariableMap:
    """变量映射词典加载与查询。"""

    def __init__(self, settings: Settings, path: Optional[Path] = None):
        self.settings = settings
        self.path = path or settings.variable_map_path
        self.version = 2
        self.synonyms: Dict[str, Dict[str, Any]] = dict(_FALLBACK_SYNONYMS)
        self.load()

    def load(self) -> None:
        if self.path.is_file():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                syn = data.get("synonyms") or {}
                if syn:
                    self.synonyms = syn
                self.version = data.get("version", 2)
            except (json.JSONDecodeError, OSError):
                pass

    # ------------------------------------------------------------------
    @staticmethod
    def _effective_family(family: str) -> str:
        """月度家族继承其小时家族变量集（era5-monthly→era5-single；land-monthly→land）。"""
        return {"era5-monthly": "era5-single", "land-monthly": "land"}.get(family, family)

    def canonical_names(self) -> List[str]:
        return list(self.synonyms.keys())

    def names_for(self, variable: str) -> List[str]:
        return list(self.synonyms.get(variable, {}).get("names", []))

    def datasets_for(self, variable: str) -> List[str]:
        return list(self.synonyms.get(variable, {}).get("datasets", []))

    @staticmethod
    def _matches(text_l: str, name: str) -> bool:
        """同义词匹配：中文/长词用子串；短 ASCII 代码按整词匹配（防 'e'/'r' 误伤）。"""
        name_l = name.lower()
        if len(name_l) < 2:
            return False
        if len(name_l) <= 3 and name_l.isascii() and name_l.isalnum():
            return re.search(rf"(?<![a-z0-9]){re.escape(name_l)}(?![a-z0-9])",
                             text_l) is not None
        return name_l in text_l

    def lookup(self, word: str, family: str) -> List[str]:
        """在 family 适用的词条中查找一个词命中哪些标准变量（去重保序）。"""
        family = self._effective_family(family)
        w = (word or "").strip().lower()
        if not w:
            return []
        hits: List[str] = []
        for var, meta in self.synonyms.items():
            if family not in meta.get("datasets", []):
                continue
            for name in meta.get("names", []):
                if self._matches(w, name):
                    hits.append(var)
                    break
        return hits

    def find_all(self, text: str, family: str) -> List[str]:
        """在整句中扫描所有命中的标准变量（按词条顺序）。"""
        family = self._effective_family(family)
        text_l = (text or "").lower()
        hits: List[str] = []
        for var, meta in self.synonyms.items():
            if family not in meta.get("datasets", []):
                continue
            for name in meta.get("names", []):
                if self._matches(text_l, name):
                    hits.append(var)
                    break
        return hits
