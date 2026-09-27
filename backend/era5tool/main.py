# -*- coding: utf-8 -*-
"""FastAPI 入口（design-final.md §2.3/§4）。

- 注册五组路由 + WS /ws/tasks + /health。
- CORS 白名单（Vite dev 5173）；静态托管 web/dist（生产）与 data/products（产物图）。
- ApiError → 统一 {code, data, message} 错误响应。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from era5tool.api import (account_routes, config_routes, data_routes,
                          download_routes, nl_routes, plot_routes)
from era5tool.api.deps import AppState
from era5tool.config.schema import (ApiError, ApiResponse, ERR_PARAM,
                                    ERROR_OK)

PROJECT_ROOT = Path(__file__).resolve().parents[2]   # era5-AItool/


def create_app() -> FastAPI:
    app = FastAPI(title="ERA5-AItool", version="0.2.0")

    app.state.app_state = AppState()
    state: AppState = app.state.app_state

    # CORS（design-final.md §2.3 白名单）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 业务异常 → 统一响应
    @app.exception_handler(ApiError)
    async def api_error_handler(_request, exc: ApiError):
        return JSONResponse(status_code=200,
                            content=ApiResponse.err(exc.code, exc.message,
                                                    exc.data).model_dump())

    # P2-3：pydantic 校验失败（路由内 RequestSchema(**dict)、请求体校验）→ 1001 参数错误
    # （同时覆盖 FastAPI RequestValidationError 子类，统一 {code,data,message} 契约）
    @app.exception_handler(ValidationError)
    async def validation_error_handler(_request, exc: ValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []))
        msg = first.get("msg", "参数校验失败")
        return JSONResponse(
            status_code=200,
            content=ApiResponse.err(ERR_PARAM,
                                    f"参数错误: {loc} {msg}" if loc else f"参数错误: {msg}").model_dump())

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_request, exc: Exception):
        return JSONResponse(status_code=200,
                            content={"code": 5000, "data": None,
                                     "message": f"服务器内部错误: {exc}"})

    # 路由
    app.include_router(nl_routes.router)
    app.include_router(download_routes.router)
    app.include_router(data_routes.router)
    app.include_router(plot_routes.router)
    app.include_router(account_routes.router)
    app.include_router(config_routes.router)

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {"status": "ok"}

    # WS /ws/tasks
    @app.websocket("/ws/tasks")
    async def ws_tasks(ws: WebSocket):
        broker = state.broker
        broker.bind_loop(__import__("asyncio").get_running_loop())
        await broker.connect(ws)
        try:
            while True:
                msg = await ws.receive_text()
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    data = {}
                if data.get("action") == "subscribe" and data.get("task_id"):
                    broker.subscribe(ws, data["task_id"])
                    await ws.send_text(json.dumps(
                        {"type": "subscribed", "task_id": data["task_id"]},
                        ensure_ascii=False))
        except WebSocketDisconnect:
            broker.disconnect(ws)
        except Exception:
            broker.disconnect(ws)

    # 产物静态托管（出图 url /products/...）
    products_dir = state.settings.products_dir
    products_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/products", StaticFiles(directory=str(products_dir)), name="products")

    # 生产静态托管（可选：web/dist）
    dist = PROJECT_ROOT / "web" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="web")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("era5tool.main:app", host="127.0.0.1", port=8000, reload=False)
