# QA 测试报告 · ERA5-AItool（Round 1 + Round 2 回归）

> QA 工程师：严过关（Edward）· 验证日期：2026-08-16
> 验证对象：`backend/`（era5tool + tests）、`web/`、`config/`
> 验证方式：独立跑测试 + 核心模块代码审查 + 新增 49 条边界/错误路径测试 + 前端 build 复核
> 环境：官方隔离 venv `C:\Users\lei\.workbuddy\binaries\python\envs\default\Scripts\python.exe`；全程 mock/隔离目录，**未消耗任何真实 CDS 配额**，未触碰真实 `~/.cdsapirc`

---

## 1. 测试规模与通过率

| 项 | 数量 |
|---|---|
| 工程师现有测试 | 35 条（原样运行） |
| QA 新增测试（`tests/test_qa_*.py`，8 个文件） | 49 条 |
| 合计收集 | 84 条 |
| **通过** | **76 条（90.5%）** |
| **失败** | **8 条（全部为源码 bug 触发，非测试自身问题）** |

新增测试文件清单：
- `test_qa_resumable_failed.py`（2）失败块续传语义
- `test_qa_orchestrator.py`（9）无凭据/cancel/resume 状态机/错误码/并发上限
- `test_qa_nl.py`（9）LLM 非法 JSON 降级、need_info 流转、`_clean_json`
- `test_qa_geometry.py`（9）area 顺序 [N,W,S,E]、跨 0° 经线、边界校验、时间
- `test_qa_varmap.py`（7）短码整词匹配防误伤、family 过滤
- `test_qa_plot.py`（4）三类图、无效 profile 容错
- `test_qa_ws.py`（4）WS 订阅/事件结构/events.jsonl 持久化
- `test_qa_concurrency.py`（5）指数退避 30s×2ⁿ max600s jitter±10%

---

## 2. 发现的问题（智能路由判定）

### 判定：**Engineer（源码 bug，需修复）**

| # | 严重度 | 文件:行 | 问题 | 最小复现 | 期望 vs 实际 |
|---|---|---|---|---|---|
| **BUG-1** | **P1 数据完整性** | `core/resumable.py:61-64,58-59,67-78` | `mark_failed` 与 `mark_done` 写**同一路径** `.done`；`is_done()` 只查文件存在 → **失败块被当作 done 跳过**，resume 永不重下失败块（§8.2 契约「重下 failed/missing 块」被破坏） | `store.mark_failed("2m_temperature/2020/01", ...)` 后 `pending_blocks(BLOCKS)` 不含该块 | 期望：失败块仍在 pending；实际：被跳过 |
| **BUG-2** | **P1 resume 完全失效** | `core/orchestrator.py:104` + `acquisition/cds_channel.py:53` | `resume()` 从 `task.params["blocks"]` 取**原始块**（submit 时存的是 `cds_req.blocks`，无 `request`/`rel_target`）；`_fetch_one_block` 访问 `block["rel_target"]` → KeyError → 所有待下块失败 | 失败任务 POST `/api/download/{id}/resume` → 仍 failed（error=BUSY_AFTER_RETRIES，failed_blocks=全部块） | 期望：resume 重下失败块 → success；实际：全部块 KeyError 失败 |
| **BUG-3** | P2 错误码契约 | `api/download_routes.py:23`（`plot_routes.py:36,41` 同） | 路由内 `RequestSchema(**dict)` 抛 pydantic `ValidationError` 未被捕获 → 落到通用 handler → **5000** 而非契约 **1001**（§5） | POST submit `{"dataset_family":"land","pressure_levels":[850]}` → code 5000 | 期望 1001；实际 5000 |
| **BUG-4** | P2 schema 契约 | `config/schema.py:53-63`（Area） | `Area` 无 `west/east∈[-180,180]`、`south/north∈[-90,90]` 边界 Field（§7.3 明确约束） | `Area(west=200)` 被接受 → 坏 CDS 请求 | 期望 pydantic 拒绝；实际接受 |
| **BUG-5** | P2 出图容错 | `plot/profiles.py:80-92` + `plot/engine.py:48-50` | profile 文件损坏（非法 JSON）→ `JSONDecodeError` 未捕获 → **5000** 而非 **3001** 或回退默认 profile（R6） | 写 `corrupt.json` 内容 `{ not json`，render profile=corrupt → code 5000 | 期望 3001 或回退；实际 5000 |

