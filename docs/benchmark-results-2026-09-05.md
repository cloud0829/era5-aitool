# ERA5-AItool 下载加速：基准与稳定性验证报告（2026-09-05）

> 范围：T1 benchmark CLI 接线核对 · T2 mock 并发×粒度矩阵 · T3 真实基准状态 ·
> T4 多任务并发稳定性 · T5 默认配置建议。
> 产出物：`backend/scripts/diagnose_download.py`（benchmark CLI）、
> `backend/bench_output/bench_mock_matrix.csv`（T2 数据）、
> `backend/scripts/check_concurrency.py`（T4 探针正式化）、本报告。
> 本文档为收尾阶段补写；T1/T2/T4 实测数据来自前一会话已落盘结果，T3 标注为
> 「待真实环境验证」（收尾阶段不再重跑真实 CDS，避免消耗配额与排队时间）。

---

## 1. 环境与方法

| 项目 | 值 |
|---|---|
| 日期 | 2026-09-05 |
| 项目根 | `D:\Desktop\era5-AItool` |
| 后端目录 | `backend\` |
| Python | 3.13.12（`C:\Users\lei\.workbuddy\binaries\python\envs\default`） |
| cdsapi | 已安装（脚本运行时导入正常） |
| aria2c | **未安装**（`--probe-aria2` 实测"可用：否"） |

aria2 说明：`aria2_enabled=true` 时下载会探测 aria2c，探测不到自动降级为
cdsapi 单连接下载（功能不受影响）。本次所有基准用例 transport 全为 **cdsapi**，
未评估 aria2 多连接增益（需先安装 aria2 再补测）。

### 方法

- **基准 CLI**：`backend/scripts/diagnose_download.py --mock --benchmark ...`
  （复用生产链路：Normalizer 切块 → `CdsChannel.prepare_blocks` 造块 →
  `CdsChannel.run_blocks` 生产调度器：含重试 / 事件 / 断点续传）。
- **mock 参数**：`workers ∈ {2,4,6,8,10}` × `granularity ∈ {day, month}`；
  day 每用例 **40 块**、month 每用例 **12 块**（2025 年共 12 个月，`--blocks 40`
  时 month 取 min(40,12)=12）；每块模拟耗时 **0.6 s**（`--mock-delay 0.6`）。
- 为什么 mock-delay 用 0.6 s：Windows 进程池 spawn 开销 ~0.1 s 量级；默认
  0.02 s 会让矩阵被启动开销主导、吞吐随并发反降，看不到调度曲线。调大到
  0.6 s（≫ spawn 开销）才能反映并发调度信号。生产运行路径不受影响。
- 每次用例在临时目录运行，断点续传不跨用例；全部 `failed=0 / retried=0`。

---

## 2. T2 — mock 并发×粒度矩阵（已落盘 CSV）

数据源：`backend/bench_output/bench_mock_matrix.csv`（day 40 块 / month 12 块）。

| 粒度 | workers | 总耗时(s) | 吞吐(块/分) | 单块均值(s) | 失败 | 重试 | 传输(aria2/cdsapi) |
|---|---|---|---|---|---|---|---|
| month | 2 | 4.26 | 168.91 | 0.36 | 0 | 0 | 0/12 |
| month | 4 | 2.52 | 285.85 | 0.21 | 0 | 0 | 0/12 |
| month | 6 | 2.05 | **350.70** | 0.17 | 0 | 0 | 0/12 |
| month | 8 | 2.18 | 330.28 | 0.18 | 0 | 0 | 0/12 |
| month | 10 | 2.26 | 318.50 | 0.19 | 0 | 0 | 0/12 |
| day | 2 | 12.76 | 188.15 | 0.32 | 0 | 0 | 0/40 |
| day | 4 | 6.78 | 354.01 | 0.17 | 0 | 0 | 0/40 |
| day | 6 | 5.09 | 471.70 | 0.13 | 0 | 0 | 0/40 |
| day | 8 | 4.00 | 599.27 | 0.10 | 0 | 0 | 0/40 |
| day | 10 | 3.64 | **659.63** | 0.09 | 0 | 0 | 0/40 |

### 结论

- **day 粒度下并发扩展近线性（调度模型健康）**：w2→w10 墙钟 12.76→3.64 s
  （3.50×），吞吐 188.15→659.63 块/分（3.51×）。w2→w4 吞吐几乎翻倍
  （+88%），之后增幅递减（w4→w6 +33%、w6→w8 +27%、w8→w10 +10%）——
  无拐点反转，说明生产调度器在块数充足时能稳定吸收更高并发。
- **month 粒度块少（12 块）时 w6 见顶**：w2→w6 吞吐升至峰值 350.70，w8/w10
  反而回落（330.28 / 318.50）。原因：仅 12 块时队列很快排空，再加工人只剩
  spawn 开销，属"块少场景加并发无收益"的正常现象，不代表调度退化。
- **全部 0 失败 / 0 重试**：mock 下载链路无异常，重试逻辑未被误触发。

---

## 3. T4 — 多任务并发稳定性（已落盘，探针已正式化）

场景：mock 下用**生产 Orchestrator**同时启动 **3 个下载任务**（变量互不相同以
避免缓存路径冲突），每任务 day 粒度 **8 块**；channel 注入**确定性 429 风暴**
（`fail_rate=0.4` + 生产 `worker_cfg` 固定 `seed=7` → `Random(7)` 前两次
draw<0.4、第三次 ≥0.4 → 每块 429×2 后于第 3 次尝试成功；既制造 429，又在
`retry_max=3` 下绝不重试耗尽，正中历史"429 误判为重试耗尽"故障点）。

结果：

- **3 任务全部 SUCCESS，`done=8/8`**，无 BrokenProcessPool、无文件锁
  PermissionError、无残留 `.tmp`、无损坏 JSON（task.json / manifest.json /
  events.jsonl 完整可解析）。
- events 中出现"等待…重试"退避日志（429 判定正确、退避恢复正常），且未出现
  重试耗尽。
- **IS_T4_PASS: YES**（原探针判定输出）。

探针处置：原一次性文件 `scripts/_t4_concurrency_probe.py`（声明"用后即删"）
**正式化迁移为可复用回归工具 `backend/scripts/check_concurrency.py`**：
增加 argparse（`--vars / --fail-rate / --mock-delay / --retry-max /
--start / --end / --timeout`，默认值保持与原 T4 场景完全一致），模块级
docstring 改写为运维说明并保留校验逻辑（终态/块统计/落盘完整性/.tmp 残留/
异常痕迹扫描），退出码 0=通过（`IS_CONCURRENCY_PASS: YES`）、1=发现问题。
README（`backend/scripts/README.md`）已补一节使用说明。重跑验证
`IS_CONCURRENCY_PASS: YES`（见 §6 冒烟）。旧探针与项目根一处误落盘的小文件
（`0`，19 B 调试输出）已删除。

---

## 4. T1 — diagnose_download.py benchmark CLI 修复说明

- **此前问题**：脚本 docstring 已声称支持 `--benchmark --workers ...`，但
  `main()` 中未接线分发，属于"文档跑不通"的半成品。
- **本次修复**：`main()` argparse 完整声明（`--benchmark/--workers/--granularity/
  --blocks/--csv/--json/--probe-aria2/--yes/--var/--year/--month/--area/
  --aria2/--mock-delay`），并在普通诊断路径之前分发
  `if args.probe_aria2: ... ; if args.benchmark: return _run_benchmark(args)`。
- `_run_benchmark` 覆盖：真实模式前置检查（缺 `.cdsapirc` / 缺 cdsapi 提前
  退出，避免在子进程被判可重试而空等 3.5 分钟）、真实模式配额估算确认
  （`--yes` 跳过）、按 (granularity × workers) 逐用例跑生产 `run_blocks`、
  输出报告 + 可选 CSV/JSON、推荐值（failed==0 中吞吐最高者；落上限提示可加
  并发；重试率 >20% 提示 429 限速）。
- **用法示例**（本报告全部表格即此命令 mock 版产出）：

```bash
cd backend
C:/Users/lei/.workbuddy/binaries/python/envs/default/Scripts/python.exe \
  scripts/diagnose_download.py --mock --benchmark \
  --workers 2,4,6 --granularity day,month --blocks 40 --csv bench.csv
