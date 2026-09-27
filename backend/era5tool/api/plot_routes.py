# -*- coding: utf-8 -*-
"""Plot 路由（design-final.md §5.3）。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from era5tool.api.deps import AppState, get_state
from era5tool.config.schema import (ApiError, ApiResponse, ERR_PARAM, ERR_PLOT,
                                    ERR_TASK_NOT_FOUND, RequestSchema)
from era5tool.models.task import Task

router = APIRouter(prefix="/api/plot", tags=["plot"])


class RenderIn(BaseModel):
    task_id: Optional[str] = None
    request_schema: Optional[Dict[str, Any]] = None
    profile: str = "default_map"
    overrides: Optional[Dict[str, Any]] = None


class ProfileIn(BaseModel):
    profile: Dict[str, Any] = Field(..., description="profile 配置内容")


def _resolve_render_input(body: RenderIn, state: AppState):
    """返回 (files, schema, family, task_id)。"""
    if body.task_id:
        task: Optional[Task] = state.orchestrator.get(body.task_id)
        if task is None:
            raise ApiError(ERR_TASK_NOT_FOUND, f"任务不存在: {body.task_id}")
        params = task.params or {}
        schema = RequestSchema(**params.get("request_schema", {}))
        files = list(task.result.get("files", [])) if task.result else []
        return files, schema, params.get("family", schema.dataset_family), task.id
    if body.request_schema:
        schema = RequestSchema(**body.request_schema)
        return [], schema, schema.dataset_family, "anon"
    raise ApiError(ERR_PARAM, "需要 task_id 或 request_schema")


@router.post("/render", response_model=ApiResponse)
def render(body: RenderIn, state: AppState = Depends(get_state)) -> ApiResponse:
    files, schema, family, task_id = _resolve_render_input(body, state)
    result = state.plot_engine.render(files, schema, family, body.profile,
                                      body.overrides, task_id=task_id)
    return ApiResponse.ok(result)


@router.get("/profiles", response_model=ApiResponse)
def list_profiles(state: AppState = Depends(get_state)) -> ApiResponse:
    return ApiResponse.ok(state.profile_store.list())


@router.get("/profiles/{name}", response_model=ApiResponse)
def get_profile(name: str, state: AppState = Depends(get_state)) -> ApiResponse:
    try:
        cfg = state.profile_store.load(name)
    except (FileNotFoundError, ValueError) as exc:
        raise ApiError(ERR_PLOT, str(exc))
    return ApiResponse.ok({"profile": cfg.to_dict()})


@router.put("/profiles/{name}", response_model=ApiResponse)
def put_profile(name: str, body: ProfileIn,
                state: AppState = Depends(get_state)) -> ApiResponse:
    cfg = state.profile_store.save(name, body.profile)
    return ApiResponse.ok({"profile": cfg.to_dict(), "version": 1})


@router.post("/profiles", response_model=ApiResponse)
def create_profile(body: ProfileIn,
                   state: AppState = Depends(get_state)) -> ApiResponse:
    name = body.profile.get("name", "custom")
    cfg = state.profile_store.save(name, body.profile)
    return ApiResponse.ok({"profile": cfg.to_dict()})
