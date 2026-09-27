# -*- coding: utf-8 -*-
"""experiments/conftest.py：共享 pytest fixture（design-final.md §10.7）。

脚本也可直接运行（python eN_*.py）；本文件仅为未来 pytest 回归提供
临时输出目录与环境变量注入。零外部依赖。
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

# 保证 experiments/ 与 mocks/ 可导入
_EXP_ROOT = os.path.dirname(os.path.abspath(__file__))
for _p in (_EXP_ROOT, os.path.join(_EXP_ROOT, "mocks")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture()
def exp_outdir(tmp_path):
    """临时实验输出目录。"""
    d = tmp_path / "outputs"
    d.mkdir(exist_ok=True)
    return str(d)


@pytest.fixture()
def no_credentials(monkeypatch):
    """确保 mock 测试不被本机真实凭据干扰。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("NO_CDSAPIRC", "1")
    return None


@pytest.fixture()
def samples_path():
    return os.path.join(_EXP_ROOT, "samples_nl_30.json")


def pytest_addoption(parser):
    parser.addoption("--real", action="store_true", default=False,
                     help="运行真实段（需凭据）")


@pytest.fixture()
def real_mode(request):
    return request.config.getoption("--real")
