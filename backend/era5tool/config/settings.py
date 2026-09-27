# -*- coding: utf-8 -*-
"""应用配置（pydantic-settings + 配置热加载）。

- settings.json（config/）为持久化用户配置；不包含任何密钥。
- DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL 从 config/.env 或环境变量读取。
- 配置热加载：`Settings.load()` 每次调用都重读文件与 env，mtime 变化即时生效。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from pydantic import AliasChoices, BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]          # era5-AItool/
BACKEND_ROOT = PROJECT_ROOT / "backend"
CONFIG_DIR_DEFAULT = PROJECT_ROOT / "config"
DATA_DIR_DEFAULT = PROJECT_ROOT / "data"
ENV_FILE = CONFIG_DIR_DEFAULT / ".env"


class LlmSettings(BaseModel):
    """DeepSeek LLM 配置（密钥仅经 .env / 环境变量 / keyring 注入，不回显）。"""

    deepseek_api_key: str = Field(
        default="", validation_alias=AliasChoices("DEEPSEEK_API_KEY", "llm__deepseek_api_key"))
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices("DEEPSEEK_BASE_URL", "llm__deepseek_base_url"))
    deepseek_model: str = Field(
        default="deepseek-chat",
        validation_alias=AliasChoices("DEEPSEEK_MODEL", "llm__deepseek_model"))

    @property
    def has_key(self) -> bool:
        return bool((self.deepseek_api_key or "").strip())


class DownloadSettings(BaseModel):
    """下载通道配置（与先行实验 E1/E5 固化参数一致）。

    下载加速三项开关（design-speedup-download.md §1.3）：
    - chunk_granularity：切块粒度（day / month / auto，hourly 生效；
      monthly 家族固定 monthly）。默认 "day"（用户拍板），回退改 "month" 即可回到旧行为。
    - cds_max_workers：并发度，默认 4 → 6。
    - aria2_enabled：aria2c 多连接传输，默认 False（探测不到自动降级）。
    其余为 auto 模式与落盘节流的可调参数，全部带默认值，老 settings.json 天然兼容。
    """

    cds_max_workers: int = Field(default=6, ge=1, le=16)
    retry_max: int = 3
    backoff_base: float = 30.0
    backoff_factor: float = 2.0
    backoff_max: float = 600.0
    backoff_jitter: float = 0.10
    mock: bool = False            # True 时使用本地 fake 客户端（无凭据/测试）
    cache_ttl_days: int = 180
    cache_max_gb: float = 50.0
    # —— 下载加速（design-speedup-download.md §1.3）——
    chunk_granularity: str = Field(default="day")   # day | month | auto（hourly 生效）
    max_blocks_per_task: int = Field(default=2000, ge=1)   # day 切块熔断上限（防一次打出上万请求）
    auto_max_block_gb: float = Field(default=2.0, gt=0.0)   # auto 模式单块体积阈值（超则降级 day）
    aria2_enabled: bool = False    # aria2c 多连接传输（探测不到自动降级）
    aria2_path: str = ""           # 显式 aria2c 路径（空 → env ERA5_ARIA2_CMD → which）
    aria2_connections: int = Field(default=8, ge=1, le=16)
    aria2_timeout_s: int = Field(default=1800, ge=10)
    submit_stagger_s: float = Field(default=1.0, ge=0.0)  # 提交抖动，削平并发 POST 突刺
    progress_persist_every: int = Field(default=20, ge=1)  # 落盘节流：每 N 个 done 块写一次
    progress_persist_interval_s: float = Field(default=1.0, ge=0.0)  # 落盘节流：距上次写盘 ≥ N 秒才写
    # resume 等待上一会话（暂停后仍在跑完当前块的旧 worker）收尾的上限秒数。
    # 旧 worker 若因真实网络挂起等迟迟不退出，无限等待会让任务卡 running 且无进度；
    # 超过该上限视为“放弃等待”并转 paused（DRAIN_TIMEOUT，可再次 resume/delete），
    # 避免任务永久砖化。默认 600s（10 分钟）远大于单块正常下载时长，不影响正常收尾。
    drain_wait_timeout_s: float = Field(default=600.0, ge=0.0)
    # —— 下载稳定性（bugfix download-gaps）——
    # 线上事故：CDS 对单数据集"排队请求数"有上限，超限用 HTTP **400** 返回
    #   "The job has been rejected / Number queued requests ... temporarily limited."
    # 旧代码按状态码把 400 判为不可重试 → 块一次都不重试就永久失败（172/172）。
    # 以下 5 项为配套的自适应降速（默认开启；全部带默认值，老 settings.json 兼容）。
    block_interleave: bool = True        # 提交顺序跨变量/跨年轮转交错（防单变量独占并发额度）
    throttle_enabled: bool = True        # 全局自适应限流闸（跨进程/跨任务共享，mock 下不上闸）
    throttle_base_s: float = Field(default=30.0, ge=0.0)     # 首次撞墙冷却秒数
    throttle_factor: float = Field(default=2.0, ge=1.0)      # 连续撞墙冷却倍增
    throttle_max_s: float = Field(default=600.0, ge=1.0)     # 单次冷却上限
    throttle_jitter_s: float = Field(default=2.0, ge=0.0)    # 放行抖动（防惊群再次撞墙）
    adaptive_concurrency: bool = True    # 撞墙时动态下调在跑并发（撞一次降 1）
    adaptive_min_workers: int = Field(default=1, ge=1, le=16)
    adaptive_recover_every: int = Field(default=6, ge=1)     # 连续成功 N 块回升 1 个并发
    # mock 故障注入模式（仅影响 mock，真实模式忽略）：
    #   retryable_429     → 抛 RetryableError(429)（默认，与既有测试一致）
    #   queue_limited_400 → 抛 QueueLimitedError（HTTP 400 + 队列限流正文，复现线上事故）
    mock_error_mode: str = "retryable_429"

    @field_validator("mock_error_mode")
    @classmethod
    def _check_mock_error_mode(cls, v: str) -> str:
        """非法故障注入模式兜底为 retryable_429（保持既有行为）。"""
        v = (v or "retryable_429").strip().lower()
        if v not in ("retryable_429", "queue_limited_400"):
            return "retryable_429"
        return v

    @field_validator("chunk_granularity")
    @classmethod
    def _check_granularity(cls, v: str) -> str:
        """非法粒度（如 "week"）兜底为 "day"，避免下游 KeyError / 全量重下。"""
        v = (v or "day").strip().lower()
        if v not in ("day", "month", "auto"):
            return "day"
        return v


class PlotSettings(BaseModel):
    """出图配置（profile 目录位于 config/plot_profiles/）。"""

    default_profile: str = "default_map"
    offline_geo_dir: str = ""     # 空 = 使用包内 offline_geo/


class Settings(BaseSettings):
    """全局配置：config/settings.json 为持久层，env 为密钥注入层。"""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_file_encoding="utf-8",
        extra="ignore", populate_by_name=True,
    )

    llm: LlmSettings = Field(default_factory=LlmSettings)
    download: DownloadSettings = Field(default_factory=DownloadSettings)
    plot: PlotSettings = Field(default_factory=PlotSettings)

    config_dir: Path = CONFIG_DIR_DEFAULT
    data_dir: Path = DATA_DIR_DEFAULT
    version: int = 2

    @property
    def settings_path(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def tasks_dir(self) -> Path:
        return self.data_dir / "tasks"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def products_dir(self) -> Path:
        return self.data_dir / "products"

    @property
    def plot_profiles_dir(self) -> Path:
        return self.config_dir / "plot_profiles"

    @property
    def variable_map_path(self) -> Path:
        return self.config_dir / "variable_map.json"

    def ensure_dirs(self) -> None:
        for d in (self.config_dir, self.data_dir, self.tasks_dir,
                  self.cache_dir, self.products_dir, self.plot_profiles_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, config_dir: Path | None = None, data_dir: Path | None = None) -> "Settings":
        """从 settings.json + env 加载；不存在的路径用默认。

        支持环境变量覆盖（测试隔离）：ERA5_CONFIG_DIR / ERA5_DATA_DIR。
        """
        cfg_dir = Path(config_dir) if config_dir else CONFIG_DIR_DEFAULT
        dat_dir = Path(data_dir) if data_dir else DATA_DIR_DEFAULT
        cfg_dir = Path(os.environ.get("ERA5_CONFIG_DIR", str(cfg_dir)))
        dat_dir = Path(os.environ.get("ERA5_DATA_DIR", str(dat_dir)))
        payload: Dict[str, Any] = {"config_dir": cfg_dir, "data_dir": dat_dir}
        settings_file = cfg_dir / "settings.json"
        if settings_file.is_file():
            try:
                with open(settings_file, "r", encoding="utf-8") as f:
                    payload.update(json.load(f))
            except (json.JSONDecodeError, OSError):
                pass
        # env 覆盖（pydantic-settings 已在构造时读取 .env / os.environ）
        s = cls(**payload)
        s.config_dir = cfg_dir
        s.data_dir = dat_dir
        return s

    def save(self) -> None:
        """原子写 settings.json（不写密钥）。"""
        self.ensure_dirs()
        payload = {
            "version": self.version,
            "llm": {"deepseek_base_url": self.llm.deepseek_base_url,
                    "deepseek_model": self.llm.deepseek_model},
            "download": self.download.model_dump(),
            "plot": self.plot.model_dump(),
        }
        tmp = self.settings_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.settings_path)

    def public_dict(self) -> Dict[str, Any]:
        """对外可见配置（绝不包含密钥）。"""
        return {
            "version": self.version,
            "llm": {"has_key": self.llm.has_key,
                    "base_url": self.llm.deepseek_base_url,
                    "model": self.llm.deepseek_model,
                    "provider": "deepseek"},
            "download": self.download.model_dump(),
            "plot": self.plot.model_dump(),
            "paths": {"config_dir": str(self.config_dir),
                      "data_dir": str(self.data_dir)},
        }
