# -*- coding: utf-8 -*-
"""账号申请半自动引导状态机（design-final.md §3.5）。

INIT → GUIDE_REGISTER → WAIT_USER → VALIDATING → READY；失败回 ERROR 可重试。
凭据写 ~/.cdsapirc + keyring；状态持久化到 data/account_state.json。
"""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Dict

from era5tool.account.keyring_store import KeyringStore
from era5tool.account.validate import validate_cds
from era5tool.config.schema import ApiError, ERR_ACCOUNT
from era5tool.config.settings import Settings

KEY_API = "cds_api_key"


class WizardState(str, Enum):
    INIT = "INIT"
    GUIDE_REGISTER = "GUIDE_REGISTER"
    WAIT_USER = "WAIT_USER"
    VALIDATING = "VALIDATING"
    READY = "READY"
    ERROR = "ERROR"


class AccountWizard:
    """半自动引导状态机。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = KeyringStore(settings)
        self._state_path = settings.data_dir / "account_state.json"
        self._state: WizardState = WizardState.INIT
        self._error: str = ""
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if self._state_path.is_file():
            try:
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                self._state = WizardState(data.get("state", "INIT"))
                self._error = data.get("error", "")
            except (json.JSONDecodeError, OSError, ValueError):
                self._state = WizardState.INIT

    def _persist(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps({"state": self._state.value, "error": self._error},
                       ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    @property
    def state(self) -> WizardState:
        return self._state

    def status(self) -> Dict[str, Any]:
        has_key = self.store.get_secret(KEY_API) is not None
        return {"state": self._state.value, "has_key": has_key,
                "error": self._error}

    def start(self) -> Dict[str, Any]:
        self._state = WizardState.GUIDE_REGISTER
        self._error = ""
        self._persist()
        return self.status()

    def submit_credentials(self, api_key: str) -> Dict[str, Any]:
        self._state = WizardState.VALIDATING
        self._error = ""
        self._persist()
        ok, err = validate_cds(api_key, self.settings)
        if not ok:
            self._state = WizardState.ERROR
            self._error = err
            self._persist()
            raise ApiError(ERR_ACCOUNT, err, {"state": self._state.value})
        # 校验通过：写凭据
        self.store.save_secret(KEY_API, api_key)
        self.store.write_cdsapirc(api_key)
        self._state = WizardState.READY
        self._persist()
        return self.status()

    def finalize(self, api_key: str) -> Dict[str, Any]:
        return self.submit_credentials(api_key)

    def clear(self) -> Dict[str, Any]:
        self.store.delete_secret(KEY_API)
        self.store.remove_cdsapirc()
        self._state = WizardState.INIT
        self._error = ""
        self._persist()
        return self.status()
