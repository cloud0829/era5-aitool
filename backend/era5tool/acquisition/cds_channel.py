# -*- coding: utf-8 -*-
"""CDS 唯一数据通道（design-final.md §3.3/§4）。

- 多进程并发（默认 ≤4），每块在 worker 内串行重试（指数退避）。
- .done + manifest 断点续传；进度经 TaskEventBus → WS 实时推送。
- mock 模式使用本地 FakeCdsClient（无凭据/测试）；real 模式用官方 cdsapi。
"""
from __future__ import annotations

import os
import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from era5tool.acquisition.cds_request import build_cds_request
from era5tool.acquisition.mock_client import (ERROR_MODE_RETRYABLE_429,
                                             FakeCdsClient, NonRetryableError,
                                             RetryableError)
from era5tool.acquisition.transport import (BlockCancelled, TransportHooks,
                                            download_block_file)
from era5tool.config.settings import Settings
from era5tool.core.concurrency import compute_backoff
from era5tool.core.events import TaskEventBus, emit_worker_event, init_worker_queue
from era5tool.core.normalizer import CdsRequest
from era5tool.core.resumable import ResumableStore, ensure_dir
from era5tool.core.throttle import THROTTLE_FILENAME, ThrottleGate
from era5tool.models.task import Task

# 真实模式异常类型（cdsapi 可能未安装 → mock-only 环境也能导入本模块）。
# 注意：不同 cdsapi 版本异常类名/位置不一，统一用守卫导入；判定主要依赖
# .response.status_code 与异常文本，不依赖具体类。
try:  # pragma: no cover - 取决于安装的 cdsapi 版本
    from cdsapi.api import ClientError as _CdsClientError
except Exception:  # pragma: no cover
    _CdsClientError = ()

# 可重试 / 不可重试 HTTP 状态码（requests.HTTPError / cdsapi ClientError 通用）
_RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)
_NON_RETRYABLE_STATUS_CODES = (400, 401, 403, 404)

# ===========================================================================
# 错误分类（bugfix download-gaps）
# ---------------------------------------------------------------------------
# 分类结果写进块结果 dict 的 `error_category`，供编排层汇总、前端展示、
# 以及自适应限流判断"这是不是一次撞墙"。
# ===========================================================================
CATEGORY_QUEUE_LIMITED = "queue_limited"   # CDS 排队/并发请求超限（瞬时，须退避+降速）
CATEGORY_RATE_LIMIT = "rate_limit"         # 429 限速（瞬时）
CATEGORY_SERVER = "server_error"           # 5xx（瞬时）
CATEGORY_NETWORK = "network"               # 超时/连接重置（瞬时）
CATEGORY_TRANSIENT = "transient"           # 其他瞬时错误（保守归类）
CATEGORY_UNKNOWN = "unknown"               # 无法判定（保守按可重试处理）
CATEGORY_NO_DATA = "no_data"               # MarsNoData：该变量/时间在数据集无数据（永久）
CATEGORY_AUTH = "auth"                     # 401/403/许可未接受（永久）
CATEGORY_BAD_REQUEST = "bad_request"       # 400 且无瞬时特征（永久）
CATEGORY_NOT_FOUND = "not_found"           # 404（永久）
CATEGORY_LOCAL_IO = "local_io"             # 本地文件/目录错误（永久）

# 命中限流闸的类别：需要"全局降速"而不只是"本块退避"
THROTTLE_CATEGORIES = frozenset({CATEGORY_QUEUE_LIMITED, CATEGORY_RATE_LIMIT})
# 可重试类别（其余为永久性失败）
RETRYABLE_CATEGORIES = frozenset({
    CATEGORY_QUEUE_LIMITED, CATEGORY_RATE_LIMIT, CATEGORY_SERVER,
    CATEGORY_NETWORK, CATEGORY_TRANSIENT, CATEGORY_UNKNOWN,
})

# —— 判定顺序至关重要 ——
# 1) 瞬时文本优先于"400/401/403/404 不可重试"的状态码判定：CDS 队列限流就是
#    用 HTTP 400 返回的（线上证据：172/172 失败块全是
#    "The job has been rejected / Number queued requests ... temporarily limited"）。
# 2) 永久文本次之（保护 "not found" / "license" / "marsnodata" 等既有判定）。
# 3) 最后才回落到状态码与其他瞬时关键词；未知一律保守判为可重试。
_TRANSIENT_TEXT_HINTS = (
    "temporarily limited",                  # CDS: Number queued requests ... temporarily limited
    "number queued requests",
    "queued requests for this dataset",
    "queue is full",
    "has been rejected",                    # CDS: The job has been rejected
    "too many requests",
    "rate limit", "rate-limit", "ratelimit",
    "throttl",
    "try again later", "please retry",
)
_PERMANENT_TEXT_HINTS = (
    # MarsNoDataError：所选变量/时间/区域在 CDS-MARS 中无数据
    # （如 10米风速 10si 在月度均值数据集不提供）→ 永久性，重试无用。
    "marsnodata", "mars returned no data", "returned no data", "no data available",
    "unauthorized", "forbidden", "not found", "license",
    # 本地文件系统错误（cdsapi retrieve 写 target 时父目录不存在会抛
    # OSError [Errno 2]）：重试 3 次也解决不了，属永久性缺陷 → 直接暴露给用户。
    "no such file", "not exist", "errno 2", "[errno",
)
_OTHER_TRANSIENT_TEXT_HINTS = (
    "timed out", "timeout", "connection", "temporary", "temporarily",
    "service unavailable", "gateway", "bad gateway", "busy",
)

