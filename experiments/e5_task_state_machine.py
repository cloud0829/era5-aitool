# -*- coding: utf-8 -*-
"""E5 · 任务状态机 + 断点续传 + 并发上限（design-final.md §10.6）。

目的：验证 pending→running→success/failed/paused 流转、`.done`+`manifest.json`
断点续传、并发不超上限、退避生效。

用法：
    python e5_task_state_machine.py            # mock（默认）
    python e5_task_state_machine.py --real     # 可选真请求（需 ~/.cdsapirc）

通过标准：
  ① 状态流转断言：pending→running→success；cancel→paused；resume→running；
     重试耗尽→failed
  ② 中断重跑：done 块被跳过（日志无重复下载），failed/missing 块重下
  ③ 并发峰值 ≤ max_workers
  ④ 退避日志含指数序列与 jitter
  ⑤ 全部断言通过输出 PASS/FAIL 汇总
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mocks"))

from exp_common import (  # noqa: E402
    BACKOFF_BASE_DEFAULT, BACKOFF_FACTOR_DEFAULT, BACKOFF_JITTER_DEFAULT,
    BACKOFF_MAX_DEFAULT, RETRY_MAX_DEFAULT, JsonlLog, OUTDIR_DEFAULT,
    ResultCollector, check_backoff_sequence, compute_backoff,
    compute_peak_concurrency, detect_credentials, ensure_dir,
)
from mocks.fake_cdsapi import FakeCdsClient, RetryableError, make_request  # noqa: E402

VARIABLES = ["2m_temperature", "total_precipitation", "surface_pressure"]
YEARS = ["2020", "2021"]
MONTHS = [f"{m:02d}" for m in range(1, 7)]


def build_blocks() -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    for var in VARIABLES:
        for year in YEARS:
            for month in MONTHS:
                blocks.append({
                    "key": f"{var}/{year}/{month}",
                    "request": make_request(var, year, month),
                })
    return blocks


# ---------------------------------------------------------------------------
# 最小断点续传存储（core/resumable.py 纯逻辑）
# ---------------------------------------------------------------------------
class ResumableStore:
    def __init__(self, task_dir: str):
        self.task_dir = task_dir
        self.manifest_path = os.path.join(task_dir, "manifest.json")

    def load(self) -> Dict[str, Dict[str, Any]]:
        if os.path.isfile(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def save(self, manifest: Dict[str, Dict[str, Any]]) -> None:
        ensure_dir(self.task_dir)
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.manifest_path)

    def mark_done(self, key: str) -> None:
        """写入 .done 标记文件（断点续传的硬标记）。"""
        marker = self.marker_path(key)
        ensure_dir(os.path.dirname(marker))
        with open(marker, "w", encoding="utf-8") as f:
            f.write("done")

    def is_done(self, key: str) -> bool:
        return os.path.isfile(self.marker_path(key))

    def marker_path(self, key: str) -> str:
        """块 .done 标记路径（key 含变量/年/月子路径，需确保父目录存在）。"""
        return os.path.join(self.task_dir, key + ".done")


# ---------------------------------------------------------------------------
# worker（进程池内执行）
# ---------------------------------------------------------------------------
def _worker_e5(args: tuple) -> Dict[str, Any]:
    """单块 worker：块内串行重试（不向 pool 重提交，保证并发不叠加）。

    事件约定（供并发峰值/断点/退避断言使用）：
      - start      每块仅 1 次（并发计数 +1 的唯一入口）
      - attempt    块内重试轮次（信息性，不计入并发）
      - done/failed  每块 1 次（并发计数 -1）
      - retry_wait  记录理论退避值 wait_nominal（断言指数序列）
    """
    block, cfg, log_path, task_dir = args
    log = JsonlLog(log_path)
    key = block["key"]
    client = FakeCdsClient(delay=cfg["delay"], fail_rate=cfg["fail_rate"],
                           seed=cfg.get("seed"), log=log)
    retry_waits: List[float] = []
    attempts = 0
    log.append(event="start", block=key, attempt=1)
    for attempt in range(1, cfg["retry_max"] + 1):
        attempts = attempt
        target = os.path.join(task_dir, "products", f"{key}.nc")
        try:
            client.retrieve(block["request"], target)
            # .done 标记（key 含变量/年/月子路径，先确保父目录）
            marker = os.path.join(task_dir, key + ".done")
            ensure_dir(os.path.dirname(marker))
            with open(marker, "w", encoding="utf-8") as f:
                f.write("done")
            log.append(event="done", block=key, attempt=attempt)
            return {"block": key, "status": "done", "attempts": attempts,
                    "retry_waits": retry_waits}
        except RetryableError:
            if attempt < cfg["retry_max"]:
                wait = compute_backoff(attempt, cfg["backoff_base"], cfg["backoff_factor"],
                                       cfg["backoff_max"], cfg["jitter"])
                # mock：实际睡眠缩放到可忽略（≤0.05s），wait_nominal 保留理论退避值
                actual = min(wait * cfg["sleep_scale"], 0.05)
                retry_waits.append(wait)
                log.append(event="retry_wait", block=key, attempt=attempt,
                           wait_nominal=round(wait, 3), wait_actual=round(actual, 3))
                log.append(event="attempt", block=key, attempt=attempt + 1)
                time.sleep(actual)
    log.append(event="failed", block=key, attempts=attempts)
    return {"block": key, "status": "failed", "attempts": attempts,
            "retry_waits": retry_waits}


def run_pool(tasks: List[tuple], concurrency: int) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    if not tasks:
        return results
    with ProcessPoolExecutor(max_workers=concurrency) as pool:
        futs = {pool.submit(_worker_e5, t): t[0]["key"] for t in tasks}
        for fut in as_completed(futs):
            results.append(fut.result())
    return results


# ---------------------------------------------------------------------------
# 编排器（core/orchestrator.py 纯逻辑）
# ---------------------------------------------------------------------------
def run_task(task_id: str, blocks: List[Dict[str, Any]], cfg: Dict[str, Any],
             outdir: str, log_path: str, concurrency: int = 4,
             stop_after: Optional[int] = None) -> Dict[str, Any]:
    """执行一次任务调度（可被 stop_after 截断模拟中断）。

    返回 task dict（含 status_history / block_stats / error）。
    """
    task_dir = ensure_dir(os.path.join(outdir, task_id))
    store = ResumableStore(task_dir)
    manifest = store.load()
    log = JsonlLog(log_path)

    # 计算待处理块：跳过 done（manifest 或 .done 标记）
    pending: List[Dict[str, Any]] = []
    done_keys: List[str] = []
    for b in blocks:
        key = b["key"]
        if manifest.get(key, {}).get("status") == "done" or store.is_done(key):
            if manifest.get(key, {}).get("status") != "done":
                manifest[key] = {"status": "done"}
            done_keys.append(key)
        else:
            pending.append(b)

    if stop_after is not None:
        pending = pending[:max(0, stop_after)]

    history = ["pending", "running"]
    task = {
        "id": task_id,
        "status": "running",
        "progress": 0.0,
        "block_stats": {"total": len(blocks), "done": len(done_keys), "failed": 0},
        "error": None,
        "status_history": list(history),
    }

    results: List[Dict[str, Any]] = []
    if pending:
        results = run_pool([(b, cfg, log_path, task_dir) for b in pending], concurrency)
        for r in results:
            if r["status"] == "done":
                manifest[r["block"]] = {"status": "done"}
            else:
                manifest[r["block"]] = {"status": "failed",
                                        "error": "BUSY_AFTER_RETRIES"}
    store.save(manifest)

    done = len(done_keys) + sum(1 for r in results if r["status"] == "done")
    failed = sum(1 for r in results if r["status"] == "failed")
    task["block_stats"] = {"total": len(blocks), "done": done, "failed": failed}
    task["progress"] = done / max(len(blocks), 1)

    # 状态判定
    all_done = done == len(blocks)
    if all_done:
        task["status"] = "success"
    elif failed > 0 and (stop_after is None):
        # 重试耗尽 → failed（§7.2：running → failed 致命错误）
        task["status"] = "failed"
        task["error"] = {"code": "BUSY_AFTER_RETRIES",
                         "message": "存在块重试耗尽",
                         "failed_blocks": [r["block"] for r in results if r["status"] == "failed"]}
    else:
        # 还有未完成块（被截断/暂停）
        task["status"] = "paused"

    history.append(task["status"])
    task["status_history"] = history

    # 持久化 task.json
    with open(os.path.join(task_dir, "task.json"), "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    return task


# ---------------------------------------------------------------------------
def clean_task_dir(outdir: str, task_id: str) -> None:
    """清理单个任务输出目录（保证每个场景从干净状态开始，无残留 .done/manifest）。"""
    import shutil
    shutil.rmtree(os.path.join(outdir, task_id), ignore_errors=True)


def run_mock(args: argparse.Namespace) -> Dict[str, Any]:
    collector = ResultCollector("E5 · 任务状态机+断点续传+并发上限 (mock)")
    outdir = ensure_dir(os.path.join(args.outdir, "e5"))
    # 清空上次运行残留（task 目录 + 日志），保证断言独立
    import shutil
    for sub in os.listdir(outdir):
        shutil.rmtree(os.path.join(outdir, sub), ignore_errors=True)
    log_path = os.path.join(outdir, "e5_calls.jsonl")
    JsonlLog(log_path).clear()

    blocks = build_blocks()
    total = len(blocks)
    print(f"[E5] 构造任务：{total} 块")

    # ---------- 场景1：正常流转 pending→running→success ----------
    print("\n[场景1] 正常流转（fail_rate=0）")
    clean_task_dir(outdir, "task_normal")
    cfg1 = {
        "delay": args.delay, "fail_rate": 0.0, "seed": args.seed,
        "retry_max": args.retry_max, "backoff_base": args.backoff_base,
        "backoff_factor": args.backoff_factor, "backoff_max": args.backoff_max,
        "jitter": args.jitter, "sleep_scale": args.sleep_scale,
    }
    JsonlLog(log_path).clear()
    t1 = run_task("task_normal", blocks, cfg1, outdir, log_path, args.concurrency)
    print(f"  status_history={t1['status_history']} stats={t1['block_stats']}")
    collector.check(
        t1["status_history"] == ["pending", "running", "success"],
        "①a pending→running→success 流转", str(t1["status_history"]))
    collector.check(t1["block_stats"]["done"] == total and t1["status"] == "success",
                    "①b 全部块完成", str(t1["block_stats"]))

    events1 = JsonlLog(log_path).read()
    peak1 = compute_peak_concurrency(events1)
    collector.check(peak1 <= args.concurrency,
                    f"③a 并发峰值 ≤ max_workers({args.concurrency})", f"peak={peak1}")
    # 每个块 start 恰好 1 次（无重复下载）
    start_counts: Dict[str, int] = {}
    for e in events1:
        if e.get("event") == "start":
            start_counts[e["block"]] = start_counts.get(e["block"], 0) + 1
    all_once = all(v == 1 for v in start_counts.values()) and len(start_counts) == total
    collector.check(all_once, "②a 正常任务每块仅下载 1 次", f"blocks={len(start_counts)}")

    # ---------- 场景2：中断→paused→resume（断点续传） ----------
    print("\n[场景2] 中断→paused→resume（断点续传）")
    clean_task_dir(outdir, "task_resume")
    JsonlLog(log_path).clear()
    t2a = run_task("task_resume", blocks, cfg1, outdir, log_path,
                   args.concurrency, stop_after=10)
    print(f"  第1次: status={t2a['status']} stats={t2a['block_stats']}")
    collector.check(t2a["status"] == "paused" and t2a["block_stats"]["done"] == 10,
                    "①c cancel/中断 → paused（保留未完成块）",
                    str(t2a["block_stats"]))

    t2b = run_task("task_resume", blocks, cfg1, outdir, log_path, args.concurrency)
    print(f"  第2次(resume): status={t2b['status']} stats={t2b['block_stats']}")
    collector.check(t2b["status"] == "success" and t2b["block_stats"]["done"] == total,
                    "①d paused → running → success（resume 续传）",
                    str(t2b["block_stats"]))

    events2 = JsonlLog(log_path).read()
    start_counts2: Dict[str, int] = {}
    for e in events2:
        if e.get("event") == "start":
            start_counts2[e["block"]] = start_counts2.get(e["block"], 0) + 1
    # 中断前完成的 10 块不应被重复下载（start 恰 1 次）；其余 26 块恰 1 次
    doubled = [k for k, v in start_counts2.items() if v > 1]
    missing = total - len(start_counts2)
    collector.check(len(doubled) == 0 and missing == 0,
                    "②b 中断重跑：done 块被跳过（无重复下载），missing 块重下",
                    f"blocks_started={len(start_counts2)}, doubled={doubled}")

    # ---------- 场景3：重试耗尽 → failed ----------
    print("\n[场景3] 重试耗尽 → failed（fail_rate=1.0, retry_max=4）")
    clean_task_dir(outdir, "task_failed")
    JsonlLog(log_path).clear()
    cfg3 = dict(cfg1)
    cfg3["fail_rate"] = 1.0
    cfg3["retry_max"] = 4   # 使每个块经历退避 30s/60s/120s（指数序列）
    cfg3["delay"] = 0.02
    t3 = run_task("task_failed", build_blocks()[:12], cfg3, outdir, log_path,
                  args.concurrency)
    print(f"  status={t3['status']} error={t3['error']}")
    collector.check(t3["status"] == "failed" and t3["error"]
                    and t3["error"]["code"] == "BUSY_AFTER_RETRIES",
                    "①e 重试耗尽 → failed（错误码 BUSY_AFTER_RETRIES）",
                    str(t3.get("error")))

    events3 = JsonlLog(log_path).read()
    # 按块分组统计退避序列（各块均为 30→60→120 指数，互不混淆）
    waits_by_block: Dict[str, List[float]] = {}
    for e in events3:
        if e.get("event") == "retry_wait":
            waits_by_block.setdefault(e["block"], []).append(e["wait_nominal"])
    collector.check(len(waits_by_block) >= 12,
                    "④a 退避日志存在（每失败块至少 1 次重试等待）",
                    f"blocks={len(waits_by_block)}")
    all_seq_ok = all(
        check_backoff_sequence(seq, args.backoff_base, args.backoff_factor,
                               args.backoff_max, args.jitter)
        for seq in waits_by_block.values())
    sample = sorted({v for seq in waits_by_block.values() for v in seq})[:6]
    collector.check(all_seq_ok,
                    "④b 退避序列符合指数（30s/60s/120s...，jitter±10%）",
                    f"blocks={len(waits_by_block)} sample={sample}")
    peak3 = compute_peak_concurrency(events3)
    collector.check(peak3 <= args.concurrency,
                    f"③b 失败场景并发峰值 ≤ max_workers({args.concurrency})", f"peak={peak3}")

    summary = collector.summary()
    return {"mode": "mock", "collector": summary,
            "normal": t1, "resume_first": t2a, "resume_second": t2b,
            "failed": t3}


def run_real(args: argparse.Namespace) -> Dict[str, Any]:
    creds = detect_credentials()
    if not creds["cds"]:
        print("[E5-real] 待凭据：需配置 ~/.cdsapirc，跳过真实段。")
        return {"mode": "real", "status": "待凭据",
                "message": "需配置 ~/.cdsapirc 后重跑 --real"}
    try:
        import cdsapi
    except ImportError as exc:
        print(f"[E5-real] cdsapi 未安装: {exc}")
        return {"mode": "real", "status": "error", "message": str(exc)}

    print("[E5-real] 检测到 ~/.cdsapirc，跑一次 1 块真实请求验证端到端…")
    client = cdsapi.Client()
    req = make_request("2m_temperature", "2020", "01")
    req["day"] = ["01"]; req["time"] = ["00:00", "12:00"]
    outdir = ensure_dir(os.path.join(args.outdir, "e5", "real"))
    target = os.path.join(outdir, "t2m/2020/01.nc")
    t0 = time.time()
    client.retrieve("reanalysis-era5-single-levels", req, target)
    elapsed = time.time() - t0
    print(f"[E5-real] 真实 retrieve 完成: {elapsed:.2f}s -> {target}")
    return {"mode": "real", "status": "done", "elapsed": round(elapsed, 3),
            "target": target}


def main() -> int:
    parser = argparse.ArgumentParser(description="E5 · 任务状态机+断点续传+并发上限")
    parser.add_argument("--real", action="store_true", help="真实 cdsapi（需 ~/.cdsapirc）")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--outdir", type=str, default=OUTDIR_DEFAULT)
    parser.add_argument("--delay", type=float, default=0.1, help="mock 单块耗时（秒）")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--retry-max", type=int, default=RETRY_MAX_DEFAULT)
    parser.add_argument("--backoff-base", type=float, default=BACKOFF_BASE_DEFAULT)
    parser.add_argument("--backoff-factor", type=float, default=BACKOFF_FACTOR_DEFAULT)
    parser.add_argument("--backoff-max", type=float, default=BACKOFF_MAX_DEFAULT)
    parser.add_argument("--jitter", type=float, default=BACKOFF_JITTER_DEFAULT)
    parser.add_argument("--sleep-scale", type=float, default=0.001,
                        help="mock 实际睡眠缩放（仅 mock 生效）")
    args = parser.parse_args()

    print("=" * 70)
    print(f"E5 · 任务状态机+断点续传+并发上限   mode={'real' if args.real else 'mock'}")
    print("=" * 70)

    result = run_real(args) if args.real else run_mock(args)
    outfile = os.path.join(ensure_dir(args.outdir), "e5_result.json")
    with open(outfile, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"[E5] 结果已写入 {outfile}")
    ok = result.get("collector", {}).get("ok", True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
