# -*- coding: utf-8 -*-
"""实验共享工具（experiment-local pure logic）。

design-final.md §10 要求：core/concurrency.py、core/resumable.py、
acquisition/cds_request.py 的纯逻辑可先复制为实验内实现（不依赖后端包）。
本模块提供：
  - compute_backoff / backoff_wait_sequence : 指数退避（30s×2^n，max 600s，jitter ±10%）
  - JsonlLog : 跨进程安全的 JSONL 追加日志（用于并发峰值/退避/断点断言）
  - detect_credentials : 统一凭据检测（~/.cdsapirc、DEEPSEEK_API_KEY）
  - ensure_dir / now_iso : 小工具
  - PASS/FAIL 断言汇总器（ResultCollector）

所有 mock 段零外部网络依赖。
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# 常量（与 design-final.md §3.3 一致）
# ---------------------------------------------------------------------------
BACKOFF_BASE_DEFAULT = 30.0        # 秒
BACKOFF_FACTOR_DEFAULT = 2.0       # ×2
BACKOFF_MAX_DEFAULT = 600.0        # 上限秒
BACKOFF_JITTER_DEFAULT = 0.10      # ±10%
RETRY_MAX_DEFAULT = 3

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # era5-AItool/
EXPERIMENTS_ROOT = os.path.dirname(os.path.abspath(__file__))
OUTDIR_DEFAULT = os.path.join(EXPERIMENTS_ROOT, "outputs")


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def ensure_dir(path: str) -> str:
    """确保目录存在并返回其路径。"""
    os.makedirs(path, exist_ok=True)
    return path


def now_iso() -> str:
    """当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_ts() -> float:
    """当前 UTC 时间戳（秒）。"""
    return time.time()


# ---------------------------------------------------------------------------
# 指数退避（§3.3：retry_max=3、backoff_base=30s、factor=2、max=600s、jitter ±10%）
# ---------------------------------------------------------------------------
def compute_backoff(attempt: int, base: float = BACKOFF_BASE_DEFAULT,
                    factor: float = BACKOFF_FACTOR_DEFAULT,
                    max_wait: float = BACKOFF_MAX_DEFAULT,
                    jitter: float = BACKOFF_JITTER_DEFAULT,
                    rng: Optional[random.Random] = None) -> float:
    """第 attempt 次重试（attempt>=1）前应等待的标称秒数（含 jitter）。

    无 jitter 标称值为 min(base * factor ** (attempt-1), max_wait)，
    实际返回值乘以 (1 ± jitter)。
    """
    if attempt < 1:
        attempt = 1
    nominal = min(base * (factor ** (attempt - 1)), max_wait)
    if jitter > 0:
        rng = rng or random.Random()
        j = rng.uniform(-jitter, jitter)
        return nominal * (1.0 + j)
    return nominal


def backoff_wait_sequence(attempts: int, base: float = BACKOFF_BASE_DEFAULT,
                          factor: float = BACKOFF_FACTOR_DEFAULT,
                          max_wait: float = BACKOFF_MAX_DEFAULT,
                          jitter: float = BACKOFF_JITTER_DEFAULT,
                          seed: int = 42) -> List[float]:
    """返回 attempts 次重试的标称等待序列（含 jitter），用于断言指数序列。"""
    rng = random.Random(seed)
    return [compute_backoff(i, base, factor, max_wait, jitter, rng) for i in range(1, attempts + 1)]


def check_backoff_sequence(waits: Sequence[float],
                           base: float = BACKOFF_BASE_DEFAULT,
                           factor: float = BACKOFF_FACTOR_DEFAULT,
                           max_wait: float = BACKOFF_MAX_DEFAULT,
                           jitter: float = BACKOFF_JITTER_DEFAULT,
                           tol: float = 1e-6) -> bool:
    """校验实际等待序列符合指数退避（含 jitter 容差）。

    waits 是某一块实际发生重试的等待序列（长度=重试次数）。
    逐项检查 wait_i 落在 [nominal_i*(1-jitter)-tol, nominal_i*(1+jitter)+tol]。
    """
    rng = random.Random(0)
    for i, w in enumerate(waits, start=1):
        nominal = min(base * (factor ** (i - 1)), max_wait)
        lo = nominal * (1.0 - jitter) - tol
        hi = nominal * (1.0 + jitter) + tol
        if not (lo <= w <= hi):
            return False
    return True


