#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDS 下载「速度 / 失败原因」一键诊断脚本（用户要求，独立运行）。

用途：
- 检查 ~/.cdsapirc 是否存在/格式正确（UID:KEY 整串）。
- 用真实凭据提交一个【极小请求】（1 变量 × 1 月 × 前 3 天 × 1 个 time 步，
  area=[30,110,25,115]），测量：提交耗时、排队→运行→完成各阶段耗时、
  下载速率（MB/s）、最终状态。
- 失败时打印完整异常（类型/str/status_code/response 前 500 字符），并按
  is_retryable_error 逻辑给出「是否可重试」结论（与后端下载通道同一判定函数）。
- 结尾输出诊断结论表（配置 OK / 凭据 OK / 网络延迟 / CDS 状态 / 预计 24 块总耗时）。

纯标准库 + cdsapi + 本项目 is_retryable_error，不依赖测试框架。

用法：
    python scripts/diagnose_download.py            # 真实网络探测（需已配 .cdsapirc）
    python scripts/diagnose_download.py --mock     # 离线 FakeCdsClient，验证脚本自身
    python scripts/diagnose_download.py --dataset reanalysis-era5-land   # 换数据集

    # —— 下载加速基准（design-speedup-download.md §3.6 / T04）——
    # 离线跑并发×粒度矩阵，快速定位最优 workers/granularity（复用生产调度器）：
    python scripts/diagnose_download.py --mock --benchmark \
        --workers 2,4,6,8 --granularity day,month --blocks 6
    # 仅探测 aria2c 可用性与版本（未安装给出 Windows/conda/apt 安装提示）：
    python scripts/diagnose_download.py --probe-aria2
    # 机器可读输出（CI 归档 / 对比）：
    python scripts/diagnose_download.py --mock --benchmark --csv bench.csv --json
    # 真实模式先打印配额估算并要求确认（防误刷配额），--yes 跳过确认：
    python scripts/diagnose_download.py --benchmark --workers 2,4,6 --granularity day,month --blocks 6 --yes
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 使 `from era5tool.acquisition.cds_channel import is_retryable_error` 可用
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from era5tool.acquisition.cds_channel import CdsChannel, is_retryable_error  # noqa: E402
from era5tool.acquisition.mock_client import FakeCdsClient  # noqa: E402
# 基准模式复用生产链路（design-speedup-download.md §3.6）：
# Normalizer 切块 + CdsChannel.prepare_blocks 造块 + CdsChannel.run_blocks 真实调度。
from era5tool.acquisition.aria2 import probe_aria2  # noqa: E402
from era5tool.config.schema import Area, RequestSchema, Timerange  # noqa: E402
from era5tool.config.settings import DownloadSettings, Settings  # noqa: E402
from era5tool.core.events import EventBroker, TaskEventBus  # noqa: E402
from era5tool.core.normalizer import Normalizer  # noqa: E402
from era5tool.core.resumable import ResumableStore  # noqa: E402
from era5tool.models.task import BlockStats, Task, TaskType  # noqa: E402

DATASET = "reanalysis-era5-single-levels"

# 极小请求：1 变量 × 2025-01 × 前 3 天 × 1 个 time 步 × 5°×5° area。
# 单文件仅数 KB～几十 KB，足以探测「凭据/网络/队列/CDS 服务」链路，不消耗大配额。
TINY_REQUEST: Dict[str, Any] = {
    "product_type": ["reanalysis"],
    "variable": ["2m_temperature"],
    "year": ["2025"],
    "month": ["01"],
    "day": ["01", "02", "03"],
    "time": ["00:00"],
    "area": [30, 110, 25, 115],  # [north, west, south, east]
    "data_format": "netcdf",  # CDS 新后端已弃用 "format"，改用 "data_format"
    "target": "tiny.nc",
}

RC_PATH = os.path.join(os.path.expanduser("~"), ".cdsapirc")


