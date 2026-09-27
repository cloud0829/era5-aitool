# -*- coding: utf-8 -*-
"""API 依赖注入（settings/session/credential）。"""
from __future__ import annotations

from fastapi import Request

from era5tool.account.keyring_store import KeyringStore
from era5tool.account.wizard import AccountWizard
from era5tool.config.settings import Settings
from era5tool.core.events import EventBroker
from era5tool.core.orchestrator import Orchestrator
from era5tool.nl.parser import NLParser
from era5tool.nl.variable_map import VariableMap
from era5tool.plot.engine import PlotEngine
from era5tool.plot.profiles import ProfileStore


class AppState:
    """应用级共享对象（挂在 app.state）。"""

    def __init__(self) -> None:
        self.settings = Settings.load()
        self.settings.ensure_dirs()
        self.broker = EventBroker()
        self.var_map = VariableMap(self.settings)
        self.nl_parser = NLParser(self.settings, self.var_map)
        self.orchestrator = Orchestrator(self.settings, self.broker)
        self.plot_engine = PlotEngine(self.settings)
        self.profile_store = ProfileStore(self.settings)
        self.keyring = KeyringStore(self.settings)
        self.wizard = AccountWizard(self.settings)


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def get_settings(request: Request) -> Settings:
    return get_state(request).settings
