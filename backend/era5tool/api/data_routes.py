# -*- coding: utf-8 -*-
"""数据管理路由（design-data-manager.md §3.2/§4）。

- GET    /api/data/files          列表（无参，全量返回 + total）
- GET    /api/data/files/metadata 单文件元数据（path=rel）
- DELETE /api/data/files          删除：path=<rel>（单删）或 paths=<rel>&paths=<rel>（批删）

沿用既有「统一响应 + ApiError → HTTP 200 + {code,data,message}」契约
（见 era5tool/main.py exception_handler 与 api/download_routes.py）。
controller 只做参数解析与封装，业务逻辑全部在 DataManagerService。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from era5tool.api.deps import AppState, get_state
from era5tool.config.schema import (ERR_FILE_BUSY, ERR_FILE_DELETE_FAILED,
                                    ERR_FILE_NOT_FOUND, ERR_PARAM, ApiError,
                                    ApiResponse)
from era5tool.data_manager.service import (MSG_BUSY, MSG_DELETE_FAILED,
                                           MSG_NOT_FOUND,
                                           DataManagerService)

router = APIRouter(prefix="/api/data", tags=["data"])


def _service(state: AppState) -> DataManagerService:
    """每次请求临时构造（无状态，开销可忽略；始终读 state 最新 settings）。"""
    return DataManagerService(state.settings)


def _aggregate(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """批删汇总：deleted/busy/failed/released_bytes + 逐文件 results。"""
    deleted = sum(1 for r in results if r["status"] == "ok")
    busy = sum(1 for r in results if r["status"] == "busy")
    # not_found 与 failed 都计入 failed（前端 Snackbar「N 个被占用/失败」）
    failed = sum(1 for r in results if r["status"] in ("not_found", "failed"))
    released = sum(int(r.get("released_bytes") or 0) for r in results)
    return {
        "requested": len(results),
        "deleted": deleted,
        "busy": busy,
        "failed": failed,
        "released_bytes": released,
        "results": results,
    }


@router.get("/files", response_model=ApiResponse)
def list_files(state: AppState = Depends(get_state)) -> ApiResponse:
    """缓存文件列表（无参、全量返回；扫描含占用状态与大小降序）。"""
    files = _service(state).list_files()
    return ApiResponse.ok({"files": [f.model_dump(mode="json") for f in files],
                           "total": len(files)})


@router.get("/files/metadata", response_model=ApiResponse)
def file_metadata(path: Optional[str] = Query(None, description="缓存相对路径（.nc）"),
                  state: AppState = Depends(get_state)) -> ApiResponse:
    """读取单文件 netCDF 头部元数据（损坏/写中由 nc_meta 兜底 ok:false）。"""
    if not path:
        raise ApiError(ERR_PARAM, "参数错误：需要 path 参数")
    meta = _service(state).read_metadata(path)   # 非法 1001；不存在 6001
    return ApiResponse.ok(meta)


@router.delete("/files", response_model=ApiResponse)
def delete_files(path: Optional[str] = Query(None, description="单删：相对路径"),
                 paths: Optional[List[str]] = Query(None,
                                                    description="批删：重复 paths 参数"),
                 state: AppState = Depends(get_state)) -> ApiResponse:
    """删除缓存文件（单删 path / 批删 paths 二选一，同时传或都缺 → 1001）。

    单删失败直接 ApiError（6001/6002/6003）；批删允许部分成功，返回逐文件结果。
    """
    if path is not None and paths is not None:
        raise ApiError(ERR_PARAM, "参数错误：path 与 paths 不能同时提供")
    if path is not None:
        results = _service(state).delete_paths([path])
        item = results[0]
        if item["status"] == "busy":
            raise ApiError(ERR_FILE_BUSY, item.get("error") or MSG_BUSY)
        if item["status"] == "not_found":
            raise ApiError(ERR_FILE_NOT_FOUND, item.get("error") or MSG_NOT_FOUND)
        if item["status"] == "failed":
            raise ApiError(ERR_FILE_DELETE_FAILED,
                           item.get("error") or MSG_DELETE_FAILED)
        # status == ok
        return ApiResponse.ok({"path": item["path"], "status": "ok",
                               "released_bytes": item["released_bytes"]})
    if paths:
        results = _service(state).delete_paths(list(paths))
        return ApiResponse.ok(_aggregate(results))
    raise ApiError(ERR_PARAM, "参数错误：需要 path（单删）或 paths（批删）")
