# -*- coding: utf-8 -*-
"""本地 fake CDS 客户端（无凭据/mock 模式）。

复用先行实验 experiments/mocks/fake_cdsapi.py 的验证思路：
- retrieve 模拟官方 cdsapi（可控耗时 delay / 失败率 fail_rate / 种子）。
- 写一个假 NetCDF 文本产物（标记块完成），不消耗真实配额。
- info() 返回内置离线核对表（ERA5/ERA5-Land）。
"""
from __future__ import annotations

import json
import os
import random
import time
from typing import Any, Dict, List, Optional

from era5tool.core.resumable import ensure_dir

# 与官方 cdsapi 错误类对齐：429 限速属于可重试错误
class RetryableError(Exception):
    def __init__(self, message: str = "HTTP 429 Too Many Requests", code: int = 429):
        super().__init__(message)
        self.code = code


class NonRetryableError(Exception):
    def __init__(self, message: str = "Bad request", code: int = 400):
        super().__init__(message)
        self.code = code


class _StubResponse:
    """最小 response 桩：只提供 `is_retryable_error` 需要的 `.status_code` / `.text`。"""

    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class QueueLimitedError(Exception):
    """模拟真实 CDS 队列限流：HTTP **400** + "temporarily limited" 正文。

    线上原文（data/tasks/t_20260905_043416_261be78b/events.jsonl，172/172 失败块）：

        400 Client Error: Bad Request for url:
        https://cds.climate.copernicus.eu/api/retrieve/v1/jobs/<uuid>/results
        The job has been rejected
        Number queued requests for this dataset is temporarily limited.
        Please configure your scripts accordingly

    这是**瞬时**错误（队列腾出空间后重试即可成功），但状态码是 400 —— 老代码
    按状态码判为不可重试 → 块一次都不重试就永久失败。mock 需要能复现该形态，
    否则"mock 全绿、线上全挂"（历史 P0 的同类教训）。
    """

    BODY = ("The job has been rejected\n"
            "Number queued requests for this dataset is temporarily limited. "
            "Please configure your scripts accordingly ")

    def __init__(self, job_id: str = "00000000-0000-0000-0000-000000000000") -> None:
        url = (f"https://cds.climate.copernicus.eu/api/retrieve/v1/jobs/"
               f"{job_id}/results")
        super().__init__(f"400 Client Error: Bad Request for url: {url}\n{self.BODY}")
        self.response = _StubResponse(400, self.BODY)


# mock 故障注入模式：
# - "retryable_429"（默认）：抛 RetryableError(429)，与既有测试/行为完全一致；
# - "queue_limited_400"：抛 QueueLimitedError（HTTP 400 + 队列限流正文），
#   用于复现线上"队列限流被误判为不可重试"的事故形态。
ERROR_MODE_RETRYABLE_429 = "retryable_429"
ERROR_MODE_QUEUE_LIMITED_400 = "queue_limited_400"
ERROR_MODES = (ERROR_MODE_RETRYABLE_429, ERROR_MODE_QUEUE_LIMITED_400)


