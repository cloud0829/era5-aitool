#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""多任务并发 + 429 退避稳定性回归探针（check_concurrency.py，可复用运维工具）。

背景：多任务（Task → Orchestrator）并行下载时曾出现过进程池破裂
（BrokenProcessPool / 'process pool terminated abruptly'）、文件锁冲突
（PermissionError）、残留 .tmp 等问题（见 design-speedup-download.md 故障复盘）。
本探针用生产链路在 mock 下把这类并发风险重放一遍，作为可重复的回归检查。

原理（与生产 worker 完全一致）：
  - 通过生产 Orchestrator **同时**启动 N 个下载任务（默认 3），每个任务
    channel 注入相同的随机失败率（默认 0.4）；
  - 生产 CdsChannel.worker_cfg 固定 seed=7 → Random(7) 前两次 draw<0.4、
    第三次 ≥0.4，于是每个块【确定性地】失败（模拟 429）2 次后于第 3 次尝试
    成功：既是 429 风暴，又在 retry_max=3 下绝不重试耗尽
    （正中"429 误判为重试耗尽"这一历史故障点）。

验证点：
  1. 多任务并发不崩：无 BrokenProcessPool / 无文件锁 PermissionError /
     无残留 .tmp；
  2. 每任务独立成功：task.json 终态 SUCCESS、done == 总块数、.done 标记齐全；
  3. 落盘无损坏：task.json / manifest.json / events.jsonl 均可解析（JSON 完整），
     且 events 中含"等待…重试"退避日志（429 判定正确、退避恢复正常）。

运行（backend 目录下）：
    python scripts/check_concurrency.py
    # 全默认 = 3 任务 × (2025-01-01..08, day 粒度) 8 块，fail_rate 0.4（确定性 429×2）

    # 自定义任务数（变量数决定任务数；建议用不同变量避免缓存路径冲突）：
    python scripts/check_concurrency.py --vars 2m_temperature,sea_surface_temperature
    # 换失败率/重试上限/时间窗：
    python scripts/check_concurrency.py --fail-rate 0.3 --retry-max 4 \
        --start 2025-02-01 --end 2025-02-05

退出码：0 = 通过（IS_CONCURRENCY_PASS: YES）；1 = 发现问题（打印清单）。

说明：仅 mock（FakeCdsClient），不触网、不耗 CDS 配额；在临时目录中运行，
结束后自动清理，不影响 data/ 目录。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from era5tool.acquisition.cds_channel import CdsChannel  # noqa: E402
from era5tool.config.schema import Area, RequestSchema, Timerange  # noqa: E402
from era5tool.config.settings import DownloadSettings, Settings  # noqa: E402
from era5tool.core.events import EventBroker  # noqa: E402
from era5tool.core.orchestrator import Orchestrator  # noqa: E402
from era5tool.models.task import TaskStatus  # noqa: E402

# 互不相同的合法 ERA5 single-level 变量 → 各任务缓存路径不冲突（默认 3 任务）。
DEFAULT_VARS = ["2m_temperature", "sea_surface_temperature", "mean_sea_level_pressure"]
# worker_cfg 生产固定 seed=7 → Random(7) 前两次 draw=0.324/0.151<0.4、第三次
# 0.651≥0.4。fail_rate=0.4 使每个块【确定性地】429×2 后于第 3 次尝试成功：
# 既是 429 风暴，又在 retry_max=3 下绝不重试耗尽（正中"429 误判耗尽"验证点）。
DEFAULT_FAIL_RATE = 0.4


class FlakyChannel(CdsChannel):
    """探针用 CdsChannel 子类：worker_cfg 注入固定 fail_rate + mock 延迟，
    便于制造 429 退避；不改动生产 cds_channel.py 的任何逻辑。"""

    def __init__(self, settings: Settings, fail_rate: float = DEFAULT_FAIL_RATE,
                 mock_delay: float = 0.3) -> None:
        super().__init__(settings)
        self._fail_rate = float(fail_rate)
        self._mock_delay = float(mock_delay)

    def worker_cfg(self, fail_rate: float = 0.0) -> Dict:
        cfg = super().worker_cfg(fail_rate=self._fail_rate)
        if self.mock:
            cfg["mock_delay"] = self._mock_delay
        return cfg


