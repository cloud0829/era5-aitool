# ERA5-AItool · 实验验证结果（EXPERIMENT_RESULTS.md）

> 环境：官方隔离 venv `C:\Users\lei\.workbuddy\binaries\python\envs\default`
> 运行日期：2026-08-16 · 全部 mock 离线运行（`--real` 需凭据，见各节「真实模式待凭据」）
> 状态：**E1–E5 全部 PASS**

## 总览

| # | 实验 | Mock 断言 | 结果 | 关键数字 |
|---|---|---|---|---|
| E1 | CDS 并发基准 | 12/12 | ✅ PASS | 并行/串行耗时比 0.25（≈3.95× 提速） |
| E2 | ERA5-Land 参数探测 | 10/10 | ✅ PASS | land 无气压层；monthly 无 day |
| E3 | NL→Schema 30 样例 | 5/5 | ✅ PASS | JSON 解析率 100%；字段正确率 155/155=100% |
| E4 | 出图管线最小验证 | 6/6 | ✅ PASS | png 33–47KB、gif 201KB/10 帧；cartopy 可用 |
| E5 | 状态机+断点续传+并发上限 | 11/11 | ✅ PASS | 36 块各下载 1 次；峰值=4；退避 30/60/120 |

---

## E1 · CDS 并发基准（e1_cds_parallel_bench.py）

**目的**：证明「多进程并行提速且不封禁」，校准 `cds_max_workers` / 退避参数。

**通过标准（mock）**：① 切块总数=36；② 并行耗时 ≤ 串行/2；③ 失败块指数退避（30s/60s/120s±jitter）；④ 全部块 done；⑤ 并发峰值 ≤ max_workers。

**实际结果：PASS（12/12）**

**关键数字**：
- 串行 54.18s → 并行(4 workers) 13.70s，耗时比 **0.25**（≈3.95× 提速，超过 2× 标准）
- 并发峰值：串行=1，并行=4（= max_workers，未超限）
- 场景B1（fail_rate=0.1, seed=42）：重试块 1/36，退避序列 [30.105]s 落在 30±10% 内
- 场景B2（fail_rate=1.0, retry_max=3, 6 块）：退避档位覆盖 30s 与 60s（27.2–31.9 / 56.2–63.7），6/6 重试耗尽→failed

**修复记录**：
- 每块使用确定性独立种子（`base + crc32(key)`），修复此前所有 worker 首抽同一随机值导致 fail_rate 不生效、重试数=0 的问题
- 退避序列断言改为「按块分组」，避免多块交错日志被误判为非指数
- 事件模型统一为 start 每块 1 次 + attempt 信息事件，修复重试时并发峰值漂移

**真实模式待凭据**：`--real` 需 `~/.cdsapirc`；本机未配置 → 自动标注待凭据并跳过。

**降级说明**：mock 实际 sleep 按 `--sleep-scale`（默认 0.001）缩放到 ≤0.05s/次，日志记录理论退避值（30/60/120s）并据此断言；`--real` 才用真实 sleep。

---

## E2 · ERA5-Land 数据集探测（e2_era5land_probe.py）

**目的**：确认 CDS 上 `reanalysis-era5-land`（hourly）与 `reanalysis-era5-land-monthly-means` 的请求参数，固化 §3.4 family 表与 variable_map。

**通过标准（mock）**：① 核对表输出；② land 系列无 pressure_levels；③ hourly 有 time 24 时次、monthly-means 无 day；④ build_cds_request 与核对表一致；⑤ mock info() 返回 0.1° 且无气压层。

**实际结果：PASS（10/10）**

**关键数字**：
- `reanalysis-era5-land`：grid=0.1，time 24 时次，有 day，无 pressure_levels
- `reanalysis-era5-land-monthly-means`：grid=0.1，time=[00:00]，无 day，无 pressure_levels
- area 顺序 [north, west, south, east] = [34.0, 118.0, 29.0, 123.0]（华东示例）
- build_cds_request 请求键集与核对表一致：{area, day, format, month, product_type, time, variable, year}

**真实模式待凭据**：`--real` 需 `~/.cdsapirc` 调 `client.info()` 在线比对；本机未配置 → 待凭据跳过。

**降级说明**：无（mock 使用内置离线核对表，与官方已知信息一致）。

---

## E3 · DeepSeek NL→Schema 30 样例（e3_nl_schema_samples.py）

**目的**：验证 NL 解析管线稳定产出合规 JSON：合法解析、Schema 校验、need_info 多轮流转、confidence<0.7 进确认、非法 JSON 降级不崩溃。

**通过标准（mock）**：① 合法 JSON 解析率 100%；② 完整样例字段正确率 ≥80%；③ need_info 正确返回 missing/questions 且 ≤3 问；④ confidence<0.7 进确认分支；⑤ 非法 JSON 触发重试后降级，不崩溃。

**实际结果：PASS（5/5）**（官方 venv，真实 pydantic 2.13.4）

**关键数字**：
- 合法 JSON 首轮解析率 **100%**（legal=22/22，含 Markdown 围栏清洗；need_info/invalid 不参与）
- 完整样例字段正确率 **100%**（matched=155/155）
- need_info 样例 5/5 正确返回 missing/questions 且 ≤3 问
- confidence<0.7 样例 2/2 进入确认分支
- 非法 JSON 样例 3/3 触发重试后降级规则/表单，不崩溃

**真实模式待凭据**：`--real` 需 `DEEPSEEK_API_KEY`（环境变量或 `config/.env`）；本机未配置 → 待凭据跳过。

