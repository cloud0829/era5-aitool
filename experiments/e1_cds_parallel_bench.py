# -*- coding: utf-8 -*-
"""E1 · CDS 并发基准（design-final.md §10.2）。

目的：证明「多进程并行提速且不封禁」，校准 cds_max_workers / 退避参数。

用法：
    python e1_cds_parallel_bench.py                 # mock 模式（默认）
    python e1_cds_parallel_bench.py --real          # 真实 cdsapi（需 ~/.cdsapirc）
    python e1_cds_parallel_bench.py --concurrency 4 --delay 1.5 --fail-rate 0.1

Mock 策略：mocks/fake_cdsapi.FakeCdsClient 模拟 retrieve（可控耗时/失败率）。
通过标准（mock）：
  ① 切块总数 = 期望块数
  ② 并发 4 下并行总耗时 ≤ 串行耗时 / 2
  ③ 失败块按指数退避重试（日志含 30s/60s 标称等待，jitter 生效）
  ④ 全部块最终 done
  ⑤ 无并发超上限（同时活跃 retrieve ≤ max_workers）

说明：真实退避 30s/60s 会让 mock 实验过慢，故 mock 默认按 --sleep-scale 缩放实际
睡眠，但日志记录「标称等待」（30s/60s 序列）并据此断言；报告会注明该缩放。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

# 允许直接以脚本方式运行（python e1_*.py），保证 mocks/exp_common 可导入
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
MONTHS = [f"{m:02d}" for m in range(1, 7)]  # 1-6 月


def build_blocks() -> List[Dict[str, Any]]:
    """构造 3 变量 × 2 年 × 6 月 = 36 块。"""
    blocks: List[Dict[str, Any]] = []
    for var in VARIABLES:
        for year in YEARS:
            for month in MONTHS:
                blocks.append({
                    "key": f"{var}/{year}/{month}",
                    "request": make_request(var, year, month),
                })
    return blocks


def _key_seed(key: str, base: int) -> int:
    """从块 key 派生的确定性种子：不同块获得不同 RNG 流，保证 fail_rate 真实生效。"""
    import zlib
    return (base + zlib.crc32(key.encode("utf-8"))) % 100000


def _run_block_serial(block: Dict[str, Any], cfg: Dict[str, Any],
                      log: JsonlLog, outdir: str) -> Dict[str, Any]:
    """串行执行一块（含重试/退避），返回结果。

    事件约定（供并发峰值/退避断言使用）：
      - start      每块仅 1 次（并发计数 +1 的唯一入口）
      - attempt    块内重试轮次（信息性，不计入并发）
      - done/failed  每块 1 次（并发计数 -1）
      - retry_wait  记录理论退避值 wait_nominal（断言指数序列）
    """
    client = FakeCdsClient(delay=cfg["delay"], fail_rate=cfg["fail_rate"],
                           seed=_key_seed(block["key"], cfg.get("seed") or 0),
                           log=log)
    key = block["key"]
    retry_waits: List[float] = []
    attempts = 0
    log.append(event="start", block=key, attempt=1)
    for attempt in range(1, cfg["retry_max"] + 1):
        attempts = attempt
        target = os.path.join(outdir, "serial", f"{key}.nc")
        try:
            client.retrieve(block["request"], target)
            log.append(event="done", block=key, attempt=attempt)
            return {"block": key, "status": "done", "attempts": attempts,
                    "retry_waits": retry_waits}
        except RetryableError:
            if attempt < cfg["retry_max"]:
                wait = compute_backoff(
                    attempt, cfg["backoff_base"], cfg["backoff_factor"],
                    cfg["backoff_max"], cfg["jitter"])
                # mock：实际睡眠缩放到可忽略（≤0.05s），wait_nominal 保留理论退避值
                actual = min(wait * cfg["sleep_scale"], 0.05)
                retry_waits.append(wait)
                log.append(event="retry_wait", block=key, attempt=attempt,
                           wait_nominal=round(wait, 3),
                           wait_actual=round(actual, 3))
                log.append(event="attempt", block=key, attempt=attempt + 1)
                time.sleep(actual)
    log.append(event="failed", block=key, attempts=attempts)
    return {"block": key, "status": "failed", "attempts": attempts,
            "retry_waits": retry_waits}


def _worker_parallel(args: tuple) -> Dict[str, Any]:
    """进程池 worker：解包配置 → 执行一块（含重试/退避）。"""
    block, cfg, log_path, outdir = args
    log = JsonlLog(log_path)
    return _run_block_serial(block, cfg, log, outdir)


def run_mock(args: argparse.Namespace) -> Dict[str, Any]:
    """mock 模式：三个子场景。"""
    outdir = ensure_dir(args.outdir)
    log_path = os.path.join(outdir, "e1_calls.jsonl")
    log = JsonlLog(log_path)
    log.clear()

    collector = ResultCollector("E1 · CDS 并发基准 (mock)")
    blocks = build_blocks()
    expected = len(blocks)
    print(f"[E1] 切块: {expected} 块 ({len(VARIABLES)}变量 × {len(YEARS)}年 × {len(MONTHS)}月)")

    # ---------- 场景 A：调度正确性 + 提速（fail_rate=0 纯测并行） ----------
    print("\n[场景A] 串行 vs 并行（fail_rate=0, 纯调度提速）")
    cfg_a = {
        "delay": args.delay, "fail_rate": 0.0, "seed": args.seed,
        "retry_max": args.retry_max, "backoff_base": args.backoff_base,
        "backoff_factor": args.backoff_factor, "backoff_max": args.backoff_max,
        "jitter": args.jitter, "sleep_scale": args.sleep_scale,
    }
    log.clear()
    t0 = time.time()
    serial_results = [_run_block_serial(b, cfg_a, log, outdir) for b in blocks]
    serial_time = time.time() - t0
    serial_done = sum(1 for r in serial_results if r["status"] == "done")
    print(f"  串行: {serial_time:.2f}s, done={serial_done}/{expected}")
    # 保存串行并发 log 用于峰值断言
    serial_events = log.read()

    log.clear()
    t0 = time.time()
    parallel_results = run_pool(blocks, cfg_a, log_path, outdir, args.concurrency)
    parallel_time = time.time() - t0
    parallel_done = sum(1 for r in parallel_results if r["status"] == "done")
    print(f"  并行(concurrency={args.concurrency}): {parallel_time:.2f}s, done={parallel_done}/{expected}")
    parallel_events = log.read()

    collector.check(expected == 36, "① 切块总数=期望块数(36)", f"实际 {expected}")
    collector.check(parallel_done == expected, "④a 场景A 全部块 done",
                    f"done={parallel_done}/{expected}")
    ratio = parallel_time / max(serial_time, 1e-9)
    collector.check(
        parallel_time <= serial_time / 2.0,
        "② 并行耗时 ≤ 串行耗时/2",
        f"serial={serial_time:.2f}s parallel={parallel_time:.2f}s ratio={ratio:.2f}")

    serial_peak = compute_peak_concurrency(serial_events)
    parallel_peak = compute_peak_concurrency(parallel_events)
    print(f"  并发峰值: serial_peak={serial_peak}, parallel_peak={parallel_peak}")
    collector.check(serial_peak <= 1, "⑤a 串行并发峰值 ≤ 1", f"peak={serial_peak}")
    collector.check(parallel_peak <= args.concurrency,
                    f"⑤b 并行并发峰值 ≤ max_workers({args.concurrency})",
                    f"peak={parallel_peak}")

    # ---------- 场景 B：退避/重试（fail_rate>0，验证指数退避 + jitter） ----------
    print(f"\n[场景B1] 现实失败率重试（fail_rate={args.fail_rate}, seed={args.seed}）")
    cfg_b = dict(cfg_a)
    cfg_b["fail_rate"] = args.fail_rate
    log.clear()
    t0 = time.time()
    retry_results = run_pool(blocks, cfg_b, log_path, outdir, args.concurrency)
    retry_time = time.time() - t0
    retry_done = sum(1 for r in retry_results if r["status"] == "done")
    retry_events = log.read()

    retried_blocks = [r for r in retry_results if r["attempts"] > 1]
    # 按块分组退避序列（各块独立指数序列，避免跨块交错误判）
    waits_by_block: Dict[str, List[float]] = {}
    for e in retry_events:
        if e.get("event") == "retry_wait":
            waits_by_block.setdefault(e["block"], []).append(e["wait_nominal"])
    waits_from_log = [w for seq in waits_by_block.values() for w in seq]
    print(f"  重试块数: {len(retried_blocks)}/{expected}, 等待次数: {len(waits_from_log)}")
    if waits_from_log:
        print(f"  标称等待样例: {sorted(set(waits_from_log))[:6]}")

    collector.check(retry_done == expected, "④b 场景B1 全部块最终 done",
                    f"done={retry_done}/{expected}")
    collector.check(len(retried_blocks) > 0,
                    "③a 存在失败块被重试", f"retried={len(retried_blocks)}")
    seq_b_ok = bool(waits_by_block) and all(
        check_backoff_sequence(seq, args.backoff_base,
                               args.backoff_factor, args.backoff_max, args.jitter)
        for seq in waits_by_block.values())
    collector.check(
        seq_b_ok,
        "③b 等待序列符合指数退避(30s/60s/120s..., jitter±10%)",
        f"blocks={len(waits_by_block)} waits={sorted(set(waits_from_log))[:6]}")
    peak_b = compute_peak_concurrency(retry_events)
    collector.check(peak_b <= args.concurrency,
                    f"⑤c 场景B1 并发峰值 ≤ max_workers({args.concurrency})",
                    f"peak={peak_b}")

    # ---------- 场景 B2：退避阶梯探针（fail_rate=1.0 强制多级退避） ----------
    print("\n[场景B2] 退避阶梯探针（fail_rate=1.0, retry_max=3, 6 块）")
    cfg_b2 = dict(cfg_a)
    cfg_b2["fail_rate"] = 1.0
    cfg_b2["retry_max"] = 3
    cfg_b2["delay"] = 0.05
    ladder_blocks = blocks[:6]
    log.clear()
    ladder_results = run_pool(ladder_blocks, cfg_b2, log_path, outdir, args.concurrency)
    ladder_events = log.read()
    # 按块分组退避序列（每块均为 30→60 两档，跨块交错会误判）
    ladder_by_block: Dict[str, List[float]] = {}
    for e in ladder_events:
        if e.get("event") == "retry_wait":
            ladder_by_block.setdefault(e["block"], []).append(e["wait_nominal"])
    ladder_waits = [w for seq in ladder_by_block.values() for w in seq]
    nominal_set = {round(w, 2) for w in ladder_waits}
    print(f"  标称等待档位: {sorted(nominal_set)}")
    ladder_seq_ok = bool(ladder_by_block) and all(
        check_backoff_sequence(seq, args.backoff_base,
                               args.backoff_factor, args.backoff_max, args.jitter)
        for seq in ladder_by_block.values())
    collector.check(
        ladder_seq_ok,
        "③c 退避阶梯序列符合指数退避（含 jitter）",
        f"blocks={len(ladder_by_block)} waits={sorted(nominal_set)}")
    has_base = any(abs(w - args.backoff_base) <= args.backoff_base * (args.jitter + 0.01)
                   for w in nominal_set)
    has_double = any(abs(w - args.backoff_base * args.backoff_factor) <= args.backoff_base * args.backoff_factor * (args.jitter + 0.01)
                     for w in nominal_set)
    collector.check(has_base and has_double,
                    "③d 标称等待覆盖 base(30s) 与 2×base(60s) 档位",
                    f"set={sorted(nominal_set)}")
    # 6 块全部 3 次尝试失败（重试耗尽路径，供 E5 交叉印证）
    exhausted = all(r["status"] == "failed" and r["attempts"] == 3 for r in ladder_results)
    collector.check(exhausted, "③e fail_rate=1.0 下重试耗尽 → failed",
                    f"failed={sum(1 for r in ladder_results if r['status']=='failed')}/6")

    print(f"\n[E1-mock] 场景B 总耗时 {retry_time:.2f}s")
    summary = collector.summary()
    return {
        "mode": "mock",
        "blocks": expected,
        "serial_time": round(serial_time, 3),
        "parallel_time": round(parallel_time, 3),
        "speedup_ratio": round(ratio, 3),
        "serial_peak": serial_peak,
        "parallel_peak": parallel_peak,
        "retried_blocks": len(retried_blocks),
        "retry_waits_count": len(waits_from_log),
        "backoff_nominal_sample": sorted(set(waits_from_log))[:6],
        "collector": summary,
    }


def run_pool(blocks: List[Dict[str, Any]], cfg: Dict[str, Any],
             log_path: str, outdir: str, concurrency: int) -> List[Dict[str, Any]]:
    """ProcessPoolExecutor 并发执行所有块。"""
    tasks = [(b, cfg, log_path, outdir) for b in blocks]
    results: List[Dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=concurrency) as pool:
        futs = {pool.submit(_worker_parallel, t): t[0]["key"] for t in tasks}
        for fut in as_completed(futs):
            results.append(fut.result())
    results.sort(key=lambda r: blocks_order(blocks, r["block"]))
    return results


def blocks_order(blocks: List[Dict[str, Any]], key: str) -> int:
    for i, b in enumerate(blocks):
        if b["key"] == key:
            return i
    return len(blocks)


def run_real(args: argparse.Namespace) -> Dict[str, Any]:
    """真实模式：需 ~/.cdsapirc；跑 1 变量×1 年×1 月小请求对比。"""
    creds = detect_credentials()
    if not creds["cds"]:
        print("[E1-real] 待凭据：需配置 ~/.cdsapirc，跳过真实段。")
        return {"mode": "real", "status": "待凭据",
                "message": "需配置 ~/.cdsapirc 后重跑 --real"}
    try:
        import cdsapi
    except ImportError as exc:
        print(f"[E1-real] cdsapi 未安装: {exc}")
        return {"mode": "real", "status": "error", "message": str(exc)}

    print("[E1-real] 检测到 ~/.cdsapirc，执行 1 变量×1 年×1 月真实小请求对比…")
    client = cdsapi.Client()
    req = make_request("2m_temperature", "2020", "01")
    req["day"] = ["01", "02"]          # 小请求，仅 2 天
    req["time"] = ["00:00", "12:00"]
    outdir = ensure_dir(args.outdir)
    target = os.path.join(outdir, "real", "t2m/2020/01.nc")
    t0 = time.time()
    client.retrieve("reanalysis-era5-single-levels", req, target)
    elapsed = time.time() - t0
    print(f"[E1-real] 真实 retrieve 完成: {elapsed:.2f}s -> {target}")
    return {"mode": "real", "status": "done", "elapsed": round(elapsed, 3),
            "target": target}


def main() -> int:
    parser = argparse.ArgumentParser(description="E1 · CDS 并发基准")
    parser.add_argument("--real", action="store_true", help="真实 cdsapi（需 ~/.cdsapirc）")
    parser.add_argument("--concurrency", type=int, default=4, help="并行 worker 数（默认 4）")
    parser.add_argument("--outdir", type=str, default=OUTDIR_DEFAULT, help="输出目录")
    parser.add_argument("--delay", type=float, default=1.5, help="mock 单块耗时（秒）")
    parser.add_argument("--fail-rate", type=float, default=0.1, help="mock 429 失败率")
    parser.add_argument("--seed", type=int, default=42, help="mock 随机种子")
    parser.add_argument("--retry-max", type=int, default=RETRY_MAX_DEFAULT, help="最大重试次数")
    parser.add_argument("--backoff-base", type=float, default=BACKOFF_BASE_DEFAULT, help="退避基数秒")
    parser.add_argument("--backoff-factor", type=float, default=BACKOFF_FACTOR_DEFAULT)
    parser.add_argument("--backoff-max", type=float, default=BACKOFF_MAX_DEFAULT)
    parser.add_argument("--jitter", type=float, default=BACKOFF_JITTER_DEFAULT)
    parser.add_argument("--sleep-scale", type=float, default=0.02,
                        help="mock 实际睡眠缩放（标称 30s→0.6s），仅 mock 生效")
    args = parser.parse_args()

    print("=" * 70)
    print(f"E1 · CDS 并发基准   mode={'real' if args.real else 'mock'}")
    print(f"     concurrency={args.concurrency} delay={args.delay} fail_rate={args.fail_rate}")
    print("=" * 70)

    if args.real:
        result = run_real(args)
    else:
        result = run_mock(args)

    outfile = os.path.join(ensure_dir(args.outdir), "e1_result.json")
    with open(outfile, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"[E1] 结果已写入 {outfile}")
    return 0 if result.get("collector", {}).get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
