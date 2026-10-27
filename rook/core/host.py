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

Plugins can be switched at runtime through the same path as boot:
:meth:`PluginHost.enable`, :meth:`PluginHost.disable` and
:meth:`PluginHost.reload` (stop, re-read settings, start again). The node's
side hook (``bind_worker`` / ``bind_host``) is attached once with
:meth:`PluginHost.set_binder` and runs for every plugin activated later too.

This module is stdlib-only: it ships inside the worker bundle.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Iterable

from . import context
from .facts import NodeFacts, evaluate_placement
from .plugin import (CORE_API_VERSION, DEFAULT_PLACEMENT, Candidate, Plugin,
                     SettingsView, _UNSET, core_api_compatible, iter_entry_points,
                     iter_package, valid_version, validate_caps)
from .pluginkit import CapError
from .registry import CapabilityRegistry, CapTimeout

log = logging.getLogger("rook.core.host")



def _why_unavailable(plugin: Plugin) -> str:
    """The reason recorded when ``available()`` says no: the plugin's own
    plain-words :meth:`Plugin.unavailable_reason` when it gives one (the
    Plugins page shows it), else the generic note."""
    generic = "available() returned False"
    try:
        why = str(plugin.unavailable_reason() or "").strip()
    except Exception:
        log.exception("%s: unavailable_reason() raised", plugin.NAMESPACE)
        return generic
    return why[:400] if why else generic

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
        # Every candidate offered to load(), by module name, so enable() can
        # bring back one that was filtered out, disabled or unavailable.
        self.candidates: dict[str, Candidate] = {}
        self._binder: Callable[[Plugin], None] | None = None
        # (on_bind, on_unbind) pairs from add_bind_hook: other holders of a
        # plugin instance (the MCP bridge) that must follow a reload.
        self._hooks: list[tuple[Callable[[Plugin], None],
                                Callable[[Plugin], None] | None]] = []
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- discovery -------------------------------------------------------
    @staticmethod
    def discover(package: "str | Iterable[str] | None" = None,
                 entry_points: bool = True) -> list[Candidate]:
        """Candidates from one package or several (in order; a later package
        may hold a module of the same name only if placement keeps the two
        apart), plus ``rook.plugins`` entry points."""
        out: list[Candidate] = []
        packages = [package] if isinstance(package, str) else list(package or ())
        for pkg in packages:
            out.extend(iter_package(pkg))
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
        ``disabled`` winning (the worker's persisted runtime disables).

        A plugin whose ``DEPENDS`` namespaces are not loaded yet waits until
        the others have been tried; if they never load it is ``unavailable``."""
        disabled = disabled or set()
        loaded: list[Plugin] = []
        waiting: list[tuple[str, Candidate, Plugin]] = []
        for cand in candidates:
            self.candidates.setdefault(cand.module, cand)
            got = self._prepare(cand, enabled, disabled)
            if got is None:
                continue
            if self._missing_deps(got[2]):
                waiting.append((cand.module, cand, got[2]))
                continue
            if self._activate(cand.module, cand, got[2]):
                loaded.append(got[2])
        progress = True
        while waiting and progress:
            progress = False
            for item in list(waiting):
                if not self._missing_deps(item[2]):
                    waiting.remove(item)
                    progress = True
                    if self._activate(*item):
                        loaded.append(item[2])
        for name, _cand, plugin in waiting:
            missing = ", ".join(self._missing_deps(plugin))
            log.info("%s: needs plugin(s) %s, which did not load; skipping", name, missing)
            self._mark(name, "unavailable", f"depends on {missing} (not loaded)")
        return loaded

    def plugin(self, namespace: str) -> Plugin | None:
        """The loaded plugin with this namespace, if any."""
        return next((p for p in self.plugins if p.NAMESPACE == namespace), None)

    def loaded(self, module: str) -> Plugin | None:
        """The loaded plugin from this module (file stem), if any."""
        return next((p for p in self.plugins if p._module == module), None)

    def _missing_deps(self, plugin: Plugin) -> list[str]:
        return [ns for ns in (plugin.DEPENDS or ()) if self.plugin(ns) is None]

    def _prepare(self, cand: Candidate, enabled: list[str] | None,
                 disabled: set[str]) -> tuple[str, Candidate, Plugin] | None:
        """Filter, import, construct, check the manifest and placement."""
        name = cand.module
        if name in disabled:
            log.info("%s (%s): disabled (persisted), skipping", name, cand.source)
            self._mark(name, "disabled", "disabled by operator")
            return None
        if enabled is not None and name not in enabled:
            return None
        try:
            plugin_obj = cand.load()
        except Exception as e:
            log.exception("%s (%s): import failed, skipping", name, cand.source)
            self._mark(name, "failed", f"import: {type(e).__name__}: {e}")
            return None
        if plugin_obj is _UNSET:
            log.warning("%s (%s): no PLUGIN export, skipping", name, cand.source)
            self._mark(name, "skipped", "no PLUGIN export")
            return None
        if plugin_obj is None:
            # Intentional opt-out: the module decided it shouldn't load here
            # (e.g. an optional integration whose dependency isn't present).
            log.debug("%s: PLUGIN is None, not active on this host", name)
            self._mark(name, "skipped", "PLUGIN is None")
            return None
        try:
            plugin = plugin_obj() if isinstance(plugin_obj, type) else plugin_obj
        except Exception as e:
            log.exception("%s: constructor raised, skipping", name)
            self._mark(name, "failed", f"init: {type(e).__name__}: {e}")
            return None
        if not isinstance(plugin, Plugin):
            log.warning("%s: PLUGIN is not a Plugin instance, skipping", name)
            self._mark(name, "skipped", "PLUGIN is not a Plugin")
            return None
        if not core_api_compatible(plugin.CORE_API):
            log.warning("%s: needs core_api %s, this core is %s; skipping",
                        name, plugin.CORE_API, CORE_API_VERSION)
            self._mark(name, "failed", f"core_api {plugin.CORE_API} incompatible with {CORE_API_VERSION}")
            return None
        if plugin.VERSION and not valid_version(plugin.VERSION):
            log.warning("%s: version %r is not <build>.<adjective>.<noun>", name, plugin.VERSION)
        if self.check_placement:
            ok, why = self._placed_here(plugin)
            if not ok:
                log.info("%s: %s, skipping", name, why)
                self._mark(name, "not_placed", why)
                return None
        return name, cand, plugin

    def _wire(self, plugin: Plugin) -> None:
        stored = {}
        if self._stored_settings is not None:
            try:
                stored = self._stored_settings(plugin.NAMESPACE) or {}
            except Exception:
                log.exception("%s: loading stored settings failed", plugin.NAMESPACE)
        plugin.__dict__["_settings"] = SettingsView(plugin, stored, self._secrets)
        plugin.__dict__["_cap_caller"] = self._cap_caller
        plugin.__dict__["_data_root"] = self._data_root
        plugin.__dict__["_deps"] = {ns: self.plugin(ns) for ns in (plugin.DEPENDS or ())}

    def _activate(self, name: str, cand: Candidate, plugin: Plugin,
                  replacing: frozenset[str] = frozenset()) -> bool:
        """Wire settings/resources/deps, check available(), register caps.
        ``replacing``: caps of the instance being reloaded, which this one
        takes over in place instead of colliding with."""
        # Wired before available() (core_api 1.1) so an enable flag or a
        # configured resource in the settings schema can gate loading.
        self._wire(plugin)
        try:
            if not plugin.available():
                log.info("%s: backend/config not present here, skipping", name)
                # The namespace lets a settings change find it again
                # (retry_unavailable): it is not loaded, so nothing else does.
                self._mark(name, "unavailable", _why_unavailable(plugin),
                           namespace=plugin.NAMESPACE)
                return False
        except Exception:
            log.exception("%s: available() raised, skipping", name)
            self._mark(name, "failed", "available() raised")
            return False
        plugin._module = name   # so runtime admin can map module -> plugin
        plugin._source = cand.source
        if not plugin.VERSION:
            plugin.VERSION = self.build_version
        caps = plugin.caps()
        errors = validate_caps(caps)
        if errors:
            log.error("%s: %s; skipping plugin", name, "; ".join(errors))
            self._mark(name, "failed", "; ".join(errors))
            return False
        done: list[str] = []
        try:
            for dotpath, fn in caps.items():
                self.registry.register(dotpath, fn, replace=dotpath in replacing)
                done.append(dotpath)
        except ValueError as e:
            for d in done:
                self.registry.unregister(d)
            log.error("%s: %s; skipping plugin", name, e)
            self._mark(name, "failed", str(e))
            return False
        self.plugins.append(plugin)
        self._mark(name, "loaded", namespace=plugin.NAMESPACE, caps=len(caps))
        log.info("loaded plugin %s (ns=%s, caps=%d)", name, plugin.NAMESPACE, len(caps))
        return True

    # -- node side hook --------------------------------------------------
    def set_binder(self, binder: Callable[[Plugin], None] | None) -> None:
        """Attach the node's side hook (the worker's ``bind_worker``, the
        hub's ``bind_host``): called now for every loaded plugin and, from
        then on, for each plugin :meth:`enable`/:meth:`reload` activates,
        before its ``start``. A binder that raises is logged and recorded
        (``bind_error``); the plugin stays loaded."""
        self._binder = binder
        for p in list(self.plugins):
            self._bind(p)

    def add_bind_hook(self, on_bind: Callable[[Plugin], None],
                      on_unbind: Callable[[Plugin], None] | None = None) -> None:
        """Wiring that must follow every (re)activation, not just boot: the
        MCP bridge hands the knowledge plugin its caller and handoff store,
        and a reload builds a fresh instance without them. ``on_bind`` runs
        now for every loaded plugin and after the node's binder for each one
        activated later (before its ``start``); ``on_unbind`` runs when one
        is disabled or reloaded, before its ``stop``. Errors are logged and
        recorded like the binder's."""
        self._hooks.append((on_bind, on_unbind))
        for p in list(self.plugins):
            self._call_hook(on_bind, p)

    def _call_hook(self, hook: Callable[[Plugin], None], plugin: Plugin) -> None:
        try:
            hook(plugin)
        except Exception as e:
            log.exception("plugin %s: binding to the node failed", plugin.NAMESPACE)
            st = self.status.get(plugin._module)
            if st is not None:
                st["bind_error"] = f"{type(e).__name__}: {e}"

    def _bind(self, plugin: Plugin) -> None:
        if self._binder is not None:
            self._call_hook(self._binder, plugin)
        for on_bind, _ in self._hooks:
            self._call_hook(on_bind, plugin)

    def _unbind(self, plugin: Plugin) -> None:
        for _, on_unbind in self._hooks:
            if on_unbind is not None:
                self._call_hook(on_unbind, plugin)

    # -- lifecycle -------------------------------------------------------
    async def _start_one(self, p: Plugin) -> None:
        try:
            await p.start()
        except Exception as e:
            log.exception("plugin %s start failed", p.NAMESPACE)
            st = self.status.get(p._module)
            if st is not None:
                st["start_error"] = f"{type(e).__name__}: {e}"

    async def _stop_one(self, p: Plugin) -> None:
        try:
            await p.stop()
        except Exception:
            log.exception("plugin %s stop failed", p.NAMESPACE)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        for p in list(self.plugins):
            await self._start_one(p)

    async def stop(self) -> None:
        for p in list(self.plugins):
            await self._stop_one(p)

    # -- runtime enable / disable / reload ---------------------------------
    def _mutex(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _dependents(self, plugin: Plugin) -> list[str]:
        return sorted(p._module or p.NAMESPACE for p in self.plugins
                      if p is not plugin and plugin.NAMESPACE in (p.DEPENDS or ()))

    def _failure(self, module: str) -> str:
        st = self.status.get(module) or {}
        return f"{module} did not load: {st.get('state', 'unknown')}" + (
            f" ({st['reason']})" if st.get("reason") else "")

    async def _activate_now(self, module: str, replacing: frozenset[str] = frozenset()
                            ) -> tuple[Plugin | None, str]:
        """The boot path for one module: prepare, activate, bind, start."""
        cand = self.candidates.get(module)
        if cand is None:
            return None, f"no such plugin module: {module}"
        got = self._prepare(cand, None, set())
        if got is None:
            return None, self._failure(module)
        plugin = got[2]
        missing = self._missing_deps(plugin)
        if missing:
            self._mark(module, "unavailable", f"depends on {', '.join(missing)} (not loaded)")
            return None, self._failure(module)
        if not self._activate(module, cand, plugin, replacing):
            return None, self._failure(module)
        self._bind(plugin)
        await self._start_one(plugin)
        # Plugins that depend on this namespace now see the new instance.
        for p in self.plugins:
            if plugin.NAMESPACE in (p.DEPENDS or ()):
                p.__dict__.setdefault("_deps", {})[plugin.NAMESPACE] = plugin
                st = self.status.get(p._module)
                if st is not None and st.get("dependency_unloaded") == plugin.NAMESPACE:
                    del st["dependency_unloaded"]
        return plugin, ""

    async def _deactivate(self, plugin: Plugin) -> list[str]:
        removed = [c for c in plugin.caps() if self.registry.unregister(c)]
        self._unbind(plugin)
        await self._stop_one(plugin)
        self.plugins[:] = [p for p in self.plugins if p is not plugin]
        return removed

    async def enable(self, module: str) -> dict:
        """Load one plugin module now, through the boot path: manifest and
        placement checks, settings wired before ``available()``, caps
        registered with collision safety, the node's side hook, ``start``.
        Returns ``{"ok", "module", "namespace", "caps"}`` or ``{"ok": False,
        "error"}``; :attr:`status` records the outcome either way. Nothing is
        persisted here (the node does that)."""
        async with self._mutex():
            p = self.loaded(module)
            if p is not None:
                return {"ok": True, "module": module, "namespace": p.NAMESPACE,
                        "already_loaded": True, "caps": sorted(p.caps())}
            plugin, err = await self._activate_now(module)
            if plugin is None:
                return {"ok": False, "module": module, "error": err}
            log.info("enabled plugin %s at runtime", module)
            return {"ok": True, "module": module, "namespace": plugin.NAMESPACE,
                    "caps": sorted(plugin.caps())}

    async def disable(self, module: str, *, reason: str = "disabled by operator") -> dict:
        """Unregister one loaded plugin's caps and stop it. Refused while
        another loaded plugin ``DEPENDS`` on it."""
        async with self._mutex():
            p = self.loaded(module)
            if p is None:
                return {"ok": False, "module": module, "error": f"plugin not loaded: {module}"}
            needed = self._dependents(p)
            if needed:
                return {"ok": False, "module": module,
                        "error": f"{module} is needed by {', '.join(needed)}; disable those first"}
            removed = await self._deactivate(p)
            self._mark(module, "disabled", reason)
            log.info("disabled plugin %s (%d caps removed)", module, len(removed))
            return {"ok": True, "module": module, "namespace": p.NAMESPACE,
                    "caps_removed": removed}

    async def reload(self, module: str) -> dict:
        """Stop a loaded plugin and load it again with freshly read settings
        (``apply="reload"`` settings take effect here): a new instance, a new
        ``available()`` check, the side hook, ``start``.

        The old instance's caps stay registered until the new instance
        replaces them, so an announce sent mid-reload still lists them (a
        call in that short window reaches the stopping instance). Caps the
        new instance no longer has are dropped once it is up.

        If it no longer loads (an enable setting turned off, ``available()``
        false, a failed import), it stays unloaded, its caps go, and the reply
        says why. Plugins that ``DEPENDS`` on it stay loaded but their
        ``dependency()`` returns ``None`` (their status gets
        ``dependency_unloaded``), so they refuse calls instead of using the
        stopped instance; enabling or reloading the plugin rebinds them."""
        async with self._mutex():
            p = self.loaded(module)
            if p is None:
                return {"ok": False, "module": module, "error": f"plugin not loaded: {module}"}
            old_caps = frozenset(p.caps())
            self._unbind(p)
            await self._stop_one(p)
            self.plugins[:] = [x for x in self.plugins if x is not p]
            plugin, err = await self._activate_now(module, replacing=old_caps)
            if plugin is None:
                for cap in old_caps:
                    self.registry.unregister(cap)
                orphaned = self._orphan_dependents(p.NAMESPACE)
                log.warning("plugin %s did not come back after a reload: %s%s", module, err,
                            f" (dependents left without it: {', '.join(orphaned)})"
                            if orphaned else "")
                return {"ok": False, "module": module, "unloaded": True, "error": err,
                        **({"dependents_unbound": orphaned} if orphaned else {})}
            for cap in old_caps - set(plugin.caps()):
                self.registry.unregister(cap)
            log.info("reloaded plugin %s", module)
            return {"ok": True, "module": module, "namespace": plugin.NAMESPACE,
                    "caps": sorted(plugin.caps())}

    def _orphan_dependents(self, namespace: str) -> list[str]:
        """Point every loaded dependent's ``dependency(namespace)`` at nothing
        (the instance it held is stopped). Returns their module names."""
        out = []
        for d in self.plugins:
            if namespace in (d.DEPENDS or ()):
                d.__dict__.setdefault("_deps", {})[namespace] = None
                st = self.status.get(d._module)
                if st is not None:
                    st["dependency_unloaded"] = namespace
                out.append(d._module or d.NAMESPACE)
        return sorted(out)

    def request_reload(self, module: str) -> bool:
        """Schedule :meth:`reload` from any thread (settings change hooks run
        in a cap's worker thread). Returns False when the host has not been
        started on a loop yet; the next start reads the new settings anyway."""
        return self._schedule(lambda: self.reload(module), f"reloading {module}")

    def retry_unavailable(self, namespace: str) -> list[str]:
        """Schedule :meth:`enable` for the modules of ``namespace`` that are
        not loaded only because ``available()`` said no (never one an
        operator switched off): a setting that plugin waits for (an address,
        an engine choice) was just changed, so it may load now. Returns the
        modules scheduled."""
        out = []
        for module, st in list(self.status.items()):
            if (st.get("state") == "unavailable" and st.get("namespace") == namespace
                    and self.loaded(module) is None
                    and self._schedule(lambda m=module: self.enable(m), f"enabling {module}")):
                out.append(module)
        return out

    def _schedule(self, make, what: str) -> bool:
        loop = self._loop
        if loop is None or loop.is_closed():
            return False

        def go() -> None:
            task = loop.create_task(make())
            task.add_done_callback(lambda t: t.cancelled() or t.exception() is None
                                   or log.error("%s failed: %s", what, t.exception()))
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            go()
        else:
            loop.call_soon_threadsafe(go)
        return True

    def settings_changed(self, namespaces: Iterable[str] | None = None,
                         stored: Callable[[str], dict] | None = None) -> dict:
        """Apply changed settings: re-read stored values into every plugin's
        view (``apply="live"`` needs nothing more) and reload the plugins in
        ``namespaces`` that declare an ``apply="reload"`` setting. Returns
        ``{"refreshed": n, "reloading": [modules]}``."""
        refreshed = self.refresh_settings(stored)
        wanted = set(namespaces) if namespaces is not None else None
        reloading = []
        for p in list(self.plugins):
            if wanted is not None and p.NAMESPACE not in wanted:
                continue
            if any(s.apply == "reload" for s in p.SETTINGS) and self.request_reload(p._module):
                reloading.append(p._module)
        return {"refreshed": refreshed, "reloading": reloading}

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

    def refresh_settings(self, stored: Callable[[str], dict] | None = None) -> int:
        """Re-read stored settings into every loaded plugin's view (after an
        operator change). Returns how many plugins were refreshed."""
        source = stored or self._stored_settings
        if source is None:
            return 0
        n = 0
        for p in self.plugins:
            if not p.SETTINGS:
                continue
            try:
                p.settings.refresh(source(p.NAMESPACE) or {})
                n += 1
            except Exception:
                log.exception("%s: refreshing stored settings failed", p.NAMESPACE)
        return n

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
        except CapTimeout as e:
            return {"ok": False, "error": f"timeout: {e}"}
        except TypeError as e:  # includes ArgError
            try:
                hint = self.registry.arg_hint(cap, args, suggest="Did you mean" not in str(e))
            except Exception:  # noqa: BLE001 - the hint is a courtesy
                hint = ""
            return {"ok": False, "error": f"bad args: {e}{hint}"}
        except CapError as e:
            # An expected, cap-authored failure: the message is the whole
            # error -- no exception type prefix, no traceback in the log.
            return {"ok": False, "error": str(e)}
        except Exception as e:
            log.exception("capability %s raised", cap)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            context.caller_identity.reset(tok)
