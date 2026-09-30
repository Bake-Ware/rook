"""The plugin host: one loader/lifecycle for the hub and for workers.

A :class:`PluginHost` owns a capability registry, the node's facts, and the
live plugin instances. It discovers plugins (built-in package scan and
``rook.plugins`` entry points), checks each one's manifest (``CORE_API``
compatibility, version shape), placement (over this node's facts) and
``available()``, wires in its settings/resources, registers its caps, and
runs ``start``/``stop``/``heartbeat`` with failure isolation: one plugin that
fails to import, raises, or collides with another's caps is recorded as
``failed`` and every other plugin still loads.

The worker (:class:`rook.worker.core.Worker`) and the hub
(:class:`rook.hub.node.HubNode`) each build one host and differ only in the
facts they pass (the hub holds the ``hub`` role) and in how calls arrive.

This module is stdlib-only: it ships inside the worker bundle.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from . import context
from .facts import NodeFacts, evaluate_placement
from .plugin import (CORE_API_VERSION, DEFAULT_PLACEMENT, Candidate, Plugin,
                     SettingsView, _UNSET, core_api_compatible, iter_entry_points,
                     iter_package, valid_version)
from .registry import CapabilityRegistry

log = logging.getLogger("rook.core.host")


class PluginHost:
    def __init__(self, registry: CapabilityRegistry | None = None,
                 facts: NodeFacts | None = None, *,
                 build_version: str = "0.dev",
                 check_placement: bool = True,
                 elect_one: Callable[[Plugin], bool] | None = None,
                 stored_settings: Callable[[str], dict] | None = None,
                 secrets: Callable[[str], "str | None"] | None = None,
                 cap_caller: Any = None,
                 data_root: str | None = None) -> None:
        self.registry = registry if registry is not None else CapabilityRegistry()
        self.facts = facts or NodeFacts()
        self.build_version = build_version
        self.check_placement = check_placement
        # run="one": a single node per band runs the plugin. The hub decides
        # (it sees the roster); without an elector a node declines.
        self._elect_one = elect_one
        self._stored_settings = stored_settings
        self._secrets = secrets
        self._cap_caller = cap_caller
        self._data_root = data_root
        self.plugins: list[Plugin] = []
        self.status: dict[str, dict] = {}

    # -- discovery -------------------------------------------------------
    @staticmethod
    def discover(package: str | None = None, entry_points: bool = True) -> list[Candidate]:
        out: list[Candidate] = []
        if package:
            out.extend(iter_package(package))
        if entry_points:
            out.extend(iter_entry_points())
        return out

    # -- loading ---------------------------------------------------------
    def _mark(self, name: str, state: str, reason: str = "", **extra: Any) -> None:
        self.status[name] = {"state": state, **({"reason": reason} if reason else {}), **extra}

    def _placed_here(self, plugin: Plugin) -> tuple[bool, str]:
        placement = plugin.PLACEMENT or DEFAULT_PLACEMENT
        if not evaluate_placement(placement.where, self.facts):
            return False, f"placement {placement.describe()['where']!r} does not match this node"
        if placement.run == "one":
            if self._elect_one is None:
                return False, "run='one' needs hub election; not elected here"
            try:
                if not self._elect_one(plugin):
                    return False, "run='one': another node was elected"
            except Exception:
                log.exception("elector raised for %s", plugin.NAMESPACE)
                return False, "run='one': elector failed"
        return True, ""

    def load(self, candidates: Iterable[Candidate], enabled: list[str] | None = None,
             disabled: set[str] | None = None) -> list[Plugin]:
        """Load candidates; returns the newly loaded plugins (also appended to
        :attr:`plugins`). ``enabled``/``disabled`` filter by module name, with
        ``disabled`` winning (the worker's persisted runtime disables)."""
        disabled = disabled or set()
        loaded: list[Plugin] = []
        for cand in candidates:
            name = cand.module
            if name in disabled:
                log.info("%s (%s): disabled (persisted), skipping", name, cand.source)
                self._mark(name, "disabled", "disabled by operator")
                continue
            if enabled is not None and name not in enabled:
                continue
            try:
                plugin_obj = cand.load()
            except Exception as e:
                log.exception("%s (%s): import failed, skipping", name, cand.source)
                self._mark(name, "failed", f"import: {type(e).__name__}: {e}")
                continue
            if plugin_obj is _UNSET:
                log.warning("%s (%s): no PLUGIN export, skipping", name, cand.source)
                self._mark(name, "skipped", "no PLUGIN export")
                continue
            if plugin_obj is None:
                # Intentional opt-out: the module decided it shouldn't load here
                # (e.g. an optional integration whose dependency isn't present).
                log.debug("%s: PLUGIN is None, not active on this host", name)
                self._mark(name, "skipped", "PLUGIN is None")
                continue
            try:
                plugin = plugin_obj() if isinstance(plugin_obj, type) else plugin_obj
            except Exception as e:
                log.exception("%s: constructor raised, skipping", name)
                self._mark(name, "failed", f"init: {type(e).__name__}: {e}")
                continue
            if not isinstance(plugin, Plugin):
                log.warning("%s: PLUGIN is not a Plugin instance, skipping", name)
                self._mark(name, "skipped", "PLUGIN is not a Plugin")
                continue
            if not core_api_compatible(plugin.CORE_API):
                log.warning("%s: needs core_api %s, this core is %s; skipping",
                            name, plugin.CORE_API, CORE_API_VERSION)
                self._mark(name, "failed", f"core_api {plugin.CORE_API} incompatible with {CORE_API_VERSION}")
                continue
            if plugin.VERSION and not valid_version(plugin.VERSION):
                log.warning("%s: version %r is not <build>.<adjective>.<noun>", name, plugin.VERSION)
            if self.check_placement:
                ok, why = self._placed_here(plugin)
                if not ok:
                    log.info("%s: %s, skipping", name, why)
                    self._mark(name, "not_placed", why)
                    continue
            try:
                if not plugin.available():
                    log.info("%s: backend/config not present here, skipping", name)
                    self._mark(name, "unavailable", "available() returned False")
                    continue
            except Exception:
                log.exception("%s: available() raised, skipping", name)
                self._mark(name, "failed", "available() raised")
                continue
            plugin._module = name   # so runtime admin can map module -> plugin
            plugin._source = cand.source
            if not plugin.VERSION:
                plugin.VERSION = self.build_version
            stored = {}
            if self._stored_settings is not None:
                try:
                    stored = self._stored_settings(plugin.NAMESPACE) or {}
                except Exception:
                    log.exception("%s: loading stored settings failed", name)
            plugin.__dict__["_settings"] = SettingsView(plugin, stored, self._secrets)
            plugin.__dict__["_cap_caller"] = self._cap_caller
            plugin.__dict__["_data_root"] = self._data_root
            caps = plugin.caps()
            done: list[str] = []
            try:
                for dotpath, fn in caps.items():
                    self.registry.register(dotpath, fn)
                    done.append(dotpath)
            except ValueError as e:
                for d in done:
                    self.registry.unregister(d)
                log.error("%s: %s; skipping plugin", name, e)
                self._mark(name, "failed", str(e))
                continue
            self.plugins.append(plugin)
            loaded.append(plugin)
            self._mark(name, "loaded", namespace=plugin.NAMESPACE, caps=len(caps))
            log.info("loaded plugin %s (ns=%s, caps=%d)", name, plugin.NAMESPACE, len(caps))
        return loaded

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        for p in list(self.plugins):
            try:
                await p.start()
            except Exception as e:
                log.exception("plugin %s start failed", p.NAMESPACE)
                st = self.status.get(p._module)
                if st is not None:
                    st["start_error"] = f"{type(e).__name__}: {e}"

    async def stop(self) -> None:
        for p in list(self.plugins):
            try:
                await p.stop()
            except Exception:
                log.exception("plugin %s stop failed", p.NAMESPACE)

    def heartbeats(self) -> dict:
        hb: dict = {}
        for p in self.plugins:
            try:
                d = p.heartbeat()
            except Exception:
                log.debug("heartbeat() raised for plugin %s", getattr(p, "NAMESPACE", "?"),
                          exc_info=True)
                continue
            if d:
                hb[p.NAMESPACE] = d
        return hb

    # -- introspection ---------------------------------------------------
    def manifests(self) -> list[dict]:
        out = []
        for p in self.plugins:
            m = p.manifest()
            m["source"] = p._source
            m.update(self.status.get(p._module, {}))
            out.append(m)
        return out

    def settings_schema(self) -> dict[str, list[dict]]:
        return {p.NAMESPACE: p.settings.schema() for p in self.plugins if p.SETTINGS}

    def tiers(self) -> dict[str, str]:
        """Compact ``{cap: r|w|x|a}`` map of declared risk tiers, for the
        announce (``docs/design/permissions.md`` 2.2). Undeclared caps are
        left out; receivers fall back to their built-in table."""
        out = {}
        for c in self.registry.list():
            risk = getattr(self.registry.meta(c), "risk", None)
            if risk:
                out[c] = {"read": "r", "write": "w", "exec": "x", "admin": "a"}[risk]
        return out

    def tool_caps(self) -> list[str]:
        """Caps whose metadata asks for a dedicated MCP tool."""
        return [c for c in self.registry.list()
                if getattr(self.registry.meta(c), "tool", False)]

    # -- dispatch --------------------------------------------------------
    async def dispatch(self, cap: str, args: Any, identity: str | None = None) -> dict:
        """Run one cap and build the band reply body (``ok`` + ``result`` or
        ``error``), with the same error shapes a worker produces."""
        if not self.registry.has(cap):
            return {"ok": False, "error": f"unknown capability: {cap}"}
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return {"ok": False, "error": "args must be an object"}
        tok = context.caller_identity.set(identity)
        try:
            return {"ok": True, "result": await self.registry.call(cap, **args)}
        except TypeError as e:
            return {"ok": False, "error": f"bad args: {e}"}
        except Exception as e:
            log.exception("capability %s raised", cap)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            context.caller_identity.reset(tok)
