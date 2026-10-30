"""Read a plugin folder's plugin.yaml into a validated, normalized Manifest."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

from . import _yaml
from .validate import ACTION_KEYS, ACTION_TYPES, Issue, cap_name, record_caps, validate

MANIFEST_FILE = "plugin.yaml"


class ManifestError(ValueError):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("; ".join(str(i) for i in issues if i.level == "error"))


@dataclass
class Manifest:
    data: dict  # normalized: defaults applied, caps keyed by full name
    folder: Path | None = None
    warnings: list[Issue] = field(default_factory=list)

    @property
    def plugin(self) -> str:
        return self.data["plugin"]

    @property
    def version(self) -> str:
        return self.data["version"]

    @property
    def caps(self) -> dict:
        return self.data["caps"]


def _paths(path) -> tuple[Path, Path]:
    path = Path(path)
    return (path / MANIFEST_FILE, path) if path.is_dir() else (path, path.parent)


def check(path, contracts: dict | None = None) -> tuple[dict | None, list[Issue]]:
    """Parse and validate without raising: (data or None, issues)."""
    file, folder = _paths(path)
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return None, [Issue("", "unreadable", "error", f"{file}: {e}")]
    return check_text(text, folder, contracts)


def check_text(text: str, folder=None, contracts: dict | None = None):
    """check() for plugin.yaml text; no folder = no file-existence checks."""
    try:
        data = _yaml.loads(text)
    except _yaml.YamlError as e:
        return None, [Issue("", "yaml", "error", str(e))]
    return data, validate(data, folder, contracts)


def load(path, contracts: dict | None = None) -> Manifest:
    """A plugin folder (or its plugin.yaml) -> Manifest. Raises ManifestError
    listing every issue when there is any error; warnings ride along."""
    data, issues = check(path, contracts)
    if data is None or any(i.level == "error" for i in issues):
        raise ManifestError(issues)
    return Manifest(normalize(data), _paths(path)[1], issues)


def _field_defaults(fields: dict):
    for spec in fields.values():
        if "schema" not in spec:
            spec.setdefault("type", "str")
        spec.setdefault("required", False)


def normalize(data: dict) -> dict:
    """Defaults applied, caps keyed by full name (record caps included). Input
    is a valid parsed manifest; the result is a new dict."""
    out = copy.deepcopy(data)
    plugin = out["plugin"]
    runs_on = out.setdefault("runs_on", "worker")
    out.setdefault("builtin", False)
    for key in ("uses", "replaces", "check"):
        out.setdefault(key, [])
    for spec in out.setdefault("settings", {}).values():
        spec.setdefault("type", "secret" if spec.get("secret") else "str")
        spec.setdefault("where", "worker")
        spec.setdefault("apply", "live")
        spec.setdefault("required", False)
        spec.setdefault("secret", spec["type"] == "secret")
    caps = {}
    for rel, cap in out.get("caps", {}).items():
        c = {"effects": [], "tags": [], "args": {}, "idempotent": False, "elevation": "never",
             "on": runs_on, "aliases": [], **cap}
        if "platforms" in out:
            c.setdefault("platforms", out["platforms"])
        _field_defaults(c["args"])
        if c.get("timeout") == "none":
            c["timeout"] = None  # never times out; absent = the host's default
        unknown = sorted(set(c.get("action", {})) - set(ACTION_TYPES) - set(ACTION_KEYS))
        if unknown:
            c["unavailable"] = f"unknown action type: {', '.join(unknown)}"
        caps[cap_name(plugin, rel)] = c
    for name, c in record_caps(out).items():
        caps[name] = {"tags": [], "args": {}, "idempotent": c["risk"] == "read",
                      "elevation": "never", "aliases": [], **c}
    out["caps"] = caps
    for rec in out.get("records", {}).values():
        _field_defaults(rec["fields"])
    for contract in out.get("contracts", {}).values():
        contract.setdefault("route", "provider")
    for route in out.get("routes", {}).values():
        route.setdefault("auth", "user")
    for ev in out.get("emits", {}).values():
        ev.setdefault("taints", False)
        _field_defaults(ev.setdefault("payload", {}))
    return out
