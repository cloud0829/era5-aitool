# -*- coding: utf-8 -*-
"""数据管理：DataManagerService 测试（design-data-manager.md T02/T05）。

覆盖：占用判定（读磁盘 task.json）、删除保护（busy）、单删成功/不存在、
PermissionError 兜底（占用→busy / 未占用→failed 的 6003 语义）、
路径穿越/绝对路径/非 .nc → 1001、read_metadata 不存在 → 6001、批删混合。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from era5tool.config.schema import ERR_FILE_NOT_FOUND, ERR_PARAM, ApiError
from era5tool.config.settings import Settings
from era5tool.data_manager.service import DataManagerService

REL_OK = "ds/2m_temperature/hourly/2020/01/05.nc"
REL_BUSY = "ds/2m_temperature/hourly/2020/02/06.nc"
REL_MISSING = "ds/2m_temperature/hourly/1999/12/31.nc"


def _make_service(tmp_path: Path) -> DataManagerService:
    """隔离 Settings + Service（data_dir=tasks+cache 都在 tmp_path 下）。"""
    settings = Settings(config_dir=tmp_path, data_dir=tmp_path)
    return DataManagerService(settings)


def _put_task(tasks_dir: Path, task_id: str, status: str,
              rel_targets) -> None:
    tdir = tasks_dir / task_id
    tdir.mkdir(parents=True, exist_ok=True)
    blocks = [{"rel_target": r} for r in rel_targets]
    payload = {
        "id": task_id,
        "status": status,
        "params": {"blocks": blocks},
    }
    (tdir / "task.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_file(root: Path, rel: str, size: int = 100) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


# ---------------------------------------------------------------------------
# 占用集合 / busy_map
# ---------------------------------------------------------------------------
def test_busy_map_collects_running_and_pending(tmp_path):
    svc = _make_service(tmp_path)
    tasks = tmp_path / "tasks"
    _put_task(tasks, "t_run", "running", [REL_OK, REL_BUSY])
    _put_task(tasks, "t_pend", "pending", [REL_OK])
    # 其他状态不产生占用
    _put_task(tasks, "t_success", "success", [REL_BUSY])
    _put_task(tasks, "t_failed", "failed", [REL_OK])
    m = svc.busy_map()
    assert REL_OK in m
    assert set(m[REL_OK]) == {"t_run", "t_pend"}
    assert REL_BUSY in m and m[REL_BUSY] == ["t_run"]


def test_busy_map_tolerates_corrupt_and_missing(tmp_path):
    svc = _make_service(tmp_path)
    tasks = tmp_path / "tasks"
    _put_task(tasks, "t_ok", "running", [REL_OK])
    # 损坏 task.json → 跳过，不抛
    (tasks / "t_bad" / "task.json").parent.mkdir(parents=True, exist_ok=True)
    (tasks / "t_bad" / "task.json").write_text("{broken json", encoding="utf-8")
    # 目录不存在 → {}
    assert DataManagerService(Settings(config_dir=tmp_path,
                                       data_dir=tmp_path / "nope")).busy_map() == {}
    m = svc.busy_map()
    assert m.get(REL_OK) == ["t_ok"]


# ---------------------------------------------------------------------------
# 删除保护 + 单删
# ---------------------------------------------------------------------------
def test_delete_busy_is_protected(tmp_path):
    svc = _make_service(tmp_path)
    cache = tmp_path / "cache"
    _write_file(cache, REL_BUSY)
    _put_task(tmp_path / "tasks", "t_run", "running", [REL_BUSY])
    result = svc.delete_paths([REL_BUSY])[0]
    assert result["status"] == "busy"
    assert result["busy_by"] == ["t_run"]
    assert (cache / REL_BUSY).is_file(), "busy 文件必须保留"


def test_delete_ok_removes_and_returns_bytes(tmp_path):
    svc = _make_service(tmp_path)
    cache = tmp_path / "cache"
    p = _write_file(cache, REL_OK, size=1234)
    result = svc.delete_paths([REL_OK])[0]
    assert result["status"] == "ok"
    assert result["released_bytes"] == 1234
    assert not p.exists()


def test_delete_not_found(tmp_path):
    svc = _make_service(tmp_path)
    result = svc.delete_paths([REL_MISSING])[0]
    assert result["status"] == "not_found"


# ---------------------------------------------------------------------------
# PermissionError/OSError 兜底（Windows 写中竞态）
# ---------------------------------------------------------------------------
def _raiser(exc):
    def _f(*_a, **_k):
        raise exc
    return _f


def test_unlink_permission_error_busy_recheck_returns_busy(tmp_path, monkeypatch):
    """unlink 抛 PermissionError 后现场重查命中 busy → 6002 语义（busy）。"""
    svc = _make_service(tmp_path)
    cache = tmp_path / "cache"
    _write_file(cache, REL_OK)
    _put_task(tmp_path / "tasks", "t_late", "running", [REL_OK])
    # 首次 busy_map（删除前）返回空；unlink 失败后再次 busy_map 返回占用
    calls = {"n": 0}
    busy_map = {REL_OK: ["t_late"]}

    def fake_busy():
        calls["n"] += 1
        if calls["n"] == 1:
            return {}
        return busy_map

    monkeypatch.setattr(svc, "busy_map", fake_busy)
    monkeypatch.setattr(os, "unlink", _raiser(PermissionError(13, "denied")))
    result = svc.delete_paths([REL_OK])[0]
    assert result["status"] == "busy"
    assert result["busy_by"] == ["t_late"]
    assert calls["n"] >= 2


def test_unlink_permission_error_not_busy_returns_failed(tmp_path, monkeypatch):
    """unlink 抛 PermissionError 且重查无占用 → 6003 语义（failed）。"""
    svc = _make_service(tmp_path)
    cache = tmp_path / "cache"
    _write_file(cache, REL_OK)

    def fake_busy():
        return {}

    monkeypatch.setattr(svc, "busy_map", fake_busy)
    monkeypatch.setattr(os, "unlink", _raiser(PermissionError(13, "denied")))
    result = svc.delete_paths([REL_OK])[0]
    assert result["status"] == "failed"
    assert "无权限" in result["error"]
    assert (cache / REL_OK).is_file(), "删除失败文件必须保留"


def test_unlink_oserror_not_busy_returns_failed(tmp_path, monkeypatch):
    svc = _make_service(tmp_path)
    _write_file(tmp_path / "cache", REL_OK)
    monkeypatch.setattr(svc, "busy_map", lambda: {})
    monkeypatch.setattr(os, "unlink", _raiser(OSError(5, "io error")))
    result = svc.delete_paths([REL_OK])[0]
    assert result["status"] == "failed"
    assert "无权限" in result["error"]


# ---------------------------------------------------------------------------
# 路径校验：穿越/绝对路径/非 .nc → 1001
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    "../outside.nc",
    "a/../../b.nc",
    "/abs/path.nc",
    "C:/windows/path.nc",
    "a\\..\\b.nc",
    "ds/var/hourly/2020.txt",
    "ds//var/hourly/2020.nc",
])
def test_resolve_rejects_bad_paths(tmp_path, bad):
    svc = _make_service(tmp_path)
    with pytest.raises(ApiError) as ei:
        svc._resolve(bad)
    assert ei.value.code == ERR_PARAM
    with pytest.raises(ApiError) as ei2:
        svc.delete_paths([bad])
    assert ei2.value.code == ERR_PARAM
    with pytest.raises(ApiError) as ei3:
        svc.read_metadata(bad)
    assert ei3.value.code == ERR_PARAM


def test_resolve_escapes_through_symlink(tmp_path):
    svc = _make_service(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.nc").write_bytes(b"x")
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    link = cache / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台不支持符号链接")
    with pytest.raises(ApiError):
        svc._resolve("link/evil.nc")


# ---------------------------------------------------------------------------
# read_metadata
# ---------------------------------------------------------------------------
def test_read_metadata_missing_raises_6001(tmp_path):
    svc = _make_service(tmp_path)
    with pytest.raises(ApiError) as ei:
        svc.read_metadata(REL_MISSING)
    assert ei.value.code == ERR_FILE_NOT_FOUND


def test_read_metadata_returns_ok_false_for_corrupt(tmp_path):
    """损坏文件（写坏字节）→ 服务返回 {ok:false}，不抛 6001（文件存在）。"""
    svc = _make_service(tmp_path)
    _write_file(tmp_path / "cache", REL_OK, size=8).write_bytes(b"not-netcdf!")
    meta = svc.read_metadata(REL_OK)
    assert meta["ok"] is False
    assert "无法读取元数据" in meta["error"]


# ---------------------------------------------------------------------------
# 批删混合
# ---------------------------------------------------------------------------
def test_batch_delete_invalid_after_valid_aborts_all(tmp_path):
    """BUG-DM-1 回归：整批预校验，非法路径不得导致前面合法文件先被删除。"""
    svc = _make_service(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / "a.nc"
    f.write_bytes(b"x" * 100)
    with pytest.raises(ApiError) as ei:
        svc.delete_paths(["a.nc", "../evil.nc"])
    assert ei.value.code == ERR_PARAM
    assert f.exists(), "整批拒绝红线：非法路径不应导致前面合法文件被删"


def test_batch_delete_mixed(tmp_path):
    svc = _make_service(tmp_path)
    cache = tmp_path / "cache"
    _write_file(cache, REL_OK, size=500)
    _write_file(cache, REL_BUSY, size=300)
    _put_task(tmp_path / "tasks", "t_run", "running", [REL_BUSY])
    results = svc.delete_paths([REL_OK, REL_BUSY, REL_MISSING])
    by_rel = {r["path"]: r for r in results}
    assert by_rel[REL_OK]["status"] == "ok"
    assert by_rel[REL_OK]["released_bytes"] == 500
    assert by_rel[REL_BUSY]["status"] == "busy"
    assert by_rel[REL_MISSING]["status"] == "not_found"
    assert (cache / REL_BUSY).is_file()
    assert not (cache / REL_OK).exists()