# 可选加 --mock-delay 0.6 控制单块模拟耗时（矩阵实测用 0.6）
```

---

## 5. T3 — 真实 CDS benchmark 状态（待真实环境验证）

- **未完成**：真实矩阵因 CDS 服务端排队/限速且执行会话被中断，未产出完整数据；
  收尾阶段为避免消耗配额不再重跑。
- **已完成过的真实单块探针**：真实单块下载成功，**约 27.7 KB / 3.28 s 下载**，
  证明真实凭据、链路、脚本真实路径可用。
- **建议补跑命令**（小体积、控配额；day+month 各 2 块 → 每 workers 组合 4 个
  请求左右，先看是否触发 429）：

```bash
cd backend
C:/Users/lei/.workbuddy/binaries/python/envs/default/Scripts/python.exe \
  scripts/diagnose_download.py --benchmark --workers 4,6,8 \
  --granularity day,month --blocks 2 --yes
```

> 判读提示：报告 `retried>20%` 提示已触 CDS 限速，该 workers 组合即为本机 +
> 账号配额下的实际上限；反之若 0 重试且吞吐仍随 workers 上升，说明还可继续上调。

---

## 6. T5 — 默认配置建议（仅建议，未落地）

**结论：维持 `cds_max_workers=6`、`chunk_granularity="day"` 为默认，不改
`backend/era5tool/config/settings.py`。**

依据：

1. day 粒度下 w6 吞吐 471.7 块/分（wall 5.09 s / 40 块），w8/w10 更高
   （599.3 / 659.6）——但 mock 只反映本机调度，**不建模 CDS 服务端在线请求
   限流**。CDS 单账号在线并发请求约束约 3–6，w6 已贴近该上限的安全边际；
   高于 6 在本机调度上有收益、但在真实 CDS 上大概率触发 429，收益需要真实
   benchmark 证明。
2. 真实更高 worker 收益未经验证前，**不宜贸然调高默认值**（避免把 429 风暴
   带进生产默认路径）。
3. `chunk_granularity="day"` 在矩阵中表现平稳（块数充足、调度近线性、断点
   续传粒度细），维持现状。
4. mock 0 失败 + T4 429 风暴无重试耗尽 → 重试/退避逻辑健康，默认 `retry_max`
   无需调整。

**可选调优（供有更高并发诉求的用户）**：本机想榨更高吞吐可试 w8/w10，但
真实 CDS 下请先跑 §5 小矩阵验证是否 0 重试；一旦 `retried>20%` 即已触限流，
回落 w6。aria2 安装后可补测多连接传输是否进一步减少单请求排队（需先
`--probe-aria2` 确认）。

**落地状态**：settings 默认值未改动；`diagnose_download.py` 允许通过 CLI
`--workers/--granularity/--aria2` 覆盖跑实测，不侵入生产默认。

---

## 7. 冒烟验证（本次收尾执行）

| 命令 | 结果 |
|---|---|
| `--probe-aria2` | aria2c 不可用（自动降级 cdsapi 单连接），退出 0 |
| `python scripts/diagnose_download.py --mock --benchmark --workers 4,6 --granularity day --blocks 4` | 接线可用（见下，退出 0） |
| `python scripts/check_concurrency.py`（正式化后重跑） | `IS_CONCURRENCY_PASS: YES`（退出 0） |

（冒烟矩阵小、`--mock-delay` 默认 0.02 s → 吞吐会被 spawn 开销压低属预期，
仅验证 CLI 接线与调度器跑通；定量结论以 §2 的 0.6 s 矩阵为准。）

## 8. 结论与遗留

- benchmark CLI 已接线并可跑（T1 ✅）；mock 矩阵数据落盘并给出调度结论
  （T2 ✅）；多任务并发稳定性验证通过且探针正式化（T4 ✅）；T5 维持
  `w6 + day` 默认并给出可选调优（✅ 建议，未落地）。
- **遗留**：
  1. 真实 CDS benchmark 未完成 → 择机用 §5 小矩阵补跑（0 重试再考虑上调并发）；
  2. aria2c 未安装 → 如需多连接增益评估，先按 `--probe-aria2` 提示安装；
  3. settings 默认值维持现状，若有真实数据证明更高并发 0 重试，可再更新默认。
- 改动文件：`backend/scripts/diagnose_download.py`（前会话，本次未改）、
  `backend/scripts/check_concurrency.py`（新增，由一次性探针正式化）、
  `backend/scripts/README.md`（补探针说明）、`docs/benchmark-results-2026-09-05.md`
  （本报告）、`backend/bench_output/bench_mock_matrix.csv`（T2 数据，前会话）；
  删除 `backend/scripts/_t4_concurrency_probe.py` 与项目根误落盘文件 `0`。
- 未改动：`orchestrator / cds_channel / task_store / settings` 默认值。
