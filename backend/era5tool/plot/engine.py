# -*- coding: utf-8 -*-
"""出图引擎（design-final.md §7.1/§10.5：空间图/时序图/动画，E4 验证）。

- 数据来源：任务缓存 NetCDF；无文件/mock 假文件 → 合成数据兜底。
- 底图：cartopy（离线可用则用海岸线/国界）；不可用降级纯 matplotlib。
- 输出：png / gif（PIL 合成）；产物写 data/products/ 并挂 /products 静态路由。
"""
from __future__ import annotations

import io
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import xarray as xr  # noqa: E402

from era5tool.config.schema import ApiError, ERR_PLOT, RequestSchema
from era5tool.config.settings import Settings
from era5tool.plot.colormaps import resolve_colormap
from era5tool.plot.profiles import PlotConfig, ProfileStore
from era5tool.plot.regrid import downsample_time, region_mean, regrid_to
from era5tool.plot.sample_data import make_sample_data


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


class PlotEngine:
    """三类图管线。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.profiles = ProfileStore(settings)
        self.products_dir = settings.products_dir

    # ------------------------------------------------------------------
    def render(self, files: List[str], schema: RequestSchema, family: str,
               profile_name: str, overrides: Optional[Dict[str, Any]] = None,
               task_id: str = "anon") -> Dict[str, Any]:
        try:
            cfg = self.profiles.load(profile_name)
        except (FileNotFoundError, ValueError) as exc:
            raise ApiError(ERR_PLOT, str(exc))
        if overrides:
            cfg = cfg.model_copy(update=overrides)

        ds = self._load_data(files, schema, family, cfg)
        var = schema.variables[0] if schema.variables else None

        # 网格规整（land 0.1° → 目标网格）
        target = cfg.grid_step if cfg.grid_step else self._default_grid(family)
        if "latitude" in ds.dims and float(ds.attrs.get("grid_step", 0)) < target - 1e-9:
            ds = regrid_to(ds, target_step=target)

        # 聚合（面板/profile 指定）
        ds = self._apply_aggregation(ds, cfg)

        if cfg.plot_type == "timeseries":
            artifact = self._render_timeseries(ds, var, cfg, task_id)
        elif cfg.plot_type == "animation":
            artifact = self._render_animation(ds, var, cfg, task_id)
        else:
            artifact = self._render_map(ds, var, cfg, task_id)

        # 清理 matplotlib figure 防内存泄漏
        plt.close("all")
        return artifact

    # ------------------------------------------------------------------
    def _default_grid(self, family: str) -> float:
        return 0.1 if family in ("land", "land-monthly") else 0.25

    def _load_data(self, files: List[str], schema: RequestSchema, family: str,
                   cfg: PlotConfig) -> xr.Dataset:
        candidates = [f for f in (files or []) if f and os.path.isfile(f)]
        if candidates:
            try:
                ds = xr.open_mfdataset(candidates, combine="by_coords")
                ds.attrs.setdefault("grid_step", self._default_grid(family))
                return ds
            except Exception:
                pass
        # 无文件 / 假文件 → 合成数据
        return make_sample_data(schema.variables, family, schema.area,
                                times=cfg.animation_frames * 2)

    def _apply_aggregation(self, ds: xr.Dataset, cfg: PlotConfig) -> xr.Dataset:
        if "time" not in ds.dims:
            return ds
        agg = cfg.aggregation
        if agg == "raw":
            return ds
        reducer = {"mean": lambda d: d.mean(dim="time", keep_attrs=True),
                   "sum": lambda d: d.sum(dim="time", keep_attrs=True),
                   "max": lambda d: d.max(dim="time", keep_attrs=True),
                   "min": lambda d: d.min(dim="time", keep_attrs=True)}.get(agg)
        if reducer is None:
            return ds
        return reducer(ds)

    # ------------------------------------------------------------------
    def _new_axis(self, fig, cfg):
        """返回 (ax, transform)；cartopy 可用时带投影，否则纯 matplotlib。"""
        if cfg.basemap == "cartopy":
            try:
                import cartopy.crs as ccrs
                import cartopy.feature as cfeature
                ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
                ax.coastlines(linewidth=0.5)
                ax.add_feature(cfeature.BORDERS, linewidth=0.3, alpha=0.6)
                ax.gridlines(draw_labels=False, linestyle="--", alpha=0.4)
                return ax, ccrs.PlateCarree()
            except Exception:
                pass
        ax = fig.add_subplot(1, 1, 1)
        ax.grid(True, linestyle="--", alpha=0.4)
        return ax, None

    def _title(self, var: str, cfg: PlotConfig) -> str:
        return cfg.title or f"{var} · {cfg.plot_type}"

    # ------------------------------------------------------------------
    def _render_map(self, ds: xr.Dataset, var: Optional[str], cfg: PlotConfig,
                    task_id: str) -> Dict[str, Any]:
        var = var or list(ds.data_vars)[0]
        data = ds[var]
        if "time" in data.dims and data.sizes["time"] > 1:
            data = data.isel(time=0)
        lat = data.latitude.values
        lon = data.longitude.values
        LON, LAT = np.meshgrid(lon, lat)

        fig = plt.figure(figsize=(9, 6))
        ax, transform = self._new_axis(fig, cfg)
        cmap = resolve_colormap(cfg.colormap)
        pcm = ax.pcolormesh(LON, LAT, data.values, cmap=cmap,
                            transform=transform, shading="auto")
        fig.colorbar(pcm, ax=ax, shrink=0.8, label=var)
        ax.set_title(self._title(var, cfg))
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        return self._save(fig, task_id, f"map_{cfg.name}", cfg.output_format)

    def _render_timeseries(self, ds: xr.Dataset, var: Optional[str],
                           cfg: PlotConfig, task_id: str) -> Dict[str, Any]:
        var = var or list(ds.data_vars)[0]
        series = region_mean(ds, cfg.region or None)[var]
        fig = plt.figure(figsize=(9, 5))
        ax = fig.add_subplot(1, 1, 1)
        ax.plot(series.time.values, series.values, marker="o", markersize=2,
                linewidth=1.2)
        ax.set_title(self._title(var, cfg))
        ax.set_xlabel("Time")
        ax.set_ylabel(var)
        ax.grid(True, linestyle="--", alpha=0.4)
        fig.autofmt_xdate()
        return self._save(fig, task_id, f"timeseries_{cfg.name}", "png")

    def _render_animation(self, ds: xr.Dataset, var: Optional[str],
                          cfg: PlotConfig, task_id: str) -> Dict[str, Any]:
        from PIL import Image
        var = var or list(ds.data_vars)[0]
        data = ds[var]
        frames_ds = downsample_time(ds, max_points=cfg.animation_frames)
        data_f = frames_ds[var]
        lat = data_f.latitude.values
        lon = data_f.longitude.values
        LON, LAT = np.meshgrid(lon, lat)
        cmap = resolve_colormap(cfg.colormap)
        vmin = float(data.min().values)
        vmax = float(data.max().values)
        images: List[Image.Image] = []
        for i in range(data_f.sizes["time"]):
            fig = plt.figure(figsize=(8, 5))
            ax, transform = self._new_axis(fig, cfg)
            ax.pcolormesh(LON, LAT, data_f.isel(time=i).values, cmap=cmap,
                          vmin=vmin, vmax=vmax, transform=transform,
                          shading="auto")
            ax.set_title(f"{self._title(var, cfg)} · frame {i + 1}")
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=90)
            plt.close(fig)
            buf.seek(0)
            images.append(Image.open(buf).convert("RGB"))
        if not images:
            raise ApiError(ERR_PLOT, "动画无帧可渲染")
        path = self._out_path(task_id, f"anim_{cfg.name}", "gif")
        images[0].save(path, format="GIF", save_all=True,
                       append_images=images[1:], duration=400, loop=0)
        return {"artifact": {"url": self._url_for(path), "format": "gif",
                             "size": os.path.getsize(path)}}

    # ------------------------------------------------------------------
    def _save(self, fig, task_id: str, name: str, fmt: str) -> Dict[str, Any]:
        path = self._out_path(task_id, name, fmt)
        fig.savefig(path, format=fmt, dpi=120, bbox_inches="tight")
        return {"artifact": {"url": self._url_for(path), "format": fmt,
                             "size": os.path.getsize(path)}}

    def _out_path(self, task_id: str, name: str, fmt: str) -> Path:
        safe = "".join(c for c in task_id if c.isalnum() or c in "-_") or "anon"
        d = self.products_dir / safe
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{name}_{_now_stamp()}.{fmt}"

    def _url_for(self, path: Path) -> str:
        rel = path.relative_to(self.products_dir).as_posix()
        return f"/products/{rel}"
