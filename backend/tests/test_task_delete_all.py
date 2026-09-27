# -*- coding: utf-8 -*-
"""QA：一键清空任务（POST /api/download/delete-all，批量删除，running 跳过）。

- 空目录 → {deleted:0, skipped_running:0} code=0；
- 混合状态（success/failed/paused/pending + running）→ 非 running 全删、
  running 跳过；磁盘 task 目录相应消失；
- delete_files=True 删除关联缓存 .nc（cache/products），False 保留；
- running 不抛错（仅跳过）；
- 路由端到端返回契约 {code, data:{deleted, skipped_running, delete_files}}。

每个用例用独立 Orchestrator + 独立 tmp 数据目录，避免污染 conftest 共享
session 的 app_state（也不受既有 271 用例残留任务影响）。running 任务通过
直接写盘构造（无需真实后台线程），确定性强。
"""
from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from era5tool.api.download_routes import router as download_router
from era5tool.config.settings import Settings
from era5tool.core.events import EventBroker
from era5tool.core.orchestrator import Orchestrator
from era5tool.models.task import Task, TaskStatus, TaskType


def _schema(year: str = "2020") -> dict:
    """构造带 request_schema 的 params：缓存文件路径可解析（family→freq/年份）。"""
    return {
        "request_schema": {
            "dataset": "reanalysis-era5-single-levels",
            "dataset_family": "era5-single",
            "variables": ["2m_temperature"],
            "timerange": {"start": f"{year}-01-01", "end": f"{year}-12-31"},
        },
        "blocks": [],
    }


def _make_orch(tmp_path: Path) -> Orchestrator:
    """独立隔离的 Orchestrator（tmp_path 下全新 config/data，不经 env 覆盖）。"""
    settings = Settings(config_dir=tmp_path / "config", data_dir=tmp_path / "data")
    settings.ensure_dirs()
    settings.download.mock = True
    return Orchestrator(settings, EventBroker())


def _add_task(orch: Orchestrator, status: TaskStatus,
              params: dict | None = None) -> Task:
    """直接写盘构造指定状态任务（不启动后台线程）。"""
    task = Task(id=f"t_{uuid.uuid4().hex[:10]}", type=TaskType.DOWNLOAD,
                status=status, params=params if params is not None else _schema())
    orch.store.save(task)
    return task


def _cache_paths(settings: Settings) -> tuple[Path, Path]:
    """single/2m_temperature/2020 的缓存与产物目录（store._delete_cache_files 语义）。"""
    rel = Path("reanalysis-era5-single-levels") / "2m_temperature" / "hourly" / "2020"
    return settings.cache_dir / rel, settings.products_dir / rel