# ---------------------------------------------------------------------------
# 跨进程 JSONL 日志（Windows 友好：按进程分片写入，读时合并，杜绝覆盖丢行）
# ---------------------------------------------------------------------------
class JsonlLog:
    """简单 JSONL 追加日志；多进程各 worker 独立调用 append()。

    Windows 上多个进程以追加模式写同一文件会互相覆盖（MSVC _O_APPEND 为
    seek+write，非原子），因此改为「每进程一个分片文件」：
      - append() 只写本进程分片 `path.<pid>.part`（单进程内无竞争，行完整）
      - read()   读取主文件 + 全部分片，按 (ts, pid, seq) 合并排序
      - clear()  删除主文件与全部分片
    事件结构示例：
      {"ts": 169..., "event": "start", "block": "t2m/2020/05", "pid": 123,
       "seq": 1, "attempt": 1}
      {"ts": 169..., "event": "done", "block": "t2m/2020/05", "pid": 123,
       "seq": 2, "attempt": 1}
      {"ts": 169..., "event": "retry_wait", "block": "...", "attempt": 1,
       "wait_nominal": 30.2, "wait_actual": 0.6}
    """

    def __init__(self, path: str):
        self.path = path
        self._seq: int = 0
        ensure_dir(os.path.dirname(path) or ".")

    def _shard_path(self, pid: Optional[int] = None) -> str:
        return f"{self.path}.{(os.getpid() if pid is None else pid)}.part"

    def _all_paths(self) -> List[str]:
        import glob
        return [self.path] + sorted(glob.glob(f"{self.path}.*.part"))

    def append(self, **entry: Any) -> None:
        entry.setdefault("ts", round(utc_ts(), 6))
        entry.setdefault("pid", os.getpid())
        self._seq += 1
        entry.setdefault("seq", self._seq)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(self._shard_path(), "a", encoding="utf-8") as f:
            f.write(line)

    def read(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for p in self._all_paths():
            if not os.path.isfile(p):
                continue
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        # 合并排序：ts 为主，pid/seq 打破同 ts 的顺序（保持进程内因果序）
        rows.sort(key=lambda e: (e.get("ts", 0.0), e.get("pid", 0), e.get("seq", 0)))
        return rows

    def clear(self) -> None:
        for p in self._all_paths():
            if os.path.isfile(p):
                os.remove(p)


def compute_peak_concurrency(events: Sequence[Dict[str, Any]],
                             start_evt: str = "start",
                             end_evts: Sequence[str] = ("done", "failed")) -> int:
    """从事件日志计算同时活跃 retrieve 的峰值。

    start 事件计数 +1；done/failed 事件计数 -1；retry_wait 期间块不活跃（休眠中），
    因此不参与计数。返回扫描过程中的最大活跃数。
    """
    active = 0
    peak = 0
    ordered = sorted(events, key=lambda e: e.get("ts", 0.0))
    for e in ordered:
        ev = e.get("event")
        if ev == start_evt:
            active += 1
            peak = max(peak, active)
        elif ev in end_evts:
            active = max(0, active - 1)
    return peak


# ---------------------------------------------------------------------------
# 凭据检测（§10.7 约定）
# ---------------------------------------------------------------------------
def detect_credentials() -> Dict[str, bool]:
    """检测本机凭据：
      - cds: ~/.cdsapirc 存在
      - deepseek: 环境变量 DEEPSEEK_API_KEY 或 config/.env 中存在
    任何实验不得硬编码真实 Key。
    """
    home = os.path.expanduser("~")
    cds_rc = os.path.join(home, ".cdsapirc")
    has_cds = os.path.isfile(cds_rc)

    env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    has_deepseek = bool(env_key)
    if not has_deepseek:
        env_path = os.path.join(PROJECT_ROOT, "config", ".env")
        if os.path.isfile(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("DEEPSEEK_API_KEY="):
                            has_deepseek = bool(line.split("=", 1)[1].strip())
                            break
            except OSError:
                pass
    return {"cds": has_cds, "deepseek": has_deepseek}


# ---------------------------------------------------------------------------
# 断言汇总器
# ---------------------------------------------------------------------------
class ResultCollector:
    """收集实验断言，输出 PASS/FAIL 汇总。"""

    def __init__(self, name: str):
        self.name = name
        self.checks: List[Dict[str, Any]] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.checks.append({"ok": bool(ok), "label": label, "detail": detail})
        tag = "PASS" if ok else "FAIL"
        suffix = f"  [{detail}]" if detail else ""
        print(f"  [{tag}] {label}{suffix}")
        return bool(ok)

    def summary(self) -> Dict[str, Any]:
        passed = sum(1 for c in self.checks if c["ok"])
        total = len(self.checks)
        print("=" * 70)
        print(f"[{self.name}] 断言汇总: {passed}/{total} PASS")
        if passed == total:
            print(f"[{self.name}] 整体结论: PASS")
        else:
            print(f"[{self.name}] 整体结论: FAIL")
            for c in self.checks:
                if not c["ok"]:
                    print(f"    - FAIL: {c['label']}  {c.get('detail', '')}")
        print("=" * 70)
        return {"passed": passed, "total": total, "ok": passed == total}


def pretty_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
