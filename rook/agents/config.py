"""The session host's configuration (a JSON file; the link token is in the environment)."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def find_claude(configured: str = "") -> str:
    """The claude binary: the configured path, else PATH, else ~/.local/bin/claude."""
    if configured:
        return os.path.expanduser(configured)
    found = shutil.which("claude")
    if found:
        return found
    return str(_home() / ".local" / "bin" / "claude")


def _pair(v, default: tuple[float, float], name: str) -> tuple[float, float]:
    if v is None:
        return default
    try:
        lo, hi = (float(x) for x in v)
    except (TypeError, ValueError):
        raise ConfigError(f"{name}: [min seconds, max seconds]") from None
    if lo <= 0 or hi < lo:
        raise ConfigError(f"{name}: 0 < min <= max")
    return lo, hi


@dataclass
class Config:
    hub_url: str = ""
    host: str = ""
    token_env: str = "ROOK_AGENTS_LINK_TOKEN"
    roles_dir: str = ""
    voice_listen: str = "0.0.0.0:8791"
    claude: str = ""
    max_forks: int = 2
    claude_config_dir: str = ""
    #: The Claude Code user config whose ``mcpServers`` may be granted to roles.
    claude_user_config: str = ""
    #: Lane restart after a crash: first delay, cap (doubling).
    restart_backoff: tuple[float, float] = (2.0, 60.0)
    #: Link reconnect: first delay, cap (doubling).
    link_backoff: tuple[float, float] = (1.0, 30.0)
    #: Most link events kept while the hub is unreachable.
    buffer_events: int = 1000
    #: Seconds a by-the-way question may take.
    btw_timeout_s: float = 90.0
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        if not isinstance(d, dict):
            raise ConfigError("the config is a JSON object")
        cfg = cls()
        for key in ("hub_url", "host", "token_env", "roles_dir", "voice_listen", "claude",
                    "claude_config_dir", "claude_user_config"):
            if key in d and d[key] is not None:
                setattr(cfg, key, str(d[key]))
        if "max_forks" in d:
            cfg.max_forks = max(0, int(d["max_forks"]))
        if "btw_timeout_s" in d:
            cfg.btw_timeout_s = max(1.0, float(d["btw_timeout_s"]))
        if "buffer_events" in d:
            cfg.buffer_events = max(1, int(d["buffer_events"]))
        cfg.restart_backoff = _pair(d.get("restart_backoff_s"), cfg.restart_backoff,
                                    "restart_backoff_s")
        cfg.link_backoff = _pair(d.get("link_backoff_s"), cfg.link_backoff, "link_backoff_s")
        if cfg.hub_url and not cfg.hub_url.startswith(("ws://", "wss://")):
            raise ConfigError("hub_url: a ws:// or wss:// URL")
        if not cfg.roles_dir:
            raise ConfigError("roles_dir is required")
        cfg.roles_dir = os.path.expanduser(cfg.roles_dir)
        cfg.claude = find_claude(cfg.claude)
        if not cfg.claude_config_dir:
            cfg.claude_config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or str(_home() / ".claude")
        cfg.claude_config_dir = os.path.expanduser(cfg.claude_config_dir)
        if not cfg.claude_user_config:
            cfg.claude_user_config = str(_home() / ".claude.json")
        cfg.claude_user_config = os.path.expanduser(cfg.claude_user_config)
        if not cfg.host:
            cfg.host = os.uname().nodename
        cfg.extra = {k: v for k, v in d.items() if k not in cfg.__dataclass_fields__}
        return cfg

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Config":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ConfigError(f"cannot read {path}: {e}") from None
        return cls.from_dict(data)

    def voice_addr(self) -> tuple[str, int] | None:
        v = (self.voice_listen or "").strip()
        if not v:
            return None
        host, _, port = v.rpartition(":")
        try:
            return (host.strip("[]") or "0.0.0.0"), int(port)
        except ValueError:
            raise ConfigError("voice_listen: host:port") from None
