# ERA5 工具 · 可落地技术方法（Implementation Plan）

> 角色：架构师「高见远」 · 本文件是 `docs/system_design.md` 的深化版
> 定位：从"选什么技术"推进到"具体怎么做"——接口定义、JSON 结构、prompt 模板、伪代码级设计、参数默认值、验证方法。
> 约束：不含完整业务代码实现；以下内容均为可落地设计，工程师可直接按图施工。

---

## 目录
- A. 六大模块具体落地做法（CDS / GCS / NL / 出图 / 账号 / 前端）
- B. 系统级契约（REST API / 任务模型 / 目录结构）
- C. 里程碑实施计划（Phase 细化 + 先行验证实验 + 风险应对表）

---

# A. 六大模块具体落地做法

---

## A1. CDS 通道落地（cdsapi + 多进程并行 + 断点续传）

### A1.1 并发切分策略

**切分维度（优先级从高到低）**：`dataset → variable → year → month`，极端情况再按 `day/10day` 切。

| 数据集类型 | 建议切分粒度 | 理由 |
|---|---|---|
| reanalysis-era5-single-levels（地表/单层） | 单变量 × 单年 × 单月 | 单请求体积适中、失败重试代价小、满足 CDS 单请求文件大小限制 |
| reanalysis-era5-pressure-levels（气压层） | 单变量 × 单年 × 单月（必要时按 10 天块） | 多 level 文件大，需更细切分 |
| reanalysis-era5-land | 单变量 × 单年 × 单月 | 0.1° 网格文件大，细粒度更稳 |
| monthly-means 系列 | 单变量 × 单年（整年一次） | 已聚合，体积小 |

**进程池设计**：
```
生产者线程: 把请求拆成 N 个 block（year×month×variable）→ 放入并发队列
工作进程:  ProcessPoolExecutor(max_workers=4) 或 subprocess.Popen 独立进程执行 cdsapi retrieve
结果收集:  每块完成写 .done 标记 + 更新 manifest，全部完成触发合并/后处理
```
- 用 `subprocess.Popen` 起独立 Python 子进程执行 `cdsapi` 是首选：隔离 GIL 与依赖，崩溃不影响主进程；备选 `ProcessPoolExecutor`。
- **并发上限默认值：4**（`config.settings.download.cds_max_workers=4`）。
  - 理由：CDS 单用户常见并发限制在 4~8 之间；4 是安全值，既明显提速又不触发封禁；可配置，压测后上调。
- 每个 block 使用**独立 cdsapi Client**，避免共享连接状态。

### A1.2 CDS 限流规避（指数退避参数）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `retry_max` | 3 | 每块最多重试次数（不算首次） |
| `backoff_base` | 30s | 首次失败等待 |
| `backoff_factor` | 2 | 等待时间倍数：30s → 60s → 120s |
| `backoff_max` | 600s | 单次等待上限（10min） |
| `jitter` | ±10% | 随机抖动，避免多进程同时重试造成"重试风暴" |
| 触发条件 | HTTP 429 / 503 / 返回"system busy" / 网络超时 | 业务性错误（400 参数错）不重试，直接 failed |

伪代码级设计：
```
def download_with_retry(block, retry=0):
    try:
        client.retrieve(request, target_file)
        mark_done(block)
    except BusyError as e:
        if retry < retry_max:
            wait = min(backoff_base * (backoff_factor ** retry), backoff_max)
            sleep(wait * uniform(0.9, 1.1))
            return download_with_retry(block, retry + 1)
        else:
            mark_failed(block, e)
    except ParamError as e:
        mark_failed(block, e)   # 不重试
```

### A1.3 断点续传机制

**已完成标记**：
- 每块输出文件命名固定规则：`{dataset}/{variable}/{year}/{variable}_{year}_{month:02d}.nc`
- 块下载完成后写空文件 `{block_file}.done`（含块信息 JSON：文件大小、sha256、完成时间）。
- 顶层维护 `manifest.json`：

```json
{
  "task_id": "t_20250701_001",
  "blocks": [
    {"key": "t2m/2020/05", "file": "data/.../t2m_2020_05.nc", "size": 123456789,
     "sha256": "ab12...", "status": "done", "updated_at": "2025-07-01T08:00:00Z"},
    {"key": "t2m/2020/06", "file": "data/.../t2m_2020_06.nc", "size": 0,
     "sha256": null, "status": "failed", "error": "busy_after_3_retries", "updated_at": "2025-07-01T09:00:00Z"}
  ],
  "created_at": "2025-07-01T07:00:00Z",
  "updated_at": "2025-07-01T09:00:00Z"
}
```

**重跑/续传逻辑**：
1. 启动时读 manifest；`status=done` 且 文件存在 且 size 匹配 → 跳过。
2. `status=done` 但文件缺失/大小不符 → 重新下载该块（防"假完成"）。
3. `status=failed` / `missing` → 重新入队下载。
4. 全部块 done → 触发后处理（合并 + 出图/交付）。
5. 提供 `POST /api/download/{task_id}/resume` 显式续传接口；应用重启后自动扫描 `data/tasks/` 下的 manifest 恢复待续传任务。

### A1.4 进度事件结构（供 WebSocket 推送）

```json
{
  "type": "progress",
  "task_id": "t_20250701_001",
  "status": "running",
  "phase": "downloading",          // queued | downloading | merging | done
  "block_key": "t2m/2020/05",
  "block_index": 3,
  "block_total": 60,
  "progress": 0.45,                // 0~1 全局进度（含已跳过块）
  "message": "t2m 2020-05 下载中 45%",
  "ts": "2025-07-01T08:00:00Z"
}
```
事件类型：`progress`（下载进度）、`status`（任务状态迁移）、`log`（日志行）、`done`（携带 result 摘要）。

---

## A2. GCS 云通道落地（arco-era5 直读）

