# backend/scripts — 运维 / 诊断脚本

## diagnose_download.py — CDS 下载「速度 / 失败原因」一键诊断

用户实测出现「真实模式下载失败」（manifest 大量 `[Errno 2] No such file` +
`process pool terminated abruptly`）后新增的诊断工具。纯标准库 + cdsapi + 项目
`is_retryable_error`，不依赖测试框架，可独立运行。

### 作用

1. **检查 `~/.cdsapirc`**：存在性 + url/key 格式。兼容两种凭据：
   - key 含冒号（`UID:APIKEY` 旧式）→ `cdsapi.Client`；
   - key 无冒号（新 CDS v2 纯 token）→ `LegacyClient`（cdsapi 0.7.7 自动选择）。
2. **真实极小请求探测**：1 变量 × 1 月 × 前 3 天 × 1 个 time 步 × 5°×5° area
   （默认 `reanalysis-era5-single-levels` / `2m_temperature` / 2025-01）。
   测量并打印：
   - 提交(POST)耗时；
   - 排队→运行→完成各阶段耗时（状态机轮询）；
   - 下载速率（MB/s，落盘文件大小实测）；
   - 请求 ID / 最终状态。
3. **失败详情**：异常类型 / str / status_code / response 前 500 字符，并按
   后端 `is_retryable_error` 同一判定逻辑给出「是否可重试」结论
   （可重试 → 后端会自动退避重试；不可重试 → 需人工处理）。
4. **诊断结论表**：配置 OK / 凭据 OK / 网络延迟 / CDS 状态 / 预计 24 块总耗时
   （24 个独立请求按 4 并发 ≈ 6 轮 × 单块耗时粗估）。

### 运行

```bash
cd backend

# 真实网络探测（默认；需已配置 ~/.cdsapirc）
C:/Users/lei/.workbuddy/binaries/python/envs/default/Scripts/python.exe scripts/diagnose_download.py

# 换数据集（例如 Land）
C:/Users/lei/.workbuddy/binaries/python/envs/default/Scripts/python.exe scripts/diagnose_download.py --dataset reanalysis-era5-land

# 离线验证脚本自身（FakeCdsClient，不触网）
C:/Users/lei/.workbuddy/binaries/python/envs/default/Scripts/python.exe scripts/diagnose_download.py --mock

# 机器可读输出（供日志/自动化消费）
C:/Users/lei/.workbuddy/binaries/python/envs/default/Scripts/python.exe scripts/diagnose_download.py --json
```

### 退出码

- `0`：链路通（极小请求 completed 且已下载成功）。
- `1`：配置缺失 / 凭据错误 / 网络失败 / CDS 任务失败 / 超时。

### 注意事项

- 真实探测会向 CDS 提交一个真实请求（极小，配额消耗可忽略）；CDS 排队
  时长取决于服务端负载，脚本有 30 分钟兜底超时。
- 脚本使用 `wait_until_complete=False` + 手动轮询以记录阶段耗时，与后端
  `Client.retrieve(name, request, target)`（默认阻塞到完成）测量等价。
- 若 `--json` 模式下也打印了完整报告，说明错误发生在极早期（如未装 cdsapi）。

## check_concurrency.py — 多任务并发 + 429 退避稳定性回归探针

背景：多任务并行下载曾出现进程池破裂（BrokenProcessPool）、文件锁冲突
（PermissionError）、残留 `.tmp` 等问题。本探针在 mock 下用**生产 Orchestrator**
同时启动 N 个下载任务并注入确定性 429 风暴，作为可重复的并发回归检查
（相关实测结果见 `docs/benchmark-results-2026-09-05.md` §T4）。

原理：
- 生产 `CdsChannel.worker_cfg` 固定 `seed=7`；配合默认 `fail_rate=0.4`，
  `Random(7)` 前两次 draw < 0.4、第三次 ≥ 0.4 → 每个块**确定性** 429×2 后于
  第 3 次尝试成功（既是 429 风暴，又在 `retry_max=3` 下绝不重试耗尽）。
- 校验：无 BrokenProcessPool / 无 PermissionError / 无残留 `.tmp`；
  `done == total`；task.json / manifest.json / events.jsonl 完整可解析；
  events 含"等待…重试"退避日志（429 判定 + 退避恢复正常）。

```bash
cd backend

# 全默认：3 任务 × 8 块/任务（2025-01-01..08，day 粒度），fail_rate=0.4
C:/Users/lei/.workbuddy/binaries/python/envs/default/Scripts/python.exe scripts/check_concurrency.py

# 自定义：变量数决定任务数（建议不同变量避免缓存路径冲突）
C:/Users/lei/.workbuddy/binaries/python/envs/default/Scripts/python.exe scripts/check_concurrency.py --vars 2m_temperature,sea_surface_temperature --fail-rate 0.3 --retry-max 4
```

退出码：`0` = 通过（`IS_CONCURRENCY_PASS: YES`）；`1` = 发现问题（打印清单）。
仅 mock、不触网、不耗配额；在临时目录运行，结束后自动清理。
