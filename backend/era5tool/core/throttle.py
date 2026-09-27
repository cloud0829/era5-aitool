# -*- coding: utf-8 -*-
"""全局自适应限流闸（跨进程 / 跨任务共享，bugfix-download-gaps）。

背景（线上事故复盘，`data/tasks/t_20260905_043416_261be78b/events.jsonl`）：
CDS 对**单个数据集的排队请求数**有上限，超限后既不是 429 也不是 5xx，而是用
**HTTP 400** 返回：

    The job has been rejected
    Number queued requests for this dataset is temporarily limited.
    Please configure your scripts accordingly

6 并发 × 多任务同时跑时大量命中；旧代码按状态码把 400 判为「不可重试」→ 块
**一次都不重试直接永久失败** → 用户看到「有的下不上、跳着时间下」。

本模块提供基于单个 JSON 文件的**软闸门**做全局降速：
- 任一 worker 命中限流 → `arm()` 把「闸释放时间」推到未来（冷却随连续命中
  次数指数增长，上限 `max_s`）；
- 所有 worker（含并发跑的其他任务）在下一次提交 CDS 请求前 `wait()` →
  全局降速，把"越失败越猛冲"的雪崩改成"撞墙就集体退一步"。

为什么用文件而不是共享内存 / Manager：
- Windows 下 `ProcessPoolExecutor` 用 **spawn**，worker 进程不继承父进程内存，
  普通全局变量 / `threading.Event` 跨进程不生效；
- `multiprocessing.Manager` 需额外守护进程与端口，杀进程/打包场景更脆；
- 闸本身是"尽力而为"的软协调，几百毫秒的滞后完全可接受。
写入用临时文件 + `os.replace` 原子替换（Windows/POSIX 均原子），读方绝不会
读到半截 JSON；任何读写异常一律降级为"不闸"，绝不拖垮下载主流程。
"""
from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

THROTTLE_FILENAME = "cds_throttle.json"

DEFAULT_BASE_S = 30.0
DEFAULT_FACTOR = 2.0
DEFAULT_MAX_S = 600.0
DEFAULT_JITTER_S = 2.0

# wait() 的单次 sleep 切片上限：切成小片便于将来被取消信号打断，
# 也避免一次性 sleep 数百秒导致 worker 完全"失联"。
_WAIT_SLICE_S = 0.5


class ThrottleGate:
    """跨进程限流闸：`arm()` 上闸（延长冷却）、`wait()` 等闸、`clear()` 解闸。

    线程/进程安全语义：读是"尽力而为"的快照，写是原子替换；并发 arm 时取
    max(已有释放时间, 本次计算值)，最坏情况是冷却略短于理想值，不影响正确性。
    """

    def __init__(self, path: str | Path, base_s: float = DEFAULT_BASE_S,
                 factor: float = DEFAULT_FACTOR, max_s: float = DEFAULT_MAX_S,
                 jitter_s: float = DEFAULT_JITTER_S) -> None:
        self._path = Path(path)
        self.base_s = float(max(base_s, 0.0))
        self.factor = float(max(factor, 1.0))
        self.max_s = float(max(max_s, 0.0))
        self.jitter_s = float(max(jitter_s, 0.0))

    # ------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    def cooldown_for(self, attempt: int = 1) -> float:
        """第 attempt 次连续命中限流对应的冷却秒数（指数增长，上限 max_s）。"""
        if attempt < 1:
            attempt = 1
        return min(self.base_s * (self.factor ** (attempt - 1)), self.max_s)

    # ------------------------------------------------------------------
    def state(self) -> Dict[str, Any]:
        """读取闸状态快照；不可读/损坏 → 返回空闸状态（绝不让限流闸拖垮下载）。"""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return {"until": 0.0, "reason": "", "hits": 0}
        if not isinstance(data, dict):
            return {"until": 0.0, "reason": "", "hits": 0}
        try:
            until = float(data.get("until", 0.0) or 0.0)
        except (TypeError, ValueError):
            until = 0.0
        try:
            hits = int(data.get("hits", 0) or 0)
        except (TypeError, ValueError):
            hits = 0
        return {"until": until, "reason": str(data.get("reason", "") or ""),
                "hits": hits}

    def _write(self, payload: Dict[str, Any]) -> None:
        """原子写闸状态（tmp + os.replace）。失败一律吞掉：闸是尽力而为的软协调。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self._path)
        except (OSError, TypeError, ValueError):
            pass

    # ------------------------------------------------------------------
    def release_at(self) -> float:
        """闸释放的 epoch 时间戳（已释放/无闸 → 0.0）。"""
        return self.state()["until"]

    def remaining(self, now: Optional[float] = None) -> float:
        """距闸释放还剩多少秒（<=0 表示未上闸）。"""
        now = time.time() if now is None else now
        return max(0.0, self.release_at() - now)

    # ------------------------------------------------------------------
    def arm(self, attempt: int = 1, reason: str = "",
            now: Optional[float] = None) -> float:
        """命中限流 → 上闸（延长冷却）。返回本次生效的冷却秒数。

        attempt 为块内第几次连续命中（1,2,3...），冷却 = base * factor^(n-1)，
        上限 max_s。多次 arm 取 max(已有 until, now+cooldown)。
        """
        now = time.time() if now is None else now
        cooldown = self.cooldown_for(attempt)
        st = self.state()
        until = max(float(st["until"]), now + cooldown)
        self._write({"until": until, "reason": str(reason or "")[:200],
                     "hits": int(st["hits"]) + 1})
        return cooldown

    def clear(self) -> None:
        """解闸（正常完成一批后立即放行）。"""
        try:
            if self._path.is_file():
                self._path.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------
    def wait(self, sleep: Callable[[float], None] = time.sleep,
             rng: Optional[random.Random] = None) -> float:
        """阻塞至闸释放；返回实际等待秒数（未上闸 → 0.0）。

        额外叠加 `jitter_s` 以内的随机抖动，避免闸释放瞬间所有 worker 同时
        冲上去再次撞墙（惊群）。
        """
        left = self.remaining()
        if left <= 0:
            return 0.0
        if self.jitter_s > 0:
            r = rng or random.Random()
            left += r.uniform(0.0, self.jitter_s)
        waited = 0.0
        while waited < left:
            slice_s = min(_WAIT_SLICE_S, left - waited)
            if slice_s <= 0:
                break
            sleep(slice_s)
            waited += slice_s
        return round(waited, 3)


def build_gate(path: str | Path, base_s: float = DEFAULT_BASE_S,
               factor: float = DEFAULT_FACTOR, max_s: float = DEFAULT_MAX_S,
               jitter_s: float = DEFAULT_JITTER_S) -> ThrottleGate:
    """工厂函数（便于测试按 tmp_path 构造独立闸）。"""
    return ThrottleGate(path, base_s=base_s, factor=factor, max_s=max_s,
                        jitter_s=jitter_s)
