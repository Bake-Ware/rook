"""Base classes for worker plugins."""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any, Callable

from .registry import CapabilityRegistry

log = logging.getLogger("rook.worker.plugin")

_UNSET = object()  # sentinel: distinguishes "no PLUGIN export" from "PLUGIN = None"


def capability(suffix: str = "") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a `Plugin` method as a capability.

    The full dotpath is ``f"{Plugin.NAMESPACE}.{suffix}"`` or just
    ``Plugin.NAMESPACE`` when suffix is empty. Sub-namespaces in `suffix` are
    fine (``"env.get"`` → ``"shell.env.get"``).
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        setattr(fn, "_rook_cap_suffix", suffix)
        return fn

    return deco


class Plugin:
    """Subclass and set :attr:`NAMESPACE`. Decorate methods with `@capability`.

    Override :meth:`start`/`stop` for setup/teardown if needed.
    """

    NAMESPACE: str = ""
    _module: str = ""   # source module stem; set by load_plugins / admin enable

    def __init__(self) -> None:
        if not self.NAMESPACE:
            raise ValueError(f"{type(self).__name__} must set NAMESPACE")

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

    def available(self) -> bool:
        """Whether this plugin can actually function on this host. Override to
        gate on a backend or config (a display for screenshots, an input tool
        for HID, PIKVM_URL, etc.) — returning False skips loading it, so the
        worker never announces capabilities it can't fulfill. Checked once at
        worker start; a worker.restart re-evaluates it."""
        return True

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def load_plugins(package_name: str, registry: CapabilityRegistry,
                 enabled: list[str] | None = None,
                 disabled: set[str] | None = None) -> list[Plugin]:
    """Import every module under `package_name`, instantiate any `Plugin` it
    exports as ``PLUGIN`` (class or instance), register its capabilities, and
    return the live plugin instances.

    `enabled` filters by plugin module name (the file's stem). `None` = all.
    `disabled` is a set of module names to skip (persisted runtime disables);
    it wins over `enabled`.
    """
    pkg = importlib.import_module(package_name)
    disabled = disabled or set()
    instances: list[Plugin] = []
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        if info.name in disabled:
            log.info("%s.%s: disabled (persisted), skipping", package_name, info.name)
            continue
        if enabled is not None and info.name not in enabled:
            continue
        mod = importlib.import_module(f"{package_name}.{info.name}")
        plugin_obj = getattr(mod, "PLUGIN", _UNSET)
        if plugin_obj is _UNSET:
            log.warning("%s.%s: no PLUGIN export, skipping", package_name, info.name)
            continue
        if plugin_obj is None:
            # Intentional opt-out: the module decided it shouldn't load here
            # (e.g. an optional integration whose dependency isn't present).
            log.debug("%s.%s: PLUGIN is None, not active on this host", package_name, info.name)
            continue
        plugin = plugin_obj() if isinstance(plugin_obj, type) else plugin_obj
        if not isinstance(plugin, Plugin):
            log.warning("%s.%s: PLUGIN is not a Plugin instance, skipping",
                        package_name, info.name)
            continue
        try:
            if not plugin.available():
                log.info("%s.%s: backend/config not present here, skipping",
                         package_name, info.name)
                continue
        except Exception:
            log.exception("%s.%s: available() raised, skipping", package_name, info.name)
            continue
        plugin._module = info.name  # so runtime admin can map module -> plugin
        for dotpath, fn in plugin.caps().items():
            registry.register(dotpath, fn)
        instances.append(plugin)
        log.info("loaded plugin %s (ns=%s, caps=%d)", info.name,
                 plugin.NAMESPACE, len(plugin.caps()))
    return instances