# 读 response.text 时的截断上限（异常文本可能带整套 HTML 错误页）
_RESP_TEXT_CAP = 2000


def _error_text(exc: BaseException) -> str:
    """拼出用于文本匹配的完整错误文本（异常 str + response.text），小写。

    真实 requests.HTTPError 的 `str(exc)` 已包含 response body（"The job has
    been rejected..."），但部分客户端只把正文留在 `.response.text` 里，故两者
    都取，去重无必要（只做子串匹配）。任何异常一律降级为空串。
    """
    parts: List[str] = []
    head = str(exc) or ""
    if head:
        parts.append(head)
    resp = getattr(exc, "response", None)
    if resp is not None:
        body = getattr(resp, "text", None)
        if isinstance(body, (bytes, bytearray)):
            try:
                body = bytes(body).decode("utf-8", "ignore")
            except Exception:  # pragma: no cover - 解码失败视为无正文
                body = ""
        if isinstance(body, str) and body:
            parts.append(body[:_RESP_TEXT_CAP])
    try:
        return "\n".join(parts).lower()
    except Exception:  # pragma: no cover
        return ""


def _status_code(exc: BaseException) -> Optional[int]:
    """从异常上鸭子类型取 HTTP 状态码（requests.HTTPError / cdsapi.ClientError）。"""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None) if resp is not None else None
    if status is None:
        status = getattr(exc, "code", None)      # mock 自有异常的 .code
        if not isinstance(status, int):
            return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def classify_error(exc: BaseException) -> str:
    """把下载异常归类为 CATEGORY_* 之一。

    判定顺序（详见 _TRANSIENT_TEXT_HINTS 上方注释）：
      1. mock 自有异常：RetryableError → transient；NonRetryableError → bad_request；
      2. 状态码 429/5xx → 瞬时（429 → rate_limit，其余 → server_error）；
      3. 文本命中限流/瞬时特征 → queue_limited / rate_limit / transient；
      4. 文本命中永久特征 → no_data / auth / not_found / local_io；
      5. 状态码 400/401/403/404 → bad_request / auth / not_found；
      6. 其他瞬时关键词 → network / transient；
      7. 未知 → unknown（保守，由调用方按可重试处理）。
    """
    # 1) mock 自有异常
    if isinstance(exc, RetryableError):
        return CATEGORY_RATE_LIMIT
    if isinstance(exc, NonRetryableError):
        return CATEGORY_BAD_REQUEST

    code = _status_code(exc)
    low = _error_text(exc)

    # 2) 明确瞬时状态码
    if code == 429:
        return CATEGORY_RATE_LIMIT
    if code in (500, 502, 503, 504):
        return CATEGORY_SERVER

    # 3) 文本瞬时特征（优先于 400 的"不可重试"判定——CDS 队列限流即 400）
    if any(h in low for h in _TRANSIENT_TEXT_HINTS):
        if code == 429 or "too many requests" in low or "rate limit" in low \
                or "rate-limit" in low or "ratelimit" in low:
            return CATEGORY_RATE_LIMIT
        return CATEGORY_QUEUE_LIMITED

    # 4) 文本永久特征
    if any(h in low for h in _PERMANENT_TEXT_HINTS):
        if any(h in low for h in ("marsnodata", "mars returned no data",
                                  "returned no data", "no data available")):
            return CATEGORY_NO_DATA
        if any(h in low for h in ("unauthorized", "forbidden", "license")):
            return CATEGORY_AUTH
        if any(h in low for h in ("no such file", "not exist", "errno 2",
                                  "[errno")):
            return CATEGORY_LOCAL_IO
        return CATEGORY_NOT_FOUND

    # 5) 状态码兜底（纯 400 且文本无瞬时特征 → 真·请求错误）
    if code == 400:
        return CATEGORY_BAD_REQUEST
    if code in (401, 403):
        return CATEGORY_AUTH
    if code == 404:
        return CATEGORY_NOT_FOUND

    # 6) 其他瞬时关键词
    if any(h in low for h in ("timed out", "timeout", "connection")):
        return CATEGORY_NETWORK
    if any(h in low for h in _OTHER_TRANSIENT_TEXT_HINTS):
        return CATEGORY_TRANSIENT

    # 7) 未知：保守
    return CATEGORY_UNKNOWN


def is_throttle_error(exc: BaseException) -> bool:
    """是否为"撞了 CDS 限流/队列墙"（命中后需全局降速，不止本块退避）。"""
    return classify_error(exc) in THROTTLE_CATEGORIES


