# ERA5-AItool · 先行验证实验（experiments/）

对应 `docs/design-final.md` §10 的 5 个先行验证实验（v2）。**全部 mock 可离线运行**，
`--real` 开关进入真实调用（需凭据，无凭据自动标注「待凭据」并跳过）。

## 环境

```bash
# 推荐使用项目隔离 venv（见团队环境说明）
python -m venv C:\Users\lei\.workbuddy\binaries\python\envs\default
# 国内网络建议加 -i https://mirrors.aliyun.com/pypi/simple/ （默认 pypi.org 很慢）
C:\Users\lei\.workbuddy\binaries\python\envs\default\Scripts\pip install \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    xarray numpy pandas matplotlib scipy cdsapi openai pydantic pillow
# cartopy 可选（python 3.13 有 cp313 wheel；无 wheel 时 E4 自动降级纯 matplotlib）
C:\Users\lei\.workbuddy\binaries\python\envs\default\Scripts\pip install \
    -i https://mirrors.aliyun.com/pypi/simple/ cartopy
```

## 目录结构

```
experiments/
├── README.md
├── conftest.py                     # pytest fixture（输出目录/凭据隔离）
├── exp_common.py                   # 退避/日志/凭据检测/断言汇总（共享）
├── exp_schema.py                   # pydantic RequestSchema（§7.3）
├── mocks/
│   ├── fake_cdsapi.py              # stub cdsapi：retrieve/info，可控耗时/失败/日志
│   ├── fake_llm.py                 # 假 DeepSeek：合法/need_info/非法 三类响应
│   └── make_sample_data.py         # 合成 xarray（0.25°/0.1° 可选）
├── samples_nl_30.json              # E3 的 30 条中英文样例（中英、ERA5/ERA5-Land）
├── e1_cds_parallel_bench.py        # CDS 并发基准
├── e2_era5land_probe.py            # ERA5-Land 参数探测
├── e3_nl_schema_samples.py         # DeepSeek NL→Schema 30 样例
├── e4_plot_minimal.py              # 出图管线最小验证
├── e5_task_state_machine.py        # 任务状态机+断点续传+并发上限
└── outputs/                        # 实验输出（图/日志/JSON 结果）
```

## 运行方式

```bash
# 全部 mock（默认，零外部依赖）
python e1_cds_parallel_bench.py
python e2_era5land_probe.py
python e3_nl_schema_samples.py
python e4_plot_minimal.py
python e5_task_state_machine.py

# 真实模式（需凭据）
python e1_cds_parallel_bench.py --real      # 需 ~/.cdsapirc
python e2_era5land_probe.py --real          # 需 ~/.cdsapirc（client.info()）
python e3_nl_schema_samples.py --real       # 需 DEEPSEEK_API_KEY
python e5_task_state_machine.py --real      # 需 ~/.cdsapirc
```

## 实验速查

| # | 验证点 | Mock 通过标准 |
|---|---|---|
| E1 | CDS 并发基准 | 并行耗时 ≤ 串行/2；退避序列正确；并发峰值 ≤ max_workers；全部块 done |
| E2 | ERA5-Land 参数 | 核对表输出；land 无 pressure_levels；monthly 无 day；build_cds_request 一致 |
| E3 | NL→Schema | 合法 JSON 解析 100%；字段正确率 ≥80%；need_info ≤3 问；低置信进确认；非法降级不崩溃 |
| E4 | 出图管线 | png/gif 三类图产出、体积合理、0.1° regrid 0.25° 可出图 |
| E5 | 状态机+续传 | 状态流转全过；中断重跑跳过 done；并发不超限；退避生效 |

## 凭据约定

- CDS：`~/.cdsapirc`（账号向导生成，禁止提交）
- DeepSeek：`config/.env` 或环境变量 `DEEPSEEK_API_KEY`（参考 `config/deepseek.example.env`）
- 任何实验不硬编码真实 Key；凭据仅经环境变量/本地配置注入。
