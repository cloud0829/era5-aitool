# -*- coding: utf-8 -*-
"""凭据安全存储（design-final.md §3.5/§11：keyring + ~/.cdsapirc + 环境变量）。

- CDS：写入 ~/.cdsapirc（权限 600）+ keyring。
- DeepSeek：keyring 优先；无 keyring 后端时回退 data/.keyring.json（gitignore）。
- 任何密钥不进入代码仓库、不进 settings.json。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

from era5tool.config.settings import Settings

SERVICE = "era5-aitool"


class KeyringStore:
    """keyring 封装（带文件回退）。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._fallback_path = settings.data_dir / ".keyring.json"

    # ------------------------------------------------------------------
    def _keyring(self):
        import keyring
        return keyring

    def save_secret(self, key: str, secret: str) -> None:
        try:
            self._keyring().set_password(SERVICE, key, secret)
            return
        except Exception:
            pass
        data = self._load_fallback()
        data[key] = secret
        self._save_fallback(data)

    def get_secret(self, key: str) -> Optional[str]:
        try:
            v = self._keyring().get_password(SERVICE, key)
            if v:
                return v
        except Exception:
            pass
        return self._load_fallback().get(key)

    def delete_secret(self, key: str) -> None:
        try:
            self._keyring().delete_password(SERVICE, key)
        except Exception:
            pass
        data = self._load_fallback()
        data.pop(key, None)
        self._save_fallback(data)

    def _load_fallback(self) -> Dict[str, str]:
        if self._fallback_path.is_file():
            try:
                return json.loads(self._fallback_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_fallback(self, data: Dict[str, str]) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._fallback_path.write_text(json.dumps(data, ensure_ascii=False),
                                       encoding="utf-8")
        try:
            os.chmod(self._fallback_path, 0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # CDS .cdsapirc
    # ------------------------------------------------------------------
    @staticmethod
    def cdsapirc_path() -> Path:
        return Path(os.path.expanduser("~")) / ".cdsapirc"

    def write_cdsapirc(self, api_key: str) -> Path:
        """将用户提供的完整凭据原样写入 ~/.cdsapirc 的 ``key:`` 行。"""
        path = self.cdsapirc_path()
        content = (
            "url: https://cds.climate.copernicus.eu/api\n"
            f"key: {api_key}\n"
        )
        path.write_text(content, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    def read_cdsapirc(self) -> Dict[str, str]:
        path = self.cdsapirc_path()
        if not path.is_file():
            return {}
        out: Dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if ":" in line and not line.strip().startswith("#"):
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip()
        return out

    def remove_cdsapirc(self) -> None:
        path = self.cdsapirc_path()
        if path.exists():
            path.unlink()
