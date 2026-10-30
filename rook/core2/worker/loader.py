"""Turn one plugin folder (or a builtin manifest) into bound, callable caps.

Pure loading: no event loop, no hooks, no state beyond the returned
:class:`Plugin`. The host (host.py) owns lifecycle, locks and calls.

A cap is **available** when this worker can serve it. It isn't when the
plugin or the cap names other ``platforms``, ``requires`` a missing ``bins``
entry, ``hardware`` this worker doesn't report, or ``settings`` that have no
value, or when its action type is unknown. Unavailable caps stay in the
manifest (the hub knows them) and are announced with the reason.

``run:`` targets are ``file.py:func`` or ``file.py:Class.method``. A class is
instantiated once per load as ``Class(ctx)``, with the plugin's own context
(the plugin, no person, as hooks get), and shared by every cap and hook of
that load; a reload builds new modules and new instances from scratch
(castling-manifest-python v1.1, gap D7 in castling/coverage.md).
"""

from __future__ import annotations

import functools
import inspect
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator

from rook.core2 import wire
from rook.kit._types import parse_duration
from rook.kit.actions.engine import _local_os as local_os   # one answer for worker.os
from rook.kit.ctx import KitError, load_run
from rook.kit.manifest import ManifestError, check, compile_fields, normalize

DEFAULT_TIMEOUT = 15.0   # spec 6.5: a cap without a declared timeout gets 15 s


class LoadError(Exception):
    """The folder can't become a plugin: bad manifest, missing or broken script."""


class PluginExit(Exception):
    """Plugin code called sys.exit. asyncio re-raises SystemExit out of the
    event loop, so it is turned into an ordinary error where it happens."""


@dataclass
class Cap:
    name: str
    spec: dict                         # the normalized manifest entry
    func: Callable[..., Any] | None    # run: target or builtin handler
    action: dict | None                # action: block
    bind: dict                         # fixed args, merged over the caller's
    timeout: float | None              # None: ``timeout: none``, never times out
    validator: Draft202012Validator


@dataclass
class Plugin:
    name: str
    version: str
    hash: str                    # spec 5.3: sha256 of canonical(parsed manifest)
    raw: dict                    # the parsed manifest, as the hub validated it
    manifest: dict               # normalized (defaults, caps by full name)
    folder: Path | None
    builtin: bool
    settings: dict               # merged values (vault names for secrets)
    caps: dict[str, Cap] = field(default_factory=dict)            # available, aliases included
    unavailable: dict[str, str] = field(default_factory=dict)     # cap -> reason
    reason: str | None = None    # set when the whole plugin can't run here (hooks don't start)
    modules: dict = field(default_factory=dict)
    instances: dict = field(default_factory=dict)
    new_ctx: Callable[[], Any] | None = None   # the plugin's own ctx, for Class(ctx)

    def resolve(self, ref: str) -> Callable[..., Any]:
        """A run target from this load (shared module cache and instances)."""
        if self.folder is None:
            raise KitError(f"{self.name}: a builtin plugin has no scripts")
        return resolve_run(self.folder, ref, self.modules, self.instances, self.new_ctx)

    def forget_modules(self) -> None:
        """Drop this load's script modules from sys.modules (unload)."""
        for mod in self.modules.values():
            if sys.modules.get(mod.__name__) is mod:
                del sys.modules[mod.__name__]
        self.modules.clear()
        self.instances.clear()


def resolve_run(folder: Path, ref: str, modules: dict, instances: dict,
                new_ctx: Callable[[], Any] | None = None) -> Callable[..., Any]:
    """``file.py:func`` or ``file.py:Class.method`` under ``folder``; a class
    is built once per ``instances`` dict as ``Class(new_ctx())``."""
    path, _, name = ref.partition(":")
    cls_name, dot, method = name.partition(".")
    if not dot:
        return _contain(load_run(folder, ref, modules))
    cls = load_run(folder, f"{path}:{cls_name}", modules)
    if not isinstance(cls, type):
        raise KitError(f"run target {ref!r}: {cls_name} is not a class")
    key = (path, cls_name)
    if key not in instances:
        instances[key] = cls(new_ctx() if new_ctx else None)
    func = getattr(instances[key], method, None)
    if not callable(func):
        raise KitError(f"run target {ref!r}: no method {method}")
    return _contain(func)