# ---------------------------------------------------------------------------
# 1. .cdsapirc 检查
# ---------------------------------------------------------------------------
def check_rc() -> Tuple[bool, str]:
    """检查 ~/.cdsapirc：存在性 + url/key 格式。

    cdsapi 0.7.7 支持两种凭据（Client.__new__ 按 key 是否含冒号自动选择）：
    - key 含冒号 → UID:APIKEY（旧式 basic auth）→ cdsapi.api.Client；
    - key 无冒号 → 新 CDS v2 纯 token → ecmwf.datastores LegacyClient。
    两者都合法；仅当 url/key 缺失或 key 为空时判失败。
    """
    if not os.path.isfile(RC_PATH):
        return False, f"缺失: {RC_PATH}（请参照 https://cds.climate.copernicus.eu 创建）"
    try:
        with open(RC_PATH, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as exc:
        return False, f"读取失败: {exc}"
    cfg: Dict[str, str] = {}
    for line in lines:
        if ":" in line:
            k, v = line.strip().split(":", 1)
            cfg[k.strip()] = v.strip()
    url = cfg.get("url", "")
    key = cfg.get("key", "")
    if not url or not key:
        return False, (f"格式不完整: 需要 url 与 key 两行（当前 url={'有' if url else '无'}, "
                       f"key={'有' if key else '无'}）")
    if ":" in key:
        uid = key.split(":", 1)[0]
        masked = f"{uid}:****(len={len(key)})"
        mode = "旧式 UID:APIKEY → cdsapi.Client"
    else:
        masked = f"token****(len={len(key)})"
        mode = "新 CDS v2 token → LegacyClient（自动）"
    return True, (f"OK\n    {RC_PATH}\n    url={url}\n    key={masked}\n"
                  f"    凭据模式: {mode}")


# ---------------------------------------------------------------------------
# 2. 真实极小请求探测
# ---------------------------------------------------------------------------
# cdsapi 0.7.7 按 .cdsapirc 的 key 是否含冒号返回两类客户端（Client.__new__）：
# - key 含冒号（UID:APIKEY 旧式）→ cdsapi.api.Result（有 .reply/.update()）；
# - key 无冒号（新 CDS v2 纯 token）→ ecmwf.datastores.Remote（有 .status/.json，
#   .status 每次访问都会 GET 刷新）。两者 retrieve(name, request, target) 签名一致，
#   download(target) 均【不创建父目录】（P0 根因 A 在两路径都成立）。
def _is_cdsapi_result(obj: Any) -> bool:
    """True → cdsapi Result；False → datastores Remote。"""
    return hasattr(obj, "reply")


def _get_state(obj: Any) -> str:
    """统一取任务状态（queued/running/completed/failed）。"""
    if _is_cdsapi_result(obj):
        return str(obj.reply.get("state", ""))
    try:
        return str(obj.status)
    except Exception:
        return ""


def _refresh(obj: Any) -> None:
    """刷新状态；cdsapi Result 需显式 update()，Remote 的 .status 已自带 GET。"""
    if _is_cdsapi_result(obj):
        try:
            obj.update()
        except Exception:
            pass


def _request_id(obj: Any) -> str:
    if _is_cdsapi_result(obj):
        return str(obj.reply.get("request_id", ""))
    return str(getattr(obj, "request_id", ""))


def probe_real(dataset: str) -> Dict[str, Any]:
    """真实网络探测：提交极小请求，记录各阶段耗时与下载速率。"""
    import cdsapi

    rows: List[Tuple[str, float]] = []  # (阶段, 耗时秒)

    # wait_until_complete=False → 提交后立即返回句柄，便于记录状态机阶段；
    # retry_max/sleep_max 调小：诊断脚本要快速暴露问题，不等 cdsapi 内部 500 次重试。
    client = cdsapi.Client(quiet=True, wait_until_complete=False,
                           retry_max=2, sleep_max=5, timeout=30)
    t_submit = time.time()
    result = client.retrieve(dataset, dict(TINY_REQUEST))
    t_after_submit = time.time()
    rows.append(("提交(POST)", t_after_submit - t_submit))

    # 状态轮询：记录每次状态变迁的时刻
    states: List[Tuple[str, float]] = []
    prev = None
    deadline = time.time() + 1800  # 30 分钟兜底，防 CDS 长时间排队挂死脚本
    while True:
        state = _get_state(result)
        if state and state != prev:
            states.append((state, time.time()))
            prev = state
        if state == "completed":
            break
        if state == "failed":
            raise RuntimeError(f"CDS 任务失败: {_request_id(result)}")
        if time.time() > deadline:
            raise TimeoutError("CDS 排队/运行超过 30 分钟，已中止（可稍后重试）")
        time.sleep(1)
        _refresh(result)

    t_completed = time.time()
    rows.append(("排队→运行", _stage_delta(states, ("queued", "running"))))
    rows.append(("运行→完成", _stage_delta(states, ("running", "completed"))))
    rows.append(("全程(提交→完成)", t_completed - t_submit))

    # 下载并测速（文件大小以落盘实测为准，兼容两类客户端）
    dl_elapsed = 0.0
    size = 0
    target_path: Optional[str] = None
    with tempfile.TemporaryDirectory(prefix="cds_diag_") as td:
        target_path = os.path.join(td, "tiny.nc")
        t0 = time.time()
        result.download(target_path)
        dl_elapsed = time.time() - t0
        size = os.path.getsize(target_path)
    rows.append(("下载", dl_elapsed))

    rate_mbps = (size / 1024.0 / 1024.0 / dl_elapsed) if (size > 0 and dl_elapsed > 0) else 0.0
    return {
        "ok": True,
        "request_id": _request_id(result),
        "size_bytes": size,
        "download_elapsed": dl_elapsed,
        "rate_mbps": rate_mbps,
        "stages": rows,
        "states": states,
        "target": target_path,
    }


def _stage_delta(states: List[Tuple[str, float]], pair: Tuple[str, str]) -> float:
    """两个状态名之间经过的秒数；找不到返回 -1.0。"""
    t_start = t_end = None
    for name, ts in states:
        if name == pair[0] and t_start is None:
            t_start = ts
        if name == pair[1]:
            t_end = ts
    if t_start is None or t_end is None:
        return -1.0
    return max(t_end - t_start, 0.0)


# ---------------------------------------------------------------------------
# 3. mock 模式（验证脚本自身，无网络）
# ---------------------------------------------------------------------------
def probe_mock(dataset: str) -> Dict[str, Any]:
    """离线验证脚本自身：FakeCdsClient 走一遍流程（无阶段状态机）。"""
    client = FakeCdsClient(delay=0.05)
    with tempfile.TemporaryDirectory(prefix="cds_diag_mock_") as td:
        target = os.path.join(td, "tiny.nc")
        t0 = time.time()
        client.retrieve(dataset, dict(TINY_REQUEST), target)
        elapsed = time.time() - t0
        size = os.path.getsize(target)
    return {
        "ok": True,
        "request_id": "mock",
        "size_bytes": size,
        "download_elapsed": elapsed,
        "rate_mbps": 0.0,
        "stages": [("mock 总耗时(FakeCdsClient)", elapsed)],
        "states": [("mock", t0)],
        "target": "(mock)",
    }


# ---------------------------------------------------------------------------
# 4. 输出
# ---------------------------------------------------------------------------
def _fmt_stage(name: str, secs: float) -> str:
    if secs < 0:
        return f"  {name:<24}  (状态未观察到)"
    return f"  {name:<24}  {secs:8.2f}s"


def print_report(info: Dict[str, Any], real: bool, rc_ok: bool, rc_msg: str,
                 error: Optional[BaseException] = None) -> int:
    """打印诊断报告；返回进程退出码（0=链路通）。"""
    width = 78
    print("=" * width)
    print(" ERA5-AItool CDS 下载诊断报告")
    print("=" * width)
    print(f"  模式      : {'真实网络探测' if real else 'mock 离线验证'}")
    print(f"  数据集    : {info.get('dataset', DATASET)}")
    print(f"  请求      : {TINY_REQUEST['variable'][0]} "
          f"{TINY_REQUEST['year'][0]}-{TINY_REQUEST['month'][0]} "
          f"day={TINY_REQUEST['day']} time={TINY_REQUEST['time']} "
          f"area={TINY_REQUEST['area']}")
    print("-" * width)

    # ① 配置
    print(" [1] .cdsapirc 配置")
    print("  结果      : " + ("OK" if rc_ok else "FAIL"))
    for ln in rc_msg.splitlines():
        print("    " + ln)

    # ② 探测结果
    print(" [2] 探测结果")
    if error is not None:
        print("  结果      : FAIL")
        print("  异常类型  : %s" % type(error).__name__)
        print("  异常信息  : %s" % (str(error) or "(空)"))
        resp = getattr(error, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
        print("  status_code: %s" % (status if status is not None else "(无 .response)"))
        if resp is not None:
            text = getattr(resp, "text", "") or ""
            print("  response  : %s" % text[:500])
        retryable = is_retryable_error(error)
        print("  是否可重试: %s（%s）" % (
            "可重试" if retryable else "不可重试",
            "后端 is_retryable_error 判定；可重试任务会自动退避重试，不可重试需人工处理"))
        # 诊断结论表（失败版）
        print("-" * width)
        print(" [3] 诊断结论")
        print("  配置 OK   : " + ("是" if rc_ok else "否"))
        print("  凭据 OK   : " + ("是（.cdsapirc 已含 url 与 key）" if rc_ok else "否（先修 .cdsapirc）"))
        print("  网络延迟  : 探测失败，无法测量（见上方异常）")
        print("  CDS 状态  : " + ("可重试故障（建议稍后重试）" if retryable else "不可重试错误（需人工修复）"))
        print("  预计 24 块: 无法估算（链路未通）")
        print("=" * width)
        return 1

    for name, secs in info["stages"]:
        print(_fmt_stage(name, secs))
    print("  请求 ID   : %s" % info.get("request_id", ""))
    print("  文件大小  : %s 字节" % info["size_bytes"])
    if info["size_bytes"] > 0 and info["download_elapsed"] > 0:
        print("  下载速率  : %.2f MB/s（%s 秒传 %s 字节）" % (
            info["rate_mbps"], round(info["download_elapsed"], 2), info["size_bytes"]))
    states_desc = " -> ".join(s for s, _ in info["states"]) or "(无状态)"
    print("  状态机    : %s" % states_desc)

    # ③ 诊断结论表
    print("-" * width)
    print(" [3] 诊断结论")
    print("  配置 OK   : " + ("是" if rc_ok else "否"))
    print("  凭据 OK   : " + ("是（.cdsapirc 已含 url 与 key）" if rc_ok else "否（先修 .cdsapirc）"))
    total = next((s for n, s in info["stages"] if n == "全程(提交→完成)"), -1.0)
    print("  网络延迟  : %.2fs（提交(POST)耗时）" % next(
        (s for n, s in info["stages"] if n == "提交(POST)"), 0.0))
    print("  CDS 状态  : " + ("正常（极小请求 completed）" if real else "mock 验证通过（未触网）"))
    # 预计 24 块：24 个独立 CDS 请求，4 并发 → 约 6 轮串行
    if total > 0 and real:
        per_round = total  # 单块（含排队+运行）
        est_24 = per_round * 6  # ceil(24 / 4) = 6 轮
        print("  预计 24 块: 约 %.0f 秒（%.0f 分钟）= 单块 %.0fs × 6 轮(4 并发)；"
              "实际块数据量远大于极小请求，耗时主要取决于 CDS 排队与真实文件大小" % (
                  est_24, est_24 / 60.0, per_round))
    elif real:
        print("  预计 24 块: 无法精确估算（本次未取到完整阶段耗时）")
    else:
        print("  预计 24 块: mock 模式不估算")
    print("=" * width)
    return 0


# ---------------------------------------------------------------------------
# 5. 下载加速基准（design-speedup-download.md §3.6 / T04）
# ---------------------------------------------------------------------------
# 设计要点：bench 跑的就是生产代码路径（Normalizer + prepare_blocks + run_blocks），
# 不另写一套调度器；EventBroker() 不 bind_loop → schedule_broadcast 直接 return，
# 因此无需启动 FastAPI 即可复用真实事件管道（worker 事件经 multiprocessing.Queue）。
@dataclass
class BenchCase:
    """单个 (granularity, workers) 用例的实测结果（机器可读字段，§3.6 验收点③）。"""

    granularity: str = ""
    workers: int = 0
    wall_s: float = 0.0            # 端到端墙钟耗时（含并发开销）
    total: int = 0
    done: int = 0
    failed: int = 0
    retried: int = 0
    throughput_bpm: float = 0.0    # 吞吐：块/分钟
    avg_block_s: float = 0.0       # 单块均摊耗时（wall/完成块数）
    transport_aria2: int = 0       # aria2 多连接下载成功的块数
    transport_cdsapi: int = 0      # cdsapi 单连接/降级下载的块数
    bytes: int = 0                 # 落盘总字节数
    error: str = ""


def build_bench_schema(var: str, year: int, month: Optional[int],
                       area_str: str, dataset: str) -> RequestSchema:
    """构造 bench 请求 schema（hourly 家族；默认 5°×5° 小区域控配额）。"""
    north, west, south, east = [float(x) for x in area_str.split(",")]
    area = Area(north=north, west=west, south=south, east=east)
    if month is not None:
        last = calendar.monthrange(year, month)[1]
        start = f"{year}-{month:02d}-01"
        end = f"{year}-{month:02d}-{last:02d}"
    else:
        start = f"{year}-01-01"
        end = f"{year}-12-31"
    return RequestSchema(dataset=dataset, dataset_family=_family_of(dataset),
                         variables=[var], timerange=Timerange(start=start, end=end),
                         area=area, frequency="hourly", aggregation="raw",
                         confidence=0.9)


def _family_of(dataset: str) -> str:
    """dataset → dataset_family（bench/诊断共用）。"""
    return {
        "reanalysis-era5-single-levels": "era5-single",
        "reanalysis-era5-pressure-levels": "era5-pressure",
        "reanalysis-era5-single-levels-monthly-means": "era5-monthly",
        "reanalysis-era5-land": "land",
        "reanalysis-era5-land-monthly-means": "land-monthly",
    }.get(dataset, "era5-single")


def build_bench_blocks(granularity: str, n: int,
                       schema: RequestSchema) -> List[Dict[str, Any]]:
    """用生产链路造块：Normalizer(granularity=...) → CdsChannel.prepare_blocks
    → 取前 n 块。绝不另写一套请求构造逻辑（§3.6 验收点⑤：day 用例每块
    request["day"] 长度 == 1，由 build_cds_request(day=...) 保证）。

    settings=None 的 Normalizer 直接采用显式 granularity（不读配置），benchmark
    完全由命令行 --granularity 控制，不被 settings.json 默认值污染。
    """
    norm = Normalizer(None, granularity=granularity)
    cds_req = norm.normalize(schema, granularity=granularity)
    # prepare_blocks 只依赖 cds_req 字段（schema/family），用临时 settings 即可。
    tmp = Settings(config_dir=Path(tempfile.gettempdir()),
                   data_dir=Path(tempfile.gettempdir()),
                   download=DownloadSettings(mock=True))
    ch = CdsChannel(tmp)
    prepared = ch.prepare_blocks(cds_req)
    return prepared[:n]


class _BenchChannel(CdsChannel):
    """基准专用 CdsChannel 子类：仅覆盖 worker_cfg 的 mock_delay（脚本内定义，
    不触碰生产 cds_channel.py）。默认延迟 0.02s 远小于 Windows 进程池 spawn 开销，
    会让 mock 矩阵被启动开销主导、吞吐随并发反降（无调度信号）；调大后单块
    模拟耗时 > spawn 开销，才能真实反映并发调度曲线。生产运行路径不受影响。
    """

    def __init__(self, settings: Settings, mock_delay_s: float = 0.02) -> None:
        super().__init__(settings)
        self._mock_delay_s = float(mock_delay_s)

    def worker_cfg(self, fail_rate: float = 0.0) -> Dict[str, Any]:
        cfg = super().worker_cfg(fail_rate=fail_rate)
        if self.mock:
            cfg["mock_delay"] = self._mock_delay_s
        return cfg


def run_bench_case(workers: int, granularity: str,
                   blocks: List[Dict[str, Any]], mock: bool,
                   aria2_enabled: bool, tmp_root: Path,
                   mock_delay_s: float = 0.02) -> BenchCase:
    """跑一个 (granularity, workers) 用例：临时 task_dir/cache_dir + Settings 覆盖
    cds_max_workers → CdsChannel.run_blocks(...)（生产调度器：重试/事件/断点续传全在）。

    返回 BenchCase；任何异常被吞并写入 error（bench 不应因单用例崩溃而中断矩阵）。
    """
    try:
        settings = Settings(config_dir=tmp_root, data_dir=tmp_root,
                            download=DownloadSettings(
                                mock=mock, cds_max_workers=workers,
                                chunk_granularity=granularity,
                                aria2_enabled=aria2_enabled))
        channel = _BenchChannel(settings, mock_delay_s=mock_delay_s)
        task_dir = Path(tmp_root) / f"bench_{granularity}_{workers}"
        store = ResumableStore(str(task_dir), settings)
        broker = EventBroker()          # 不 bind_loop → WS 广播直接 return
        bus = TaskEventBus("bench", task_dir, broker)
        task = Task(id="bench", type=TaskType.DOWNLOAD,
                    block_stats=BlockStats(total=len(blocks)))
        t0 = time.time()
        bus.start()
        try:
            results = channel.run_blocks(task, list(blocks), store, bus,
                                          None, None)
        finally:
            bus.stop()
        wall = time.time() - t0
        done = sum(1 for r in results if r.get("status") == "done")
        failed = sum(1 for r in results if r.get("status") == "failed")
        retried = sum(1 for r in results if r.get("retried"))
        aria2 = sum(1 for r in results if r.get("transport") == "aria2")
        cdsapi = sum(1 for r in results
                     if r.get("transport") in ("cdsapi", "cdsapi_fallback"))
        total_bytes = sum(int(r.get("bytes") or 0)
                          for r in results if r.get("status") == "done")
        throughput = (done / wall * 60.0) if wall > 0 else 0.0
        avg_block = (wall / done) if done > 0 else 0.0
        return BenchCase(granularity=granularity, workers=workers,
                         wall_s=round(wall, 2), total=len(blocks), done=done,
                         failed=failed, retried=retried,
                         throughput_bpm=round(throughput, 2),
                         avg_block_s=round(avg_block, 2),
                         transport_aria2=aria2, transport_cdsapi=cdsapi,
                         bytes=total_bytes)
    except Exception as exc:  # bench 单用例异常不应拖垮整个矩阵
        return BenchCase(granularity=granularity, workers=workers, wall_s=-1.0,
                         total=len(blocks), done=0, failed=len(blocks),
                         error=f"{type(exc).__name__}: {exc}")


def print_bench_report(cases: List[BenchCase], csv_path: Optional[str] = None,
                       json_out: bool = False) -> int:
    """打印并发×粒度矩阵 + 推荐值（§3.6）。

    推荐逻辑：在 failed==0 的用例中取吞吐最高者；若最优点落在 workers 上限 →
    提示可继续上调；若重试率 > 20%（疑似 429）→ 提示已触限速，不建议再加并发。
    返回进程退出码（0=全部用例无失败）。
    """
    if json_out:
        payload = {
            "cases": [c.__dict__ for c in cases],
            "recommended": _recommend(cases),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if csv_path:
            _write_csv(csv_path, cases)
        return 0 if all(c.failed == 0 for c in cases) else 1

    width = 78
    print("=" * width)
    print(" ERA5-AItool 下载基准报告")
    print("=" * width)
    print(f" 用例数    : {len(cases)}")
    print("-" * width)
    print(f" {'粒度':<8}{'并发':>5}{'总耗时':>10}{'吞吐(块/分)':>13}"
          f"{'单块均值':>10}{'失败':>5}{'重试':>5}{'传输(aria2/cdsapi)':>20}")
    for c in cases:
        if c.error:
            print(f" {c.granularity:<8}{c.workers:>5}  ERROR: {c.error}")
            continue
        trans = f"{c.transport_aria2}/{c.transport_cdsapi}"
        print(f" {c.granularity:<8}{c.workers:>5}{c.wall_s:>9.1f}s"
              f"{c.throughput_bpm:>13.2f}{c.avg_block_s:>9.1f}s"
              f"{c.failed:>5}{c.retried:>5}{trans:>20}")
    print("-" * width)

    rec = _recommend(cases)
    if rec is None:
        print(" 推荐：所有用例均有失败，请检查网络/凭据后重试。")
    else:
        print(f" 推荐：granularity={rec.granularity}, "
              f"cds_max_workers={rec.workers}（吞吐 {rec.throughput_bpm} 块/分，"
              f"失败 {rec.failed}）")
        max_workers = max((c.workers for c in cases), default=0)
        if rec.workers >= max_workers:
            print(" 提示：最优点已落在并发上限，仍可继续上调 workers 试更高吞吐。")
        retry_rate = rec.retried / max(rec.total, 1)
        if retry_rate > 0.2:
            print(" 提示：重试率 > 20%（疑似 429 限速），不建议继续加并发。")
    print("=" * width)

    if csv_path:
        _write_csv(csv_path, cases)
        print(f" CSV 已写出: {csv_path}")
    return 0 if all(c.failed == 0 for c in cases) else 1


def _recommend(cases: List[BenchCase]) -> Optional[BenchCase]:
    """在 failed==0 的用例中取吞吐最高者（供 print_bench_report 与 JSON 输出复用）。"""
    ok = [c for c in cases if c.failed == 0 and not c.error and c.throughput_bpm > 0]
    if not ok:
        return None
    return max(ok, key=lambda c: c.throughput_bpm)


def _write_csv(csv_path: str, cases: List[BenchCase]) -> None:
    """机器可读 CSV（字段遵循 §3.6 验收点③）。"""
    fields = ["granularity", "workers", "wall_s", "throughput_bpm", "avg_block_s",
              "failed", "retried", "transport_aria2", "transport_cdsapi", "bytes"]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for c in cases:
            w.writerow({k: c.__dict__.get(k, "") for k in fields})


def print_probe_aria2() -> int:
    """仅探测 aria2c：打印 可用/路径/版本/来源；未安装给出安装提示。退出码恒 0。"""
    width = 78
    info = probe_aria2("")
    print("=" * width)
    print(" ERA5-AItool aria2c 探测")
    print("=" * width)
    if info.available:
        print(f" 可用      : 是")
        print(f" 路径      : {info.path}")
        print(f" 版本      : {info.version or '(未知)'}")
        print(f" 来源      : {info.source}（explicit/env/which）")
        print(" 说明      : aria2_enabled=true 时将走多连接传输；探测不到自动降级 cdsapi。")
    else:
        print(" 可用      : 否（将自动降级为 cdsapi 单连接下载，功能不受影响）")
        print("-" * width)
        print(" 安装提示（三选一）：")
        print("   Windows : winget install aria2.aria2   或   choco install aria2")
        print("             （或官方 release 解压后把绝对路径填到 config/settings.json")
        print("              的 download.aria2_path）")
        print("   conda   : conda install -c conda-forge aria2")
        print("   Linux   : apt install aria2    （或 yum install aria2）")
        print("   macOS   : brew install aria2")
        print("  校验     : 安装后重跑 `python scripts/diagnose_download.py --probe-aria2`")
    print("=" * width)
    return 0


def _print_quota_estimate(schema: RequestSchema, cases_blocks: int,
                          workers_list: List[int],
                          gran_list: List[str]) -> None:
    """真实模式配额估算（防误刷配额，§3.6 验收点④）。"""
    est_requests = cases_blocks * len(workers_list) * len(gran_list)
    print("=" * 78)
    print(" ⚠ 真实模式配额确认")
    print("=" * 78)
    print(f" 数据集    : {schema.dataset}")
    print(f" 变量      : {', '.join(schema.variables)}")
    print(f" 时间范围  : {schema.timerange.start} .. {schema.timerange.end}")
    print(f" area      : {schema.area.cds_area()}")
    print(f" 矩阵      : granularity={gran_list} × workers={workers_list}")
    print(f" 每用例块数: {cases_blocks}")
    print(f" 预计请求数: 约 {est_requests} 个 CDS 请求（granularity×workers×块数，"
          f"断点续传不重复计）")
    print(" 说明      : 每个请求消耗真实 CDS 配额与排队资源；小区域/单变量可显著降低开销。")
    print("=" * 78)


def _parse_int_list(s: str) -> List[int]:
    """'2,4,6,8' → [2,4,6,8]；非法值剔除。"""
    out: List[int] = []
    for part in s.split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def _parse_gran_list(s: str) -> List[str]:
    """'day,month' → ['day','month']；仅保留 day/month（auto 对 bench 意义不大）。"""
    out: List[str] = []
    for part in s.split(","):
        part = part.strip().lower()
        if part in ("day", "month"):
            out.append(part)
    return out


def _run_benchmark(args: argparse.Namespace) -> int:
    """基准模式主流程（§3.6 / T04 验收点①—⑤）。"""
    mock = bool(args.mock)
    workers_list = _parse_int_list(args.workers) or [2, 4, 6, 8]
    gran_list = _parse_gran_list(args.granularity) or ["day", "month"]
    n = max(1, int(args.blocks))
    schema = build_bench_schema(args.var, int(args.year),
                                int(args.month) if args.month else None,
                                args.area, args.dataset)

    # 真实模式前置检查（比 worker 子进程内炸开更早、更清晰）：
    # 缺 .cdsapirc → 直接退出；缺 cdsapi → 直接退出（否则 ImportError 在子进程
    # 被判为可重试 → 30/60/120s 无效退避，浪费 3.5 分钟才失败）。
    if not mock:
        rc_ok, rc_msg = check_rc()
        if not rc_ok:
            print(f"真实基准需要有效 ~/.cdsapirc：\n{rc_msg}")
            return 1
        try:
            import cdsapi  # noqa: F401
        except ImportError:
            print("真实基准需要 cdsapi：请运行 pip install cdsapi。")
            return 1

    # 真实模式：配额确认（防误刷配额）
    if not mock:
        _print_quota_estimate(schema, n, workers_list, gran_list)
        if not args.yes:
            if not sys.stdin.isatty():
                print("真实模式需显式确认：请加 --yes（了解配额消耗后）。")
                return 2
            ans = input("确认提交真实 CDS 请求？[y/N] ").strip().lower()
            if ans != "y":
                print("已取消（未提交任何请求）。")
                return 2

    # 每个 (granularity, workers) 组合跑一个用例；benchmark 默认不开 aria2（不触外部二进制）。
    # 如需测 aria2 增益：先 --probe-aria2 确认可用，再设 download.aria2_enabled=True
    # 并通过 ERA5_ARIA2_CMD 注入（spawn 子进程可继承 env）。
    aria2_enabled = bool(args.aria2)
    mock_delay_s = max(0.0, float(getattr(args, "mock_delay", 0.02) or 0.02))
    cases: List[BenchCase] = []
    with tempfile.TemporaryDirectory(prefix="cds_bench_") as tmp:
        tmp_root = Path(tmp)
        for g in gran_list:
            blocks = build_bench_blocks(g, n, schema)
            for w in workers_list:
                cases.append(run_bench_case(w, g, blocks, mock,
                                            aria2_enabled, tmp_root,
                                            mock_delay_s=mock_delay_s))
    return print_bench_report(cases, csv_path=args.csv, json_out=args.json)


def main() -> int:
    parser = argparse.ArgumentParser(description="ERA5-AItool CDS 下载速度/失败原因诊断")
    parser.add_argument("--mock", action="store_true",
                        help="离线 FakeCdsClient 验证脚本自身（不触网）")
    parser.add_argument("--dataset", default=DATASET,
                        help="数据集名（默认 %(default)s）")
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 输出机器可读结果（含错误时也输出）")
    # —— 下载加速基准（design-speedup-download.md §3.6 / T04）——
    parser.add_argument("--benchmark", action="store_true",
                        help="并发×粒度基准模式（mock 或真实小任务矩阵）")
    parser.add_argument("--workers", default="2,4,6,8",
                        help="并发度列表，逗号分隔（默认 %(default)s）")
    parser.add_argument("--granularity", default="day,month",
                        help="切块粒度列表：day,month（默认 %(default)s）")
    parser.add_argument("--blocks", default="12",
                        help="每用例最多下载块数（默认 %(default)s；day 粒度按年首部取）")
    parser.add_argument("--csv", default=None,
                        help="矩阵结果 CSV 写出路径（默认不写）")
    parser.add_argument("--probe-aria2", action="store_true",
                        help="仅探测 aria2c 可用性并退出")
    parser.add_argument("--yes", action="store_true",
                        help="真实基准跳过配额确认（了解配额消耗后使用）")
    parser.add_argument("--var", default="2m_temperature",
                        help="基准变量（默认 %(default)s）")
    parser.add_argument("--year", default="2025",
                        help="基准年份（默认 %(default)s）")
    parser.add_argument("--month", default=None,
                        help="基准月份 1-12；缺省整年（month/day 均有 ≥12 块，便于矩阵）")
    parser.add_argument("--area", default="30,110,25,115",
                        help="基准区域 north,west,south,east（默认 %(default)s，5°×5° 控配额）")
    parser.add_argument("--aria2", action="store_true",
                        help="基准启用 aria2 传输（需先 --probe-aria2 确认可用；默认关）")
    parser.add_argument("--mock-delay", default="0.02",
                        help="mock 每块模拟耗时秒（默认 %(default)s；Windows spawn 开销大，"
                             "矩阵建议 0.4~1.0 才有并发调度信号；真实模式忽略）")
    args = parser.parse_args()

    # 独立子命令：aria2 探测 / 基准矩阵（先于普通诊断路径分发）
    if args.probe_aria2:
        return print_probe_aria2()
    if args.benchmark:
        return _run_benchmark(args)

    rc_ok, rc_msg = check_rc()
    error: Optional[BaseException] = None
    info: Dict[str, Any] = {"ok": False, "dataset": args.dataset}

    if args.mock:
        try:
            info = probe_mock(args.dataset)
            info["dataset"] = args.dataset
        except Exception as exc:  # 脚本自身应尽量自解释
            error = exc
    elif not rc_ok:
        error = RuntimeError("~/.cdsapirc 缺失或格式错误，无法进行真实探测")
    else:
        try:
            import cdsapi  # noqa: F401
        except ImportError:
            error = ImportError(
                "未安装 cdsapi：请运行 pip install cdsapi（当前环境已装 0.7.7）")
        if error is None:
            try:
                info = probe_real(args.dataset)
                info["dataset"] = args.dataset
            except Exception as exc:  # 收集完整失败信息（类型/str/status/response）
                error = exc

    if args.json:
        payload: Dict[str, Any] = {"ok": info.get("ok", False) and error is None,
                                   "dataset": args.dataset, "rc_ok": rc_ok}
        if error is not None:
            resp = getattr(error, "response", None)
            status = getattr(resp, "status_code", None) if resp is not None else None
            payload["error"] = {"type": type(error).__name__, "str": str(error),
                                "status_code": status,
                                "retryable": is_retryable_error(error)}
        else:
            payload.update({"request_id": info.get("request_id"),
                            "size_bytes": info.get("size_bytes"),
                            "rate_mbps": info.get("rate_mbps"),
                            "stages": dict(info.get("stages", []))})
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if error is None else 1

    return print_report(info, real=not args.mock, rc_ok=rc_ok, rc_msg=rc_msg,
                        error=error)


if __name__ == "__main__":
    sys.exit(main())