def _json_ok(path: Path) -> bool:
    """文件存在且可被 json.load 完整解析（无 JSONDecodeError/OSError）。"""
    if not path.is_file():
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
        return True
    except (json.JSONDecodeError, OSError):
        return False


def _check_task(tid: str, orch: Orchestrator, settings: Settings,
                problems: List[str]) -> int:
    """对单个任务做 6 项落盘/状态校验；返回该任务 events 中 429 退避日志行数。"""
    tdir = settings.tasks_dir / tid
    t = orch.get(tid)
    bs = t.block_stats
    print(f"task {tid}: status={t.status.value} done={bs.done} failed={bs.failed} "
          f"skipped={bs.skipped} total={bs.total}")
    # 1) 终态与块统计
    if t.status != TaskStatus.SUCCESS:
        problems.append(f"{tid}: 终态非 SUCCESS（{t.status.value}）error={t.error}")
    if bs.failed != 0:
        problems.append(f"{tid}: 存在失败块 {bs.failed}")
    if bs.done != bs.total:
        problems.append(f"{tid}: done({bs.done}) != total({bs.total})")
    # 2) task.json 完整可解析（严格读一次）
    if not _json_ok(tdir / "task.json"):
        problems.append(f"{tid}: task.json 缺失/损坏")
    # 3) manifest 完整可解析
    if not _json_ok(tdir / "manifest.json"):
        problems.append(f"{tid}: manifest.json 缺失/损坏")
    # 4) events.jsonl 每行均为合法 JSON；统计 429 退避重试日志行（mock 注入
    #    RetryableError → 触发"等待…重试"日志），佐证"429 判定正确且退避恢复"。
    ev_path = tdir / "events.jsonl"
    retry_logs = 0
    if not ev_path.is_file():
        problems.append(f"{tid}: events.jsonl 缺失")
    else:
        bad_lines = 0
        for ln in ev_path.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            msg = str(obj.get("message", ""))
            if "等待" in msg and "重试" in msg:
                retry_logs += 1
        print(f"        (events 中 429/可重试退避重试日志行: {retry_logs})")
        if bad_lines:
            problems.append(f"{tid}: events.jsonl 损坏行 {bad_lines}")
    # 5) .done 标记齐全且无残留 .tmp（key 含 var/年/月子路径 → rglob 递归）
    done_markers = list(tdir.rglob("*.done"))
    tmp_left = list(tdir.rglob("*.tmp"))
    if len(done_markers) != bs.total:
        problems.append(f"{tid}: .done 标记 {len(done_markers)} != {bs.total}")
    if tmp_left:
        problems.append(f"{tid}: 残留 .tmp {[p.name for p in tmp_left]}")
    # 6) 全局扫目录/文本，捕获进程池/文件锁类异常痕迹
    scan_text = ""
    for p in tdir.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".jsonl", ".flag", ".done", ".tmp"):
            try:
                scan_text += p.read_text(encoding="utf-8", errors="ignore") + "\n"
            except OSError:
                problems.append(f"{tid}: 无法读取 {p.name}")
    for bad in ("BrokenProcessPool", "PermissionError", "FileNotFoundError",
                "WORKER_POOL_BROKEN", "OSError"):
        if bad in scan_text:
            problems.append(f"{tid}: 检出异常痕迹 {bad}")
    return retry_logs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ERA5-AItool 多任务并发 + 429 退避稳定性回归探针（mock）")
    parser.add_argument("--vars", default=",".join(DEFAULT_VARS),
                        help="逗号分隔变量列表（变量数=任务数，默认 %(default)s；"
                             "建议不同变量避免缓存路径冲突）")
    parser.add_argument("--fail-rate", type=float, default=DEFAULT_FAIL_RATE,
                        help="每块模拟 429 失败率（默认 %(default)s；0.4+生产"
                             "seed=7 → 每块确定性 429×2 后成功）")
    parser.add_argument("--mock-delay", type=float, default=0.3,
                        help="mock 每块模拟耗时秒（默认 %(default)s）")
    parser.add_argument("--retry-max", type=int, default=3,
                        help="每块最大重试次数（默认 %(default)s，≥1）")
    parser.add_argument("--start", default="2025-01-01",
                        help="时间窗起点（默认 %(default)s，day 粒度每任务按日切块）")
    parser.add_argument("--end", default="2025-01-08",
                        help="时间窗终点（默认 %(default)s，含当天 → 8 块/任务）")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="等待全部任务终态的总超时秒（默认 %(default)s）")
    args = parser.parse_args()

    variables: List[str] = [v.strip() for v in args.vars.split(",") if v.strip()]
    if not variables:
        print("--vars 不能为空。")
        return 1
    fail_rate = max(0.0, min(1.0, float(args.fail_rate)))
    retry_max = max(1, int(args.retry_max))
    timeout_s = max(5.0, float(args.timeout))
    mock_delay = max(0.0, float(args.mock_delay))
    if fail_rate != DEFAULT_FAIL_RATE:
        print(f"提示: fail_rate={fail_rate} ≠ 默认 0.4，'第 3 次成功'的确定性不再保证，"
              f"但仍能重放 429 退避路径。")

    tmp = tempfile.TemporaryDirectory(prefix="t4_concurrency_")
    root = Path(tmp.name)
    settings = Settings(
        config_dir=root / "config", data_dir=root / "data",
        download=DownloadSettings(mock=True, cds_max_workers=len(variables),
                                  retry_max=retry_max,
                                  chunk_granularity="day",
                                  backoff_base=30.0, backoff_factor=2.0,
                                  backoff_max=600.0, backoff_jitter=0.1))
    settings.ensure_dirs()
    broker = EventBroker()
    orch = Orchestrator(settings, broker)

    task_ids: List[str] = []
    for i, var in enumerate(variables):
        # 用独立 FlakyChannel 替换默认 channel（仍走生产 Orchestrator 全流程）
        orch.channel = FlakyChannel(settings, fail_rate=fail_rate,
                                    mock_delay=mock_delay)
        schema = RequestSchema(
            dataset="reanalysis-era5-single-levels",
            dataset_family="era5-single",
            variables=[var],
            timerange=Timerange(start=args.start, end=args.end),
            area=Area(north=30.0, west=110.0, south=25.0, east=115.0),
            frequency="hourly", aggregation="raw")
        task = orch.submit(schema)
        task_ids.append(task.id)
        print(f"[submit] {task.id} var={var} fail_rate={fail_rate}")

    # 并发轮询直至全部终态（SUCCESS/FAILED/PAUSED）
    deadline = time.time() + timeout_s
    statuses: Dict[str, str] = {tid: "pending" for tid in task_ids}
    while time.time() < deadline:
        terminal = True
        for tid in task_ids:
            t = orch.get(tid)
            statuses[tid] = t.status.value
            if t.status not in (TaskStatus.SUCCESS, TaskStatus.FAILED,
                                TaskStatus.PAUSED):
                terminal = False
        if terminal:
            break
        time.sleep(0.2)

    print("\n==== 并发稳定性结果 ====")
    problems: List[str] = []
    retry_log_total = 0
    for tid in task_ids:
        retry_log_total += _check_task(tid, orch, settings, problems)

    tmp.cleanup()
    print("\n==== 判定 ====")
    if problems:
        print("IS_CONCURRENCY_PASS: NO")
        for p in problems:
            print(" -", p)
        return 1
    print(f"IS_CONCURRENCY_PASS: YES —— {len(task_ids)} 任务并发全部 SUCCESS，"
          f"无池破裂/无文件锁/无落盘损坏；"
          f"{len(task_ids)} 任务合计退避重试日志 {retry_log_total} 行"
          f"（429 判定+退避恢复正常，无重试耗尽）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
