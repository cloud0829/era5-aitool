# -*- coding: utf-8 -*-
"""Download 路由（design-final.md §5.2）。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from era5tool.api.deps import AppState, get_state
from era5tool.config.schema import ApiError, ApiResponse, ERR_TASK_NOT_FOUND, RequestSchema
from era5tool.models.task import TaskType

router = APIRouter(prefix="/api/download", tags=["download"])


class SubmitIn(BaseModel):
    request_schema: Dict[str, Any]


class DeleteAllIn(BaseModel):
    """一键清空任务：是否同时删除已下载的缓存 .nc 文件（与单任务删除语义一致）。"""

    delete_files: bool = True


@router.post("/submit", response_model=ApiResponse)
def submit(body: SubmitIn, state: AppState = Depends(get_state)) -> ApiResponse:
    schema = RequestSchema(**body.request_schema)
    task = state.orchestrator.submit(schema, TaskType.DOWNLOAD)
    return ApiResponse.ok({"task_id": task.id})


@router.get("/list", response_model=ApiResponse)
def list_tasks(status: Optional[str] = Query(None),
               page: int = Query(1, ge=1),
               size: int = Query(20, ge=1, le=100),
               state: AppState = Depends(get_state)) -> ApiResponse:
    return ApiResponse.ok(state.orchestrator.list(status=status, page=page, size=size))


# 声明顺序注意：delete-all 放在任何可能吞路径的 /{task_id} 风格路由之前最稳妥。
# 实际 /api/download/delete-all 为 2 段、GET /{task_id} 为 2 段但方法不同、POST
# /{task_id}/cancel 为 3 段，均不冲突；置于此处保持路由表清晰可读。
@router.post("/delete-all", response_model=ApiResponse)
def delete_all_tasks(body: DeleteAllIn,
                     state: AppState = Depends(get_state)) -> ApiResponse:
    result = state.orchestrator.delete_all(delete_files=body.delete_files)
    return ApiResponse.ok({**result, "delete_files": body.delete_files})


@router.get("/{task_id}", response_model=ApiResponse)
def get_task(task_id: str, state: AppState = Depends(get_state)) -> ApiResponse:
    task = state.orchestrator.get(task_id)
    return ApiResponse.ok(task.to_dict())


@router.post("/{task_id}/cancel", response_model=ApiResponse)
def cancel_task(task_id: str, state: AppState = Depends(get_state)) -> ApiResponse:
    task = state.orchestrator.cancel(task_id)
    return ApiResponse.ok({"task_id": task.id, "status": task.status.value})


@router.post("/{task_id}/resume", response_model=ApiResponse)
def resume_task(task_id: str, state: AppState = Depends(get_state)) -> ApiResponse:
    task = state.orchestrator.resume(task_id)
    return ApiResponse.ok({"task_id": task.id})


@router.post("/{task_id}/retry-failed", response_model=ApiResponse)
def retry_failed_task(task_id: str,
                      state: AppState = Depends(get_state)) -> ApiResponse:
    """一键补漏：只重下失败块（已成功的块自动跳过，不重跑全量）。

    用于任务部分/全部失败后补齐缺口——底层先清除失败标记，再走 resume 路径
    （重建 blocks → pending_blocks 跳过 .done → 只提交失败/缺失块）。
    """
    task = state.orchestrator.retry_failed(task_id)
    return ApiResponse.ok({"task_id": task.id, "status": task.status.value})


@router.delete("/{task_id}", response_model=ApiResponse)
def delete_task(task_id: str, delete_files: bool = Query(False),
                state: AppState = Depends(get_state)) -> ApiResponse:
    ok = state.orchestrator.delete(task_id, delete_files=delete_files)
    if not ok:
        raise ApiError(ERR_TASK_NOT_FOUND, f"任务不存在: {task_id}")
    return ApiResponse.ok({"task_id": task_id})