### A2.1 前置验证实验设计（关键！）

> 目的：在开发前用最小实验核实 arco-era5 的**覆盖范围 / 变量清单 / 更新延迟 / 网格类型**，据此固化路由规则（`system_design.md §4.2`）。绝不能凭二手信息假设。

**探测目标（bucket 结构）**：
```
gs://gcp-public-data-arco-era5/            # 桶顶层
├── ar/                                    # ARCO 常规处理
│   ├── full_37-1h-0p25deg-chunk-1.zarr    # 0.25° 规则网格
│   ├── full_37-1h-0p25deg-chunk-2.zarr
│   └── ...
├── co/                                    # 高斯网格 CO 处理
│   ├── full_37-1h-0p25deg-400x250.zarr
│   └── ...
└── ...
```

**探测步骤（伪代码级）**：
```
1. fs = gcsfs.GCSFileSystem(token='anon')
   dirs = fs.ls('gcp-public-data-arco-era5')            # 列出顶层目录
2. for each zarr path 候选:
      mapper = fs.get_mapper('gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr')
      ds = xr.open_zarr(mapper, consolidated=True)
      print(ds)                                          # 打印 Dataset
      print(list(ds.data_vars))                          # 变量清单
      print(ds.time.min().values, ds.time.max().values)  # 时间范围 → 与今天差值=更新延迟
      print(ds.latitude.values[:3], ds.longitude.values[:3])  # 网格类型/范围
3. 检查是否含 ERA5-Land：arco 官方主要提供 ERA5（单层+气压层），
   ERA5-Land 一般不在其中 → 验证结论记入覆盖矩阵
```

**输出「覆盖矩阵核对清单」**（模板，工程师逐项填写）：

| 探测项 | 探测路径/方法 | 预期/结论 | 对路由的影响 |
|---|---|---|---|
| 顶层目录清单 | `fs.ls(bucket)` | ar/ co/ 等 | 确认可用 store |
| 单层变量清单 | `ds.data_vars` | 含 t2m、sp、tp… | 变量白名单 |
| 气压层变量/层数 | 打开 pressure-levels store | 37 层？ | 决定 co/重采样 |
| 时间最新值 | `ds.time.max()` | 距今 X 天 | 路由时效阈值 |
| 时间最早值 | `ds.time.min()` | 1940-01-01？ | 覆盖范围 |
| 网格类型 | lat/lon 数组 | 0.25° 规则 / 高斯 | 是否需重采样 |
| 是否含 ERA5-Land | 顶层 ls 查找 land 关键字 | 大概率无 | Land → CDS |
| 变量缺失清单 | 与 CDS 变量全集比对 | 差集 | 差集 → CDS |

**通过标准**：覆盖矩阵填写完成；`ar` 0.25° store 可打开、变量清单可得、延迟量化（天）；结论能直接写成 `config/arco_coverage.json` 供路由读取。

### A2.2 高斯网格 → 0.25° 规则网格重采样

- 触发场景：走 `co/`（高斯网格）或气压层数据需要与规则网格统一时。
- 首选方案：**`xarray.interp`（线性插值）**——轻量、无额外依赖。
```
target_lat = np.arange(-90, 90.25, 0.25)   # 0.25° 规则网格
target_lon = np.arange(0, 360, 0.25)       # 注意经度约定：arco 常用 0~360
ds_r = ds.interp(latitude=target_lat, longitude=target_lon, method="linear")
```
- 备选方案：`xesmf`（保守/双线性 regrid）——当需要面积守恒（如降水总量）时用 `method='conservative'`。
- **注意事项**：
  1. 先 `ds = ds.sortby("latitude")` 排序坐标，避免插值错乱。
  2. 经度统一约定：目标网格与底图（cartopy）的经度范围一致（-180~180 或 0~360 二选一，出图层统一）。
  3. `interp` 不处理 NaN：先 `ds = ds.ffill("time")` 或对缺失区 mask，避免 NaN 扩散。
  4. 内存控制：用 dask chunk（`ds.chunk({"time": 100})`）或按时间分块插值。
  5. 默认策略：**尽量优先用 `ar/` 0.25° 规则网格 store**，只有用户要气压层细分时才走 `co/` + 重采样，把重采样场景压到最小。

### A2.3 本地缓存策略

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `cache.root` | `data/cache/` | 缓存根目录 |
| `cache.ttl_days` | 180 | 超过 N 天未访问的缓存可清理 |
| `cache.max_gb` | 50 | 缓存上限，超限按 LRU 清理 |
| `cache.no_cache` | false | 调试开关：true 时直读不落盘 |

目录结构：`data/cache/{dataset}/{variable}/{freq}/{year}/{month}.nc`（与 CDS 产物一致，天然去重）。
清理实现：启动时 + 每次写入后检查；按 `atime/mtime` 排序删除最旧，直到低于上限。

---

## A3. 自然语言解析层落地（LLM + 规则兜底）

### A3.1 LLM System Prompt 完整结构草稿（可直接交付工程师）

