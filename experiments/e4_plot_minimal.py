# -*- coding: utf-8 -*-
"""E4 · 出图管线最小验证（design-final.md §10.5）。

目的：合成数据验证三类图管线与离线底图策略（cartopy 底图国内可用性）。

用法：
    python e4_plot_minimal.py            # 全离线（合成数据）
    python e4_plot_minimal.py --outdir experiments/outputs

通过标准：
  ① 空间图输出 png（含 colorbar/标题/底图或海岸线，无报错）
  ② 时序图输出 png（区域平均曲线）
  ③ 动画输出 gif（≥8 帧，PIL 合成）
  ④ 文件大小合理（png < 5MB、gif < 20MB）
  ⑤ ERA5-Land 0.1° 数据 regrid 到 0.25° 后仍可出图
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from typing import Any, Dict, List, Optional

import matplotlib
matplotlib.use("Agg")  # 无头环境

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mocks"))

from exp_common import OUTDIR_DEFAULT, ResultCollector, ensure_dir  # noqa: E402

# cartopy 可用性（python 3.13 可能无 wheel，自动降级）
try:
    import cartopy.crs as ccrs          # noqa: F401
    import cartopy.feature as cfeature  # noqa: F401
    HAS_CARTOPY = True
except Exception:  # noqa: BLE001
    HAS_CARTOPY = False

try:
    from PIL import Image
    HAS_PIL = True
except Exception:  # noqa: BLE001
    HAS_PIL = False

from mocks.make_sample_data import (  # noqa: E402
    make_sample_data, region_mean_series, regrid_to,
)

PNG_MAX_BYTES = 5 * 1024 * 1024     # 5MB
GIF_MAX_BYTES = 20 * 1024 * 1024    # 20MB
ANIM_FRAMES = 10                    # ≥8 帧


def render_map(ds, out_path: str, title: str = "2m Temperature",
               var: str = "2m_temperature", time_index: int = 0,
               grid_step: Optional[float] = None) -> str:
    """空间分布图（含 colorbar/标题；cartopy 可用则加海岸线，否则纯 matplotlib 降级）。"""
    data = ds[var].isel(time=time_index).values
    lat = ds.latitude.values
    lon = ds.longitude.values
    grid_step = grid_step or float(ds.attrs.get("grid_step", 0.25))

    fig = plt.figure(figsize=(7, 5))
    if HAS_CARTOPY:
        ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
        ax.coastlines(resolution="110m", linewidth=0.6)
        ax.add_feature(cfeature.BORDERS, linewidth=0.4, alpha=0.7)
        im = ax.pcolormesh(lon, lat, data, cmap="RdBu_r", shading="auto",
                           transform=ccrs.PlateCarree())
        ax.set_extent([lon.min(), lon.max(), lat.min(), lat.max()], crs=ccrs.PlateCarree())
        try:
            ax.gridlines(draw_labels=True, dms=True, x_inline=False, y_inline=False, alpha=0.4)
        except Exception:  # noqa: BLE001 - 版本兼容：个别 cartopy 版本 gridlines 参数差异
            try:
                ax.gridlines(draw_labels=True, alpha=0.4)
            except Exception:  # noqa: BLE001
                pass
    else:
        ax = fig.add_subplot(1, 1, 1)
        im = ax.imshow(data, origin="lower", cmap="RdBu_r",
                       extent=[lon.min(), lon.max(), lat.min(), lat.max()],
                       aspect="auto")
        ax.set_xlabel("longitude"); ax.set_ylabel("latitude")
    cb = fig.colorbar(im, ax=ax, orientation="vertical", pad=0.02)
    cb.set_label(f"{var} [{ds[var].attrs.get('units', '')}]")
    ax.set_title(f"{title} (t={time_index}, grid={grid_step}°)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def render_timeseries(ds, out_path: str, var: str = "2m_temperature") -> str:
    """区域平均时间序列图。"""
    series = region_mean_series(ds, var)
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot(ds.time.values, series.values, marker="o", markersize=2, linewidth=1)
    ax.set_xlabel("time (hour index)"); ax.set_ylabel(f"{var} [{ds[var].attrs.get('units', '')}]")
    ax.set_title("Region-mean time series")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def render_animation(ds, out_path: str, var: str = "2m_temperature",
                     n_frames: int = ANIM_FRAMES) -> str:
    """时间动画 gif（PIL 合成，≥8 帧）。"""
    if not HAS_PIL:
        raise RuntimeError("缺少 PIL，无法合成 gif")
    n_time = ds.sizes["time"]
    frame_idx = np.linspace(0, n_time - 1, min(n_frames, n_time)).astype(int)
    frames: List[Image.Image] = []
    for ti in frame_idx:
        data = ds[var].isel(time=int(ti)).values
        lat = ds.latitude.values
        lon = ds.longitude.values
        fig = plt.figure(figsize=(5, 4))
        ax = fig.add_subplot(1, 1, 1)
        im = ax.imshow(data, origin="lower", cmap="RdBu_r",
                       extent=[lon.min(), lon.max(), lat.min(), lat.max()], aspect="auto")
        ax.set_title(f"{var} t={int(ti)}")
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=90)
        plt.close(fig)
        buf.seek(0)
        frames.append(Image.open(buf).convert("RGB"))
    frames[0].save(out_path, save_all=True, append_images=frames[1:],
                   duration=300, loop=0)
    return out_path


def verify_gif_frames(path: str) -> int:
    """返回 gif 帧数（PIL 读取）。"""
    if not HAS_PIL:
        return -1
    with Image.open(path) as img:
        return img.n_frames


def run(args: argparse.Namespace) -> Dict[str, Any]:
    outdir = ensure_dir(os.path.join(args.outdir, "e4"))
    collector = ResultCollector("E4 · 出图管线最小验证")
    print(f"[E4] cartopy 可用: {HAS_CARTOPY} | PIL 可用: {HAS_PIL}")

    artifacts: Dict[str, str] = {}

    # ---------- 0.25°（ERA5 风格）三类图 ----------
    ds = make_sample_data(grid_step=0.25, n_time=24)
    map_png = os.path.join(outdir, "map_025.png")
    ts_png = os.path.join(outdir, "timeseries_025.png")
    gif = os.path.join(outdir, "anim_025.gif")
    render_map(ds, map_png, title="ERA5-style 0.25°")
    render_timeseries(ds, ts_png)
    render_animation(ds, gif)
    artifacts.update({"map_025": map_png, "timeseries_025": ts_png, "gif_025": gif})

    # ---------- 0.1°（ERA5-Land 风格）+ regrid 到 0.25° ----------
    ds_land = make_sample_data(grid_step=0.1, n_time=24)
    ds_regrid = regrid_to(ds_land, target_step=0.25)
    map_land_png = os.path.join(outdir, "map_land_010_regrid_025.png")
    render_map(ds_regrid, map_land_png, title="ERA5-Land 0.1° → regrid 0.25°",
               grid_step=0.25)
    artifacts["map_land_regrid"] = map_land_png

    # ---------- 断言 ----------
    for key, path in artifacts.items():
        if not os.path.isfile(path):
            print(f"  [FAIL] {key} 未生成: {path}")
    collector.check(all(os.path.isfile(p) for p in artifacts.values()),
                    "① 空间图/时序图/动画均生成 png/gif")

    sizes = {k: os.path.getsize(v) for k, v in artifacts.items()}
    print("  文件大小:", {k: f"{v/1024:.1f}KB" for k, v in sizes.items()})
    png_ok = all(sizes[k] < PNG_MAX_BYTES for k in artifacts if k.endswith(("png", "map_025", "timeseries_025", "map_land_regrid")))
    collector.check(png_ok, "④a png 文件大小 < 5MB", str({k: sizes[k] for k in sizes if 'png' in k}))
    collector.check(sizes["gif_025"] < GIF_MAX_BYTES, "④b gif 文件大小 < 20MB",
                    f"{sizes['gif_025']/1024:.1f}KB")

    n_frames = verify_gif_frames(gif)
    collector.check(n_frames >= 8, "③ gif 帧数 ≥8（PIL 合成）", f"frames={n_frames}")

    collector.check(os.path.isfile(map_land_png), "⑤ ERA5-Land 0.1° regrid→0.25° 后仍可出图",
                    map_land_png)

    # ② 时序图存在且非空（区域平均曲线）
    ts_size = sizes["timeseries_025"]
    collector.check(ts_size > 10 * 1024, "② 时序图 png 输出且非空",
                    f"{ts_size/1024:.1f}KB")

    summary = collector.summary()
    return {
        "cartopy_available": HAS_CARTOPY,
        "pillow_available": HAS_PIL,
        "basemap": "cartopy 海岸线" if HAS_CARTOPY else "降级：纯 matplotlib（无底图/简化坐标轴）",
        "artifacts": artifacts,
        "sizes_bytes": sizes,
        "gif_frames": n_frames,
        "collector": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="E4 · 出图管线最小验证")
    parser.add_argument("--outdir", type=str, default=OUTDIR_DEFAULT, help="输出目录")
    args = parser.parse_args()

    print("=" * 70)
    print("E4 · 出图管线最小验证   mode=offline")
    print("=" * 70)

    result = run(args)
    outfile = os.path.join(ensure_dir(args.outdir), "e4_result.json")
    with open(outfile, "w", encoding="utf-8") as f:
        json_dump(result, f)
    print(f"[E4] 结果已写入 {outfile}")
    return 0 if result["collector"]["ok"] else 1


def json_dump(obj: Any, f) -> None:
    import json
    json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    raise SystemExit(main())
