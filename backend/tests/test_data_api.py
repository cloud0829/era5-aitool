# -*- coding: utf-8 -*-
"""数据管理：路由端到端测试（design-data-manager.md T05 / §3.2）。

通过 monkeypatch 把 app_state.settings.data_dir 指向临时目录，实现 API 级隔离：
- GET /files 列表（period/human_size/size 降序/busy 标记/空目录）
- GET /files/metadata（真实 netCDF4 成功 / 损坏 ok:false / 不存在 6001 / 穿越 1001）
- DELETE /files 单删（ok/6001/6002/穿越 1001/缺参与同时传 1001）
- DELETE /files 批删（混合结果汇总）
"""
from __future__ import annotations

from pathlib import Path

import netCDF4
import numpy as np
import pytest

from era5tool.config.schema import ERR_FILE_BUSY, ERR_FILE_NOT_FOUND, ERR_PARAM

REL_YEAR = "reanalysis-era5-single-levels/2m_temperature/monthly/2020.nc"
REL_MONTH = "reanalysis-era5-single-levels/2m_temperature/hourly/2020/01.nc"
REL_DAY = "reanalysis-era5-single-levels/2m_temperature/hourly/2020/01/05.nc"
REL_MISSING = "reanalysis-era5-single-levels/2m_temperature/hourly/1999/12/31.nc"


@pytest.fixture()
def data_env(app_state, tmp_path, monkeypatch):
    """把 app 状态 settings.data_dir 指到临时目录，隔离 /api/data 的数据读写。"""
    monkeypatch.setattr(app_state.settings, "data_dir", tmp_path)
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tasks").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write(root: Path, rel: str, size: int) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


def _put_running_task(tasks_dir: Path, task_id: str, rel_target: str) -> None:
    import json
    tdir = tasks_dir / task_id
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "task.json").write_text(json.dumps({
        "id": task_id, "status": "running",
        "params": {"blocks": [{"rel_target": rel_target}]},
    }), encoding="utf-8")


def _make_nc(path: Path) -> None:
    ds = netCDF4.Dataset(str(path), "w", format="NETCDF4")
    try:
        ds.createDimension("time", None)
        ds.createDimension("latitude", 2)
        ds.createDimension("longitude", 2)
        t = ds.createVariable("time", "f8", ("time",))
        t.units = "hours since 2020-01-01 00:00:00"
        t[:] = np.array([0.0, 6.0])
        lat = ds.createVariable("latitude", "f8", ("latitude",))
        lat.units = "degrees_north"
        lat[:] = np.array([30.0, 40.0])
        lon = ds.createVariable("longitude", "f8", ("longitude",))
        lon.units = "degrees_east"
        lon[:] = np.array([110.0, 120.0])
        v = ds.createVariable("t2m", "f4",
                              ("time", "latitude", "longitude"))
        v.units = "K"
        v.long_name = "2 metre temperature"
        v[:] = np.full((2, 2, 2), 288.15, dtype="f4")
    finally:
        ds.close()


# ---------------------------------------------------------------------------
# GET /api/data/files
# ---------------------------------------------------------------------------
def test_list_files_parses_sorted(data_env, client):
    cache = data_env / "cache"
    _write(cache, REL_YEAR, 300)
    _write(cache, REL_MONTH, 100)
    _write(cache, REL_DAY, 200)
    r = client.get("/api/data/files")
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["total"] == 3
    assert [f["size"] for f in data["files"]] == [300, 200, 100]  # size 降序
    by_rel = {f["rel_path"]: f for f in data["files"]}
    assert by_rel[REL_YEAR]["period"] == "2020"
    assert by_rel[REL_MONTH]["period"] == "2020-01"
    assert by_rel[REL_DAY]["period"] == "2020-01-05"
    assert by_rel[REL_DAY]["human_size"] == "200 B"
    assert all(f["status"] == "ready" for f in data["files"])


def test_list_files_busy_flag(data_env, client):
    cache = data_env / "cache"
    _write(cache, REL_DAY, 100)
    _put_running_task(data_env / "tasks", "t_run", REL_DAY)
    r = client.get("/api/data/files")
    body = r.json()
    row = next(f for f in body["data"]["files"]
               if f["rel_path"] == REL_DAY)
    assert row["status"] == "busy"
    assert row["busy_by"] == ["t_run"]


def test_list_files_empty_dir(data_env, client):
    r = client.get("/api/data/files")
    body = r.json()
    assert body["code"] == 0
    assert body["data"] == {"files": [], "total": 0}


def test_list_files_degrades_unparsed(data_env, client):
    cache = data_env / "cache"
    _write(cache, "foo.nc", 10)
    r = client.get("/api/data/files")
    row = r.json()["data"]["files"][0]
    assert row["parsed"] is False
    assert row["dataset"] == "未知"


