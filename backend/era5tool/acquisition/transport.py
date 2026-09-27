# -*- coding: utf-8 -*-
"""传输策略编排（design-speedup-download.md §3.4）。

在 worker 进程内被 `_fetch_one_block` 调用，**无全局状态、进程安全**。
策略：aria2 优先（多连接加速），失败 / 拿不到 URL / 校验不过 → 无损降级
cdsapi（不消耗重试次数、不改变失败分类）。

异常语义（§3.4 / §4.2）：只有【阶段A】或【最终降级下载】抛出的异常才向上传播，
交给 `_fetch_one_block` 的 `is_retryable_error` + 指数退避处理；aria2 自身失败
一律吞掉转降级（如 returncode != 0 / 大小不符 / 取消之外的任何异常）——这是
"传输侧增强零风险回退"的根基：探测不到 aria2c 或任何异常都绝不比今天更糟。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple


class BlockCancelled(Exception):
    """aria2 子进程被取消 → 由 `_fetch_one_block` 转成 `{status: "cancelled"}`。

    不属于可重试异常：取消是用户/编排层意图，不应进入指数退避重试；worker 命中
    后直接返回 cancelled（不调用 mark_failed，与既有取消语义一致）。
    """


@dataclass
class TransportResult:
    """单块传输结果（§7-⑤）。

    transport: aria2 | cdsapi | cdsapi_fallback
      - aria2           → aria2 多连接下载成功（传输侧加速生效）
      - cdsapi          → 开关关闭 / 未探测到 aria2c → 走默认单连接下载
      - cdsapi_fallback → 开关开着但本块 aria2 失败/校验不过/拿不到 URL → 降级
    bytes / elapsed / rate_mbps：用于诊断与 benchmark 统计（可缺失，消费方用 .get）
    fallback_reason：降级原因（空字符串 = 无降级）
    """

    transport: str = "cdsapi"
    bytes: int = 0
    elapsed: float = 0.0
    rate_mbps: float = 0.0
    fallback_reason: str = ""


@dataclass
class TransportHooks:
    """worker → transport 的回调（§3.4）。

    cancel_check：读 cancel.flag，返回 True 表示块已取消（用于秒级 kill aria2 子进程）。
    on_log：emit_worker_event，便于在传输阶段也发进度日志（前端可忽略新字段）。
    """

    cancel_check: Optional[Callable[[], bool]] = None
    on_log: Optional[Callable[[Dict[str, Any]], None]] = None


def resolve_result_url(handle: Any) -> Tuple[Optional[str], Optional[int]]:
    """鸭子类型解析下载 URL 与期望字节数；**不 import cdsapi**（mock-only 环境可导入）。

    覆盖 4 类句柄（§3.4）：
    1) hasattr(handle, "location")       → (location, content_length)
                                          （cdsapi.Result / datastores.Results / FakeResult）
    2) hasattr(handle, "get_results")    → handle.get_results() 后递归解析
                                          （datastores.Remote，wait_until_complete=False）
    3) 其他（dict / None / 非对象）       → (None, None) → 走 cdsapi 路径
    任何属性访问/调用异常 → (None, None)（URL 解析失败绝不能拖垮下载，直接降级）。
    """
    try:
        if hasattr(handle, "location"):
            loc = getattr(handle, "location", None)
            if not loc:
                return (None, None)
            size = getattr(handle, "content_length", None)
            if isinstance(size, (int, float)) and not isinstance(size, bool):
                size = int(size)
            else:
                size = None
            return (str(loc), size)
        if hasattr(handle, "get_results"):
            return resolve_result_url(handle.get_results())
    except Exception:
        # 句柄访问异常（极少数 datastores 形态）→ 视为拿不到 URL，降级
        return (None, None)
    return (None, None)


def _cdsapi_download(client, block: Dict[str, Any], target: str) -> TransportResult:
    """cdsapi 单连接下载（默认/兜底路径，§3.4 分支 A）。

    保留既有写文件语义（真实 cdsapi 不建父目录，worker 已提前 ensure_dir）。
    返回 TransportResult(transport="cdsapi")；异常向上传播由调用方分类重试。
    """
    client.retrieve(block["dataset"], block["request"], target)
    sz = 0
    if os.path.isfile(target):
        try:
            sz = os.path.getsize(target)
        except OSError:
            sz = 0
    return TransportResult(transport="cdsapi", bytes=sz)


def _aria2_download(handle: Any, url: Optional[str], size: Optional[int],
                    block: Dict[str, Any], target: str,
                    cfg: Dict[str, Any],
                    hooks: Optional[TransportHooks]) -> TransportResult:
    """aria2 两阶段下载封装（§4.2）。

    - 成功（run.ok 且 verify_size 通过）→ 返回 transport="aria2"，含速率统计；
    - cancel_check 命中 → raise BlockCancelled（不降级、不 mark_failed）；
    - 其他（returncode != 0 / 大小不符 / 子进程异常）→ cleanup_partial 清场后
      返回 transport="cdsapi_fallback"，由调用方负责 cdsapi 兜底下载（不抛异常）。
    """
    from era5tool.acquisition.aria2 import aria2_download, cleanup_partial, verify_size

    cancel_check = hooks.cancel_check if hooks is not None else None
    run = aria2_download(
        url, target,
        bin_path=cfg["aria2_bin"],
        connections=int(cfg["aria2_connections"]),
        timeout_s=int(cfg["aria2_timeout_s"]),
        expected_size=size,
        cancel_check=cancel_check,
    )
    if run.cancelled:
        # 取消信号来自 cancel.flag（与 _fetch_one_block 顶部的取消检查同源）：
        # 不降级、不 mark_failed，直接向上抛，交由 worker 转 cancelled。
        raise BlockCancelled("aria2 download cancelled by cancel_check")
    if run.ok and verify_size(target, size):
        rate = (run.bytes / (1024.0 * 1024.0)) / run.elapsed if run.elapsed > 0 else 0.0
        return TransportResult(transport="aria2", bytes=run.bytes,
                               elapsed=run.elapsed, rate_mbps=round(rate, 3))
    # aria2 失败或校验不过：清理半截文件（含 target.aria2 控制文件），交给调用方
    # cdsapi 兜底下载（同一 job，不重排队、不耗配额、不消耗重试次数）。
    cleanup_partial(target)
    reason = (run.stderr_tail or f"aria2 rc={run.returncode}").strip()[:200]
    return TransportResult(transport="cdsapi_fallback", bytes=0,
                           fallback_reason=reason)


def download_block_file(client, block: Dict[str, Any], target: str,
                        cfg: Dict[str, Any],
                        hooks: Optional[TransportHooks] = None) -> TransportResult:
    """单块文件落地（worker 内调用，无全局状态，进程安全）。

    决策分支（§3.4 / §4.2）：
    A. not cfg["aria2_enabled"] or not cfg["aria2_bin"]
       → client.retrieve(dataset, request, target)          transport="cdsapi"
    B. 两阶段：
       handle = client.retrieve(dataset, request)            # 阶段A：等 CDS 服务端准备
       url, size = resolve_result_url(handle)
       url is None                → handle.download(target)  transport="cdsapi_fallback"
       run = aria2_download(...)  → 成功 → transport="aria2"
       其他                        → cleanup_partial(target); handle.download(target)
                                     transport="cdsapi_fallback" + fallback_reason

    异常语义：只有【阶段A】或【最终降级下载】抛出的异常才向上传播，交给
    `_fetch_one_block` 的 is_retryable_error + 指数退避处理；aria2 自身失败
    一律吞掉转降级（不消耗 retry 次数、不改变原有失败分类）。
    """
    # 路由门控：aria2 仅在开关打开且探测到二进制时启用（§7-④）
    if not (cfg.get("aria2_enabled") and cfg.get("aria2_bin")):
        return _cdsapi_download(client, block, target)

    # —— 分支 B：两阶段 ——
    try:
        # 阶段A：提交并等待 CDS 服务端准备（瓶颈，不可加速；不传 target 拿句柄）。
        # 抛异常（MARS 准备/排队/配额/阶段A 失败）→ 向上传播，走既有重试分类。
        handle = client.retrieve(block["dataset"], block["request"])
    except Exception:
        raise

    url, size = resolve_result_url(handle)
    if url is None:
        # 拿不到下载 URL（极少数 datastores 形态）→ 直接 cdsapi 兜底下载，
        # 同一 job、不重排队、不耗配额。
        try:
            handle.download(target)
        except Exception:
            raise
        sz = os.path.getsize(target) if os.path.isfile(target) else 0
        if hooks is not None and hooks.on_log is not None:
            hooks.on_log({"type": "log", "level": "warn",
                          "message": f"{block.get('key', '')} 无下载 URL，降级 cdsapi"})
        return TransportResult(transport="cdsapi_fallback", bytes=sz,
                               fallback_reason="no_location")

    # 有 URL → aria2 多连接下载（失败/校验不过在内部转 fallback 结果，不抛）
    tr = _aria2_download(handle, url, size, block, target, cfg, hooks)
    if tr.transport == "aria2":
        return tr

    # aria2 失败/校验不过 → 无损降级：同一 job 用 cdsapi 主连接再拉一次
    # （_aria2_download 已 cleanup_partial 清场，避免半截文件冒充产物被 mark_done）。
    reason = tr.fallback_reason or "aria2_failed"
    try:
        handle.download(target)
    except Exception:
        # 最终降级下载失败 → 向上传播，进入 is_retryable_error 分类（与阶段A 一致）
        raise
    sz = os.path.getsize(target) if os.path.isfile(target) else 0
    if hooks is not None and hooks.on_log is not None:
        hooks.on_log({"type": "log", "level": "warn",
                      "message": f"{block.get('key', '')} aria2 失败降级 cdsapi：{reason}"})
    return TransportResult(transport="cdsapi_fallback", bytes=sz,
                           fallback_reason=reason)