### 判定：**QA（测试自身 bug，已自行修复）**
- `test_qa_varmap.py::test_short_code_msl_whole_word`：误把 `mslp`（本身是 `mean_sea_level_pressure` 的合法同义词）当作「应不匹配」用例 → 已改为 `xmsl` 验证整词边界。
- `test_qa_orchestrator.py` 两处 cancel/resume 用例的 PENDING 取消竞态 → 改为「先等 running 再 cancel」+ 等待原线程退出，已稳定。

### 判定：**NoOne（通过/仅备注）**
- 详见 §4 备注项（不阻塞，建议后续加固）。

---

## 3. 前端 build 复核

```
cd web && npm run build
tsc --noEmit -p tsconfig.json  ✅
vite build                      ✅ 1010 modules, dist/ 产出成功
```

- 前端 `api/client.ts` 与后端路由**一一对应**：`/api/nl/parse`、`/api/nl/clarify`、`/api/download/{submit,list,{id},cancel,resume,delete}`、`/api/plot/{render,profiles}`、`/api/account/{status,validate,finalize,credentials,test-download}`、`/api/config/{,variable-map,llm}`，WS `/ws/tasks`（`ws.ts` 带重连与 subscribe）——路径/方法全部匹配。
- 统一响应解包 `{code,data,message}` 在前端拦截器实现，code≠0 抛业务错误。

---

## 4. 代码审查备注（不阻塞，建议工程师后续加固）

| # | 位置 | 备注 |
|---|---|---|
| N1 | `core/orchestrator.py:74-93` | `cancel()` 对 **PENDING** 任务直接置 PAUSED，但 submit 已启动的后台线程仍会启动并 `PAUSED→RUNNING`（随后因 cancel.flag 回到 paused）；存在短暂「running」与双线程窗口。建议 `_run_download` 线程启动时先检查 cancel.flag。 |
| N2 | `core/orchestrator.py:95-109` | `resume()` 未同步把状态置 RUNNING（在后台线程里才转移）→ 连续两次 resume 可能双线程重复下载。建议在锁内同步置 RUNNING 后再起线程。 |
| N3 | `core/normalizer.py:74-96` | land 家族未实现「必要时按 10 天块」切分（§3.4/R2），仍为变量×年×月。0.1° 月文件可能过大。设计偏差，非阻塞。 |
| N4 | `acquisition/cds_channel.py:146-151` | cancel 时未收集已取消 future 的结果 → `paused_blocks` 计数可能为 0（事件/状态展示不精确）。 |
| N5 | `core/task_store.py:78-88` | `delete(delete_files=True)` 先 `rmtree(task_dir)` 再 `self.get()` 返回 None → 缓存产物清理实际不执行。 |
| N6 | `nl/variable_map.py:111-115` | 单字符 ASCII 短码（`e`/`r`/`q`）在 `_matches` 中 `len<2` 恒 False → 永不匹配（防误伤目标达成，但该词典条目实际不可用，属可接受设计取舍）。 |
| N7 | `core/resumable.py:26` | `ResumableStore.__init__` 的 `settings` 参数未使用（无害，可清理）。 |

---

## 5. 遗留问题清单（Round 1 结束时）

- **8 个失败测试全部对应源码 bug（BUG-1 ~ BUG-5）**，已给出文件:行与最小复现，路由至工程师修复。
- 修复后需回归：`test_qa_resumable_failed.py`、`test_qa_orchestrator.py`（4 条）、`test_qa_geometry.py`（2 条）、`test_qa_plot.py`（1 条）。
- 无凭据/非法 NL/取消/WS 事件/短码防误伤/三类图/退避等其余 76 条全部通过。

## 6. Round 1 结论