# ---------------------------------------------------------------------------
# GET /api/data/files/metadata
# ---------------------------------------------------------------------------
def test_metadata_success(data_env, client):
    cache = data_env / "cache"
    p = _write(cache, REL_DAY, 1)
    _make_nc(p)
    r = client.get("/api/data/files/metadata", params={"path": REL_DAY})
    body = r.json()
    assert body["code"] == 0
    meta = body["data"]
    assert meta["ok"] is True
    assert meta["format"] == "NETCDF4"
    assert {v["name"] for v in meta["variables"]} >= {"t2m", "time"}
    assert meta["time_range"]["start"] == "2020-01-01 00:00:00"


def test_metadata_corrupt_file_ok_false(data_env, client):
    cache = data_env / "cache"
    _write(cache, REL_DAY, 8).write_bytes(b"garbage-not-netcdf")
    r = client.get("/api/data/files/metadata", params={"path": REL_DAY})
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["ok"] is False
    assert "无法读取元数据" in body["data"]["error"]


def test_metadata_missing_6001(data_env, client):
    r = client.get("/api/data/files/metadata",
                   params={"path": REL_MISSING})
    assert r.json()["code"] == ERR_FILE_NOT_FOUND


def test_metadata_bad_path_1001(data_env, client):
    for bad in ("../escape.nc", "C:/abs.nc", "foo.txt"):
        r = client.get("/api/data/files/metadata", params={"path": bad})
        assert r.json()["code"] == ERR_PARAM, bad


# ---------------------------------------------------------------------------
# DELETE /api/data/files（单删）
# ---------------------------------------------------------------------------
def test_delete_single_ok(data_env, client):
    cache = data_env / "cache"
    _write(cache, REL_DAY, 777)
    r = client.delete("/api/data/files", params={"path": REL_DAY})
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "ok"
    assert body["data"]["released_bytes"] == 777
    assert not (cache / REL_DAY).exists()


def test_delete_single_missing_6001(data_env, client):
    r = client.delete("/api/data/files", params={"path": REL_MISSING})
    assert r.json()["code"] == ERR_FILE_NOT_FOUND


def test_delete_single_busy_6002(data_env, client):
    cache = data_env / "cache"
    _write(cache, REL_DAY, 100)
    _put_running_task(data_env / "tasks", "t_run", REL_DAY)
    r = client.delete("/api/data/files", params={"path": REL_DAY})
    body = r.json()
    assert body["code"] == ERR_FILE_BUSY
    assert (cache / REL_DAY).is_file()
    assert "占用" in body["message"]


def test_delete_single_bad_path_1001(data_env, client):
    for bad in ("../escape.nc", "/abs/x.nc", "a\\b.nc", "foo.txt"):
        r = client.delete("/api/data/files", params={"path": bad})
        assert r.json()["code"] == ERR_PARAM, bad


def test_delete_missing_and_both_params_1001(data_env, client):
    assert client.delete("/api/data/files").json()["code"] == ERR_PARAM
    # 同时传 path 与 paths → 1001（URL 手动拼接避免 params 编码歧义）
    both = client.delete(
        f"/api/data/files?path={REL_DAY}&paths={REL_MONTH}")
    assert both.json()["code"] == ERR_PARAM


# ---------------------------------------------------------------------------
# DELETE /api/data/files（批删）
# ---------------------------------------------------------------------------
def test_batch_delete_mixed(data_env, client):
    cache = data_env / "cache"
    _write(cache, REL_YEAR, 500)
    _write(cache, REL_DAY, 300)
    _put_running_task(data_env / "tasks", "t_run", REL_DAY)
    # 批删：1 个可删 + 1 个 busy + 1 个不存在
    r = client.delete("/api/data/files", params=[
        ("paths", REL_YEAR), ("paths", REL_DAY), ("paths", REL_MISSING),
    ])
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["requested"] == 3
    assert data["deleted"] == 1
    assert data["busy"] == 1
    assert data["failed"] == 1
    assert data["released_bytes"] == 500
    by_rel = {x["path"]: x for x in data["results"]}
    assert by_rel[REL_YEAR]["status"] == "ok"
    assert by_rel[REL_DAY]["status"] == "busy"
    assert by_rel[REL_MISSING]["status"] == "not_found"
    assert (cache / REL_DAY).is_file()
    assert not (cache / REL_YEAR).exists()


def test_batch_delete_all_ok(data_env, client):
    cache = data_env / "cache"
    _write(cache, REL_YEAR, 100)
    _write(cache, REL_MONTH, 200)
    r = client.delete("/api/data/files", params=[
        ("paths", REL_YEAR), ("paths", REL_MONTH),
    ])
    data = r.json()["data"]
    assert data["deleted"] == 2
    assert data["busy"] == 0 and data["failed"] == 0
    assert data["released_bytes"] == 300
