# ERA5-AItool · 自然语言下载 + 出图工具

纯本地 Web 工具：**说一句话 → 下载 ERA5 / ERA5-Land 数据 → 出图**。
后端 FastAPI（`127.0.0.1:8000`）+ 前端 Vite/React（`127.0.0.1:5173`）同机运行。

- 单一 CDS 数据通道（并发 ≤4 + 指数退避 + `.done`/manifest 断点续传）
- 自然语言解析：DeepSeek（有 Key）/ 规则兜底（无 Key 离线可用）
- ERA5 与 ERA5-Land 双家族：0.25°/0.1° 网格、hourly/monthly、无气压层差异自动处理
- 三类出图：空间图 / 时序图 / 动画（cartopy 底图，离线降级纯网格）
- 账号半自动向导：`~/.cdsapirc` + 系统钥匙串，凭据绝不入库

> 权威设计：`docs/design-final.md`（v2 最终版）· 先行验证实验：`experiments/`（E1–E5 全 PASS）

## 目录结构

```
era5-AItool/
├── docs/                  # 设计文档（design-final.md 为权威施工依据）
├── backend/               # Python 后端（FastAPI）
│   ├── pyproject.toml     # 依赖分组 extras: [runtime,cds,nl,plot,account,all]
│   ├── era5tool/
│   │   ├── main.py        # 入口：路由 + WS + CORS + 静态托管
│   │   ├── api/           # nl/download/plot/account/config 五组路由
│   │   ├── core/          # orchestrator/normalizer/concurrency/resumable/events/task_store
│   │   ├── acquisition/   # cds_channel.py（唯一通道）+ cds_request.py + mock_client.py
│   │   ├── nl/            # llm_parser(DeepSeek)/rule_parser/session/parser/variable_map
│   │   ├── plot/          # engine(三类图)/profiles/regrid/colormaps/sample_data
│   │   ├── account/       # wizard/keyring_store/validate
│   │   ├── config/        # settings(pydantic-settings)/schema
│   │   └── models/        # 任务模型与状态机
│   └── tests/             # pytest 冒烟（35 用例）
├── web/                   # 前端（Vite + React18 + TS + Tailwind + MUI + Zustand）
│   └── src/               # 六页面：总览/对话/出图/配置/任务/向导
├── config/                # 用户级配置
│   ├── settings.json      # 并发/重试/mock 等（可被 PUT /api/config 修改）
│   ├── deepseek.example.env  # DEEPSEEK_API_KEY 模板（复制为 .env）
│   ├── plot_profiles/     # 出图 profile（default_map.json）
│   └── variable_map.json  # 变量映射词典（~25 词条，含 ERA5-Land）
├── data/                  # 运行时（gitignore）：cache/ tasks/ products/
└── experiments/           # 先行验证实验（E1–E5，全 PASS）
```

## 环境准备

```bash
# Python（官方隔离 venv）
C:/Users/lei/.workbuddy/binaries/python/envs/default/Scripts/pip install \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    "fastapi" "uvicorn[standard]" "pydantic-settings" httpx structlog python-dotenv \
    xarray numpy pandas matplotlib scipy cartopy cdsapi openai pydantic keyring netCDF4

# 前端
cd web
npm install            # 已配置 registry.npmmirror.com
```

## 启动

```bash
# 1) 后端（终端 A）
cd backend
C:/Users/lei/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m uvicorn era5tool.main:app --host 127.0.0.1 --port 8000

# 2) 前端（终端 B，开发模式）
cd web
npm run dev            # http://127.0.0.1:5173 （/api、/ws 已代理到 8000）

# 生产构建（可选，后端会自动托管 web/dist）
cd web && npm run build
```

健康检查：`curl http://127.0.0.1:8000/health` → `{"status":"ok"}`

## 配置

| 配置 | 位置 | 说明 |
|---|---|---|
| CDS 凭据 | `~/.cdsapirc`（向导生成，权限 600）+ keyring | 无凭据时下载走 mock 模式 |
| DeepSeek Key | `config/.env` 或环境变量 `DEEPSEEK_API_KEY` | 无 Key 时 NL 用规则兜底 |
| 应用配置 | `config/settings.json` / `PUT /api/config` | 并发/重试/mock 开关 |
| 出图配置 | `config/plot_profiles/*.json` | 热加载（mtime），面板覆盖优先 |
| 变量词典 | `config/variable_map.json` | 中英同义词 + 适用 family |

DeepSeek 配置模板：

```env
DEEPSEEK_API_KEY=sk-xxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

## REST / WS 契约

- 统一前缀 `/api`；响应统一 `{code, data, message}`（code=0 成功）。
- 错误码：1001 参数 / 1002 缺 CDS / 1003 缺 DeepSeek / 1004 LLM 解析失败 /
  2001 任务不存在 / 2002 状态不允许 / 3001 出图失败 / 4001 账号校验失败 / 5001 配置错误。
- 接口共 24 REST + 1 WS：NL(parse/clarify)、Download(submit/get/list/cancel/resume/delete)、
  Plot(render/profiles CRUD)、Account(status/validate/finalize/credentials/test-download)、
  Config(config/variable-map/llm)、`GET /health`、`WS /ws/tasks`（progress/status/log/done 事件）。
- 详细清单见 `docs/design-final.md` §5；前后端类型见 `web/src/types.ts`。

## 测试

```bash
cd backend
C:/Users/lei/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest tests/ -q
# 35 passed（health + 状态机 + normalizer + NL 规则 + resumable + plot + API 全链路，mock CDS）
```