**降级说明**：mock 使用 `mocks/fake_llm.py` 预置合法/need_info/非法三类响应，零外部网络。

---

## E4 · 出图管线最小验证（e4_plot_minimal.py）

**目的**：合成数据验证三类图管线（空间图/时序图/动画）与离线底图策略（cartopy 国内可用性）。

**通过标准**：① 空间图 png（含 colorbar/标题/底图或海岸线）；② 时序图 png；③ 动画 gif（≥8 帧）；④ png < 5MB、gif < 20MB；⑤ 0.1° regrid 0.25° 后仍可出图。

**实际结果：PASS（6/6）**（官方 venv，**cartopy 0.25.0 可用**，无需降级）

**关键数字**：
- 输出文件：`map_025.png` 46.0KB、`timeseries_025.png` 32.7KB、`gif_025.gif` 201.2KB（10 帧）、`map_land_010_regrid_025.png` 46.6KB
- gif 帧数 10 ≥ 8（PIL 合成）
- 0.1° ERA5-Land 合成数据 regrid→0.25° 后仍正常出图（xarray interp，依赖 scipy）

**真实模式待凭据**：E4 全离线（合成数据），无真实凭据需求。

**降级说明**：cartopy 0.25.0 在 Python 3.13 有 cp313 wheel，本机已安装并实际用于底图；若目标环境无 cartopy wheel，E4 自动降级纯 matplotlib（仅海岸线/网格）。

---

## E5 · 任务状态机 + 断点续传 + 并发上限（e5_task_state_machine.py）

**目的**：验证 pending→running→success/failed/paused 流转、`.done`+`manifest.json` 断点续传、并发不超上限、退避生效。

**通过标准（mock）**：① 状态流转（含 cancel→paused、resume→success、重试耗尽→failed）；② 中断重跑跳过 done 块、缺失块重下；③ 并发峰值 ≤ max_workers；④ 退避序列指数（30/60/120s±jitter）；⑤ 全部断言通过。

**实际结果：PASS（11/11）**

**关键数字**：
- 场景1：36 块全部 done，status_history=[pending, running, success]，**每块 start 恰 1 次（blocks=36）**，并发峰值=4
- 场景2：第1次 stop_after=10 → paused（done=10/36）；第2次 resume → success（done=36/36）；**无重复下载（doubled=[]，missing=0）**
- 场景3（fail_rate=1.0, retry_max=4, 12 块）：12/12 failed，错误码 BUSY_AFTER_RETRIES；每块退避序列 [≈30, ≈60, ≈120]（如 [31.4, 65.2, 125.4]）；并发峰值=4

**修复记录（本轮）**：
1. `.done` 标记父目录缺失（key 含 `2m_temperature/2020/02` 子路径）→ 写入前 `ensure_dir(dirname)`
2. Windows 多进程写同一 JSONL 互相覆盖丢行（MSVC `_O_APPEND` 非原子）→ 改为**按进程分片**（`path.<pid>.part`），读时按 (ts,pid,seq) 合并
3. 事件模型：start 每块仅 1 次，重试记 `attempt` 信息事件 → 修复并发峰值从 16 漂移到 4
4. 场景3 retry_max 2→4 + 退避断言按块分组 → 序列真实呈现 30/60/120 指数档
5. 每场景运行前清理该任务输出目录，杜绝残留 `.done`/manifest 干扰断言

**真实模式待凭据**：`--real` 需 `~/.cdsapirc` 跑 1 块真实 retrieve；本机未配置 → 待凭据跳过。

**降级说明**：mock 实际 sleep 缩放 ≤0.05s/次，日志记录理论退避值断言；`--real` 才用真实 sleep。

---

## 环境说明（2026-08-16）

- 官方 venv：`C:\Users\lei\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
- Python 版本：3.13.12（`binaries\python\versions\3.13.12`）
- 已安装：xarray 2026.7.0 / numpy 2.5.2 / pandas 3.0.5 / matplotlib 3.11.1 / scipy 1.18.0 / cartopy 0.25.0 / cdsapi 0.7.7 / openai 3.1.0 / pydantic 2.13.4 / pillow 12.3.0
- cartopy：**可用**（cp313 wheel 存在，无需降级）
- 安装源：默认 pypi.org 极慢（~12kB/s），已改用阿里云镜像 `-i https://mirrors.aliyun.com/pypi/simple/`（~8MB/s），README 已同步
- `--real` 待凭据项：E1（~/.cdsapirc）、E2（~/.cdsapirc）、E3（DEEPSEEK_API_KEY）、E5（~/.cdsapirc）；E4 无真实凭据需求

## 复现命令

```bash
cd experiments
C:/Users/lei/.workbuddy/binaries/python/envs/default/Scripts/python.exe e1_cds_parallel_bench.py
C:/Users/lei/.workbuddy/binaries/python/envs/default/Scripts/python.exe e2_era5land_probe.py
C:/Users/lei/.workbuddy/binaries/python/envs/default/Scripts/python.exe e3_nl_schema_samples.py
C:/Users/lei/.workbuddy/binaries/python/envs/default/Scripts/python.exe e4_plot_minimal.py
C:/Users/lei/.workbuddy/binaries/python/envs/default/Scripts/python.exe e5_task_state_machine.py
```

结果 JSON：`outputs/e1_result.json` … `outputs/e5_result.json`；日志：`outputs/e*_calls.jsonl`（按进程分片合并）。
