"""Hub deploy configuration (one JSON file).

Looked up as ``--config``, then ``$ROOK_HUB_DEPLOY_CONFIG``, then
``/etc/rook/hub-deploy.json``. Example (systemd, the recommended layout)::

    {
      "root": "/opt/rook-hub",
      "mode": "systemd",
      "systemd": {"scope": "system"},
      "python": "/opt/rook-hub/venv/bin/python",
      "databases": ["/var/lib/rook/*.db"],
      "keep": 5,
      "deadman_minutes": 10,
      "services": {
        "dashboard": {"unit": "rook-hub-dashboard.service",
                      "health": ["http://127.0.0.1:7005/"], "order": 10},
        "mcp": {"unit": "rook-hub-mcp.service",
                "health": ["tcp://127.0.0.1:8765"], "order": 90}
      }
    }

``mode`` picks the ONE release selector: ``systemd`` = a single
``<unit>.d/90-release.conf`` drop-in per unit; ``symlink`` = one
``current-<service>`` symlink per service (for installs not run by systemd, or
systemd units that point at the symlink).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("/etc/rook/hub-deploy.json")
DROPIN_NAME = "90-release.conf"

# The hub's two services. Preflight imports every ``modules`` entry and every
# submodule of each ``packages`` entry (except ``__main__``) with the service's
# interpreter, from the new release, before anything is switched.
DEFAULT_SERVICES = {
    "dashboard": {"unit": "rook-hub-dashboard.service", "order": 10,
                  "modules": ["rook.remote.bootstrap"], "packages": ["rook.remote"]},
    "mcp": {"unit": "rook-hub-mcp.service", "order": 90, "disruptive": True,
            "modules": ["rook.band_mcp.server"],
            "packages": ["rook.band_mcp", "rook.knowledge"]},
}


class ConfigError(Exception):
    pass


@dataclass
class Service:
    name: str
    unit: str = ""
    modules: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    health: list[str] = field(default_factory=list)
    order: int = 50
    python: str = ""
    restart: str = ""          # shell command; overrides systemctl restart
    link: str = ""             # symlink mode: path of the selector symlink
    disruptive: bool = False   # restarting it drops clients (the MCP: every agent)


@dataclass
class Config:
    path: Path
    root: Path
    mode: str
    python: str
    scope: str = "system"
    unit_dir: Path | None = None
    databases: list[str] = field(default_factory=list)
    keep: int = 5
    deadman_minutes: float = 10
    health_timeout: float = 60
    settle_seconds: float = 3
    pubkey: str = ""
    services: dict[str, Service] = field(default_factory=dict)

    @property
    def releases(self) -> Path:
        return self.root / "releases"

    @property
    def state(self) -> Path:
        return self.root / "state"

    def release_dir(self, version: str) -> Path:
        return self.releases / version

    def ordered(self, names=None) -> list[Service]:
        svcs = [s for s in self.services.values() if names is None or s.name in names]
        return sorted(svcs, key=lambda s: (s.order, s.name))

    def python_for(self, svc: Service) -> str:
        return svc.python or self.python

    def systemd_unit_dir(self) -> Path:
        if self.unit_dir:
            return self.unit_dir
        if self.scope == "user":
            base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
            return Path(base) / "systemd" / "user"
        return Path("/etc/systemd/system")

    def dropin_path(self, svc: Service) -> Path:
        return self.systemd_unit_dir() / f"{svc.unit}.d" / DROPIN_NAME

    def link_path(self, svc: Service) -> Path:
        return Path(svc.link).expanduser() if svc.link else self.root / f"current-{svc.name}"


def resolve_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("ROOK_HUB_DEPLOY_CONFIG", "").strip()
    return Path(env).expanduser() if env else DEFAULT_PATH


def load(explicit: str | None = None) -> Config:
    path = resolve_path(explicit)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"no hub deploy config at {path} (pass --config or set "
                          "ROOK_HUB_DEPLOY_CONFIG; see docs/operations/hub-deploy.md)") from None
    except ValueError as e:
        raise ConfigError(f"{path}: invalid JSON: {e}") from None
    return from_dict(raw, path)


def from_dict(raw: dict, path: Path = Path("<memory>")) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    root = raw.get("root")
    if not root:
        raise ConfigError("config needs 'root' (the directory holding releases/ and state/)")
    mode = raw.get("mode", "systemd")
    if mode not in ("systemd", "symlink"):
        raise ConfigError(f"mode must be 'systemd' or 'symlink', not {mode!r}")
    sd = raw.get("systemd") or {}
    scope = sd.get("scope", "system")
    if scope not in ("system", "user"):
        raise ConfigError("systemd.scope must be 'system' or 'user'")
    python = raw.get("python") or ""
    if not python:
        raise ConfigError("config needs 'python' (the services' interpreter, e.g. the hub venv)")
    services_raw = raw.get("services")
    if services_raw is None:
        services_raw = DEFAULT_SERVICES
    if not isinstance(services_raw, dict) or not services_raw:
        raise ConfigError("'services' must be a non-empty object")
    services = {}
    known = set(Service.__dataclass_fields__) - {"name"}
    for name, spec in services_raw.items():
        spec = dict(spec or {})
        base = dict(DEFAULT_SERVICES.get(name, {}))
        base.update(spec)
        unknown = set(base) - known
        if unknown:
            raise ConfigError(f"service {name}: unknown keys {sorted(unknown)}")
        svc = Service(name=name, **base)
        if isinstance(svc.health, str):
            svc.health = [svc.health]
        if mode == "systemd" and not svc.unit:
            raise ConfigError(f"service {name}: mode 'systemd' needs 'unit'")
        if mode == "symlink" and not (svc.unit or svc.restart):
            raise ConfigError(f"service {name}: needs 'restart' (a command) or 'unit'")
        services[name] = svc
    cfg = Config(
        path=path, root=Path(root).expanduser(), mode=mode, python=python, scope=scope,
        unit_dir=Path(sd["unit_dir"]).expanduser() if sd.get("unit_dir") else None,
        databases=list(raw.get("databases") or []),
        keep=int(raw.get("keep", 5)),
        deadman_minutes=float(raw.get("deadman_minutes", 10)),
        health_timeout=float(raw.get("health_timeout", 60)),
        settle_seconds=float(raw.get("settle_seconds", 3)),
        pubkey=raw.get("pubkey", "") or "",
        services=services,
    )
    if cfg.keep < 2:
        raise ConfigError("keep must be at least 2 (current + one rollback target)")
    return cfg