```
【角色】
你是 ERA5 气象再分析数据下载助手。用户用中文或英文自然语言描述数据需求，
你必须把需求转成符合 CDS API 规范的结构化 JSON。

【输出硬性约束】
1. 只输出一个 JSON 对象，禁止输出解释、Markdown 代码块或多余文字。
2. 字段必须符合下方 JSON Schema；未提到可推导的字段给合理默认值。
3. 若信息不足无法确定，输出 {"need_info": ["字段名", ...], "questions": ["面向用户的追问问题", ...]}，
   每次最多追问 3 个字段，不要臆造数值。

【字段与转换规则】
- dataset: 从给定枚举中选择；默认 reanalysis-era5-single-levels
- variables: 中文变量名必须映射为 ERA5 标准英文变量名（见映射表）；映射不确定时加
  "confidence": <0~1> 并可用 need_info 确认
- timerange: ISO 8601（YYYY-MM-DD）；"近五年"按当前日期推算；无结束日期默认今天
- area: bbox = [west, south, east, north]（西经为负、南纬为负）；"长三角"等区域词用
  内置区域词典解析；未提及默认全球
- aggregation: raw | mean | sum | max | min；提到"平均/月均/年均"才设，否则 raw
- frequency: hourly | daily | monthly；提到"逐日/逐月"才设，否则 hourly

【JSON Schema】
{...见 A3.2...}

【变量映射表（摘要）】
temperature → 2m_temperature；precipitation → total_precipitation；
wind → 10m_u_component_of_wind + 10m_v_component_of_wind；...

【示例】
用户: "下载最近五年长江三角洲五六月地表温度"
输出: {"dataset":"reanalysis-era5-single-levels","variables":["2m_temperature"],
      "timerange":{"start":"2020-06-01","end":"2025-06-30"},
      "area":{"west":118,"south":29,"east":123,"north":34},
      "frequency":"hourly","aggregation":"raw","confidence":0.9}
```

### A3.2 NL 输出 JSON Schema（字段级定义）

```json
{
  "type": "object",
  "required": ["dataset", "variables", "timerange"],
  "properties": {
    "dataset": {
      "type": "string",
      "enum": [
        "reanalysis-era5-single-levels",
        "reanalysis-era5-pressure-levels",
        "reanalysis-era5-land",
        "reanalysis-era5-single-levels-monthly-means"
      ],
      "description": "CDS 数据集名",
      "default": "reanalysis-era5-single-levels"
    },
    "variables": {
      "type": "array",
      "items": {"type": "string"},
      "minItems": 1,
      "description": "ERA5 标准变量名（已映射）",
      "example": ["2m_temperature"]
    },
    "pressure_levels": {
      "type": "array",
      "items": {"type": "integer"},
      "optional": true,
      "description": "仅 pressure-levels 数据集必填",
      "example": [850, 500]
    },
    "timerange": {
      "type": "object",
      "required": ["start", "end"],
      "properties": {
        "start": {"type": "string", "format": "date", "example": "2020-06-01"},
        "end":   {"type": "string", "format": "date", "example": "2025-06-30"}
      }
    },
    "area": {
      "type": "object",
      "required": ["west", "south", "east", "north"],
      "properties": {
        "west":  {"type": "number", "minimum": -180, "maximum": 180},
        "south": {"type": "number", "minimum": -90,  "maximum": 90},
        "east":  {"type": "number", "minimum": -180, "maximum": 180},
        "north": {"type": "number", "minimum": -90,  "maximum": 90}
      },
      "default": {"west": -180, "south": -90, "east": 180, "north": 90}
    },
    "frequency": {"type": "string", "enum": ["hourly", "daily", "monthly"], "default": "hourly"},
    "aggregation": {"type": "string", "enum": ["raw", "mean", "sum", "max", "min"], "default": "raw"},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": "映射可信度，<0.7 时进入补参/确认"}
  }
}
```

### A3.3 多轮对话状态机

```
状态: need_info <--> ready（每轮解析后校验）
- 解析成功且必填字段齐全 → ready → 提交编排层
- 缺字段或 confidence < 0.7 → need_info：返回 questions（≤3 个），等待用户补充
- 合并规则：新回答覆盖旧字段，其余保留（配置合并）
- max_turns = 5：超过仍未 ready → 强制切换到前端参数表单（绝不无限循环）
- 会话上下文：内存 session（key=session_id），保存已确认字段 + 对话轮次
```

### A3.4 规则兜底解析器 + variable_map.json

**规则集设计**：

| 规则组 | 匹配方式 | 示例 | 产出字段 |
|---|---|---|---|
| 时间正则 | 正则匹配 | `(\d{4})年(\d{1,2})月`、`近(\d+)年`、`(\d{4})-(\d{4})` | timerange |
| 区域关键词 | 词典查表 | 长三角→[118,29,123,34]；京津冀/珠三角/东北/全国/省名 | area |
| 变量词典 | 全词→子串→同义词三级匹配 | "温度"→t2m；"降水"→tp；"风速"→si10 | variables |
| 统计词 | 关键词 | "平均/月均/年均"→mean；"累计/总量"→sum | aggregation |
| 频率词 | 关键词 | "逐日/每天"→daily；"逐月/每月"→monthly | frequency |

**variable_map.json 结构**：
```json
{
  "version": 1,
  "updated_at": "2025-07-01",
  "synonyms": {
    "2m_temperature": ["温度", "气温", "地表气温", "temperature", "temp", "t2m"],
    "skin_temperature": ["地表温度", "skin temperature", "skt"],
    "total_precipitation": ["降水", "降雨", "降水量", "precipitation", "precip", "tp"],
    "convective_precipitation": ["对流降水", "convective precipitation", "cp"],
    "10m_u_component_of_wind": ["纬向风", "u风", "u-wind", "10u"],
    "10m_v_component_of_wind": ["经向风", "v风", "v-wind", "10v"],
    "10m_wind_speed": ["风速", "wind speed", "si10"],
    "mean_sea_level_pressure": ["海平面气压", "气压", "mslp", "msl"],
    "2m_dewpoint_temperature": ["露点温度", "dewpoint", "d2m"],
    "relative_humidity": ["相对湿度", "湿度", "relative humidity", "r"],
    "total_cloud_cover": ["云量", "总云量", "cloud cover", "tcc"],
    "surface_solar_radiation_downwards": ["太阳辐射", "辐射", "ssrd"],
    "surface_thermal_radiation_downwards": ["热辐射", "strd"],
    "snow_depth": ["雪深", "积雪", "snow depth", "sd"],
    "evaporation": ["蒸发", "evaporation", "e"],
    "volumetric_soil_water_layer_1": ["土壤湿度", "soil moisture", "swvl1"],
    "sea_surface_temperature": ["海温", "sea surface temperature", "sst"],
    "visibility": ["能见度", "visibility"],
    "specific_humidity": ["比湿", "水汽", "specific humidity", "q"],
    "2m_temperature_max": ["最高气温", "max temperature"],
    "2m_temperature_min": ["最低气温", "min temperature"],
    "10m_wind_direction": ["风向", "wind direction"]
  }
}
```
> 首批 22 个标准变量（约 60 个中英同义词），覆盖 温度/降水/风/气压/湿度/云/辐射/雪/蒸发/土壤/海温/能见度 等高频需求。工程师可按此结构扩展。