def _contain(func: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def run(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except SystemExit as e:
                raise PluginExit(f"exited with code {e.code}") from None
    else:
        @functools.wraps(func)
        def run(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except SystemExit as e:
                raise PluginExit(f"exited with code {e.code}") from None
    return run


def merge_settings(decls: Mapping, local: Mapping, hub: Mapping) -> dict:
    """Declared settings only. ``where: worker`` values come from this machine
    (falling back to what the hub sent for it); ``hub`` and ``person`` values
    only from the hub, so a local file can't override a band-wide value.

    TODO(castling:hub-dispatch): ``where: person`` values belong to the
    caller; until the call carries them they come from the hub map."""
    out = {}
    for key, decl in decls.items():
        if decl.get("where", "worker") == "worker" and key in local:
            out[key] = local[key]
        elif key in hub:
            out[key] = hub[key]
    return out


def _missing(requires: Mapping, os_name: str, hardware: set, settings: Mapping, decls: Mapping) -> str | None:
    bins = [b for b in requires.get("bins") or () if shutil.which(b) is None]
    if bins:
        return f"missing bin: {', '.join(bins)}"
    hw = [h for h in requires.get("hardware") or () if h not in hardware]
    if hw:
        return f"missing hardware: {', '.join(hw)}"
    unset = [s for s in requires.get("settings") or ()
             if settings.get(s, (decls.get(s) or {}).get("default")) in (None, "")]
    if unset:
        return f"setting not set: {', '.join(unset)}"
    return None


def _platform(platforms, os_name: str) -> str | None:
    if platforms and os_name not in platforms:
        return f"not on {os_name} (runs on {', '.join(platforms)})"
    return None


def load_folder(folder, *, os_name: str | None = None, hardware=(),
                values: Callable[[str], tuple[Mapping, Mapping]] | None = None,
                plugin_ctx: Callable[[Plugin], Any] | None = None,
                contracts: dict | None = None) -> Plugin:
    """Validate ``folder``'s plugin.yaml and bind its worker caps.
    ``values(plugin)`` gives that plugin's (worker-local, hub) setting values;
    ``plugin_ctx(plugin)`` builds the ctx a ``Class.method`` class is made with.
    Raises LoadError for an invalid manifest or a script that won't import."""
    folder = Path(folder).resolve()
    data, issues = check(folder, contracts)
    if data is None or any(i.level == "error" for i in issues):
        raise LoadError(str(ManifestError(issues)))
    if data.get("builtin"):
        raise LoadError(f"{data['plugin']}: builtin manifests are announced by core, not loaded from a folder")
    local, hub = values(data["plugin"]) if values else ({}, {})
    return _bind(data, folder, None, os_name or local_os(), set(hardware), local, hub, plugin_ctx)


def load_builtin(data: dict, handlers: Mapping[str, Callable[..., Any]], *,
                 os_name: str | None = None, hardware=()) -> Plugin:
    """A ``builtin: true`` manifest (core, the voice service) with the Python
    callables that implement its caps, ``handler(ctx, **args)`` keyed by full
    cap name. A cap with no handler is unavailable."""
    if not data.get("builtin"):
        raise LoadError(f"{data.get('plugin')}: not a builtin manifest")
    return _bind(data, None, handlers, os_name or local_os(), set(hardware), {}, {})


def _bind(data: dict, folder: Path | None, handlers, os_name: str, hardware: set,
          local: Mapping, hub: Mapping, plugin_ctx: Callable[[Plugin], Any] | None = None) -> Plugin:
    norm = normalize(data)
    decls = norm.get("settings") or {}
    settings = merge_settings(decls, local, hub)
    try:
        digest = wire.manifest_hash(data)
    except ValueError as e:   # no canonical form (spec 3.2): the hub couldn't have signed it either
        raise LoadError(f"{norm['plugin']}: {e}") from None
    p = Plugin(name=norm["plugin"], version=str(norm["version"]), hash=digest,
               raw=data, manifest=norm, folder=folder, builtin=bool(norm.get("builtin")),
               settings=settings)
    if plugin_ctx is not None:
        p.new_ctx = lambda: plugin_ctx(p)
    p.reason = _platform(norm.get("platforms"), os_name) or \
        _missing(norm.get("requires") or {}, os_name, hardware, settings, decls)
    try:
        for name, spec in norm["caps"].items():
            if spec.get("on", "worker") != "worker":
                continue   # a hub cap: the hub serves it
            why = p.reason or spec.get("unavailable") or _platform(spec.get("platforms"), os_name) \
                or _missing(spec.get("requires") or {}, os_name, hardware, settings, decls)
            func = None
            if why is None and "run" in spec:
                func = p.resolve(spec["run"])
            elif why is None and handlers is not None:
                func = handlers.get(name)
                if func is None:
                    why = "not implemented on this worker"
            elif why is None and "action" not in spec:
                why = "no action or run"   # record caps and the like: served elsewhere
            if why is not None:
                p.unavailable[name] = why
                continue
            cap = Cap(name, spec, func, spec.get("action"), dict(spec.get("bind") or {}),
                      _timeout(spec), Draft202012Validator(compile_fields(spec.get("args") or {})))
            for alias in [name, *spec.get("aliases", ())]:
                p.caps[alias] = cap
    except BaseException as e:   # a script that fails to import (even sys.exit) fails the load only
        p.forget_modules()
        if isinstance(e, KeyboardInterrupt):
            raise
        raise LoadError(f"{p.name}: {getattr(e, 'message', None) or f'{type(e).__name__}: {e}'}") from e
    return p


def _timeout(spec: dict) -> float | None:
    """Absent: the 15 s default. ``timeout: none`` (normalized to None, manifest 1.1): never."""
    if "timeout" not in spec:
        return DEFAULT_TIMEOUT
    return None if spec["timeout"] is None else parse_duration(spec["timeout"])
