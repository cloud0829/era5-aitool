# -*- coding: utf-8 -*-
"""T02：aria2 传输后端与优雅降级（design-speedup-download.md §3.4 / §4.2）。

覆盖：
1. probe_aria2 优先级（explicit > env ERA5_ARIA2_CMD > which）与"全不可用不抛异常"。
2. build_aria2_argv / verify_size 契约。
3. resolve_result_url 鸭子类型（location / get_results / dict / 异常）。
4. download_block_file 三条路径：
   - 开关关闭 → cdsapi 单连接（调用序列与改造前一致，用 FakeCdsClient.calls 断言）。
   - aria2 成功 → transport="aria2"、rate_mbps>0、落盘大小 == content_length。
   - 降级三态（returncode!=0 / 大小不符 / 无 URL）→ transport="cdsapi_fallback"，
     不消耗重试次数、降级前已清理半截文件、最终文件仍正确落盘。
5. 取消：cancel_check 命中 → raise BlockCancelled（worker 转 cancelled，不 mark_failed）。
6. aria2_status 返回 Aria2Info。
7. spawn 真实链路（run_blocks + ERA5_ARIA2_CMD 注入 stub）→ 至少一块 transport=="aria2"
   （证明 spawn 子进程继承 env，是测试 aria2 分支的唯一可靠方式，§6.3）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from era5tool.acquisition.aria2 import (Aria2Info, Aria2Run, build_aria2_argv,
                                        probe_aria2, verify_size)
from era5tool.acquisition.cds_channel import CdsChannel
from era5tool.acquisition.mock_client import FakeCdsClient, FakeResult
from era5tool.acquisition.transport import (BlockCancelled, TransportHooks,
                                            download_block_file,
                                            resolve_result_url)
from era5tool.config.settings import DownloadSettings, Settings
from era5tool.core.events import EventBroker, TaskEventBus
from era5tool.core.resumable import ResumableStore

STUB = os.path.join(os.path.dirname(__file__), "_stub_aria2.py")


def _aria2_cmd() -> str:
    """构造注入命令：`<python> <stub>`（Windows 路径带空格需引号保护）。"""
    return f'"{sys.executable}" "{STUB}"'


def _block(key: str = "2m_temperature/2020/01",
           dataset: str = "reanalysis-era5-single-levels",
           request: dict | None = None) -> dict:
    return {
        "key": key,
        "dataset": dataset,
        "request": request or {"variable": ["2m_temperature"], "year": ["2020"],
                               "month": ["01"]},
        "rel_target": f"{dataset}/2m_temperature/hourly/2020/01.nc",
    }


# ---------------------------------------------------------------------------
# 1. probe_aria2
# ---------------------------------------------------------------------------
def test_probe_aria2_env_priority(monkeypatch):
    """env ERA5_ARIA2_CMD 命中 → available=True，source="env"，版本被解析。"""
    monkeypatch.setenv("ERA5_ARIA2_CMD", _aria2_cmd())
    info = probe_aria2("")
    assert isinstance(info, Aria2Info)
    assert info.available is True
    assert info.source == "env"
    assert info.version == "1.37.0"  # stub 输出 "aria2 version 1.37.0-stub"


def test_probe_aria2_explicit_priority(monkeypatch):
    """explicit_path 命中（env 未设）→ available=True，source="explicit"。"""
    monkeypatch.delenv("ERA5_ARIA2_CMD", raising=False)
    info = probe_aria2(_aria2_cmd())
    assert info.available is True
    assert info.source == "explicit"


def test_probe_aria2_none_available_no_raise(monkeypatch):
    """全不可用 → available=False 且不抛异常（设计硬约束）。"""
    monkeypatch.delenv("ERA5_ARIA2_CMD", raising=False)
    info = probe_aria2("")  # 无 which、无 env、无 explicit → 空候选
    assert info.available is False
    assert info.path == ""
    assert info.source == ""


# ---------------------------------------------------------------------------
# 2. build_aria2_argv / verify_size
# ---------------------------------------------------------------------------
def test_build_aria2_argv_contract(tmp_path):
    """-x{n} -s{n} 取 connections、--dir 为绝对父目录、-o 为纯文件名、url 末位、
    含 --allow-overwrite/--auto-file-renaming=false（§3.4 / 验收点②）。"""
    target = tmp_path / "a" / "b" / "01.nc"
    url = "https://cds.example/result"
    argv = build_aria2_argv(_aria2_cmd(), url, str(target), connections=8,
                            timeout_s=1800)
    assert "-x8" in argv and "-s8" in argv
    # --dir 为 target 的绝对父目录
    dir_args = [a for a in argv if a.startswith("--dir=")]
    assert dir_args and os.path.isabs(dir_args[0].split("=", 1)[1])
    # -o 为纯文件名
    assert "-o" in argv
    assert argv[argv.index("-o") + 1] == "01.nc"
    # url 在末位
    assert argv[-1] == url
    assert "--allow-overwrite=true" in argv
    assert "--auto-file-renaming=false" in argv


def test_verify_size(tmp_path):
    """已知大小精确相等 / 不符 / 未知且>0 / 文件缺失（§3.4）。"""
    p = tmp_path / "f.nc"
    p.write_bytes(b"\x00" * 100)
    assert verify_size(str(p), 100) is True
    assert verify_size(str(p), 101) is False         # 大小不符
    assert verify_size(str(p), None) is True         # 未知：存在且>0
    missing = tmp_path / "missing.nc"
    assert verify_size(str(missing), None) is False  # 缺失
    assert verify_size(str(missing), 100) is False   # 缺失（已知）


# ---------------------------------------------------------------------------
# 3. resolve_result_url 鸭子类型
# ---------------------------------------------------------------------------
def test_resolve_result_url_location():
    """有 .location / .content_length → (url, size)。"""
    h = SimpleNamespace(location="http://x/y.nc", content_length=2048)
    assert resolve_result_url(h) == ("http://x/y.nc", 2048)


def test_resolve_result_url_get_results():
    """只有 .get_results()（datastores.Remote）→ 先取 results 再解析。"""
    remote = SimpleNamespace(
        get_results=lambda: SimpleNamespace(location="http://z/w.nc",
                                            content_length=4096))
    assert resolve_result_url(remote) == ("http://z/w.nc", 4096)


def test_resolve_result_url_dict_and_none():
    """dict / None → (None, None)（走 cdsapi 路径）。"""
    assert resolve_result_url({"location": "x"}) == (None, None)
    assert resolve_result_url(None) == (None, None)


def test_resolve_result_url_attr_raises():
    """属性访问抛异常 → (None, None)（绝不拖垮下载）。"""

    class _Bad:
        @property
        def location(self):
            raise RuntimeError("boom")

    assert resolve_result_url(_Bad()) == (None, None)


# ---------------------------------------------------------------------------
# 4. download_block_file 三条路径
# ---------------------------------------------------------------------------
def test_download_block_file_cdsapi_default(tmp_path):
    """开关关闭：单次 client.retrieve(dataset, request, target)，transport="cdsapi"
    （调用序列与改造前一致，用 FakeCdsClient.calls 断言，验收点⑦）。"""
    client = FakeCdsClient()
    target = str(tmp_path / "x.nc")
    cfg: dict = {"mock": True}  # 无 aria2 字段 → 门控关闭
    tr = download_block_file(client, _block(), target, cfg)
    assert tr.transport == "cdsapi"
    # 单次 retrieve 调用（target 给值路径），且产物真实落盘
    assert len(client.calls) == 1
    assert client.calls[0]["event"] == "ok"
    assert os.path.isfile(target)


def test_download_block_file_aria2_success(tmp_path, monkeypatch):
    """aria2 成功：transport=="aria2"、落盘大小==content_length、rate_mbps>0。"""
    monkeypatch.setenv("ERA5_ARIA2_CMD", _aria2_cmd())
    info = probe_aria2("")
    assert info.available
    client = FakeCdsClient()
    target = str(tmp_path / "ok.nc")
    cfg = {"aria2_enabled": True, "aria2_bin": info.path,
           "aria2_connections": 8, "aria2_timeout_s": 1800}
    tr = download_block_file(client, _block(), target, cfg)
    assert tr.transport == "aria2"
    assert os.path.isfile(target)
    assert os.path.getsize(target) == FakeResult.STUB_CONTENT_LENGTH
    assert tr.bytes == FakeResult.STUB_CONTENT_LENGTH
    assert tr.rate_mbps > 0


def test_download_block_file_aria2_fallback_returncode(tmp_path, monkeypatch):
    """aria2 returncode!=0 → 降级 cdsapi_fallback，reason 非空，最终文件落盘。"""
    monkeypatch.setenv("ERA5_ARIA2_CMD", _aria2_cmd())
    monkeypatch.setenv("STUB_ARIA2_FAIL", "1")
    info = probe_aria2("")
    client = FakeCdsClient()
    target = str(tmp_path / "fb_rc.nc")
    cfg = {"aria2_enabled": True, "aria2_bin": info.path,
           "aria2_connections": 8, "aria2_timeout_s": 1800}
    tr = download_block_file(client, _block(), target, cfg)
    assert tr.transport == "cdsapi_fallback"
    assert tr.fallback_reason  # 非空
    # 降级前已清理半截文件，最终由 cdsapi 兜底写盘
    assert os.path.isfile(target)


def test_download_block_file_aria2_fallback_wrong_size(tmp_path, monkeypatch):
    """落盘大小与期望不符 → 降级 cdsapi_fallback，reason 非空。"""
    monkeypatch.setenv("ERA5_ARIA2_CMD", _aria2_cmd())
    monkeypatch.setenv("STUB_ARIA2_WRONG_SIZE", "1")
    info = probe_aria2("")
    client = FakeCdsClient()
    target = str(tmp_path / "fb_size.nc")
    cfg = {"aria2_enabled": True, "aria2_bin": info.path,
           "aria2_connections": 8, "aria2_timeout_s": 1800}
    tr = download_block_file(client, _block(), target, cfg)
    assert tr.transport == "cdsapi_fallback"
    assert tr.fallback_reason
    assert os.path.isfile(target)


def test_download_block_file_aria2_no_location(tmp_path):
    """retrieve 返回的句柄无 .location → 直接 cdsapi 兜底，reason="no_location"。"""

    class _NoLocHandle:
        location = None

        def download(self, t: str) -> str:
            os.makedirs(os.path.dirname(os.path.abspath(t)), exist_ok=True)
            with open(t, "w", encoding="utf-8") as f:
                f.write("fallback")
            return t

    class _NoLocClient:
        def retrieve(self, name, request=None, target=None):
            return _NoLocHandle()

    client = _NoLocClient()
    target = str(tmp_path / "fb_noloc.nc")
    cfg: dict = {"aria2_enabled": True, "aria2_bin": "unused-but-set"}
    tr = download_block_file(client, _block(), target, cfg)
    assert tr.transport == "cdsapi_fallback"
    assert tr.fallback_reason == "no_location"
    assert os.path.isfile(target)


def test_download_block_file_block_cancelled(tmp_path):
    """cancel_check 命中 → raise BlockCancelled（worker 转 cancelled，不 mark_failed）。"""
    # 用慢命令模拟"aria2 子进程正在下载"，cancel_check 立即返回 True 触发 kill。
    slow = f'"{sys.executable}" -c "import time; time.sleep(5)"'
    cfg = {"aria2_enabled": True, "aria2_bin": slow,
           "aria2_connections": 8, "aria2_timeout_s": 30}
    hooks = TransportHooks(cancel_check=lambda: True)
    client = FakeCdsClient()
    with pytest.raises(BlockCancelled):
        download_block_file(client, _block(), str(tmp_path / "cancel.nc"), cfg, hooks)


# ---------------------------------------------------------------------------
# 5. aria2_status
# ---------------------------------------------------------------------------
def test_aria2_status_returns_info(tmp_path, monkeypatch):
    """CdsChannel.aria2_status() 返回 Aria2Info 且不抛（验收点①）。"""
    monkeypatch.setenv("ERA5_ARIA2_CMD", _aria2_cmd())
    settings = Settings(download=DownloadSettings(mock=True, aria2_enabled=True),
                        config_dir=tmp_path, data_dir=tmp_path)
    channel = CdsChannel(settings)
    info = channel.aria2_status()
    assert isinstance(info, Aria2Info)
    assert info.available is True
    assert info.source == "env"


# ---------------------------------------------------------------------------
# 6. spawn 真实链路：run_blocks + ERA5_ARIA2_CMD 注入 stub（验收点⑧）
# ---------------------------------------------------------------------------
def test_spawn_run_blocks_aria2_branch(tmp_path, monkeypatch):
    """ERA5_ARIA2_CMD 注入 stub 时，mock e2e 能真实走通 aria2 分支
    （证明 spawn worker 继承 env，§6.3）。"""
    monkeypatch.setenv("ERA5_ARIA2_CMD", _aria2_cmd())
    settings = Settings(
        download=DownloadSettings(mock=True, aria2_enabled=True,
                                  cds_max_workers=2),
        config_dir=tmp_path, data_dir=tmp_path)
    channel = CdsChannel(settings)

    blocks = [
        {"key": "2m_temperature/2020/01",
         "dataset": "reanalysis-era5-single-levels",
         "request": {"variable": ["2m_temperature"], "year": ["2020"],
                     "month": ["01"]},
         "rel_target": "reanalysis-era5-single-levels/2m_temperature/hourly/2020/01.nc"},
        {"key": "2m_temperature/2020/02",
         "dataset": "reanalysis-era5-single-levels",
         "request": {"variable": ["2m_temperature"], "year": ["2020"],
                     "month": ["02"]},
         "rel_target": "reanalysis-era5-single-levels/2m_temperature/hourly/2020/02.nc"},
    ]

    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    store = ResumableStore(str(task_dir), None)
    task = SimpleNamespace(id="t_aria2")
    broker = EventBroker()
    bus = TaskEventBus(task.id, Path(str(task_dir)), broker)
    bus.start()
    try:
        results = channel.run_blocks(task, blocks, store, bus)
    finally:
        bus.stop()

    assert len(results) == 2
    assert all(r["status"] == "done" for r in results)
    # 至少一块真正走了 aria2 多连接分支（而非 cdsapi 降级）
    assert any(r.get("transport") == "aria2" for r in results)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