def is_retryable_error(exc: BaseException) -> bool:
    """判定下载异常是否值得重试（保留旧签名，内部改由 classify_error 统一裁决）。

    分类规则（真实模式"零重试"根因修复，P0 + bugfix-download-gaps）：
    1. mock 自有异常：RetryableError → True；NonRetryableError → False。
    2. 带 HTTP 状态码（requests.HTTPError / cdsapi ClientError 均带 .response）：
       429/5xx → True（瞬时，可重试）。
    3. **文本瞬时特征（限流/队列/被拒）→ True，优先级高于 400 的不可重试判定**：
       CDS 队列限流用 HTTP 400 返回 "The job has been rejected / Number queued
       requests for this dataset is temporarily limited."，这是线上 172/172
       失败块的真实形态，按状态码会误判为永久失败。
    4. 文本永久特征（MarsNoData / 401 / 403 / 404 / license / 本地 IO）→ False。
    5. 状态码 400/401/403/404 且文本无瞬时特征 → False（真·请求错误）。
    6. 其他瞬时关键词（timeout / connection / gateway / busy）→ True。
    7. 其他未知异常 → 保守按 True 处理（宁可重试，避免瞬时故障误判为终态）。
    """
    return classify_error(exc) in RETRYABLE_CATEGORIES


def _friendly_error(exc: BaseException, block: Dict[str, Any]) -> str:
    """把 MarsNoData 类错误翻译成用户友好的中文诊断。

    返回非空字符串表示有友好的用户提示（将写入失败结果 dict 的 `user_message`
    字段）；返回空串表示无特殊提示，沿用原始 error 文本。
    """
    low = (str(exc) or "").lower()
    no_data_hits = ("no data", "marsnodata", "returned no data",
                    "mars returned no data")
    if not any(h in low for h in no_data_hits):
        return ""
    variable = block.get("variable", "未知")
    return (
        f"该变量「{variable}」在所选数据集/频率下 CDS 返回无数据（MarsNoDataError）。"
        f"常见原因：部分派生量（如 10米风速 10si、风速分量等）在月度均值数据集中不提供。"
        f"建议：① 改用逐小时(hourly)频率重试；② 或改选该 dataset 直接提供的分量变量"
        f"（如 10米U风分量 10u / 10米V风分量 10v）。"
    )


def _make_client(cfg: Dict[str, Any]):
    if cfg.get("mock"):
        return FakeCdsClient(delay=cfg.get("mock_delay", 0.02),
                             fail_rate=cfg.get("fail_rate", 0.0),
                             seed=cfg.get("seed"),
                             error_mode=cfg.get("mock_error_mode",
                                                ERROR_MODE_RETRYABLE_429))
    import cdsapi
    return cdsapi.Client()


def _make_throttle_gate(cfg: Dict[str, Any]) -> Optional[ThrottleGate]:
    """按 worker cfg 构造全局限流闸；未启用/缺路径 → None（完全不介入）。

    cfg 由主进程 `CdsChannel.worker_cfg()` 下发（spawn 子进程无法继承内存状态）。
    """
    if not cfg.get("throttle_enabled"):
        return None
    gate_path = cfg.get("throttle_file") or ""
    if not gate_path:
        return None
    return ThrottleGate(gate_path,
                        base_s=float(cfg.get("throttle_base_s", 30.0) or 0.0),
                        factor=float(cfg.get("throttle_factor", 2.0) or 1.0),
                        max_s=float(cfg.get("throttle_max_s", 600.0) or 0.0),
                        jitter_s=float(cfg.get("throttle_jitter_s", 0.0) or 0.0))