**匹配策略**：先对同义词做**全词匹配**（分词后精确命中）→ 再**子串匹配** → 最后**同义词表扩展匹配**；命中多个标准变量时全部返回，并标注 `confidence`（全词 0.95 / 子串 0.8 / 同义扩展 0.7）。

### A3.5 离线兜底流程（无 LLM Key 完整路径）

```
无 LLM Key / 网络失败
  → 规则解析器（A3.4）
  → Schema 校验（pydantic）
  → 必填齐全？→ 是 → ready → 提交编排层
  → 否 → 返回 missing_fields + 前端渲染参数表单
  → 用户表单填写 → 合并（表单覆盖规则结果）→ ready
```
关键点：**规则解析永远可用**；参数表单是最后一道兜底，保证任何情况下都能发起下载。

---

## A4. 出图引擎落地

### A4.1 三类图实现管线

**① 空间分布图（map）**：
```
读 xarray(缓存产物或 GCS 直读)
→ 时间切片/聚合（取某时刻 或 mean over 时间，按 profile.aggregation）
→ 重采样到目标网格（A2.2，仅当需要）
→ 按 profile.projection 创建 cartopy axes
→ 底图：coastlines / 国界 / 省界（profile.basemap，离线底图数据）
→ pcolormesh 或 contourf 填充 + colormap + colorbar
→ 标题（title_template）→ 输出 png/pdf（figure_size × dpi）
```

**② 时间序列图（timeseries）**：
```
读 xarray
→ 区域平均：ds.weighted(cos(lat)).mean(dim=["latitude","longitude"])
→ 时间重采样（raw→daily/monthly，按 profile.aggregation 或用户频率）
→ matplotlib 折线图（x=time, y=变量, 单位/图例/标题）
→ 输出 png
```

**③ 动画（animation）**：
```
读 xarray → 按时间帧切片（可抽样 N 帧，默认 24 帧）
→ 每帧渲染空间图（复用 ① 的渲染函数，关闭交互元素）
→ PIL 合成 GIF 或 ffmpeg 合成 mp4（默认 gif，可配）
→ 前端 <img> 或 <video> 展示；可选输出 HTML（多帧静态图轮播）
```

**输出格式**：`png`（默认）/ `pdf` / `gif` / `mp4` / `html`，由 profile.output_format 决定。

### A4.2 plot profile 配置文件结构 + 示例

```json
{
  "profile_name": "default_map",
  "description": "默认空间分布图",
  "plot_type": "map",                    // map | timeseries | animation
  "projection": {"name": "PlateCarree", "central_longitude": 105},
  "basemap": {
    "coastlines": true,
    "resolution": "50m",
    "country_boundaries": true,
    "china_province": false,
    "offline_data_dir": "config/geo/offline"
  },
  "colormap": "RdYlBu_r",
  "color_range": {"vmin": null, "vmax": null, "auto_percentile": [2, 98]},
  "aggregation": "mean",
  "output_format": "png",
  "figure_size": [12, 8],
  "dpi": 150,
  "title_template": "{variable} | {timerange}",
  "animation": {"max_frames": 24, "fps": 4}
}
```
> 每个 profile 一个文件：`config/plot_profiles/{name}.json`；列表由 `settings.json` 的 `plot.default_profile` 指向。**出图配置面板修改的字段与这里一一对应**，保存即写回 profile 文件。

### A4.3 配置热加载机制

- **方案：惰性重读 + 文件 mtime 比对（不引入常驻 watcher）**。
  - `PlotConfig.load(profile_name)` 每次出图前执行：读取 profile 文件 mtime，与内存缓存比对；有变化则重新 `json.load` + pydantic 校验。
  - 面板保存 → 写 profile 文件（原子写：tmp+rename）→ 前端 toast"已生效"。
  - 下一次出图自动用新配置，**无需重启服务**。
- 进阶：若未来需要"预览即生效"，可加 `watchdog` 监听 `config/plot_profiles/`，但初版 mtime 方案已满足"改配置可生效"。
- 配置版本号：profile 文件头可带 `"version": 2`，前端显示版本，避免多人/多次修改混淆。

---

## A5. 账号向导落地

### A5.1 状态机步骤枚举 + 每步动作

| 步骤 | 枚举 | 动作 |
|---|---|---|
| 初始 | `INIT` | 读 keyring/`.cdsapirc`，检测是否已有凭据 → 有则 `READY` |
| 引导注册 | `GUIDE_REGISTER` | 前端打开 CDS 注册页（新标签页）+ 图文说明（人机验证需人工完成） |
| 等待用户 | `WAIT_USER` | 展示表单：UID + API Key 输入（Key 用 password 掩码） |
| 校验中 | `VALIDATING` | 调 `cdsapi` 探测校验（A5.3），带 loading |
| 就绪 | `READY` | 写 `.cdsapirc` + keyring；提供"测试下载"按钮 |
| 失败 | `ERROR` | 展示失败原因；提供"重试"（回 VALIDATING）或"重新引导"（回 GUIDE_REGISTER） |

### A5.2 .cdsapirc 格式模板 + keyring 约定

