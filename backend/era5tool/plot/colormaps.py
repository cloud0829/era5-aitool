# -*- coding: utf-8 -*-
"""配色注册表（design-final.md §7.1 PlotConfig.colormap）。"""
from __future__ import annotations

from typing import Dict

import matplotlib.cm as cm
import matplotlib.colors as mcolors

# 名称 → matplotlib colormap（含常用反转）
COLORMAPS: Dict[str, object] = {
    "viridis": cm.viridis,
    "plasma": cm.plasma,
    "magma": cm.magma,
    "RdYlBu_r": cm.RdYlBu_r,
    "RdYlBu": cm.RdYlBu,
    "jet": cm.jet,
    "coolwarm": cm.coolwarm,
    "terrain": cm.terrain,
    "Blues": cm.Blues,
    "Reds": cm.Reds,
    "Greens": cm.Greens,
}


def resolve_colormap(name: str):
    """按名解析 colormap；未知回退 viridis。"""
    if name in COLORMAPS:
        return COLORMAPS[name]
    if isinstance(name, str) and name in dir(cm):
        return getattr(cm, name)
    try:
        return mcolors.ListedColormap(mcolors.CSS4_COLORS[name])
    except (KeyError, TypeError):
        return cm.viridis