def _fetch_one_block(args: tuple) -> Dict[str, Any]:
    """worker：单块下载（块内串行重试，不进 pool 再排）。

    重试分类（is_retryable_error）：
    - 可重试（429/5xx/瞬时网络等）→ 指数退避后继续循环；
    - 不可重试（400/401/403/404/许可等）→ 立即失败，不 sleep 不重试。
    结果 dict 带 retried（attempts>1 才算真的重试过）与 error（失败原因）。
    """
    block, cfg, task_dir, cache_dir, task_id = args
    key = block["key"]
    client = _make_client(cfg)
    store = ResumableStore(task_dir, None)
    retry_max = int(cfg["retry_max"])
    attempts = 0
    last_error = ""
    friendly = ""
    last_category = CATEGORY_UNKNOWN
    # 全局自适应限流闸（跨进程共享）：撞墙后所有 worker 一起退，避免雪崩。
    gate = _make_throttle_gate(cfg)
    throttled = False
    # 提交抖动：削平 N 个 worker 同时 POST CDS 造成的 429 突刺（§3.4）。
    # mock 下 worker_cfg 已把 submit_stagger_s 置 0（避免测试被无意义 sleep 拖慢）。
    stagger = float(cfg.get("submit_stagger_s", 0.0) or 0.0)
    if stagger > 0:
        time.sleep(random.uniform(0, stagger))
    # 传输层回调：cancel_check 读 cancel.flag（与下方顶部取消检查同源，供 aria2 子进程
    # 秒级 kill）；on_log 复用 worker 事件发射（前端可忽略新增的 transport 字段）。
    cancel_flag = os.path.join(task_dir, "cancel.flag")
    hooks = TransportHooks(
        cancel_check=lambda: os.path.isfile(cancel_flag),
        on_log=lambda ev: emit_worker_event(ev),
    )
    emit_worker_event({"type": "start", "task_id": task_id, "phase": "downloading",
                       "block_key": key, "attempt": 1,
                       "message": f"开始下载 {key}"})
    for attempt in range(1, retry_max + 1):
        attempts = attempt
        # 取消检查（取消标记文件）
        if os.path.isfile(cancel_flag):
            emit_worker_event({"type": "log", "task_id": task_id, "level": "warn",
                               "block_key": key, "message": f"{key} 已取消"})
            return {"block": key, "status": "cancelled", "attempts": attempts}
        # 限流闸：任一 worker 撞过 CDS 队列/限速墙 → 这里集体等待放行后再提交，
        # 把"越失败越猛冲"改成"撞墙就一起退一步"（bugfix download-gaps）。
        if gate is not None:
            w = gate.wait()
            if w > 0:
                emit_worker_event({"type": "log", "task_id": task_id,
                                   "level": "info", "block_key": key,
                                   "attempt": attempt, "waited_s": w,
                                   "message": f"{key} 等待限流闸放行 {w:.1f}s"})
        target = os.path.join(cache_dir, block["rel_target"])
        # P0 根因修复：真实 cdsapi.Client.retrieve 在写 target 文件时【不会创建父目录】
        # （cdsapi 0.7.7 Result._download → open(target,"wb")），而 prepare_blocks 生成的
        # rel_target 是 dataset/var/freq/year/leaf 深层路径，父目录从未被创建 → 真实模式
        # 每块抛 FileNotFoundError([Errno 2] No such file...) → 被 is_retryable_error
        # 保守判为可重试 → 30/60/120s 无效退避 → "重试耗尽"。mock 的 FakeCdsClient.retrieve
        # 内部自带 ensure_dir，掩盖了该缺陷（mock 测试全绿）。这里在 retrieve 前显式建目录。
        # 该 ensure_dir 对 aria2 分支同样必要：aria2c 的 --dir 不会自动创建多级父目录。
        ensure_dir(os.path.dirname(os.path.abspath(target)))
        try:
            # 传输策略编排：aria2 优先、失败/无 URL/校验不过无损降级 cdsapi
            # （design-speedup-download.md §3.4 / §4.2）。重试/退避/取消/ensure_dir/
            # 进度事件逻辑全部保留，只替换"下载这一行"。
            tr = download_block_file(client, block, target, cfg, hooks)
            store.mark_done(key)
            emit_worker_event({"type": "log", "task_id": task_id, "level": "info",
                               "block_key": key, "transport": tr.transport,
                               "rate_mbps": tr.rate_mbps,
                               "message": f"{key} 下载完成（{tr.transport}）"})
            return {"block": key, "status": "done", "attempts": attempts,
                    "target": target, "transport": tr.transport,
                    "bytes": tr.bytes, "rate_mbps": tr.rate_mbps}
        except BlockCancelled:
            # 取消：不 mark_failed，转 cancelled（与顶部取消检查语义一致；不消耗重试次数）
            emit_worker_event({"type": "log", "task_id": task_id, "level": "warn",
                               "block_key": key, "message": f"{key} 已取消（aria2）"})
            return {"block": key, "status": "cancelled", "attempts": attempts}
        except Exception as exc:  # 真实 cdsapi/requests 异常与 mock 异常统一走分类
            last_error = str(exc) or type(exc).__name__
            last_category = classify_error(exc)
            # 用户友好诊断：MarsNoDataError 等给出中文提示（仅当非空才写入结果 dict，
            # 不影响既有行为——原始 last_error 仍保留供 is_retryable_error 判定与调试）。
            friendly = _friendly_error(exc, block)
            if last_category not in RETRYABLE_CATEGORIES:
                # 不可重试：立即失败（不 sleep 不重试）
                emit_worker_event({"type": "log", "task_id": task_id, "level": "error",
                                   "block_key": key, "attempt": attempt,
                                   "error_category": last_category,
                                   "message": f"{key} 不可重试，直接失败：{last_error}"})
                store.mark_failed(key, last_error[:200])
                result = {"block": key, "status": "failed", "attempts": attempts,
                          "retried": False, "error": last_error,
                          "error_category": last_category}
                if friendly:
                    result["user_message"] = friendly
                return result
            # —— 可重试（429/5xx/**CDS 队列限流 400**/网络瞬时）——
            throttled = last_category in THROTTLE_CATEGORIES
            if attempt < retry_max:
                wait = compute_backoff(attempt, cfg["backoff_base"],
                                       cfg["backoff_factor"], cfg["backoff_max"],
                                       cfg["backoff_jitter"])
                # 限流自适应：撞到 CDS 队列/限速墙 → 上全局闸并至少等一个冷却周期，
                # 冷却随连续命中指数增长（base → base*factor → ...，上限 max_s）。
                if throttled and gate is not None:
                    cooldown = gate.arm(attempt, reason=last_category)
                    wait = max(wait, cooldown)
                actual = wait if not cfg.get("mock") else min(wait * 0.001, 0.05)
                emit_worker_event({"type": "log", "task_id": task_id, "level": "info",
                                   "block_key": key, "attempt": attempt,
                                   "error_category": last_category,
                                   "throttled": throttled,
                                   "wait_nominal": round(wait, 3),
                                   "message": f"{key} 第{attempt}次失败"
                                              f"（{last_category}），等待 {wait:.1f}s 重试"})
                time.sleep(actual)
    # 重试耗尽（可重试错误一直失败）
    store.mark_failed(key, "BUSY_AFTER_RETRIES")
    emit_worker_event({"type": "log", "task_id": task_id, "level": "error",
                       "error_category": last_category, "throttled": throttled,
                       "block_key": key, "message": f"{key} 重试耗尽"})
    result = {"block": key, "status": "failed", "attempts": attempts,
              "retried": attempts > 1, "error": last_error,
              "error_category": last_category, "throttled": throttled}
    if friendly:
        result["user_message"] = friendly
    return result


