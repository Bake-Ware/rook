"""Config loader with hot reload support."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"
_ENV_PATH = _DEFAULT_CONFIG_PATH.parent / ".env"


class Config:
    """Flat, readable config. No indirection layers."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else _DEFAULT_CONFIG_PATH
        self._data: dict[str, Any] = {}
        self._mtime: float = 0.0
        # Load .env from project root
        load_dotenv(_ENV_PATH)
        self.reload()

    # -- public ---------------------------------------------------------------

    def reload(self) -> bool:
        """Reload from disk if changed. Returns True if reloaded."""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        if mtime == self._mtime:
            return False
        with open(self.path, "r", encoding="utf-8") as f:
            self._data = yaml.safe_load(f) or {}
        self._mtime = mtime
        return True

    @property
    def models(self) -> dict[str, dict[str, Any]]:
        return self._data.get("models", {})

    @property
    def default_model(self) -> str:
        return self._data.get("default_model", "local")

    @property
    def aliases(self) -> dict[str, str]:
        return self._data.get("aliases", {})

    @property
    def memory(self) -> dict[str, Any]:
        return self._data.get("memory", {})

    @property
    def voice(self) -> dict[str, Any]:
        return self._data.get("voice", {})

    @property
    def discord(self) -> dict[str, Any]:
        return self._data.get("discord", {})

    @property
    def tasks(self) -> dict[str, Any]:
        return self._data.get("tasks", {})

    def get(self, dotpath: str, default: Any = None) -> Any:
        """Dot-path lookup: config.get('voice.stt') -> 'whisper'."""
        node = self._data
        for key in dotpath.split("."):
            if isinstance(node, dict):
                node = node.get(key)
            else:
                return default
            if node is None:
                return default
        return node

    def resolve_env(self, key_env: str) -> str | None:
        """Resolve an environment variable name to its value."""
        if not key_env:
            return None
        return os.environ.get(key_env)
