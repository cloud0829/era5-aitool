# -*- coding: utf-8 -*-
"""pytest 共享 fixture：隔离配置/数据目录 + mock CDS 模式。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# 必须在导入 era5tool.main 之前设置隔离目录（AppState 在 import 时创建）
_TMP = Path(tempfile.mkdtemp(prefix="era5_test_"))
_CONFIG = _TMP / "config"
_DATA = _TMP / "data"
_CONFIG.mkdir(parents=True, exist_ok=True)
_DATA.mkdir(parents=True, exist_ok=True)

# 测试默认配置：mock 模式开、低并发、月粒度（保持既有 e2e 快且不触发外部二进制）、aria2 关
(_CONFIG / "settings.json").write_text(json.dumps({
    "download": {"mock": True, "cds_max_workers": 2, "retry_max": 2,
                 "chunk_granularity": "month", "aria2_enabled": False},
    "plot": {"default_profile": "default_map"},
}), encoding="utf-8")

os.environ["ERA5_CONFIG_DIR"] = str(_CONFIG)
os.environ["ERA5_DATA_DIR"] = str(_DATA)
# 禁止测试读到真实凭据
os.environ.pop("DEEPSEEK_API_KEY", None)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from era5tool.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def app_state():
    return app.state.app_state