**`.cdsapirc`（生成在用户主目录 `~/.cdsapirc`）**：
```
url: https://cds.climate.copernicus.eu/api
key: <UID>:<API_KEY>
```
- 生成时机：`VALIDATING` 通过后原子写（先写临时文件再 rename）。
- **keyring 约定**：`service_name = "era5-tool"`，`username = <UID>`，存 `API_KEY`。
  - 读取优先级：`keyring.get("era5-tool", uid)` → 环境变量 `CDSAPI_KEY` → `.cdsapirc`。
  - 删除接口 `DELETE /api/account/credentials`：清 keyring + 删 `.cdsapirc` + 提醒用户去 CDS 官网吊销 Key。
- **安全红线**：UID/Key 绝不写入日志、数据库、前端 localstorage；前端只在输入时持有，提交后立即清空。

### A5.3 校验 API 调用方法

- **首选：`cdsapi` 连接探测（不消耗配额）**：
  ```
  client = cdsapi.Client(url=..., key=uid+":"+key)   # 构造即校验格式
  client.info()   # 或 api.info()：拉取产品列表/用户信息，凭据无效会抛错
  ```
- **次选：最小探测 retrieve（验证"能真正下载"）**：单变量 × 单小时 × 最小区域（如 1×1 度）请求，成功即证明可下载。
  - 建议初版只做 `info()` 校验（快、无配额消耗）；"测试下载"按钮再做最小 retrieve。
- 校验失败分类：`401 无效凭据` / `403 未接受许可`（提示去官网勾选数据许可协议）/ `网络错误`，分别给出不同提示文案。

---

## A6. 前端外壳落地

### A6.1 页面路由结构

| 路径 | 页面 | 职责 |
|---|---|---|
| `/` | 首页/总览 | 状态总览：账号状态、最近任务、快捷入口 |
| `/wizard` | 账号向导 | A5 状态机可视化 + UID/Key 表单 |
| `/chat` | 自然语言对话 | 对话输入 → NL 解析 → 补参表单（need_info 时）→ 提交下载 |
| `/plot` | 出图展示 | 选择数据集/任务 → 出图 → 图片/动画预览 + 下载 |
| `/config` | 出图配置 | profile 列表/编辑（A4.2 字段表单）+ 保存即生效 |
| `/tasks` | 任务列表 | 任务状态、进度条（WS 实时）、取消/续传 |

导航：顶部 AppBar（MUI）+ 侧边栏；傻瓜模式默认只显示 首页/聊天/出图，向导与配置折叠进"设置"。

### A6.2 前端 ↔ 后端交互契约

- **REST**：见 B1 清单；统一 `{code, data, message}`。
- **WebSocket**：`ws://<host>/ws/tasks`（连接即订阅全部任务事件）。
  - 客户端发送：`{"action": "subscribe", "task_id": "t_xxx"}`（可选，按任务订阅）。
  - 服务端推送事件类型：`progress` / `status` / `log` / `done`（结构见 A1.4）。
  - 断线重连：指数退避（1s→2s→4s→…→30s 封顶）；重连后客户端主动 `GET /api/download/list` 拉一次全量状态补差。

---

# B. 系统级契约（工程师施工依据）

## B1. REST API 清单表

> 统一前缀 `/api`；响应统一 `{"code": 0, "data": {...}, "message": "ok"}`（code=0 成功，非 0 为错误码）。

### B1.1 NL 组
| 方法 | 路径 | 入参 | 出参 data | 用途 |
|---|---|---|---|---|
| POST | `/api/nl/parse` | `{text, session_id}` | `{request_schema \| need_info: {missing, questions}, session_id}` | 自然语言 → 结构化请求 |
| POST | `/api/nl/clarify` | `{session_id, answers: {field: value}}` | `{request_schema \| need_info, session_id}` | 多轮补参 |

### B1.2 Download 组
| 方法 | 路径 | 入参 | 出参 data | 用途 |
|---|---|---|---|---|
| POST | `/api/download/submit` | `{request_schema, channel_override?}` | `{task_id}` | 提交下载任务（自动路由通道） |
| GET | `/api/download/{task_id}` | — | `{task}`（B2 模型） | 查询任务状态 |
| GET | `/api/download/list` | `{status?, page?, size?}` | `{tasks, total}` | 任务列表 |
| POST | `/api/download/{task_id}/cancel` | — | `{task_id, status}` | 取消（终止未完成块） |
| POST | `/api/download/{task_id}/resume` | — | `{task_id}` | 断点续传 |
| DELETE | `/api/download/{task_id}` | `{delete_files?}` | `{task_id}` | 删除任务（可选删产物） |

### B1.3 Plot 组
| 方法 | 路径 | 入参 | 出参 data | 用途 |
|---|---|---|---|---|
| POST | `/api/plot/render` | `{task_id \| request_schema, profile, overrides?}` | `{artifact: {url, format, size}}` | 渲染出图 |
| GET | `/api/plot/profiles` | — | `{profiles: [...]}` | 列出可用 profile |
| GET | `/api/plot/profiles/{name}` | — | `{profile}` | 读取单个 profile |
| PUT | `/api/plot/profiles/{name}` | `{profile}` | `{profile, version}` | 保存 profile（热加载生效） |
| POST | `/api/plot/profiles` | `{profile}` | `{profile}` | 新建 profile |

### B1.4 Account 组
| 方法 | 路径 | 入参 | 出参 data | 用途 |
|---|---|---|---|---|
| GET | `/api/account/status` | — | `{state: INIT\|READY\|ERROR, uid?, has_key}` | 账号状态 |
| POST | `/api/account/validate` | `{uid, api_key}` | `{valid, error?}` | 校验凭据（A5.3） |
| POST | `/api/account/finalize` | `{uid, api_key}` | `{state: READY}` | 校验通过后写 `.cdsapirc` + keyring |
| DELETE | `/api/account/credentials` | — | `{state: INIT}` | 清除凭据 |
| POST | `/api/account/test-download` | — | `{task_id}` | 最小探测下载（验证可下载性） |