def _touch(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    (p / "part.nc").write_text("x", encoding="utf-8")


def _make_app(tmp_path: Path) -> tuple[FastAPI, Orchestrator]:
    """最小 FastAPI：仅挂 download 路由 + 独立 orchestrator（契约 e2e 用）。"""
    orch = _make_orch(tmp_path)
    app = FastAPI(title="era5-delete-all-test")
    app.state.app_state = SimpleNamespace(orchestrator=orch,
                                          settings=orch.settings)
    app.include_router(download_router)
    return app, orch


# ---------------------------------------------------------------------------
# 1. 空任务目录
# ---------------------------------------------------------------------------
def test_delete_all_empty_dir_returns_zero(tmp_path):
    orch = _make_orch(tmp_path)
    assert orch.delete_all(delete_files=True) == {
        "deleted": 0, "skipped_running": 0}
    assert orch.delete_all(delete_files=False) == {
        "deleted": 0, "skipped_running": 0}


def test_delete_all_api_empty_dir_contract(tmp_path):
    """路由 e2e：空目录 → HTTP200 {code:0, data:{deleted:0, skipped_running:0}}。"""
    app, orch = _make_app(tmp_path)
    r = TestClient(app).post("/api/download/delete-all",
                             json={"delete_files": True})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["message"] == "ok"
    assert body["data"] == {"deleted": 0, "skipped_running": 0,
                            "delete_files": True}


# ---------------------------------------------------------------------------
# 2. 混合状态：非 running 全删 + running 跳过 + 磁盘目录消失
# ---------------------------------------------------------------------------
def test_delete_all_mixed_skips_running(tmp_path):
    orch = _make_orch(tmp_path)
    succ = _add_task(orch, TaskStatus.SUCCESS)
    fail = _add_task(orch, TaskStatus.FAILED)
    pause = _add_task(orch, TaskStatus.PAUSED)
    pend = _add_task(orch, TaskStatus.PENDING)
    running = _add_task(orch, TaskStatus.RUNNING)

    res = orch.delete_all(delete_files=False)
    assert res == {"deleted": 4, "skipped_running": 1}

    for t in (succ, fail, pause, pend):
        assert not orch.store.task_dir(t.id).is_dir(), \
            f"非 running 任务 {t.id} 目录应被删除"
    assert orch.store.task_dir(running.id).is_dir(), "running 任务目录应保留"
    # 全量枚举已空 → 只剩 running 那一条
    remain = orch.store.list_all()
    assert [t.id for t in remain] == [running.id]


# ---------------------------------------------------------------------------
# 3. delete_files 语义：True 删缓存 / False 保留
# ---------------------------------------------------------------------------
def test_delete_all_true_removes_cache_files(tmp_path):
    orch = _make_orch(tmp_path)
    task = _add_task(orch, TaskStatus.SUCCESS)
    cache_root, products_root = _cache_paths(orch.settings)
    _touch(cache_root)
    _touch(products_root)

    orch.delete_all(delete_files=True)

    assert not orch.store.task_dir(task.id).is_dir()
    assert not cache_root.exists(), "delete_files=True 应删除 cache .nc 目录"
    assert not products_root.exists(), "delete_files=True 应删除 products 目录"


def test_delete_all_false_keeps_cache_files(tmp_path):
    orch = _make_orch(tmp_path)
    task = _add_task(orch, TaskStatus.SUCCESS)
    cache_root, products_root = _cache_paths(orch.settings)
    _touch(cache_root)
    _touch(products_root)

    orch.delete_all(delete_files=False)

    assert not orch.store.task_dir(task.id).is_dir(), "任务目录仍删除"
    assert cache_root.exists(), "delete_files=False 应保留缓存文件"
    assert products_root.exists(), "delete_files=False 应保留产物"


def test_delete_all_api_true_removes_cache(tmp_path):
    """路由级验证 delete_files=True 会带掉缓存。"""
    app, orch = _make_app(tmp_path)
    _add_task(orch, TaskStatus.SUCCESS)
    cache_root, _ = _cache_paths(orch.settings)
    _touch(cache_root)

    r = TestClient(app).post("/api/download/delete-all",
                             json={"delete_files": True})
    assert r.json()["code"] == 0
    assert not cache_root.exists()


# ---------------------------------------------------------------------------
# 4. running 不抛错（仅跳过）
# ---------------------------------------------------------------------------
def test_delete_all_running_no_error(tmp_path):
    orch = _make_orch(tmp_path)
    r1 = _add_task(orch, TaskStatus.RUNNING)
    r2 = _add_task(orch, TaskStatus.RUNNING)
    s1 = _add_task(orch, TaskStatus.SUCCESS)

    res = orch.delete_all(delete_files=True)   # 不应抛任何异常
    assert res == {"deleted": 1, "skipped_running": 2}
    assert orch.store.task_dir(r1.id).is_dir()
    assert orch.store.task_dir(r2.id).is_dir()
    assert not orch.store.task_dir(s1.id).is_dir()


# ---------------------------------------------------------------------------
# 5. 路由端到端契约
# ---------------------------------------------------------------------------
def test_delete_all_api_contract_with_running(tmp_path):
    app, orch = _make_app(tmp_path)
    succ = _add_task(orch, TaskStatus.SUCCESS)
    fail = _add_task(orch, TaskStatus.FAILED)
    pause = _add_task(orch, TaskStatus.PAUSED)
    running = _add_task(orch, TaskStatus.RUNNING)

    r = TestClient(app).post("/api/download/delete-all",
                             json={"delete_files": True})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["deleted"] == 3
    assert data["skipped_running"] == 1
    assert data["delete_files"] is True
    assert not orch.store.task_dir(succ.id).is_dir()
    assert not orch.store.task_dir(fail.id).is_dir()
    assert not orch.store.task_dir(pause.id).is_dir()
    assert orch.store.task_dir(running.id).is_dir()


def test_delete_all_api_default_delete_files_true(tmp_path):
    """请求体缺省 delete_files → 默认 True（与行删除 delete_files=true 对齐）。"""
    app, orch = _make_app(tmp_path)
    _add_task(orch, TaskStatus.SUCCESS)
    cache_root, _ = _cache_paths(orch.settings)
    _touch(cache_root)

    r = TestClient(app).post("/api/download/delete-all", json={})
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["delete_files"] is True
    assert body["data"]["deleted"] == 1
    assert not cache_root.exists()
