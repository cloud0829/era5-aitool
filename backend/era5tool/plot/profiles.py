# -*- coding: utf-8 -*-
"""出图配置 profile（design-final.md §3.6/§7.1）。

优先级：面板临时覆盖 > 用户 profile 文件 > 内置默认。
热加载：惰性重读 + mtime 比对；保存用原子写（tmp+rename）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from era5tool.config.settings import Settings


class PlotConfig(BaseModel):
    """出图配置（与前端 ConfigPanel 字段一一对应）。"""

    name: str = "default_map"
    plot_type: str = "map"                 # map | timeseries | animation
    projection: Dict[str, Any] = Field(default_factory=lambda: {"type": "PlateCarree"})
    basemap: str = "cartopy"               # cartopy | plain
    colormap: str = "RdYlBu_r"
    aggregation: str = "raw"               # raw | mean | sum | max | min
    output_format: str = "png"             # png | gif
    title: str = ""
    grid_step: Optional[float] = None      # 覆盖 family 默认网格（None=按 family）
    animation_frames: int = 10
    region: Dict[str, float] = Field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump(mode="json")


DEFAULT_MAP_PROFILE: Dict[str, Any] = {
    "name": "default_map",
    "plot_type": "map",
    "projection": {"type": "PlateCarree"},
    "basemap": "cartopy",
    "colormap": "RdYlBu_r",
    "aggregation": "raw",
    "output_format": "png",
    "title": "",
    "animation_frames": 10,
}


class ProfileStore:
    """profile 目录管理（config/plot_profiles/*.json）。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.dir = settings.plot_profiles_dir
        self._mtime: Dict[str, float] = {}
        self._cache: Dict[str, PlotConfig] = {}
        self.ensure_defaults()

    def ensure_defaults(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if not (self.dir / "default_map.json").is_file():
            self._atomic_write("default_map", DEFAULT_MAP_PROFILE)

    # ------------------------------------------------------------------
    def _path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def list(self) -> Dict:
        profiles = []
        if self.dir.is_dir():
            for p in sorted(self.dir.glob("*.json")):
                try:
                    profiles.append(PlotConfig(**json.loads(p.read_text(encoding="utf-8"))).to_dict())
                except (json.JSONDecodeError, ValueError, OSError):
                    continue
        return {"profiles": profiles}

    def load(self, name: str) -> PlotConfig:
        path = self._path(name)
        mtime = path.stat().st_mtime if path.is_file() else 0.0
        # mtime 热加载
        if self._mtime.get(name) == mtime and name in self._cache:
            return self._cache[name]
        if not path.is_file():
            raise FileNotFoundError(f"profile 不存在: {name}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"profile 文件损坏: {name}（{exc}）")
        cfg = PlotConfig(**data)
        self._mtime[name] = mtime
        self._cache[name] = cfg
        return cfg

    def save(self, name: str, data: Dict[str, Any]) -> PlotConfig:
        cfg = PlotConfig(**{**data, "name": name})
        self._atomic_write(name, cfg.to_dict())
        self._mtime[name] = self._path(name).stat().st_mtime
        self._cache[name] = cfg
        return cfg

    def _atomic_write(self, name: str, data: Dict[str, Any]) -> None:
        path = self._path(name)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