### B1.5 Config 组
| 方法 | 路径 | 入参 | 出参 data | 用途 |
|---|---|---|---|---|
| GET | `/api/config` | — | `{settings, channel_preference, arco_coverage}` | 读全局配置 |
| PUT | `/api/config` | `{settings}` | `{settings}` | 写全局配置（下载并发数/缓存/通道偏好） |
| GET | `/api/config/variable-map` | — | `{variable_map}` | 读变量映射（前端联想用） |

## B2. 任务模型与状态机

**字段级定义**：
```json
{
  "id": "t_20250701_001",
  "type": "download | plot | nl_parse | test_download",
  "status": "pending | running | success | failed | paused",
  "progress": 0.0,
  "params": { "request_schema": {...}, "channel": "cds | gcs", "profile": "default_map" },
  "block_stats": { "total": 60, "done": 27, "failed": 2 },
  "result": { "artifact_url": "...", "files": ["..."], "cache_hit": false },
  "error": { "code": "BUSY_AFTER_RETRIES", "message": "...", "block_key": "t2m/2020/05" },
  "created_at": "2025-07-01T07:00:00Z",
  "updated_at": "2025-07-01T09:00:00Z"
}
```

**状态流转规则**：
```
pending → running        （worker 领取）
running → success        （全部块 done）
running → failed         （致命错误：参数错误/所有块重试耗尽）
running → paused         （用户取消/暂停，未完成块保留）
paused  → running        （resume 续传）
failed  → running        （resume 重跑 failed/missing 块）
success → (不可逆，如需重出图走 plot/render)
```
持久化：内存 + `data/tasks/{task_id}/task.json`（启动时扫描恢复）。

## B3. 详细目录结构落地版

```
era5-AItool/
├── docs/
│   ├── system_design.md            # 第一版架构设计
│   ├── implementation-plan.md      # 本文件（落地技术方法）
│   ├── class-diagram.mermaid
│   └── sequence-diagram.mermaid
├── backend/
│   ├── pyproject.toml              # 依赖分组 extras: [cds, cloud, nl, plot, all]
│   ├── era5tool/
│   │   ├── main.py                 # FastAPI 入口：注册路由 + WS + 启动清理
│   │   ├── api/
│   │   │   ├── deps.py             # 依赖注入（settings/session）
│   │   │   ├── nl_routes.py        # /api/nl/*
│   │   │   ├── download_routes.py  # /api/download/*
│   │   │   ├── plot_routes.py      # /api/plot/*
│   │   │   ├── account_routes.py   # /api/account/*
│   │   │   └── config_routes.py    # /api/config/*
│   │   ├── core/
│   │   │   ├── orchestrator.py     # 任务状态机 + 调度
│   │   │   ├── router.py           # 通道路由（读 arco_coverage.json）
│   │   │   ├── concurrency.py      # 并发池/限流/指数退避
│   │   │   ├── resumable.py        # manifest + 断点续传
│   │   │   ├── events.py           # WS 事件广播（A1.4）
│   │   │   └── task_store.py       # 任务持久化（data/tasks/*.json）
│   │   ├── acquisition/
│   │   │   ├── cds_channel.py      # A1：切块/retry/标记
│   │   │   ├── gcs_channel.py      # A2：GCS 直读 + 缓存
│   │   │   └── coverage.py         # 读 arco_coverage.json 提供路由数据
│   │   ├── nl/
│   │   │   ├── parser.py           # 解析器调度（LLM/规则/表单合并）
│   │   │   ├── llm_parser.py       # A3.1 prompt + 调用 + JSON 校验
│   │   │   ├── rule_parser.py      # A3.4 规则集
│   │   │   ├── session.py          # 多轮对话状态机（need_info/ready/max_turns）
│   │   │   └── variable_map.json   # A3.4 词典
│   │   ├── plot/
│   │   │   ├── engine.py           # A4.1 三类图管线
│   │   │   ├── profiles.py         # profile 加载/热加载（mtime）
│   │   │   ├── reproject.py        # A2.2 重采样
│   │   │   ├── colormaps.py
│   │   │   └── offline_geo/        # 离线底图数据（省界/国界）
│   │   ├── account/
│   │   │   ├── wizard.py           # A5.1 状态机
│   │   │   ├── keyring_store.py    # A5.2 keyring/.cdsapirc
│   │   │   └── validate.py         # A5.3 info()/最小retrieve 校验
│   │   ├── config/
│   │   │   ├── settings.py         # pydantic-settings（含 cache/并发默认值）
│   │   │   └── schema.py           # B1/B2 请求响应 Schema
│   │   ├── models/
│   │   │   └── task.py             # B2 任务模型
│   │   └── data/                   # 运行时：cache/ tasks/ products/（gitignore）
│   └── tests/
│       ├── test_nl_rule.py         # 规则解析单测（含 30 条样例）
│       ├── test_resumable.py
│       └── benchmark/              # 先行验证实验脚本（见 C2）
│           ├── bench_gcs_read.py
│           ├── bench_cds_parallel.py
│           ├── probe_arco_coverage.py
│           └── bench_nl_samples.py
├── web/
│   ├── package.json / vite.config.ts / tailwind.config.js
│   ├── src/
│   │   ├── main.tsx / App.tsx
│   │   ├── api/
│   │   │   ├── client.ts           # REST 封装（统一响应解包）
│   │   │   └── ws.ts               # WS 客户端（重连/订阅）
│   │   ├── store/appStore.ts       # Zustand：账号/任务/配置状态
│   │   ├── components/
│   │   │   ├── Wizard/             # 账号向导（A5 状态渲染）
│   │   │   ├── ChatPanel/          # 对话 + 补参表单
│   │   │   ├── PlotPanel/          # 出图展示（img/video/html）
│   │   │   ├── ConfigPanel/        # profile 编辑表单
│   │   │   ├── TaskList/           # 任务列表 + 进度条
│   │   │   └── common/             # 布局/按钮/Toast
│   │   └── pages/                  # A6.1 路由页面
│   └── src-tauri/                  # 桌面壳（本地起后端 + 前端静态资源）
└── config/                         # 用户级配置（gitignore）
    ├── settings.json               # 并发/缓存/通道偏好/默认profile
    ├── plot_profiles/*.json        # A4.2
    ├── arco_coverage.json          # A2.1 探测结论（路由依据）
    └── variable_map.json           # A3.4（可扩展）
```

