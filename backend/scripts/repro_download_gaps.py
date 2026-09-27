# -*- coding: utf-8 -*-
"""复现脚本：CDS 队列限流 400 被误判为「不可重试」导致块大面积永久失败。

用法（在 backend/ 下）：
    python scripts/repro_download_gaps.py

模拟真实现场（见 data/tasks/t_20260905_043416_261be78b/events.jsonl）：
- 2 变量 × 12 月 = 24 块（变量外层循环 → 变量 A 的块全排在前面）；
- 6 并发；CDS 以 85% 概率返回
  `400 ... The job has been rejected / Number queued requests ... temporarily limited.`
输出每个变量的 done/failed 分布，用于对照用户报告「第二个变量全军覆没」。
"""
from __future__ import annotations

import sys
import tempfile
from collections import Counter
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

# 测试隔离目录（避免污染真实 data/）
_TMP = Path(tempfile.mkdtemp(prefix="era5_repro_"))
(_TMP / "config").mkdir(parents=True, exist_ok=True)
(_TMP / "data").mkdir(parents=True, exist_ok=True)
import os  # noqa: E402
os.environ["ERA5_CONFIG_DIR"] = str(_TMP / "config")
os.environ["ERA5_DATA_DIR"] = str(_TMP / "data")

from era5tool.acquisition.cds_channel import CdsChannel, is_retryable_error  # noqa: E402
from era5tool.acquisition.mock_client import QueueLimitedError  # noqa: E402
from era5tool.config.settings import Settings  # noqa: E402
from era5tool.core.events import EventBroker, TaskEventBus  # noqa: E402
from era5tool.core.resumable import ResumableStore  # noqa: E402
from era5tool.models.task import Task  # noqa: E402


def make_blocks() -> list:
    """变量外层循环（与 normalizer._split_blocks 一致）：A 全在前，B 全在后。"""
    blocks = []
    for var in ("10m_u_component_of_wind", "10m_v_component_of_wind"):
        for m in range(1, 13):
            key = f"{var}/2020/{m:02d}"
            blocks.append({
                "key": key, "variable": var, "year": 2020, "month": m, "day": None,
                "dataset": "reanalysis-era5-single-levels", "freq": "hourly",
                "rel_target": f"reanalysis-era5-single-levels/{var}/hourly/2020/{m:02d}.nc",
                "request": {"variable": [var], "year": ["2020"], "month": [f"{m:02d}"]},
            })
    return blocks


def main() -> int:
    print("=" * 72)
    print("复现：CDS 队列限流 400 的失败分类与实际下载分布")
    print("=" * 72)

    err = QueueLimitedError()
    print("\n[1] 现场错误信息：")
    print("   ", str(err).replace("\n", " | ")[:160])
    print(f"    status_code = {err.response.status_code}")
    print(f"    is_retryable_error(...) = {is_retryable_error(err)}   "
          f"{'<== BUG：应为 True（队列限流是瞬时错误）' if not is_retryable_error(err) else '(正确)'}")

    settings = Settings.load()
    settings.download.mock = True
    settings.download.cds_max_workers = 6
    settings.download.retry_max = 3
    settings.download.backoff_base = 0.01
    settings.download.backoff_factor = 2.0
    settings.download.backoff_max = 0.05
    settings.download.backoff_jitter = 0.0
    settings.download.mock_error_mode = "queue_limited_400"
    settings.download.mock_fail_rate_override = None

    channel = CdsChannel(settings)
    task = Task(id="t_repro")
    task_dir = settings.tasks_dir / task.id
    task_dir.mkdir(parents=True, exist_ok=True)
    store = ResumableStore(task_dir, settings)
    broker = EventBroker()
    bus = TaskEventBus(task.id, task_dir, broker)

    blocks = make_blocks()
    bus.start()
    try:
        results = channel.run_blocks(task, blocks, store, bus, None, fail_rate=0.85)
    finally:
        bus.stop()

    stat: Counter = Counter()
    for r in results:
        var = r["block"].split("/")[0][:24]
        stat[(var, r["status"])] += 1

    print("\n[2] 实际下载分布（24 块 = 2 变量 × 12 月，CDS 85% 概率队列限流）：")
    for var in ("10m_u_component_of_wind", "10m_v_component_of_wind"):
        d, f = stat[(var, "done")], stat[(var, "failed")]
        print(f"    {var:26s} done={d:2d}  failed={f:2d}")
    total_done = sum(v for (_, s), v in stat.items() if s == "done")
    print(f"    合计 done={total_done}/{len(blocks)}")

    # 交错顺序对照
    from era5tool.core.normalizer import interleave_blocks
    ordered = interleave_blocks(make_blocks())
    print("\n[3] 交错后的提交顺序（前 8 块）：")
    for b in ordered[:8]:
        print("   ", b["key"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