def _arm_pool_drain_signal(pool: ProcessPoolExecutor,
                           settle_event: Optional[threading.Event]) -> None:
    """取消/池破裂路径在 `pool.shutdown(wait=False, cancel_futures=True)` **之前**调用。

    run_blocks 需快速返回（保证暂停 50ms 内响应）——但**不能**指望二次
    `pool.shutdown(wait=True)` 等待 worker：CPython 的 Executor.shutdown 置位
    `_shutdown_thread` 后，后续 shutdown(wait=True) 会立即返回而不真正等待正在执行
    长任务的 worker；且 shutdown(wait=False) 会清空 pool._processes。因此必须在
    shutdown **前**捕获 worker 进程句柄，起守护线程轮询其存活；当全部 worker 真正
    退出（在跑块下载完成后自行结束）后 set(settle_event)，供编排层 resume 等待
    上一会话收尾（避免新旧会话并发下载同一块 → 重复请求 / 并发写同一 .nc）。

    真实 cdsapi.retrieve 阻塞分钟级不可中断，故暂停后旧 worker 仍会跑完当前块：
    本机制让 resume 在这些旧 worker 全部退出后才重建 pending 并发起新一轮下载，
    此时旧块已写 .done → 被跳过 → 不重复下载。
    """
    if settle_event is None:
        return
    procs: List[Any] = []
    try:
        procs = [p for p in (getattr(pool, "_processes", None) or {}).values()]
    except Exception:  # pragma: no cover - 防御私有属性形态差异
        procs = []
    if not procs:
        settle_event.set()
        return
    threading.Thread(target=_wait_processes_exit, args=(procs, settle_event),
                     daemon=True, name="pool-drain-watch").start()


# 缓存路径里不允许出现的字符（Windows 非法文件名字符 + 路径分隔符 + 通配符 +
# 控制字符）。变量/年份等若含这些字符，会被直接拼进 rel_target → 在磁盘上生成
# 名为 `*` 之类的诡异目录（用户报告现象之一）→ 这里在构造路径前就拦下。
_ILLEGAL_PATH_CHARS = frozenset('\\/:*?"<>|') | {chr(i) for i in range(32)}


def _assert_safe_path_token(value: Any, what: str) -> str:
    """校验并返回一个可安全用作缓存路径分量的字符串。

    合法：非空、非 "." / ".."、不含路径分隔符与 Windows 非法文件名字符。
    非法 → raise ValueError（由编排层转 ERR_PARAM 给用户，绝不落盘垃圾目录）。
    """
    s = str(value)
    if not s or s in (".", ".."):
        raise ValueError(f"非法{what}（不能用于缓存路径）: {value!r}")
    bad = sorted({c for c in s if c in _ILLEGAL_PATH_CHARS})
    if bad:
        raise ValueError(
            f"非法{what}（含非法路径字符 {' '.join(repr(c) for c in bad)}）: {value!r}")
    return s


def _wait_processes_exit(processes: List[Any],
                         settle_event: threading.Event) -> None:
    """轮询 worker 进程直至全部退出，随后置位 settle_event（守护线程）。"""
    try:
        for p in processes:
            try:
                while p.is_alive():
                    time.sleep(0.1)
            except Exception:  # pragma: no cover - 进程对象异常视为已结束
                break
    finally:
        try:
            settle_event.set()
        except Exception:  # pragma: no cover
            pass


