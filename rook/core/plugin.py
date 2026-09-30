"""The Rook plugin contract, shared by the hub and workers.

A plugin is a :class:`Plugin` subclass exported from a module as ``PLUGIN``.
Its manifest lives in class attributes; its capabilities are methods marked
with :func:`capability`. See ``docs/design/plugins.md`` for the full contract.

Minimal plugin (unchanged from the original worker API)::

    class Clock(Plugin):
        NAMESPACE = "clock"

        @capability("now")
        def now(self) -> float:
            return time.time()

    PLUGIN = Clock

Everything else is optional::

    class Info(Plugin):
        NAMESPACE = "hub"
        VERSION = "1.brisk.otter"
        CORE_API = ">=1.0,<2"
        PLACEMENT = place("is_hub", run="one")
        SETTINGS = (
            setting("greeting", str, default="hi", scope="hub",
                    env="ROOK_HUB_GREETING", label="Greeting"),
            resource("embedder", default="cap://any/embed.text"),
        )
        GUIDANCE = {"hub.info": "Call this first to learn what the hub runs."}
        SKILL = "## hub\\nhub.info returns ..."

        @capability("info", risk="read", tool=True, fields="*")
        def info(self) -> dict: ...

This module is stdlib-only: it ships inside the worker bundle.
"""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

log = logging.getLogger("rook.core.plugin")

#: The plugin API version this core implements. Plugins declare the range they
#: support in ``CORE_API``; minor bumps are additive, a major bump breaks.
CORE_API_VERSION = "1.0"

RISKS = ("read", "write", "exec", "admin")
SCOPES = ("hub", "band", "worker", "user")
RUN_MODES = ("all", "one")
VERSION_RE = re.compile(r"^\d+\.[a-z]+\.[a-z]+$")

_UNSET = object()  # sentinel: distinguishes "no PLUGIN export" from "PLUGIN = None"


# -- capabilities ------------------------------------------------------------

@dataclass(frozen=True)
class CapMeta:
    """Declared contract of one capability. Enforced by the core registry
    (``limit``/``fields``), the permissions layer (``risk``) and the hub MCP
    bridge (``tool``)."""

    suffix: str = ""
    risk: str | None = None          # read|write|exec|admin; None = undeclared (treated as exec)
    tags: tuple = ()                 # sensitive|destructive|physical (permissions 2.1)
    limit: int | None = None         # default page size, enforced by core
    fields: Any = None               # None = no projection, "*" = all by default, [..] = default keys
    tool: bool = False               # also expose as a dedicated MCP tool (hub-placed plugins)
    description: str | None = None   # overrides the docstring's first paragraph for tools/rosters

    def describe(self) -> dict:
        out: dict[str, Any] = {}
        if self.risk:
            out["risk"] = self.risk
        if self.tags:
            out["tags"] = list(self.tags)
        if self.limit:
            out["limit"] = self.limit
        if self.fields is not None:
            out["fields"] = self.fields if self.fields == "*" else list(self.fields)
        if self.tool:
            out["tool"] = True
        return out