class FakeResult:
    """两阶段句柄（对齐 cdsapi.Result / datastores.Results，design-speedup-download.md §7-⑦）。

    `retrieve(target=None)` 返回本对象；调用方（transport.download_block_file）：
    - `resolve_result_url(handle)` → `(handle.location, handle.content_length)` 取下载
      URL 与期望字节数，交给 aria2 多连接下载；
    - 或 `handle.download(target)` 兜底下载（同一 CDS job，不重排队、不耗配额）。
    属性名 `.location / .content_length / .download(target)` 与真实两类客户端
    同名同义（§1.5 / §3.4）。

    `content_length` 必须 == aria2 测试 stub 实际写入字节数（见
    `tests/_stub_aria2.py` 的 `STUB_FILE_SIZE`），否则 `verify_size` 在 success
    用例里判失败——大小语义对齐真实 `Result`（§7-⑦：mock 要能走真分支而非"未知大小"兜底）。
    """

    # 与 tests/_stub_aria2.py 的 STUB_FILE_SIZE 保持一致（aria2 success 校验用）。
    STUB_CONTENT_LENGTH = 1024

    def __init__(self, name: str, request: Dict[str, Any],
                 location: str = "file:///fake/cds/result.nc") -> None:
        self.name = name
        self.request = request or {}
        self.location = location                  # 任意非空 URL 均可（stub 忽略内容）
        self.content_length = self.STUB_CONTENT_LENGTH

    def download(self, target: str) -> str:
        """兜底下载：写与 `retrieve(target=...)` 相同的假 NetCDF 产物（§7-⑦）。

        内部也要 `ensure_dir`——与真实 cdsapi 行为对齐，避免 mock 掩盖真实缺陷
        （这是历史 P0 的教训：mock 自带 ensure_dir 掩盖了真实客户端不建父目录的 bug）。
        """
        ensure_dir(os.path.dirname(os.path.abspath(target)))
        payload = {"fake": True, "name": self.name, "request": self.request,
                   "created_at": time.time(),
                   "target": os.path.abspath(target)}
        with open(target, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return target


_INFO_TABLE: Dict[str, Dict[str, Any]] = {
    "reanalysis-era5-single-levels": {
        "name": "reanalysis-era5-single-levels", "grid": 0.25,
        "time": [f"{h:02d}:00" for h in range(24)],
        "day": [f"{d:02d}" for d in range(1, 32)],
        "has_pressure_levels": False, "license": "需勾选 ERA5 许可",
    },
    "reanalysis-era5-pressure-levels": {
        "name": "reanalysis-era5-pressure-levels", "grid": 0.25,
        "time": [f"{h:02d}:00" for h in range(24)],
        "day": [f"{d:02d}" for d in range(1, 32)],
        "has_pressure_levels": True, "license": "需勾选 ERA5 许可",
    },
    "reanalysis-era5-land": {
        "name": "reanalysis-era5-land", "grid": 0.1,
        "time": [f"{h:02d}:00" for h in range(24)],
        "day": [f"{d:02d}" for d in range(1, 32)],
        "has_pressure_levels": False, "license": "需勾选 Land 许可（CDS 数据集许可页）",
    },
    "reanalysis-era5-land-monthly-means": {
        "name": "reanalysis-era5-land-monthly-means", "grid": 0.1,
        "time": ["00:00"], "day": None,
        "has_pressure_levels": False, "license": "需勾选 Land 许可（CDS 数据集许可页）",
    },
}


class FakeCdsClient:
    """stub cdsapi.Client。"""

    def __init__(self, delay: float = 0.05, fail_rate: float = 0.0,
                 seed: Optional[int] = None,
                 error_mode: str = ERROR_MODE_RETRYABLE_429):
        self.delay = float(delay)
        self.fail_rate = float(fail_rate)
        self._rng = random.Random(seed)
        self.error_mode = (error_mode if error_mode in ERROR_MODES
                           else ERROR_MODE_RETRYABLE_429)
        self.calls: List[Dict[str, Any]] = []

    def _raise_failure(self) -> None:
        """按 error_mode 抛故障（默认与历史行为一致：429 可重试）。"""
        if self.error_mode == ERROR_MODE_QUEUE_LIMITED_400:
            raise QueueLimitedError()
        raise RetryableError("HTTP 429 Too Many Requests (mock)")

    def retrieve(self, name: str, request: Optional[Dict[str, Any]] = None,
                 target: Optional[str] = None):
        """对齐官方 cdsapi.Client.retrieve(name, request, target=None) 签名。

        - target 给值：写假 NetCDF 产物到 target，返回 dict（现状，无网络）；
        - target 为 None：返回 `FakeResult` 两阶段句柄（design-speedup-download.md
          §7-⑦），对齐 cdsapi.Result / datastores.Results 的
          `.location / .content_length / .download(target)` 三件套，使 aria2
          分支在 mock 下也能真实走通（证明传输链路而非仅 cdsapi 单连接）；
        - fail_rate 命中时按 `error_mode` 抛异常（两阶段都适用，模拟阶段A 失败
          → 重试）：默认 RetryableError(429)；`queue_limited_400` 模式抛
          QueueLimitedError（HTTP 400 + 队列限流正文，复现线上事故形态）。
        """
        started = time.time()
        if self.delay > 0:
            time.sleep(self.delay)
        if self._rng.random() < self.fail_rate:
            self._raise_failure()
        if target is None:
            # 两阶段句柄：阶段A 成功后返回的 Result，调用方再 resolve_result_url
            # 拿 .location 走 aria2，或 .download(target) 兜底，均不重排队。
            return FakeResult(name=name, request=request or {})
        ensure_dir(os.path.dirname(os.path.abspath(target)))
        payload = {"fake": True, "name": name, "request": request,
                   "created_at": time.time(),
                   "target": os.path.abspath(target)}
        with open(target, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        self.calls.append({"event": "ok", "elapsed": round(time.time() - started, 4)})
        return {"status": "done", "target": target}

    def info(self, name: str) -> Dict[str, Any]:
        if name not in _INFO_TABLE:
            raise NonRetryableError(f"Unknown dataset: {name}", code=404)
        return _INFO_TABLE[name]