---

# C. 里程碑实施计划

## C1. Phase 0–6 细化任务序列

> 每个任务带 文件/模块级粒度、依赖、验收与验证方法。

### Phase 0 · 脚手架与配置体系
| 任务 | 文件/模块 | 依赖 | 验收/验证方法 |
|---|---|---|---|
| 后端骨架 + 依赖分组 | `backend/pyproject.toml`, `era5tool/main.py` | 无 | `pip install -e .[all]` 成功；`uvicorn` 起服务，`GET /health` 200 |
| 配置体系 | `config/settings.py`, `config/settings.json` | 0.1 | 单测：改 settings.json → 加载生效；默认值正确 |
| 统一响应与 Schema | `config/schema.py` | 0.1 | 单测：非法请求返回非 0 code |
| 前端骨架 | `web/` 全套脚手架 | 无 | `npm run dev` 起页；调 `/api/health` 联通 |

### Phase 1 · CDS 通道下载
| 任务 | 文件/模块 | 依赖 | 验收/验证方法 |
|---|---|---|---|
| CDS 切块与单块下载 | `acquisition/cds_channel.py` | P0 | 单块（1变量×1年×1月）下载成功，产出 NetCDF |
| 并发池 + 指数退避 | `core/concurrency.py` | 1.1 | **基准实验 ②**：4 并发 vs 串行，提速 ≥2x 且无封禁 |
| 断点续传 + manifest | `core/resumable.py`, `models/task.py` | 1.1 | 单测：kill 进程后 resume，跳过 done 块，failed 块重下 |
| 任务状态机 + 进度事件 | `core/orchestrator.py`, `core/events.py` | 1.2,1.3 | 单测状态流转；WS 收到 progress/done |
| 下载 REST | `api/download_routes.py` | 1.4 | curl 提交/查询/取消任务全流程 |

### Phase 2 · GCS 云通道
| 任务 | 文件/模块 | 依赖 | 验收/验证方法 |
|---|---|---|---|
| **arco 覆盖探测（先行）** | `tests/benchmark/probe_arco_coverage.py` | P0 | **实验 ③**：输出覆盖矩阵，固化 `config/arco_coverage.json` |
| GCS 直读模块 | `acquisition/gcs_channel.py` | 2.1 | **实验 ①**：覆盖内 5 年长三角单变量，GCS 直读显著快于 CDS |
| 通道路由 | `core/router.py`, `acquisition/coverage.py` | 2.2 | 单测：各场景路由结果与判定表一致 |
| 重采样 | `plot/reproject.py` | 2.2 | 单测：高斯→0.25° 后 lat/lon 网格正确；降水面积守恒（若用 xesmf） |
| 本地缓存 | `core/task_store.py` + 缓存配置 | 2.2 | 单测：二次读取命中缓存不重新拉取；TTL/上限清理生效 |

### Phase 3 · 自然语言解析层
| 任务 | 文件/模块 | 依赖 | 验收/验证方法 |
|---|---|---|---|
| 规则解析器 + variable_map | `nl/rule_parser.py`, `variable_map.json` | P0 | 单测 30 条样例：字段正确率 ≥80%；词典覆盖首批 22 变量 |
| 多轮会话状态机 | `nl/session.py` | 3.1 | 单测：need_info↔ready 流转；max_turns=5 后强制表单 |
| LLM 解析器 + prompt | `nl/llm_parser.py` | 3.1 | **实验 ④**：30 条样例 LLM 输出合法 JSON + 字段正确率 ≥80% |
| NL REST | `api/nl_routes.py` | 3.2,3.3 | curl：NL→schema；补参→ready→提交 |

### Phase 4 · 出图引擎
| 任务 | 文件/模块 | 依赖 | 验收/验证方法 |
|---|---|---|---|
| 空间分布图 | `plot/engine.py` map 管线 | P1/P2 | 对样例数据出图 png，肉眼核验底图/配色/标题 |
| 时间序列 + 动画 | `plot/engine.py` ts/animation | 4.1 | 出 png/gif；动画帧数/尺寸符合 profile |
| profile 加载 + 热加载 | `plot/profiles.py` | 4.1 | 单测：改 profile 文件 → 下次渲染自动生效（mtime 触发） |
| 出图 REST + ConfigPanel | `api/plot_routes.py`, `web/components/ConfigPanel` | 4.2,4.3 | 前端改配色/投影 → 保存 → 重新出图即用新配置 |

### Phase 5 · 账号向导
| 任务 | 文件/模块 | 依赖 | 验收/验证方法 |
|---|---|---|---|
| 凭据存储 | `account/keyring_store.py` | P0 | 单测：keyring 写入/读取/删除；生成 `.cdsapirc` 格式正确；日志无 Key 泄露 |
| 校验模块 | `account/validate.py` | 5.1 | 无效 Key 返回 401 分类错误；有效 Key（测试账号）通过 |
| 向导状态机 + REST | `account/wizard.py`, `api/account_routes.py` | 5.2 | curl 走通 INIT→VALIDATING→READY；失败→ERROR→重试 |
| 前端 Wizard 页 | `web/components/Wizard` | 5.3 | 页面状态与后端一致；Key 输入即清空 |

