# -*- coding: utf-8 -*-
"""并发/限流/指数退避（design-final.md §3.3，E1/E5 固化参数）。

- 并发上限默认 cds_max_workers=4（R1：限速保护）。
- 指数退避：retry_max=3、backoff_base=30s、factor=2、max=600s、jitter ±10%。
- 块内串行重试（先行实验 E5 验证：重试不进 pool 再排，杜绝并发叠加）。
"""
from __future__ import annotations

import random
from typing import Optional, Sequence

BACKOFF_BASE_DEFAULT = 30.0
BACKOFF_FACTOR_DEFAULT = 2.0
BACKOFF_MAX_DEFAULT = 600.0
BACKOFF_JITTER_DEFAULT = 0.10
RETRY_MAX_DEFAULT = 3
MAX_WORKERS_DEFAULT = 4


def compute_backoff(attempt: int, base: float = BACKOFF_BASE_DEFAULT,
                    factor: float = BACKOFF_FACTOR_DEFAULT,
                    max_wait: float = BACKOFF_MAX_DEFAULT,
                    jitter: float = BACKOFF_JITTER_DEFAULT,
                    rng: Optional[random.Random] = None) -> float:
    """第 attempt 次重试（attempt>=1）前应等待的标称秒数（含 jitter）。"""
    if attempt < 1:
        attempt = 1
    nominal = min(base * (factor ** (attempt - 1)), max_wait)
    if jitter > 0:
        rng = rng or random.Random()
        j = rng.uniform(-jitter, jitter)
        return nominal * (1.0 + j)
    return nominal


def check_backoff_sequence(waits: Sequence[float],
                           base: float = BACKOFF_BASE_DEFAULT,
                           factor: float = BACKOFF_FACTOR_DEFAULT,
                           max_wait: float = BACKOFF_MAX_DEFAULT,
                           jitter: float = BACKOFF_JITTER_DEFAULT,
                           tol: float = 1e-6) -> bool:
    """校验实际等待序列符合指数退避（含 jitter 容差）。"""
    for i, w in enumerate(waits, start=1):
        nominal = min(base * (factor ** (i - 1)), max_wait)
        lo = nominal * (1.0 - jitter) - tol
        hi = nominal * (1.0 + jitter) + tol
        if not (lo <= w <= hi):
            return False
    return True