def capability(suffix: str = "", *, risk: str | None = None, limit: int | None = None,
               fields: Any = None, tool: bool = False,
               description: str | None = None, tier: str | None = None,
               tags: Iterable[str] = ()) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a `Plugin` method as a capability.

    The full dotpath is ``f"{Plugin.NAMESPACE}.{suffix}"`` or just
    ``Plugin.NAMESPACE`` when suffix is empty. Sub-namespaces in `suffix` are
    fine (``"env.get"`` → ``"shell.env.get"``).

    Keyword metadata is optional and additive (see :class:`CapMeta`); a bare
    ``@capability("x")`` behaves exactly as it always has. ``tier`` is an
    alias of ``risk`` (the permissions spec's name for it).
    """
    if tier is not None:
        if risk is not None and risk != tier:
            raise ValueError("pass risk= or tier=, not both")
        risk = tier
    tags = tuple(tags)
    if risk is not None and risk not in RISKS:
        raise ValueError(f"risk must be one of {RISKS}, got {risk!r}")
    if limit is not None and (not isinstance(limit, int) or limit <= 0):
        raise ValueError("limit must be a positive int")
    if fields is not None and fields != "*" and not (
            isinstance(fields, (list, tuple)) and all(isinstance(f, str) for f in fields)):
        raise ValueError('fields must be None, "*" or a list of names')
    has_meta = any(v not in (None, False, ()) for v in (risk, limit, fields, tool, description, tags))
    meta = CapMeta(suffix=suffix, risk=risk, tags=tags, limit=limit,
                   fields=tuple(fields) if isinstance(fields, list) else fields,
                   tool=tool, description=description)

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        setattr(fn, "_rook_cap_suffix", suffix)
        if has_meta:
            setattr(fn, "_rook_cap_meta", meta)
        return fn

    return deco


# -- placement ---------------------------------------------------------------

@dataclass(frozen=True)
class Placement:
    """Where a plugin runs: a predicate over node facts plus a run mode.
    ``run="all"``: every matching node. ``run="one"``: a single node per band,
    chosen by the hub (only honoured for hub placement in this wave)."""

    where: Any = None     # expression string, callable(NodeFacts) -> bool, or None (anywhere)
    run: str = "all"

    def describe(self) -> dict:
        where = self.where if (self.where is None or isinstance(self.where, str)) \
            else getattr(self.where, "__name__", "<callable>")
        return {"where": where, "run": self.run}


def place(where: Any = None, run: str = "all") -> Placement:
    if run not in RUN_MODES:
        raise ValueError(f"run must be one of {RUN_MODES}")
    if isinstance(where, str):
        from .facts import compile_placement
        compile_placement(where)  # fail at import time on a typo
    return Placement(where=where, run=run)


#: Default placement for plugins that don't declare one: workers only. Built-in
#: worker plugins keep loading on every worker; nothing lands on the hub unless
#: it asks for ``is_hub``.
DEFAULT_PLACEMENT = Placement(where="not is_hub", run="all")


# -- settings & resources ----------------------------------------------------

_TYPES: dict[str, type] = {"str": str, "int": int, "float": float, "bool": bool,
                           "list": list, "dict": dict, "resource": str}


@dataclass(frozen=True)
class Setting:
    """One entry of a plugin's settings schema. The schema drives validation,
    env overrides, the (wave-2) settings UI and its history."""

    name: str
    type: str = "str"
    default: Any = None
    scope: str = "hub"            # hub|band|worker|user
    secret: bool = False          # value lives in the vault, never in settings storage
    env: str | None = None        # env var that overrides the stored value
    label: str = ""
    help: str = ""
    choices: tuple = ()

    def describe(self) -> dict:
        out = {"name": self.name, "type": self.type, "scope": self.scope,
               "label": self.label or self.name}
        if not self.secret:
            out["default"] = self.default
        for k in ("secret", "env", "help"):
            v = getattr(self, k)
            if v:
                out[k] = v
        if self.choices:
            out["choices"] = list(self.choices)
        return out

    def coerce(self, value: Any) -> Any:
        if value is None:
            return None
        want = _TYPES[self.type]
        if self.type == "bool" and isinstance(value, str):
            v = value.strip().lower()
            if v in ("1", "true", "yes", "on"):
                return True
            if v in ("0", "false", "no", "off", ""):
                return False
            raise ValueError(f"{self.name}: not a boolean: {value!r}")
        if self.type in ("list", "dict") and isinstance(value, str):
            import json
            value = json.loads(value)
        if self.type in ("int", "float") and isinstance(value, bool):
            raise ValueError(f"{self.name}: expected {self.type}")
        out = want(value) if not isinstance(value, want) else value
        if self.type == "resource":
            parse_resource(out)
        if self.choices and out not in self.choices:
            raise ValueError(f"{self.name}: {out!r} not in {list(self.choices)}")
        return out


def setting(name: str, type: "type | str" = str, default: Any = None, *,
            scope: str = "hub", secret: bool = False, env: str | None = None,
            label: str = "", help: str = "", choices: Iterable = ()) -> Setting:
    tname = type if isinstance(type, str) else getattr(type, "__name__", "")
    if tname not in _TYPES:
        raise ValueError(f"setting {name!r}: unsupported type {type!r}")
    if scope not in SCOPES:
        raise ValueError(f"setting {name!r}: scope must be one of {SCOPES}")
    s = Setting(name=name, type=tname, default=default, scope=scope, secret=secret,
                env=env, label=label, help=help, choices=tuple(choices))
    if default is not None and not secret:
        s.coerce(default)  # a bad default is a plugin bug; fail at import time
    return s


def resource(name: str, default: str | None = None, *, scope: str = "hub",
             env: str | None = None, label: str = "", help: str = "",
             secret: bool = False) -> Setting:
    """A connection string setting: ``cap://<worker|any>/<cap>``, ``http(s)://``,
    ``sqlite:///path``. Operator-set; the plugin reads it with
    :meth:`Plugin.resource`."""
    return setting(name, "resource", default, scope=scope, env=env, label=label,
                   help=help, secret=secret)


RESOURCE_SCHEMES = ("cap", "http", "https", "sqlite", "file")


@dataclass(frozen=True)
class Resource:
    url: str
    scheme: str
    target: str = ""    # cap: worker name/id or "any"; http: host[:port]
    path: str = ""      # cap: the capability; sqlite/file: filesystem path
    _caller: Any = field(default=None, compare=False, repr=False)

    async def call(self, args: dict | None = None, timeout: float = 15.0) -> Any:
        """Invoke a ``cap://`` resource through the host's band caller."""
        if self.scheme != "cap":
            raise TypeError(f"{self.url}: only cap:// resources are callable")
        if self._caller is None:
            raise RuntimeError("this host cannot place band calls")
        return await self._caller(self.path, args or {}, self.target, timeout)


def parse_resource(url: str, caller: Any = None) -> Resource:
    u = urlparse(url)
    scheme = (u.scheme or "").lower()
    if scheme not in RESOURCE_SCHEMES:
        raise ValueError(f"unsupported resource scheme in {url!r}; use one of {RESOURCE_SCHEMES}")
    if scheme == "cap":
        cap = u.path.lstrip("/")
        if not u.netloc or not cap:
            raise ValueError(f"cap resource must look like cap://<worker|any>/<cap>, got {url!r}")
        return Resource(url, scheme, u.netloc, cap, caller)
    if scheme in ("sqlite", "file"):
        return Resource(url, scheme, u.netloc, u.path)
    return Resource(url, scheme, u.netloc, u.path)


class SettingsView:
    """Resolved settings for one plugin: env override > stored value > secret
    (vault) > default. Invalid values fall through to the next source with a
    warning, so a typo in an env var degrades instead of killing the plugin."""

    def __init__(self, plugin: "Plugin", stored: dict | None = None,
                 secrets: Callable[[str], "str | None"] | None = None) -> None:
        self._plugin = plugin
        self._schema = {s.name: s for s in plugin.SETTINGS}
        self._stored = dict(stored or {})
        self._secrets = secrets

    def schema(self) -> list[dict]:
        return [s.describe() for s in self._schema.values()]

    def vault_key(self, name: str) -> str:
        return f"plugin.{self._plugin.NAMESPACE}.{name}"

    def source(self, name: str) -> str:
        s = self._schema[name]
        if s.env and os.environ.get(s.env) is not None:
            return "env"
        if s.secret:
            return "vault"
        if name in self._stored:
            return "stored"
        return "default"

    def __getitem__(self, name: str) -> Any:
        s = self._schema.get(name)
        if s is None:
            raise KeyError(f"{self._plugin.NAMESPACE}: no setting {name!r}")
        candidates: list[tuple[str, Any]] = []
        if s.env and os.environ.get(s.env) is not None:
            candidates.append((f"env {s.env}", os.environ[s.env]))
        if s.secret:
            if self._secrets is not None:
                try:
                    candidates.append(("vault", self._secrets(self.vault_key(name))))
                except Exception:
                    log.warning("%s: vault lookup failed", self.vault_key(name))
        elif name in self._stored:
            candidates.append(("stored", self._stored[name]))
        for where, raw in candidates:
            if raw is None:
                continue
            try:
                return s.coerce(raw)
            except (ValueError, TypeError) as e:
                log.warning("%s.%s: ignoring invalid %s value (%s)",
                            self._plugin.NAMESPACE, name, where, e)
        return s.default

    def get(self, name: str, default: Any = None) -> Any:
        try:
            v = self[name]
        except KeyError:
            return default
        return default if v is None else v

    def as_dict(self, reveal_secrets: bool = False) -> dict:
        return {n: (self[n] if reveal_secrets or not s.secret else "***")
                for n, s in self._schema.items()}


# -- the plugin base ---------------------------------------------------------

class Plugin:
    """Subclass and set :attr:`NAMESPACE`. Decorate methods with `@capability`.

    Override :meth:`start`/`stop` for setup/teardown if needed. Every manifest
    attribute below is optional; defaults keep the original worker behaviour.
    """

    NAMESPACE: str = ""
    NAME: str = ""                       # defaults to the module stem
    VERSION: str = ""                    # <build>.<adjective>.<noun>; defaults to the host build
    CORE_API: str = ">=1.0,<2"           # range of CORE_API_VERSION this plugin supports
    PLACEMENT: Placement | None = None   # None = DEFAULT_PLACEMENT (workers only)
    SETTINGS: tuple = ()                 # setting(...) / resource(...) entries
    MIGRATIONS: str | None = None        # dir of NNN_name.sql files, relative to the module
    GUIDANCE: dict = {}                  # slot -> default guidance text (operator-editable)
    SKILL: str = ""                      # markdown fragment merged into the agent skill doc
    PANEL: dict | None = None            # optional web panel {"title", "path"}

    _module: str = ""   # source module stem; set by the loader / admin enable
    _source: str = ""   # "package:<pkg>" or "entry_point:<name>"

    def __init__(self) -> None:
        if not self.NAMESPACE:
            raise ValueError(f"{type(self).__name__} must set NAMESPACE")

    # capabilities ------------------------------------------------------
    def caps(self) -> dict[str, Callable[..., Any]]:
        out: dict[str, Callable[..., Any]] = {}
        for name in dir(self):
            attr = getattr(self, name, None)
            if attr is None or not callable(attr):
                continue
            suffix = getattr(attr, "_rook_cap_suffix", None)
            if suffix is None:
                continue
            full = self.NAMESPACE if not suffix else f"{self.NAMESPACE}.{suffix}"
            out[full] = attr
        return out

    # host-provided context ---------------------------------------------
    @property
    def settings(self) -> SettingsView:
        view = self.__dict__.get("_settings")
        if view is None:
            view = SettingsView(self)
            self.__dict__["_settings"] = view
        return view

    def resource(self, name: str) -> Resource | None:
        """The operator-configured connection for resource setting ``name``."""
        url = self.settings.get(name)
        if not url:
            return None
        return parse_resource(url, self.__dict__.get("_cap_caller"))

    @property
    def data_dir(self) -> Path:
        """Private state directory for this plugin on this node (created on
        first use): ``<node state>/plugins/<namespace>``."""
        base = self.__dict__.get("_data_root")
        path = Path(base) / self.NAMESPACE if base else Path(".rook-plugin-data") / self.NAMESPACE
        path.mkdir(parents=True, exist_ok=True)
        return path

    def migrate(self, conn: Any) -> list[int]:
        """Apply this plugin's pending SQL migrations to ``conn`` (sqlite3);
        call it from :meth:`start` after opening the store."""
        from .migrations import apply
        return apply(conn, self.migrations_path(), self.NAMESPACE)

    def migrations_path(self) -> Path | None:
        if not self.MIGRATIONS:
            return None
        mod = importlib.import_module(type(self).__module__)
        base = Path(getattr(mod, "__file__", "") or ".").parent
        return base / self.MIGRATIONS

    def manifest(self) -> dict:
        placement = self.PLACEMENT or DEFAULT_PLACEMENT
        m = {
            "name": self.NAME or self._module or self.NAMESPACE,
            "namespace": self.NAMESPACE,
            "version": self.VERSION or "",
            "core_api": self.CORE_API,
            "placement": placement.describe(),
            "caps": sorted(self.caps()),
        }
        if self.SETTINGS:
            m["settings"] = [s.describe() for s in self.SETTINGS]
        if self.MIGRATIONS:
            m["migrations"] = self.MIGRATIONS
        if self.GUIDANCE:
            m["guidance"] = sorted(self.GUIDANCE)
        if self.SKILL:
            m["skill"] = True
        if self.PANEL:
            m["panel"] = dict(self.PANEL)
        return m

    # lifecycle hooks ---------------------------------------------------
    def available(self) -> bool:
        """Whether this plugin can actually function on this host. Override to
        gate on a backend or config (a display for screenshots, an input tool
        for HID, PIKVM_URL, etc.) — returning False skips loading it, so the
        worker never announces capabilities it can't fulfill. Checked once at
        worker start; a worker.restart re-evaluates it."""
        return True

    def heartbeat(self) -> dict | None:
        """Optional compact status merged into the worker's announce (~every
        30s) under this plugin's namespace, so live state (battery level,
        temperature, load…) rides the heartbeat the whole band already sees —
        no polling. Keep it TINY and cheap (a few scalar fields); it runs on
        every announce and inflates the packet. Return ``None`` (the default)
        to contribute nothing. Must not raise — core guards it, but be quick."""
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


# -- versioning --------------------------------------------------------------

def _vtuple(v: str) -> tuple[int, ...]:
    parts = []
    for p in str(v).strip().split("."):
        if not p.isdigit():
            break
        parts.append(int(p))
    if not parts:
        raise ValueError(f"not a version: {v!r}")
    while len(parts) < 2:
        parts.append(0)
    return tuple(parts)


def core_api_compatible(spec: str, have: str = CORE_API_VERSION) -> bool:
    """Whether ``have`` satisfies a range like ``">=1.0,<2"``, ``"==1.0"`` or a
    bare major ``"1"`` (meaning ``>=1.0,<2``). An unparsable spec is
    incompatible."""
    try:
        h = _vtuple(have)
        spec = (spec or "").strip()
        if not spec:
            return True
        if spec[0].isdigit():
            lo = _vtuple(spec)
            return lo <= h < (lo[0] + 1, 0)
        for clause in spec.split(","):
            clause = clause.strip()
            m = re.match(r"^(>=|<=|==|!=|>|<)\s*([\d.]+)$", clause)
            if not m:
                return False
            op, v = m.group(1), _vtuple(m.group(2))
            ok = {">=": h >= v, "<=": h <= v, "==": h == v, "!=": h != v,
                  ">": h > v, "<": h < v}[op]
            if not ok:
                return False
        return True
    except ValueError:
        return False


def valid_version(v: str) -> bool:
    return bool(VERSION_RE.match(v or ""))


# -- discovery ---------------------------------------------------------------

@dataclass
class Candidate:
    """A discovered plugin, before placement/availability are decided."""

    module: str
    source: str
    load: Callable[[], Any]   # returns the module's PLUGIN export (or _UNSET)


def iter_package(package_name: str) -> list[Candidate]:
    """Built-in discovery: every non-underscore module of a package."""
    pkg = importlib.import_module(package_name)
    out = []
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        modname = f"{package_name}.{info.name}"
        out.append(Candidate(info.name, f"package:{package_name}",
                             lambda m=modname: getattr(importlib.import_module(m), "PLUGIN", _UNSET)))
    return out


ENTRY_POINT_GROUP = "rook.plugins"


def iter_entry_points(group: str = ENTRY_POINT_GROUP) -> list[Candidate]:
    """Third-party discovery: installed distributions declaring
    ``[project.entry-points."rook.plugins"] name = "pkg.module:PLUGIN"``
    (or just ``"pkg.module"``, whose ``PLUGIN`` export is used)."""
    try:
        from importlib.metadata import entry_points
        eps = entry_points()
        selected = eps.select(group=group) if hasattr(eps, "select") else eps.get(group, [])
    except Exception:
        return []
    out = []
    for ep in selected:
        def load(ep=ep):
            obj = ep.load()
            if isinstance(obj, type) or isinstance(obj, Plugin) or obj is None:
                return obj
            return getattr(obj, "PLUGIN", _UNSET)  # entry point named a module
        out.append(Candidate(ep.name, f"entry_point:{ep.name}", load))
    return out


def load_plugins(package_name: str, registry: Any,
                 enabled: list[str] | None = None,
                 disabled: set[str] | None = None) -> list[Plugin]:
    """Import every module under `package_name`, instantiate any `Plugin` it
    exports as ``PLUGIN`` (class or instance), register its capabilities, and
    return the live plugin instances.

    `enabled` filters by plugin module name (the file's stem). `None` = all.
    `disabled` is a set of module names to skip (persisted runtime disables);
    it wins over `enabled`.

    Kept for compatibility (worker CLI, runtime enable); it performs no
    placement check. New code uses :class:`rook.core.host.PluginHost`.
    """
    from .host import PluginHost
    host = PluginHost(registry=registry, check_placement=False)
    return host.load(iter_package(package_name), enabled=enabled, disabled=disabled)