### Phase 6 · GUI 整合与傻瓜化
| 任务 | 文件/模块 | 依赖 | 验收/验证方法 |
|---|---|---|---|
| 页面整合 + 路由 | `web/pages/*`, AppBar/侧栏 | P1–P5 | 五页面导航可用；傻瓜模式隐藏高级项 |
| WS 进度 + 任务列表 | `web/api/ws.ts`, `TaskList` | 6.1 | 下载时进度条实时更新；断线重连补状态 |
| 一键模板 | `ChatPanel` 快捷模板 | 6.1 | 点击"近五年长三角五六月地表温度"→全流程出图 |
| Tauri 打包 | `web/src-tauri` | 6.2 | 桌面应用可安装运行；本地后端自动拉起 |
| 端到端验收 | 全链路 | 6.3 | 验收脚本：无 Key 用户走表单也能下载出图；有 Key 走 NL 全流程 |

## C2. 先行验证实验清单（先于正式开发跑通，证明关键假设）

| # | 实验 | 目的 | 最小脚本思路（伪代码） | 通过标准 |
|---|---|---|---|---|
| ① | GCS 直读速度基准 | 证明"云通道快"假设成立 | `open_zarr(ar 0.25° store)` → 选长三角 bbox + 5 年 t2m → `.sel().mean()` → 计时；对比同条件 CDS 预估耗时 | 直读+计算 ≤ 60s；CDS 预估 ≥ 10min（快 10 倍以上） |
| ② | CDS 并行 vs 串行基准 | 证明"多进程并行提速且不封禁" | 同一请求（3 变量×2 年×6 月）：串行逐块 vs `ProcessPoolExecutor(4)`，各测耗时与错误 | 4 并发耗时 ≤ 串行 1/2；无 429/封禁；错误率 0 |
| ③ | arco 覆盖探测 | 核实覆盖/变量/延迟/网格（A2.1） | `fs.ls(bucket)` → 各 store `open_zarr` → 打印 data_vars/time 范围/lat-lon → 输出覆盖矩阵 | 覆盖矩阵完整；`ar` store 可开、延迟量化、Land 覆盖结论明确 |
| ④ | LLM NL→Schema 样例测试 | 验证 prompt 能稳定产出合规 JSON | 30 条中文样例（含模糊表达）→ LLM 解析 → pydantic 校验 + 字段正确率统计 | 合法 JSON 率 100%；字段正确率 ≥80%；need_info 触发合理 |
| ⑤ | 出图管线最小验证 | 验证 cartopy 底图在国内可用性 | 样例数据 → 空间图管线 → 输出 png | 出图成功；底图（海岸线/国界）渲染正常（离线数据可用） |

> 每个实验产出报告（耗时/结论/截图），作为后续 Phase 的准入依据。**实验 ①③ 决定 GCS 通道是否按计划启用；实验 ② 决定并发默认值；实验 ④ 决定 prompt 是否需迭代。**

## C3. 风险与技术债务落地应对表

| # | 风险/技术债 | 落地应对措施 | 责任人/阶段 |
|---|---|---|---|
| R1 | arco 覆盖不确定（Land/变量/延迟） | 实验 ③ 前置探测，产出覆盖矩阵 + `arco_coverage.json`；路由兜底 CDS | 架构+工程 / P2 前 |
| R2 | GCS 国内访问受限 | 实验 ① 验证网络可达性；配置 `channel.gcs_enabled=false` 开关；失败自动回退 CDS | 工程 / P2 |
| R3 | CDS 限速/封禁 | 并发默认 4 + 指数退避（30s×2^n，max 600s）+ jitter；实验 ② 压测调参 | 工程 / P1 |
| R4 | LLM 成本/不稳定 | 规则兜底优先；LLM 仅增强；max_turns 防死循环；输出强制 JSON 校验失败自动降级规则 | 工程 / P3 |
| R5 | NL 准确率不达标 | 30 条样例集回归测试；variable_map 持续扩充；confidence<0.7 强制确认 | 工程+产品 / P3 持续 |
| R6 | 出图底图在线依赖不可用 | 内置离线底图（config/plot/offline_geo/），禁在线下载 | 工程 / P4 |
| R7 | 高斯网格重采样误差 | 默认走 ar 0.25° 规则网格；仅必要场景重采样；降水用守恒插值（xesmf） | 工程 / P2 |
| R8 | Key 泄露 | keyring + `.cdsapirc` 权限 600；日志过滤；前端输入即清；.gitignore 红线 | 工程 / P5 |
| R9 | 缓存无限膨胀 | TTL=180 天 + max_gb=50 + LRU 清理；no_cache 调试开关 | 工程 / P2 |
| R10 | 多用户/云端扩展（未来） | 接口层已解耦；届时任务表迁 DB（SQLite→Postgres）+ Celery | 架构 / 后续版本 |
| R11 | cdsapi 版本/API 变更 | 依赖锁版本；`acquisition/cds_channel.py` 隔离官方 API 变更 | 工程 / 持续 |
| R12 | 大文件内存溢出 | dask chunk + 分块插值；出图前按时间抽样 | 工程 / P2,P4 |

---

## 附：本文件与第一版的对应关系
- 第一版（system_design.md）回答"选什么"；本文件回答"怎么做/参数定多少/怎么验证"。
- 所有默认值（并发 4、退避 30s×2^n、TTL 180、max_turns 5、正确率 ≥80% 等）均为**初始建议值**，由 C2 实验校准后固化。
- 后续若用户拍板第一版 R1–R10（LLM 选型/桌面形态/GCS 开关等），本文件对应参数随之调整，架构不变。