class CdsChannel:
    """唯一数据通道。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.max_workers = settings.download.cds_max_workers
        self.cache_dir = settings.cache_dir

    @property
    def mock(self) -> bool:
        """动态读取（允许运行时切换 mock/real）。"""
        return bool(self.settings.download.mock)

    # ------------------------------------------------------------------
    def has_cds_credentials(self) -> bool:
        import os
        home = os.path.expanduser("~")
        return os.path.isfile(os.path.join(home, ".cdsapirc"))

    def prepare_blocks(self, cds_req: CdsRequest) -> List[Dict[str, Any]]:
        """为每个块生成最终 CDS 请求与缓存相对路径。

        切块粒度支持 day / month / monthly（design-speedup-download.md §3.4）：
        - day 块（含 day 字段）→ build_cds_request(..., day=b["day"])，
          rel_target 加 "/{dd}"：dataset/var/hourly/{year}/{mm}/{dd}.nc
        - month 块（month 有值、day 为 None）→ 整月请求，rel = .../{mm}.nc
        - monthly 家族 → rel = .../{year}.nc
        """
        out: List[Dict[str, Any]] = []
        monthly = cds_req.family in ("land-monthly", "era5-monthly")
        freq = "monthly" if monthly else "hourly"
        for b in cds_req.blocks:
            # 每块只请求自己的变量（variables=[b["variable"]]）：修复多变量任务
            # 每块都下载 schema.variables 全部变量 → 下载量/配额/磁盘翻倍的缺陷。
            var = _assert_safe_path_token(b["variable"], "变量名")
            year = _assert_safe_path_token(b["year"], "年份")
            day = b.get("day")
            if day is not None:
                req = build_cds_request(cds_req.schema, int(year), b.get("month"),
                                        variables=[var], day=day)
                leaf = f"{b['month']:02d}/{day:02d}.nc"
            else:
                req = build_cds_request(cds_req.schema, int(year), b.get("month"),
                                        variables=[var])
                leaf = f"{b['month']:02d}.nc" if b.get("month") else f"{year}.nc"
            # 用正斜杠拼接缓存相对路径（与 §7-② 约定、task_store._delete_cache_files
            # 的 Path(dataset)/var/freq/year 结构保持一致；Windows 下 os.path.join
            # 会产出反斜杠，与既有断言/跨平台路径约定不符。
            rel = "/".join([cds_req.dataset, var, freq, str(year), leaf])
            out.append({**b, "request": req, "rel_target": rel,
                        "dataset": cds_req.dataset, "freq": freq})
        return out

    def worker_cfg(self, fail_rate: float = 0.0) -> Dict[str, Any]:
        d = self.settings.download
        # aria2 主进程探测一次：把结果与配置一起下发 worker（§7-④），
        # worker 内不再重新 which（365 块 × which 是浪费，且结果必须一致）。
        # 探测不到 → aria2_bin="" → 门控关闭 → 走默认 cdsapi 单连接下载（与今天一致）。
        from era5tool.acquisition.aria2 import Aria2Info, probe_aria2
        info: Aria2Info = probe_aria2(d.aria2_path)
        aria2_ok = bool(d.aria2_enabled) and info.available
        return {
            "mock": self.mock,
            "mock_delay": 0.02,
            "fail_rate": fail_rate,
            "seed": 7,
            "retry_max": d.retry_max,
            "backoff_base": d.backoff_base,
            "backoff_factor": d.backoff_factor,
            "backoff_max": d.backoff_max,
            "backoff_jitter": d.backoff_jitter,
            # —— 下载加速：传输后端（design-speedup-download.md §3.4 / §7-④）——
            "aria2_enabled": aria2_ok,
            "aria2_bin": info.path if aria2_ok else "",
            "aria2_connections": d.aria2_connections,
            "aria2_timeout_s": d.aria2_timeout_s,
            "mock_error_mode": d.mock_error_mode,
            # 提交抖动：mock 下置 0（避免测试被无意义 sleep 拖慢，§3.4）
            "submit_stagger_s": 0.0 if self.mock else d.submit_stagger_s,
            # —— 自适应限流闸（bugfix download-gaps）——
            # 闸文件放在 data_dir（而非 task_dir）：CDS 的"排队请求数"上限是
            # **按数据集/账号**计的，多个任务并发跑时必须共享同一个闸才有效。
            # mock 模式下不上闸（无真实队列，与 submit_stagger_s 置 0 同理，
            # 避免测试被 30s 级冷却拖慢）。
            "throttle_enabled": bool(d.throttle_enabled) and not self.mock,
            "throttle_file": str(Path(self.settings.data_dir) / THROTTLE_FILENAME),
            "throttle_base_s": d.throttle_base_s,
            "throttle_factor": d.throttle_factor,
            "throttle_max_s": d.throttle_max_s,
            "throttle_jitter_s": d.throttle_jitter_s,
        }

    def _build_worker_cfg(self, fail_rate: float = 0.0) -> Dict[str, Any]:
        """取 worker 配置。

        兼容两种 worker_cfg 签名（保守原则：不得破坏既有测试/脚本的 monkeypatch）：
        - 新版：`worker_cfg(self, fail_rate=...)`（本项目默认实现）——直接透传；
        - 旧版：`worker_cfg(self)`——历史测试大量用 `def _cfg(self)` 整体替换
          `CdsChannel.worker_cfg` 以注入自有的 mock_delay/fail_rate，不接受关键字
          参数。此时回退为无参调用，并**沿用其自带 fail_rate**（不覆写），
          否则会把这些测试精心注入的失败率冲成 0 → 断言失效。
        """
        try:
            cfg = self.worker_cfg(fail_rate=fail_rate)
        except TypeError:
            cfg = self.worker_cfg()
        return dict(cfg or {})

    def aria2_status(self) -> "Aria2Info":
        """探测结果（供 /api/config 与诊断脚本展示），不抛异常（§3.4）。"""
        from era5tool.acquisition.aria2 import probe_aria2
        return probe_aria2(self.settings.download.aria2_path)


    # ------------------------------------------------------------------
    def run_blocks(self, task: Task, blocks: List[Dict[str, Any]],
                   store: ResumableStore, bus: TaskEventBus,
                   cancel_event=None,
                   on_block_done: Optional[Callable[[Dict[str, Any], int, int], None]] = None,
                   settle_event: Optional[threading.Event] = None,
                   fail_rate: float = 0.0) -> List[Dict[str, Any]]:
        """并行执行块列表；返回每块结果。cancel_event 置位时停止提交新块。

        settle_event（可选）：指示本会话进程池【真正退出】的事件。
        - 正常结束/失败：本方法在 finally 中对所有 worker shutdown(wait=True) 完成后
          同步 set(settle_event)。
        - 取消/池破裂路径：run_blocks 快速返回（保证暂停即时响应），但取消时在跑的
          旧 worker 无法被 kill（真实 cdsapi retrieve 阻塞分钟级）；_arm_pool_drain_signal
          捕获 worker 进程句柄并起守护线程轮询，待其全部退出后 set(settle_event)。
        resume 时编排层等待该事件，确保上一会话旧 worker 不再写 .nc / mark_done 后，
        才重建 pending（旧块已 .done → 被跳过）并发起新一轮下载——从根上消除
        “暂停→立即 resume 新旧两会话并发下载同一块”的重复下载/并发写文件冲突。

        on_block_done(result, completed, total)：每个块（含 failed/cancelled，
        不含 break 提前退出路径）完成后调用一次，供编排层实时更新进度/落盘。
        不传该参数时行为与历史版本完全一致。

        流式分批提交（streaming submit）：不再一次性把全部块 submit 进进程池，
        而是维护 `remaining`（待提交）与 `running`（future→key）两组状态，循环里
        **在等待任何 future 完成之前就先检查 cancel_event**——这是修复"点击暂停却
        看起来卡住"的核心：旧实现用 as_completed 全量收集，取消检查只在第一个
        future 完成后才执行，长块下载期间完全感知不到 cancel_event。
        """
        if not blocks:
            return []
        results: List[Dict[str, Any]] = []
        task_dir = str(store.task_dir)
        cache_dir = str(self.cache_dir)
        cfg = self._build_worker_cfg(fail_rate)
        total = len(blocks)
        # —— 自适应并发（bugfix download-gaps）——
        # 起始并发 = 配置值；每当有块因"撞限流/队列墙"失败，就把在跑并发下调 1
        # （下限 adaptive_min_workers）；连续 adaptive_recover_every 个块成功则
        # 回升 1。与 worker 内的全局限流闸互补：闸负责"退避多久"，这里负责
        # "同时最多几个在冲"，双管齐下把雪崩压住。
        adaptive = bool(getattr(self.settings.download, "adaptive_concurrency", True))
        min_workers = max(1, min(int(
            getattr(self.settings.download, "adaptive_min_workers", 1) or 1),
            self.max_workers))
        recover_every = max(1, int(
            getattr(self.settings.download, "adaptive_recover_every", 6) or 6))
        eff_workers = self.max_workers
        ok_streak = 0

        # 不用 `with`：取消路径需要 shutdown(wait=False) 尽快返回，而 with 退出会
        # 隐式 shutdown(wait=True) 阻塞等待正在退避 sleep / 长时间 cdsapi retrieve
        # 的 worker（最长可达数分钟）→ 取消卡死（根因 B 加固，详见下方注释）。
        pool = ProcessPoolExecutor(max_workers=self.max_workers,
                                   initializer=init_worker_queue,
                                   initargs=(bus.queue,))
        cancelled = False
        try:
            remaining = list(blocks)          # 待提交块（按原序）
            running: Dict[Any, str] = {}       # future → block key
            processed: set = set()             # 已 collect 的 key（防重复计数）
            completed = 0

            while True:
                # —— 关键修复点：在等待任何 future 之前就检查取消 ——
                if cancel_event is not None and cancel_event.is_set():
                    # 取消收尾：保证 results 完整性（N4：paused_blocks 计数精确）
                    # 1) 对 running 中尚未真正执行的 future 调 cancel()（已处于执行中
                    #    的会返回 False，由 worker 自行跑完并 mark_done，其结果不进
                    #    results，但 ResumableStore.pending_blocks 会据 .done 跳过，
                    #    故 resume 不会重复下载）。
                    for f in list(running.keys()):
                        f.cancel()
                    # 2) 所有"未被 collect 的块"（remaining 未提交 + running 未 collect）
                    #    显式加入 results 并标 cancelled（attempts=0），使编排层
                    #    cancelled 列表在取消场景下非空（≥1）。
                    for b in remaining:
                        if b["key"] not in processed:
                            results.append({"block": b["key"],
                                            "status": "cancelled", "attempts": 0})
                            processed.add(b["key"])
                    for k in running.values():
                        if k not in processed:
                            results.append({"block": k,
                                            "status": "cancelled", "attempts": 0})
                            processed.add(k)
                    # 根因 B 加固：立即释放未运行任务，不等待正在退避 sleep 或
                    # 阻塞在 cdsapi retrieve（排队/下载轮询）里的 worker。
                    # shutdown 幂等：finally 中 cancelled=True 时不再二次
                    # shutdown(wait=True)；worker 会在当前工作结束后读到队列哨兵
                    # 自行退出（cancel.flag 在每轮 attempt 开头也会被检查）。
                    cancelled = True
                    # Bug 修复：旧 worker（在跑块，真实 retrieve 不可中断）仍会继续
                    # 写 .nc + mark_done。先捕获 worker 进程句柄再 shutdown（shutdown
                    # 会清空 pool._processes），由守护线程轮询到它们全部退出后置位
                    # settle_event；resume 会等它，避免新旧会话并发下载同一块。
                    _arm_pool_drain_signal(pool, settle_event)
                    pool.shutdown(wait=False, cancel_futures=True)
                    break

                # 补满在跑槽位（流式提交：只在 len(running) < 当前生效并发时提交新块）
                while len(running) < eff_workers and remaining:
                    b = remaining.pop(0)
                    f = pool.submit(_fetch_one_block,
                                    (b, cfg, task_dir, cache_dir, task.id))
                    running[f] = b["key"]

                # 全部完成（无待提交、无在跑）→ 正常结束
                if not running and not remaining:
                    break

                # 等待任意一个在跑块完成（用短超时轮询，确保 cancel_event 在两次
                # 完成之间也能被及时感知，而不是阻塞到下一完成才检查）。
                try:
                    done_futs, _ = wait(running.keys(), timeout=0.05,
                                        return_when=FIRST_COMPLETED)
                except (BrokenProcessPool, TimeoutError) as exc:
                    # 进程池破裂：running 中其余未完成块 + remaining 全部标
                    # failed(WORKER_POOL_BROKEN)；不在回调里重发，由编排层末尾汇总兜底。
                    err = (str(exc) or type(exc).__name__)[:200]
                    for f, k in list(running.items()):
                        if k not in processed:
                            results.append({"block": k, "status": "failed",
                                            "attempts": 1, "retried": False,
                                            "error": "WORKER_POOL_BROKEN"})
                            processed.add(k)
                    for b in remaining:
                        if b["key"] not in processed:
                            results.append({"block": b["key"], "status": "failed",
                                            "attempts": 1, "retried": False,
                                            "error": "WORKER_POOL_BROKEN"})
                            processed.add(b["key"])
                    cancelled = True
                    _arm_pool_drain_signal(pool, settle_event)
                    pool.shutdown(wait=False, cancel_futures=True)
                    break

                # 收集本轮完成的 future
                for f in done_futs:
                    key = running.pop(f)
                    if key in processed:
                        continue
                    processed.add(key)
                    try:
                        r = f.result()
                    except BrokenProcessPool as exc:
                        # 根因 B：worker 进程被系统/父进程终止。该 future 与剩余全部
                        # future 均已不可恢复 → 直接批量标记 failed，避免反复抛异常。
                        err = (str(exc) or type(exc).__name__)[:200]
                        r = {"block": key, "status": "failed", "attempts": 1,
                             "retried": False, "error": err}
                        results.append(r)
                        completed += 1
                        for f2, k2 in list(running.items()):
                            if k2 not in processed:
                                results.append({"block": k2, "status": "failed",
                                                "attempts": 1, "retried": False,
                                                "error": "WORKER_POOL_BROKEN"})
                                processed.add(k2)
                        for b in remaining:
                            if b["key"] not in processed:
                                results.append({"block": b["key"], "status": "failed",
                                                "attempts": 1, "retried": False,
                                                "error": "WORKER_POOL_BROKEN"})
                                processed.add(b["key"])
                        # 当前崩溃 future 照常走一次进度/回调（与历史兜底路径一致）；
                        # 批量标记的剩余块不进回调（等同取消路径，由编排层末尾汇总兜底）。
                        progress = completed / max(total, 1)
                        bus.emit({"type": "progress", "phase": "downloading",
                                  "block_key": key, "block_index": completed,
                                  "block_total": total, "progress": round(progress, 4),
                                  "status": "running",
                                  "message": f"块 {completed}/{total} 完成"})
                        if on_block_done is not None:
                            on_block_done(r, completed, total)
                        cancelled = True
                        _arm_pool_drain_signal(pool, settle_event)
                        pool.shutdown(wait=False, cancel_futures=True)
                        break
                    except Exception as exc:  # worker 崩溃兜底（文案截断，防事件/DB 撑爆）
                        err = (str(exc) or type(exc).__name__)[:200]
                        r = {"block": key, "status": "failed", "attempts": 1,
                             "retried": False, "error": err}
                    results.append(r)
                    completed += 1
                    # —— 自适应并发：撞墙降速 / 连续成功回升 ——
                    if adaptive:
                        if r.get("throttled"):
                            ok_streak = 0
                            if eff_workers > min_workers:
                                eff_workers -= 1
                                bus.emit({"type": "log", "task_id": task.id,
                                          "level": "info",
                                          "message": f"CDS 限流，自适应降速：并发 "
                                                     f"{eff_workers + 1} → {eff_workers}"})
                        elif r.get("status") == "done":
                            ok_streak += 1
                            if ok_streak >= recover_every and eff_workers < self.max_workers:
                                eff_workers += 1
                                ok_streak = 0
                    progress = completed / max(total, 1)
                    bus.emit({"type": "progress", "phase": "downloading",
                              "block_key": key, "block_index": completed,
                              "block_total": total, "progress": round(progress, 4),
                              "status": "running",
                              "message": f"块 {completed}/{total} 完成"})
                    if on_block_done is not None:
                        on_block_done(r, completed, total)
        finally:
            if not cancelled:
                # 正常结束（含 pool 破裂后 break）：等待所有 worker 退出。
                pool.shutdown(wait=True)
                # 正常路径：worker 已全部退出 → 立即置位 settle（resume 可放行）。
                # 取消/破裂路径：settle 由 _arm_pool_drain_signal 起的守护线程在旧
                # worker 全部退出后置位（run_blocks 已提前 return，不能再这里 set，
                # 否则 resume 会误以为旧 worker 已收尾而并发下载同一块）。
                if settle_event is not None:
                    settle_event.set()
            # 取消路径：已在 break 前 shutdown(wait=False, cancel_futures=True)，
            # 不再二次阻塞等待（shutdown 幂等，此处跳过可避免取消卡死）。
        return results
