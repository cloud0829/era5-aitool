# -*- coding: utf-8 -*-
"""QA：并发/退避模块（design-final.md §3.3：指数退避 30s×2^n max600s jitter±10%）。"""
from __future__ import annotations

import random

from era5tool.core.concurrency import (BACKOFF_BASE_DEFAULT, BACKOFF_FACTOR_DEFAULT,
                                       BACKOFF_JITTER_DEFAULT, BACKOFF_MAX_DEFAULT,
                                       check_backoff_sequence, compute_backoff)


def test_compute_backoff_exponential():
    """attempt=1→30s, 2→60s, 3→120s（±10% jitter 容差）。"""
    rng = random.Random(42)
    waits = [compute_backoff(i, rng=rng) for i in (1, 2, 3)]
    assert check_backoff_sequence(waits)


def test_backoff_capped_at_max():
    """封顶 600s：attempt 很大时 nominal=600，含 jitter 仍在 [540,660]。"""
    w = compute_backoff(20)
    assert 540.0 <= w <= 660.0


def test_backoff_jitter_within_tolerance():
    rng = random.Random(7)
    for attempt in (1, 2, 3):
        nominal = min(BACKOFF_BASE_DEFAULT * (BACKOFF_FACTOR_DEFAULT ** (attempt - 1)),
                      BACKOFF_MAX_DEFAULT)
        w = compute_backoff(attempt, rng=rng)
        assert nominal * 0.9 - 1e-6 <= w <= nominal * 1.1 + 1e-6


def test_check_backoff_sequence_rejects_wrong():
    assert not check_backoff_sequence([1.0, 2.0, 3.0])  # 与 30/60/120 不符
    assert check_backoff_sequence([30.0, 60.0, 120.0], jitter=0.0)


def test_zero_jitter_no_random():
    assert compute_backoff(1, jitter=0.0) == BACKOFF_BASE_DEFAULT
