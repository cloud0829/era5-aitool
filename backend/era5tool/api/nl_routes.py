# -*- coding: utf-8 -*-
"""NL 路由（design-final.md §5.1）。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from era5tool.api.deps import AppState, get_state
from era5tool.config.schema import ApiResponse

router = APIRouter(prefix="/api/nl", tags=["nl"])


class ParseIn(BaseModel):
    text: str = Field(..., min_length=1)
    session_id: Optional[str] = None


class ClarifyIn(BaseModel):
    session_id: str
    answers: Dict[str, Any]


@router.post("/parse", response_model=ApiResponse)
def parse(body: ParseIn, state: AppState = Depends(get_state)) -> ApiResponse:
    result = state.nl_parser.parse(body.text, body.session_id)
    data: Dict[str, Any] = {
        "session_id": result.session_id,
        "engine": result.engine,
        "confirm": result.confirm,
    }
    if result.request_schema is not None:
        data["request_schema"] = result.request_schema.model_dump(mode="json")
    if result.need_info is not None:
        data["need_info"] = result.need_info.model_dump(mode="json")
    return ApiResponse.ok(data)


@router.post("/clarify", response_model=ApiResponse)
def clarify(body: ClarifyIn, state: AppState = Depends(get_state)) -> ApiResponse:
    result = state.nl_parser.clarify(body.session_id, body.answers)
    data: Dict[str, Any] = {
        "session_id": result.session_id,
        "engine": result.engine,
        "confirm": result.confirm,
    }
    if result.request_schema is not None:
        data["request_schema"] = result.request_schema.model_dump(mode="json")
    if result.need_info is not None:
        data["need_info"] = result.need_info.model_dump(mode="json")
    return ApiResponse.ok(data)
