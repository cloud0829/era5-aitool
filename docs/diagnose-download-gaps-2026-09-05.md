# 下载缺口问题 · 诊断与修复报告

- 日期：2026-09-05
- 报告人：齐活林（交付总监）
- 状态：**根因实证确认，修复已完成（待全量回归）**
- 用户原始描述：「任务下载的进程池有问题，数据有的下不上而且会跳着时间下」

---

## 一、物证：下载产物大面积缺失

任务请求：变量 `10m_u_component_of_wind` + `10m_v_component_of_wind`，时段 2005–2025（21 年）。
按 month 粒度应为 **每变量 252 个 .nc 文件**。

### 实得分布（`data/cache/reanalysis-era5-single-levels/`）

```
### 10m_u_component_of_wind   → 实得 52 / 应得 252（缺 200，79%）
  2005 (2): 04 08
  2006 (5): 02 03 04 06 07
  2007 (7): 04 05 06 08 09 10 12
  2008 (5): 01 02 03 08 12
  2009 (3): 01 02 03
  2010 (6): 06 07 08 09 10 11
  2011 (1): 02
  2012 (0):   2013 (0):   2014 (0):        ← 连续三年整年为 0
  2015 (2): 05 09
  2016 (0):                                 ← 整年为 0
  2017 (4): 06 07 08 09
  2018 (9): 02 03 04 05 06 07 09 11 12
  2019 (4): 05 06 10 11
  2020 (0):                                 ← 整年为 0
  2021 (2): 03 12
  2022 (2): 07 10
  2023 (0):   2024 (0):   2025 (0):         ← 连续三年整年为 0

### 10m_v_component_of_wind   → 实得 0 / 应得 252（100% 缺失）
  年目录已创建：2005 2006 2007 2008 2009 2010 2011 2015 2016 2017
  但其中 .nc 文件数 = 0

### 10m_wind_speed
  出现名为 `*` 的非法目录（疑似变量名/年份通配未展开）
```

---

## 二、关键线索

### 线索 1：目录建了但文件没落盘

`10m_v` 的年目录**全部存在**，说明 worker 已执行到
`cds_channel._fetch_one_block` L176 的 `ensure_dir(os.path.dirname(target))`
（该调用在 `retrieve` 之前）。

⇒ **排除**路径/建目录类缺陷，失败发生在 retrieve 请求本身或写盘阶段。

### 线索 2：块生成顺序按「变量外层循环」

`core/normalizer.py` L158-185：

```
for var in variables:        # ← 变量在外层
    for year in years:
        for month in months:
            blocks.append(...)
```

⇒ 全部 `10m_u` 的块排在列表最前，`10m_v` 的块全部排在其后。
配合 `cds_channel.run_blocks` 的 `remaining.pop(0)` 顺序提交，
先提交的 `10m_u` 优先占用全部并发额度。

### 线索 3：失败呈「先到先得」形态

- `10m_u`：**稀疏随机成功**（52/252）
- `10m_v`：**全灭**（0/252）

这是典型的「有限并发额度被先到者耗尽，后到者饿死」特征，
而非「数据本身不可用」（否则 `10m_u` 也应全灭）。

### 线索 4：提速配置变更

上次会话（2026-09-05）为提速做了如下调整：

| 参数 | 原值 | 现值 | 影响 |
|---|---|---|---|
| `cds_max_workers` | 4 | **6** | 并发 +50% |
| `chunk_granularity` | month | **day** | 块数暴涨数十倍 |
| `submit_stagger_s` | — | 1.0 | 抖动不足削峰 |
| `retry_max` / `backoff_base` | 3 / 30s | 不变 | 退避 30/60/120s |

块数预估：month 粒度 252 块/变量 → day 粒度可达 **数千块**。

---

## 三、根因结论（已实证，非假设）

### R1 · 【主根因】CDS 队列限流用 HTTP 400 返回，被误判"不可重试"→ 一次都不重试直接永久失败

线上事故任务 `data/tasks/t_20260905_043416_261be78b/events.jsonl`（810 行）实证：

```
172 次  "The job has been rejected / Number queued requests for this dataset
        is temporarily limited."   ← 全部 172 个失败块
1 次    429
```

- CDS 对**单个数据集的排队请求数**有上限，超限返回 HTTP **400**（语义是"瞬时限流，
  稍等即可成功"），而非 429/5xx。
- 旧 `is_retryable_error` 按状态码把 400 一刀切判为**不可重试** →
  块**一次都不重试**，30/60/120s 退避完全没机会执行 → 172/172 本可恢复的
  瞬时错误全部永久失败。
- 该任务 `task.json` 最终：`total=264, done=29, failed=172`，且任务卡在
  `running` 残留（会话异常中断，进程池已死）。

### R2 · 【放大因素】块按"变量外层循环"排序 → 第二变量被饿死

