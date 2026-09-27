# -*- coding: utf-8 -*-
"""数据管理门面：DataManagerService（design-data-manager.md §3.1）。

编排 扫描 / 占用 / 元数据 / 删除 四类能力，供 api/data_routes.py 调用：

- busy_map()     委托 occupied.collect_busy_map（读磁盘 task.json，与下载链路一致）；
- list_files()   扫描缓存目录 + 填 status/busy_by（size 降序默认）；
- read_metadata()校验路径 → 不存在 6001 → read_netcdf_header（ok:false 兜底不抛）；
- delete_paths() 批量删除（含 现场重查占用 + PermissionError/OSError 兜底）。
- _resolve()     防穿越：仅允许 cache_dir 内 .nc 相对路径；非法 → ApiError(1001)。

安全与错误码契约见 design-data-manager.md §8：单文件流程
路径校验 → 存在性 → 占用重查 → os.unlink → PermissionError/OSError 兜底。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from era5tool.config.schema import (ERR_FILE_NOT_FOUND, ERR_PARAM, ApiError)
from era5tool.config.settings import Settings
from era5tool.data_manager import occupied
from era5tool.data_manager.catalog import CacheFileEntry, scan_cache_files
from era5tool.data_manager.nc_meta import read_netcdf_header

# 中文提示（与既有业务文案风格一致；供路由单删 ApiError / 批删逐文件 results 复用）
MSG_NOT_FOUND = "文件不存在或已被外部移除"
MSG_BUSY = "文件正被下载任务占用，禁止删除"
MSG_DELETE_FAILED = "无权限或文件被其他程序占用，删除失败"
MSG_ILLEGAL_PATH = "非法路径：仅允许缓存目录内的 .nc 相对路径"


class DataManagerService:
    """数据管理服务门面。"""

    def __init__(self, settings: Settings) -> None:
        # 只持 settings（自持 TaskStore 语义：直接读 settings.tasks_dir 的 task.json，
        # 与 orchestrator 的持久化存储一致；每调用动态读目录，支持配置热加载/测试隔离）
        self.settings = settings

    # ------------------------------------------------------------------
    def busy_map(self) -> Dict[str, List[str]]:
        """占用集合 {rel_target: [task_id,...]}（running/pending 任务 blocks 的 rel_target）。"""
        return occupied.collect_busy_map(self.settings.tasks_dir)

    def list_files(self) -> List[CacheFileEntry]:
        """扫描缓存目录建列表行（含 status/busy_by），size 降序默认。"""
        return scan_cache_files(self.settings.cache_dir, self.busy_map())

    # ------------------------------------------------------------------
    def read_metadata(self, rel: str) -> Dict[str, Any]:
        """读取单个文件元数据。

        - 路径非法（绝对路径/../非 .nc）→ ApiError(1001)；
        - 文件不存在/外部已移除 → ApiError(6001)；
        - netCDF 头部读取结果直接返回（内部任何异常已转 ok:false，不再抛）。
        """
        path = self._resolve(rel)
        if not path.is_file():
            raise ApiError(ERR_FILE_NOT_FOUND, MSG_NOT_FOUND)
        return read_netcdf_header(path)

    # ------------------------------------------------------------------
    def delete_paths(self, rels: List[str]) -> List[Dict[str, Any]]:
        """批量删除；返回逐文件结果。

        每项 {path, status: ok|busy|not_found|failed, error?, busy_by?, released_bytes}。
        - 入参即校验：任一 rel 非法（_resolve 抛 1001）→ 整批拒绝（安全红线）；
        - 删除前**现场重查 busy_map**；
        - Windows 写中竞态 → os.unlink 抛 PermissionError/OSError → 捕获后再查 busy：
          命中转 busy（6002 语义），否则 failed（6003 语义），绝不 500、绝不误报成功。
        """
        # 统一预校验：任一 rel 非法即整批拒绝（ApiError 1001），且不执行任何删除。
        # 校验必须发生在任何删除副作用之前，否则"合法在前、非法在后"会先删后报错（非原子）。
        for rel in rels:
            self._resolve(rel)
        return [self._delete_one(rel) for rel in rels]

    def _delete_one(self, rel: str) -> Dict[str, Any]:
        path = self._resolve(rel)          # 非法 → ApiError(1001)（已被 delete_paths 统一预校验拦截）
        if not path.is_file():
            return {"path": rel, "status": "not_found", "error": MSG_NOT_FOUND,
                    "released_bytes": 0}
        owners = self.busy_map().get(rel)
        if owners:
            return {"path": rel, "status": "busy", "error": MSG_BUSY,
                    "busy_by": list(owners), "released_bytes": 0}
        try:
            size = int(path.stat().st_size)
            # 用 os.unlink 而非 Path.unlink：便于测试 monkeypatch 模拟
            # Windows「文件被 worker 占用」抛 PermissionError/OSError 的竞态路径。
            os.unlink(path)
            return {"path": rel, "status": "ok", "released_bytes": size}
        except FileNotFoundError:
            # 列表后外部已删 → 语义 ≈404
            return {"path": rel, "status": "not_found", "error": MSG_NOT_FOUND,
                    "released_bytes": 0}
        except PermissionError:
            # Windows 常见：worker 正 open(target,"wb") 写中（瞬时未进 busy_map）
            # → 再查一次占用：命中转 busy(6002)，否则按无权限 failed(6003)。
            owners2 = self.busy_map().get(rel)
            if owners2:
                return {"path": rel, "status": "busy", "error": MSG_BUSY,
                        "busy_by": list(owners2), "released_bytes": 0}
            return {"path": rel, "status": "failed", "error": MSG_DELETE_FAILED,
                    "released_bytes": 0}
        except OSError:
            owners3 = self.busy_map().get(rel)
            if owners3:
                return {"path": rel, "status": "busy", "error": MSG_BUSY,
                        "busy_by": list(owners3), "released_bytes": 0}
            return {"path": rel, "status": "failed", "error": MSG_DELETE_FAILED,
                    "released_bytes": 0}

    # ------------------------------------------------------------------
    def _resolve(self, rel: str) -> Path:
        """防穿越解析：仅允许 cache_dir 内的 .nc 相对路径。

        拒绝：绝对路径（/ 开头 / 盘符）、含 ".." 段、反斜杠形态、非 .nc、
        空段（双斜杠等）、解算后逃逸出 cache_dir 的路径（含符号链接逃逸）。
        非法一律 ApiError(ERR_PARAM=1001)。
        """
        if not isinstance(rel, str) or not rel or rel.strip() != rel:
            raise ApiError(ERR_PARAM, MSG_ILLEGAL_PATH)
        s = rel.strip()
        # Windows 绝对路径/盘符/UNC 直接拒绝；统一反斜杠视为非法形态
        if s.startswith("/") or "\\" in s or len(s) >= 2 and s[1] == ":":
            raise ApiError(ERR_PARAM, MSG_ILLEGAL_PATH)
        if not s.endswith(".nc"):
            raise ApiError(ERR_PARAM, MSG_ILLEGAL_PATH)
        # 禁止空段（双斜杠）、"." 段与 ".." 穿越段
        if any(seg in ("", ".", "..") for seg in s.split("/")):
            raise ApiError(ERR_PARAM, MSG_ILLEGAL_PATH)
        root = self.settings.cache_dir.resolve()
        candidate = (root / s).resolve()
        # 解算后必须仍在 cache_dir 之下（resolve 已消解符号链接/..）
        if candidate == root or root not in candidate.parents:
            raise ApiError(ERR_PARAM, MSG_ILLEGAL_PATH)
        return candidate
