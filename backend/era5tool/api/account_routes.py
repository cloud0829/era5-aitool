# -*- coding: utf-8 -*-
"""Account 路由（design-final.md §5.4）。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from era5tool.account.validate import validate_cds
from era5tool.api.deps import AppState, get_state
from era5tool.config.schema import ApiResponse
from era5tool.config.schema import RequestSchema
from era5tool.models.task import TaskType

router = APIRouter(prefix="/api/account", tags=["account"])


class CredentialIn(BaseModel):
    api_key: str


@router.get("/status", response_model=ApiResponse)
def status(state: AppState = Depends(get_state)) -> ApiResponse:
    return ApiResponse.ok(state.wizard.status())


@router.post("/validate", response_model=ApiResponse)
def validate(body: CredentialIn, state: AppState = Depends(get_state)) -> ApiResponse:
    ok, err = validate_cds(body.api_key, state.settings)
    return ApiResponse.ok({"valid": ok, "error": err} if not ok else {"valid": True})


@router.post("/finalize", response_model=ApiResponse)
def finalize(body: CredentialIn, state: AppState = Depends(get_state)) -> ApiResponse:
    result = state.wizard.finalize(body.api_key)
    return ApiResponse.ok(result)


@router.delete("/credentials", response_model=ApiResponse)
def clear_credentials(state: AppState = Depends(get_state)) -> ApiResponse:
    return ApiResponse.ok(state.wizard.clear())


@router.post("/test-download", response_model=ApiResponse)
def test_download(state: AppState = Depends(get_state)) -> ApiResponse:
    """最小探测下载：1 变量 × 1 年 × 1 月（验证可下载性）。"""
    from datetime import date
    from era5tool.config.schema import Area, Timerange
    schema = RequestSchema(
        dataset="reanalysis-era5-single-levels",
        dataset_family="era5-single",
        variables=["2m_temperature"],
        timerange=Timerange(start=f"{date.today().year}-01-01",
                            end=f"{date.today().year}-01-31"),
        area=Area(west=118, south=29, east=123, north=34),
        frequency="hourly", aggregation="raw", confidence=0.9,
    )
    task = state.orchestrator.submit(schema, TaskType.TEST_DOWNLOAD)
    return ApiResponse.ok({"task_id": task.id})