`Normalizer._split_blocks` 是 `for var: for year: for month:`，变量 A 全部块排在
最前。配合 run_blocks 的 `remaining.pop(0)` 顺序提交，在队列墙下：
**先排的变量占满并发额度与 CDS 排队名额，后排变量一块都抢不到** →
线上 10m_u 拿到 29 块、10m_v **0 块**（事故 manifest 变量分布）。

### R3 · 【缺口无法自愈】失败即整任务 FAILED，resume 全量重跑加剧雪崩

`orchestrator` 汇总只要有 1 块失败 → 任务 FAILED；用户 resume → 又一轮全量并发
429/400 风暴 → 越补越堵，缺口永远补不齐（用户感知"跳着时间下"）。

---

## 四、修复方案（已实施）

| # | 修复 | 文件 |
|---|---|---|
| 1 | 错误分类重构：**瞬时文本优先于状态码**——400+queued/temporarily-limited → `queue_limited`（可重试+上全局闸）；纯 400 仍不可重试（不误伤） | `cds_channel.py` |
| 2 | **跨进程全局限流闸** `ThrottleGate`（文件原子写，data_dir 共享）：撞墙 → 全体 worker 退避（指数冷却 30→600s+抖动），把"越失败越猛冲"改成"撞墙集体退一步" | `core/throttle.py`（新） |
| 3 | **自适应并发**：撞墙降 1 在跑并发（下限 1），连续 6 块成功回升 1 | `cds_channel.run_blocks` |
| 4 | **提交顺序去偏**：跨变量/跨年轮转交错（纯排序，不影响断点续传 key） | `normalizer.interleave_blocks` |
| 5 | **部分成功语义**：`outcome=all_success/partial_success/all_failed` + `failure_summary` 失败原因分类计数 + hint 引导 | `orchestrator` |
| 6 | **一键补漏** `POST /download/{id}/retry-failed`：清失败标记、只重下失败块（done 块 0 重复下载） | `download_routes.py` + `resumable.clear_failed` + 前端按钮 |
| 7 | 失败块结果带 `error_category`/`throttled`，事件日志可回溯分类 | `cds_channel` |

配置（settings，全部带默认值，老 settings.json 兼容）：
`block_interleave=True`、`throttle_enabled=True`、`throttle_base_s=30`、
`throttle_max_s=600`、`adaptive_concurrency=True`、`adaptive_min_workers=1`、
`adaptive_recover_every=6`。

---

## 五、改动文件清单

| 文件 | 改动 |
|---|---|
| `backend/era5tool/acquisition/cds_channel.py` | 错误分类重构（`classify_error`/`is_throttle_error`/`is_retryable_error`）、worker 内限流闸 wait/arm、`_build_worker_cfg` 兼容旧签名、结果带 `error_category`/`throttled` |
| `backend/era5tool/core/throttle.py` | **新增** 跨进程全局限流闸 `ThrottleGate`（原子文件写、指数冷却、惊群抖动） |
| `backend/era5tool/core/normalizer.py` | **新增** `interleave_blocks`（跨变量/跨年轮转交错，纯排序） |
| `backend/era5tool/core/orchestrator.py` | 提交前交错、`outcome` 三态、`failure_summary`、`retry_failed()`、部分成功写 `result.files`、`hint` |
| `backend/era5tool/core/resumable.py` | **新增** `clear_failed()`（只清失败标记，不动 done） |
| `backend/era5tool/config/settings.py` | 7 项新配置（交错/限流闸/自适应并发，均带默认值兼容旧 settings） |
| `backend/era5tool/api/download_routes.py` | **新增** `POST /download/{id}/retry-failed` 补漏 API |
| `backend/era5tool/acquisition/mock_client.py` | `QueueLimitedError` + `error_mode` 故障注入（复现线上 400 形态） |
| `web/src/api/client.ts` | **新增** `downloadRetryFailed` |
| `web/src/pages/TasksPage.tsx` | failed 任务操作位由"续传"改为"**补漏**（重下失败块）" |
| `tests/test_bugfix_download_gaps_classify.py` | **新增 14 例**：400 队列限流可重试 / 纯 400 不误伤 / 既有分类全回归 / mock 端到端 |
| `tests/test_bugfix_download_gaps_interleave.py` | **新增 11 例**：交错形态与不变量 / 前缀均衡 / e2e 高失败率两变量都有份 / 不影响断点续传 |
| `tests/test_bugfix_download_gaps_retry.py` | **新增 9 例**：ThrottleGate 单元 / clear_failed / partial_success 汇总 / 补漏状态守卫 / 补漏只重下失败块 e2e |

> 说明：初版取证报告中疑似的 `10m_wind_speed/*` 非法目录为 find 输出换行误读，实际不存在；`10m_wind_speed` 目录为正常后续任务产物。

## 六、测试基线

- 修复前：285 passed
- 修复后：**310+（全量回归最终数以 pytest 输出为准）**，含新增 34 例专项测试
- 前端：`npm run build` 通过
