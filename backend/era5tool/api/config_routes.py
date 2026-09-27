# -*- coding: utf-8 -*-
"""Config 路由（design-final.md §5.5：含 DeepSeek 配置与变量词典）。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from era5tool.api.deps import AppState, get_state
from era5tool.config.schema import ApiResponse

router = APIRouter(prefix="/api/config", tags=["config"])


class SettingsIn(BaseModel):
    settings: Dict[str, Any]


class LlmIn(BaseModel):
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None


@router.get("", response_model=ApiResponse)
def get_config(state: AppState = Depends(get_state)) -> ApiResponse:
    return ApiResponse.ok(state.settings.public_dict())


@router.put("", response_model=ApiResponse)
def put_config(body: SettingsIn, state: AppState = Depends(get_state)) -> ApiResponse:
    cfg = body.settings
    if "download" in cfg:
        state.settings.download = state.settings.download.model_copy(
            update={k: v for k, v in cfg["download"].items() if v is not None})
    if "plot" in cfg:
        state.settings.plot = state.settings.plot.model_copy(
            update={k: v for k, v in cfg["plot"].items() if v is not None})
    state.settings.save()
    return ApiResponse.ok(state.settings.public_dict())


@router.get("/variable-map", response_model=ApiResponse)
def get_variable_map(state: AppState = Depends(get_state)) -> ApiResponse:
    return ApiResponse.ok({"variable_map": {
        "version": state.var_map.version,
        "synonyms": state.var_map.synonyms,
    }})


@router.get("/llm", response_model=ApiResponse)
def get_llm(state: AppState = Depends(get_state)) -> ApiResponse:
    llm = state.settings.llm
    return ApiResponse.ok({
        "has_key": llm.has_key,
        "base_url": llm.deepseek_base_url,
        "model": llm.deepseek_model,
        "provider": "deepseek",
    })


@router.put("/llm", response_model=ApiResponse)
def put_llm(body: LlmIn, state: AppState = Depends(get_state)) -> ApiResponse:
    llm = state.settings.llm
    if body.base_url:
        llm.deepseek_base_url = body.base_url
    if body.model:
        llm.deepseek_model = body.model
    if body.api_key is not None:
        key = body.api_key.strip()
        llm.deepseek_api_key = key
        if key:
            state.keyring.save_secret("deepseek_api_key", key)
        else:
            state.keyring.delete_secret("deepseek_api_key")
        # 同时写入 config/.env（gitignore），保证子进程/重启可读
        env_path = state.settings.config_dir / ".env"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        lines: Dict[str, str] = {}
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, _, v = line.partition("=")
                    lines[k.strip()] = v.strip()
        if key:
            lines["DEEPSEEK_API_KEY"] = key
        else:
            lines.pop("DEEPSEEK_API_KEY", None)
        env_path.write_text(
            "\n".join(f"{k}={v}" for k, v in lines.items()) + "\n", encoding="utf-8")
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
    state.settings.save()
    return ApiResponse.ok({"has_key": state.settings.llm.has_key})