- **判定：FAIL（8 个源码 bug 待工程师修复）**
- 核心结论：主链路（NL 规则解析、submit→success、出图三类图、WS 广播、前端构建）**可用**；但 **resume 断点续传存在两个 P1 级缺陷（失败块被跳过 + resume 传入原始块导致 KeyError）**，属必须修复项。

---

# Round 2 回归（工程师修复后）

## 7. Round 2 结果

| 项 | 结果 |
|---|---|
| Round 1 的 8 个失败测试（BUG-1~5） | **全部通过（8/8）✅** |
| 全量回归 | 1 次 **83/84**、后续 3 次中 2 次失败（**flaky**） |
| 失败测试 | `test_qa_orchestrator.py::test_resume_completed_task_rejected` |
| 失败根因 | **新发现 P1 源码 bug：任务 ID 碰撞**（非 Round 1 缺陷，由 N1 修复暴露） |

## 8. 新发现问题（Round 2，路由判定：Engineer）

| # | 严重度 | 文件:行 | 问题 | 复现 | 期望 vs 实际 |
|---|---|---|---|---|---|
| **BUG-6** | **P1 数据损坏** | `core/task_store.py:17-20`（`new_task_id`） | 任务 ID 仅为**秒级时间戳** `t_YYYYMMDD_HHMMSS`，无随机/序号 → **同一秒内多次 submit 生成相同 ID**，后建任务覆盖前者 `task.json`、共享目录（cancel.flag/.done/events.jsonl 交叉污染） | 快速连续 `o.submit()`×2 → 两次返回相同 `t_20260816_074230`（已实测） | 期望 ID 唯一；实际相同。生产上同秒两次提交即互相破坏 |
| 连带 | — | `core/orchestrator.py:155-161`（N1） | N1「启动查 cancel.flag」在新逻辑下放大了 BUG-6：复用被取消任务目录的新任务会读到残留 `cancel.flag` → 误判「未启动已取消」→ 任务停在 paused | 全量回归中 `test_resume_completed_task_rejected` 偶发看到 paused | 期望 success；实际 paused（因为复用了带 cancel.flag 的同 ID 目录） |

**建议修复**（工程师侧，一行级）：`new_task_id()` 追加随机后缀，例如
`return f"t_{ts}_{uuid.uuid4().hex[:8]}"`（或线程安全计数器/`secrets.token_hex`）。
修复后本报告 §8 相关测试应全绿；建议团队对该修复做一次快速复验（非 QA 第 3 轮，由工程师自测或另行安排）。

## 9. Round 2 结论

- Round 1 的 5 个源码缺陷（P1×2 + P2×3）**已全部修复并验证通过**；工程师额外修复的 N1/N2/N4/N5 与 `core/events.py` 写锁亦生效（WS/事件持久化测试全绿）。
- **新暴露 P1（BUG-6 任务 ID 碰撞）**：真实生产缺陷（同秒提交互相覆盖），且使全量回归 flaky。
- **最终判定：FAIL（Round 2 有 1 个开放 P1 Known Issue）** —— 已按 2 轮硬上限归档，路由至工程师修复，不再进入第 3 轮 QA 循环。


## 10. Round 2 收尾（BUG-6 修复后复验）

| 项 | 结果 |
|---|---|
| `new_task_id()` 代码审查 | ✅ `core/task_store.py:17-26`：秒级时间戳 + 8 位 UUID 后缀，含说明注释 |
| 唯一性实测 | ✅ 快速连续 200 次 → **200/200 唯一**（修复前 5/5 相同）；格式仍以 `t_` 开头，现有断言兼容 |
| 全量回归 | ✅ 连续 3 次 **84/84 passed**（16.2s / 17.1s / 16.3s），`test_resume_completed_task_rejected` 不再 flaky |
| Round 1 8/8 修复回归 | ✅ 保持通过 |

## 11. 最终结论

**PASS（84/84 全绿稳定）**。Round 1 的 5 个源码缺陷（P1×2 + P2×3）与 Round 2 新发现的 P1（BUG-6 任务 ID 碰撞）已全部修复并验证；工程师额外修复的 N1/N2/N4/N5 与 `core/events.py` 写锁均生效。无遗留 Known Issue，QA 验证闭环完成。
