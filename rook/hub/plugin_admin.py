"""Band-wide plugin management behind Manage > Plugins.

One view of every plugin on the hub and on each online worker, built from what
already exists: the hub node's own plugin host and each worker's
``worker.plugin.list`` (the same listing the Settings worker page shows).
Switches go through ``worker.plugin.enable/disable/reload`` on workers (they
persist on the worker) and :meth:`rook.hub.node.HubNode.set_plugin_enabled`
on the hub (persisted in the settings store). Band defaults for new workers
and presets live in :mod:`rook.hub.plugin_profiles`.

Offline workers are never called: the roster only holds live ones. Each
worker's listing is cached for :data:`LIST_TTL` seconds and dropped after a
switch on it, so the page can refresh freely.

Starlette-free (the account API in ``plugins_web`` wraps it), so it is tested
directly.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import inspect
import logging
import pkgutil
import time
from typing import Any

from ..core.authz import TIERS
from . import plugin_profiles as pp
from .plugin_catalog import NOT_IMPLEMENTED, CatalogCache

log = logging.getLogger("rook.hub.plugin_admin")

LIST_TTL = 45.0   # longer than the page's 20 s poll: most polls hit the cache
LIST_TIMEOUT = 8.0
TOGGLE_TIMEOUT = 25.0
_TIER_CODE = {"r": "read", "w": "write", "x": "exec", "a": "admin"}
#: What a plugin's highest tier means to someone deciding whether to turn it on.
HIGH = ("exec", "admin")


def _first_line(text: str | None) -> str:
    for line in (text or "").strip().splitlines():
        line = line.strip()
        if line:
            # "shell.* — run shell commands" -> "run shell commands"
            for sep in (" — ", " - "):
                head, found, rest = line.partition(sep)
                if found and ("*" in head or "." in head) and len(head) < 60:
                    line = rest[:1].upper() + rest[1:]
                    break
            return line[:240]
    return ""


def _doc(fn: Any) -> str:
    meta = getattr(fn, "_rook_cap_meta", None)
    if meta is not None and getattr(meta, "description", None):
        return meta.description
    return _first_line(inspect.getdoc(fn))


@functools.lru_cache(maxsize=1)
def plugin_index() -> dict[str, dict]:
    """``{"hub:<module>" | "worker:<module>": info}`` for the built-in plugin
    classes (imported, never constructed). Modules that do not import here
    are left out; the matrix fills them from what workers report."""
    from ..core.plugin import DEFAULT_PLACEMENT
    from .settings_schema import PLUGIN_TITLES
    out: dict[str, dict] = {}
    for side, package in (("hub", "rook.hub.plugins"), ("worker", "rook.worker.plugins")):
        try:
            pkg = importlib.import_module(package)
        except Exception:
            log.exception("cannot import %s for the plugin index", package)
            continue
        for info in pkgutil.iter_modules(pkg.__path__):
            if info.name.startswith("_"):
                continue
            try:
                mod = importlib.import_module(f"{package}.{info.name}")
            except Exception as err:
                log.warning("plugin index: %s.%s does not import (%s)", package, info.name, err)
                continue
            obj = getattr(mod, "PLUGIN", None)
            cls = obj if isinstance(obj, type) else (type(obj) if obj is not None else None)
            if cls is None or not getattr(cls, "NAMESPACE", ""):
                continue
            caps = []
            for cap, fn in sorted(cls.declared_caps().items()):
                meta = getattr(fn, "_rook_cap_meta", None)
                caps.append({"cap": cap, "risk": getattr(meta, "risk", None) or "exec",
                             "doc": _doc(fn)})
            ns = cls.NAMESPACE
            title = PLUGIN_TITLES.get(ns) or info.name.replace("_", " ").capitalize()
            out[f"{side}:{info.name}"] = {
                "side": side, "module": info.name, "namespace": ns, "title": title,
                "description": _first_line(cls.__doc__) or _first_line(mod.__doc__),
                "origin": "builtin", "caps": caps,
                "settings": len(getattr(cls, "SETTINGS", ()) or ()),
                "placement": (cls.PLACEMENT or DEFAULT_PLACEMENT).describe()["where"],
                "depends": list(getattr(cls, "DEPENDS", ()) or ()),
            }
    return out


def _risk(tiers: set[str]) -> dict:
    ordered = sorted((t for t in tiers if t in TIERS), key=TIERS.index)
    return {"tiers": ordered, "top": ordered[-1] if ordered else None}


def _origin_of(source: str | None, module: str) -> str:
    source = source or ""
    if source.startswith("entry_point:"):
        return "entry-point"
    if "rook_android" in source:
        return "android"
    if source.startswith("package:"):
        return "builtin"
    if module.endswith("_android") or module in pp.ANDROID_RISK:
        return "android"
    return "unknown"


def _kind(reason: str) -> str:
    r = (reason or "").lower()
    if "placement" in r or "run='one'" in r:
        return "platform" if ("app " in r or "app=" in r or "'os'" in r or "os ==" in r
                              or "has(" in r) else "placement"
    if "depends on" in r:
        return "dependency"
    if "plugin is none" in r or "import" in r:
        return "dependency"
    if "available()" in r or "backend" in r or "setting" in r:
        return "available"
    return "available"


def cell_from_status(loaded: bool, st: dict, *, disabled: bool = False,
                     excluded: bool = False, protected: bool = False,
                     reason: str = "") -> dict:
    """One matrix cell: ``state`` on|off|unavailable|error, a ``reason``/
    ``kind`` when it is not on, and the ``toggle`` allowed (enable|disable|None)."""
    state = st.get("state") or ""
    why = st.get("reason") or reason or ""
    if loaded:
        err = st.get("start_error") or st.get("bind_error")
        if err:
            return {"state": "error", "reason": str(err), "kind": "start",
                    "toggle": None if protected else "disable"}
        return {"state": "on", "toggle": None if protected else "disable"}
    if disabled or state == "disabled":
        return {"state": "off", "reason": why or "switched off by an operator", "kind": "operator",
                "toggle": "enable"}
    if excluded:
        return {"state": "off", "reason": why or "left out by --enable", "kind": "excluded",
                "toggle": "enable"}
    if state == "not_placed":
        return {"state": "unavailable", "reason": why, "kind": _kind(why), "toggle": None}
    if state == "failed":
        return {"state": "error", "reason": why, "kind": "failed", "toggle": "enable"}
    if state == "skipped":
        return {"state": "unavailable", "reason": why or "the module opted out here",
                "kind": "dependency", "toggle": None}
    kind = _kind(why)
    return {"state": "unavailable", "reason": why or "not available here (backend or setting "
            "missing)", "kind": kind, "toggle": "enable"}


class PluginAdmin:
    def __init__(self, node: Any, catalogs: CatalogCache | None = None) -> None:
        self.node = node
        self.catalogs = catalogs or CatalogCache()
        self._lists: dict[str, tuple[float, dict]] = {}

    # -- plumbing ----------------------------------------------------------
    @property
    def _client(self) -> Any:
        return getattr(self.node, "client", None)

    @property
    def _svc(self) -> Any:
        return getattr(self.node, "settings", None)

    def _roster(self) -> dict[str, dict]:
        client = self._client
        if client is None:
            return {}
        hub_id = getattr(self.node, "worker_id", None)
        try:
            return {wid: w for wid, w in client.workers.items()
                    if wid != hub_id and not w.get("local")}
        except Exception:
            return {}

    def _worker(self, name: str) -> tuple[str, dict]:
        roster = self._roster()
        if name in roster:
            return name, roster[name]
        hits = [(wid, w) for wid, w in roster.items() if (w.get("name") or "") == name]
        if len(hits) != 1:
            raise LookupError(f"worker {name!r} is not online" if not hits
                              else f"more than one online worker is named {name!r}")
        return hits[0]

    def _bands(self) -> list[dict]:
        svc = self._svc
        try:
            return svc.bands() if svc is not None else []
        except Exception:
            return []

    def _band_for_label(self, label: str) -> str | None:
        svc = self._svc
        try:
            return svc._band_for_label(label) if svc is not None else None
        except Exception:
            return None

    def resolve_band(self, ref: str | None) -> str | None:
        """A band id from an id, a label (enrollment or band-wire) or nothing
        (the primary band, else the only active one)."""
        bands = [b for b in self._bands() if b.get("active")]
        ref = str(ref or "").strip()
        if ref:
            for b in bands:
                if ref in (b["id"], b.get("label")):
                    return b["id"]
            got = self._band_for_label(ref)
            if got:
                return got
        primary = [b for b in bands if b.get("primary")]
        if primary:
            return primary[0]["id"]
        return bands[0]["id"] if len(bands) == 1 else (bands[0]["id"] if bands else None)

    @property
    def _store(self) -> Any:
        return getattr(self.node, "settings_store", None)

    def _journal(self, cap: str, worker: str, actor: str, args: dict, reply: dict) -> None:
        j = getattr(self.node, "journal", None)
        if j is None:
            return
        try:
            j.record(cap=cap, worker=worker, identity=actor, args=args, reply=reply,
                     audit={"actor": actor},
                     authz={"principal": actor, "decision": "allow",
                            "rule": "operator account (Plugins page)"})
        except Exception:
            log.debug("journaling %s failed", cap, exc_info=True)

    # -- listings ----------------------------------------------------------
    async def _list_one(self, wid: str, w: dict, refresh: bool) -> dict:
        hit = self._lists.get(wid)
        if hit and not refresh and time.time() - hit[0] < LIST_TTL:
            return hit[1]
        if "worker.plugin.list" not in (w.get("caps") or []):
            out = {"error": "this worker's build does not list its plugins"}
        else:
            try:
                reply = await self._client.call(cap="worker.plugin.list", args={}, target=wid,
                                                timeout=LIST_TIMEOUT,
                                                identity="system:rook-plugins")
                res = reply.get("result") if reply.get("ok", True) else None
                out = res if isinstance(res, dict) else {
                    "error": str(reply.get("error") or "no listing")}
            except Exception as err:
                out = {"error": str(err) or type(err).__name__}
        self._lists[wid] = (time.time(), out)
        return out

    def forget(self, wid: str | None = None) -> None:
        if wid is None:
            self._lists.clear()
        else:
            self._lists.pop(wid, None)

    async def matrix(self, band: str | None = None, refresh: bool = False) -> dict:
        """Rows (plugins) x columns (hub + online workers); see the module doc."""
        node = self.node
        try:
            synced = await node.sync_plugin_state("system:rook-plugins")
        except Exception:
            log.exception("syncing stored hub plugin switches failed")
            synced = []
        index = plugin_index()
        roster = self._roster()
        listings = await asyncio.gather(*(self._list_one(wid, w, refresh)
                                          for wid, w in roster.items()))
        bands = self._bands()
        band_names = {b.get("label"): b for b in bands}
        columns = [{"id": "hub", "name": getattr(node, "name", "rook"), "kind": "hub",
                    "online": True, "can_toggle": True, "can_reload": True,
                    "version": getattr(node, "version", None),
                    "os": (node.facts.hw.get("os") if getattr(node, "facts", None) else None)}]
        per_worker: dict[str, dict[str, dict]] = {}
        for (wid, w), listing in zip(roster.items(), listings):
            facts = w.get("facts") or {}
            band_id = self._band_for_label(w.get("band") or "")
            bname = next((b["name"] for b in bands if b["id"] == band_id), None) \
                or (band_names.get(w.get("band")) or {}).get("name")
            caps = w.get("caps") or []
            columns.append({
                "id": wid, "name": w.get("name") or wid[:8], "kind": "worker", "online": True,
                "band": w.get("band"), "band_id": band_id, "band_name": bname or w.get("band"),
                "os": facts.get("os"), "app": facts.get("app"), "version": w.get("version"),
                "can_toggle": "worker.plugin.enable" in caps,
                "can_reload": "worker.plugin.reload" in caps,
                "error": listing.get("error"),
                "enable_arg": listing.get("enable")})
            per_worker[wid] = {r.get("module"): r for r in listing.get("plugins") or []
                               if isinstance(r, dict) and r.get("module")}
        # Rows: every hub candidate, every built-in worker plugin, and every
        # module a worker reports (Android, entry points, newer builds).
        rows: dict[str, dict] = {}

        def row(side: str, module: str) -> dict:
            key = f"{side}:{module}"
            if key not in rows:
                base = index.get(key)
                rows[key] = {"key": key, "side": side, "module": module,
                             "namespace": (base or {}).get("namespace"),
                             "title": (base or {}).get("title")
                             or module.replace("_", " ").capitalize(),
                             "description": (base or {}).get("description", ""),
                             "origin": (base or {}).get("origin", "unknown"),
                             "caps": list((base or {}).get("caps", [])),
                             "settings": (base or {}).get("settings", 0),
                             "placement": (base or {}).get("placement"),
                             "depends": (base or {}).get("depends", []),
                             "protected": module in (pp.PROTECTED_HUB if side == "hub"
                                                     else pp.PROTECTED_WORKER),
                             "cells": {}, "_tiers": set()}
            return rows[key]

        host = node.host
        hub_state = pp.hub_state(self._store)
        for module, cand in sorted(host.candidates.items()):
            st = host.status.get(module) or {}
            builtin = f"hub:{module}" in index
            if not builtin and st.get("state") == "not_placed":
                continue   # an entry point placed on workers: its worker row shows it
            r = row("hub", module)
            if not builtin:
                r["origin"] = _origin_of(cand.source, module)
            p = host.loaded(module)
            if p is not None:
                r["namespace"] = p.NAMESPACE
                if not r["caps"]:
                    r["caps"] = [{"cap": c, "risk": getattr(host.registry.meta(c), "risk", None)
                                  or "exec", "doc": ""} for c in sorted(p.caps())]
            r["cells"]["hub"] = cell_from_status(p is not None, st, protected=r["protected"],
                                                 disabled=hub_state.get(module) is False)
            r["hub_switch"] = hub_state.get(module)
        for key, info in index.items():
            if info["side"] == "worker":
                row("worker", info["module"])
        for wid, plugins in per_worker.items():
            for module, wr in plugins.items():
                r = row("worker", module)
                if r["origin"] == "unknown":
                    r["origin"] = _origin_of(wr.get("source"), module)
                if not r["namespace"] and wr.get("namespace"):
                    r["namespace"] = wr["namespace"]
                if f"worker:{module}" not in index and wr.get("caps"):
                    tiers = (roster[wid].get("tiers") or {})
                    known = {c["cap"] for c in r["caps"]}
                    for c in wr["caps"]:
                        if c not in known:
                            r["caps"].append({"cap": c, "risk": _TIER_CODE.get(tiers.get(c), "exec"),
                                              "doc": ""})
        # Cells.
        for r in rows.values():
            if r["side"] == "worker":
                r["cells"]["hub"] = {"state": "na", "reason": "runs on workers", "kind": "placement",
                                     "toggle": None}
            for col in columns[1:]:
                wid = col["id"]
                if r["side"] == "hub":
                    r["cells"][wid] = {"state": "na", "reason": "runs on the hub",
                                       "kind": "placement", "toggle": None}
                    continue
                if col.get("error"):
                    r["cells"][wid] = {"state": "unknown", "reason": col["error"], "toggle": None}
                    continue
                wr = per_worker.get(wid, {}).get(r["module"])
                if wr is None:
                    r["cells"][wid] = {"state": "absent", "kind": "platform", "toggle": None,
                                       "reason": "not part of this worker's build"}
                    continue
                cell = cell_from_status(bool(wr.get("loaded")), wr,
                                        disabled=bool(wr.get("disabled")),
                                        excluded=bool(wr.get("excluded")),
                                        protected=r["protected"],
                                        reason=wr.get("reason") or "")
                if not col["can_toggle"]:
                    cell["toggle"] = None
                r["cells"][wid] = cell
            r["risk"] = _risk({c["risk"] for c in r["caps"]})
            r.pop("_tiers", None)
            if r["settings"] and r["namespace"]:
                r["settings_link"] = f"settings?view=plugin&target={r['namespace']}"
        band_id = self.resolve_band(band)
        defaults = pp.worker_defaults(band_id, self._store) if band_id else {}
        for r in rows.values():
            if r["side"] == "worker":
                r["default"] = defaults.get(r["module"])
        ordered = sorted(rows.values(), key=lambda r: (r["side"] != "hub", r["origin"] != "builtin",
                                                         r["title"].lower()))
        return {"columns": columns, "rows": ordered, "bands": bands, "band": band_id,
                "defaults": defaults, "presets": self.presets(band_id, hub_state),
                "synced": synced, "generated_at": time.time()}

    # -- presets -------------------------------------------------------------
    def _enabled_choices(self) -> dict[str, bool]:
        """Hub module -> its ``enabled`` setting, where an operator set one
        (stored or in the environment): a choice, unlike loaded state."""
        svc = self._svc
        out: dict[str, bool] = {}
        for module, key in pp.HUB_ENABLED_SETTINGS.items():
            try:
                if svc is not None:
                    r = svc.resolve(key)
                    if r.get("source") != "default" and isinstance(r.get("value"), bool):
                        out[module] = r["value"]
                    continue
                row = self._store.get(key, "hub", "") if self._store is not None else None
                if row and isinstance(row.get("value"), bool):
                    out[module] = row["value"]
            except Exception:
                continue
        return out

    def preset_states(self, band_id: str | None, hub_state: dict | None = None) -> dict:
        hub_state = pp.hub_state(self._store) if hub_state is None else hub_state
        defaults = pp.worker_defaults(band_id, self._store) if band_id else {}
        return pp.preset_states(hub_state, defaults, self._enabled_choices())

    def presets(self, band_id: str | None, hub_state: dict | None = None) -> list[dict]:
        """Each preset with its risk and its box: ``selected`` from the stored
        choices (``state`` on/off; ``default`` when nothing is stored or the
        plugins were switched one by one), never from what is loaded."""
        index = plugin_index()
        states = self.preset_states(band_id, hub_state)
        shown = pp.preset_selected(states)
        out = []
        for p in pp.PRESETS:
            tiers: set[str] = set()
            for side in ("hub", "worker"):
                for m in p[side]:
                    info = index.get(f"{side}:{m}")
                    if info:
                        tiers |= {c["risk"] for c in info["caps"]}
                    else:
                        tiers |= set(pp.ANDROID_RISK.get(m, ()))
            st = states[p["id"]]
            out.append({**p, "risk": _risk(tiers), "selected": shown[p["id"]],
                        "state": "default" if st is None else ("on" if st else "off")})
        return out

    async def apply_presets(self, selected: list[str], band: str | None, actor: str) -> dict:
        """Tick exactly ``selected``: for each preset whose box changed, switch
        its hub plugins now (persisted) and store the band's defaults for new
        workers. Presets left as they were change nothing."""
        selected = [pp.preset(s)["id"] for s in dict.fromkeys(selected or ())]
        band_id = self.resolve_band(band)
        hub, worker = pp.preset_changes(selected, self.preset_states(band_id))
        results = []
        stored = pp.hub_state(self._store)
        for module, on in sorted(hub.items()):
            if module not in self.node.host.candidates:
                continue
            loaded = self.node.host.loaded(module) is not None
            if on and loaded and stored.get(module) is True:
                continue
            if not on and not loaded and stored.get(module) is not True:
                continue   # already off (e.g. not configured): nothing to keep off
            res = await self.node.set_plugin_enabled(module, on, actor)
            results.append({"target": "hub", "module": module, "enable": on,
                            "ok": bool(res.get("ok")), "error": res.get("error")})
        defaults = {}
        if band_id:
            defaults = pp.set_worker_defaults(self._store, band_id, worker, actor=actor,
                                              note="preset: " + (", ".join(selected) or "none"))
        reply = {"ok": all(r["ok"] for r in results), "presets": selected, "band": band_id,
                 "hub": results, "worker_defaults": defaults,
                 "note": None if band_id else "no band yet: defaults for new workers not stored"}
        self._journal("plugin.presets.apply", "rook", actor,
                      {"presets": selected, "band": band_id}, reply)
        return reply

    def set_default(self, band: str | None, module: str, value: Any, actor: str) -> dict:
        band_id = self.resolve_band(band)
        if not band_id:
            raise ValueError("no band to store defaults for")
        if value not in (True, False, None):
            raise ValueError("value must be true, false or null")
        defaults = pp.set_worker_defaults(self._store, band_id, {module: value}, actor=actor,
                                          note="Plugins page")
        reply = {"ok": True, "band": band_id, "defaults": defaults}
        self._journal("plugin.defaults.set", "rook", actor,
                      {"band": band_id, "module": module, "value": value}, reply)
        return reply

    # -- switches ------------------------------------------------------------
    async def toggle_hub(self, module: str, enable: bool, actor: str) -> dict:
        res = await self.node.set_plugin_enabled(module, enable, actor)
        self._journal("hub.plugin.enable" if enable else "hub.plugin.disable", "rook", actor,
                      {"module": module}, res)
        return {"target": "hub", "module": module, "enable": enable, "ok": bool(res.get("ok")),
                "error": res.get("error"), "result": res}

    async def toggle_worker(self, worker: str, module: str, enable: bool, actor: str) -> dict:
        wid, w = self._worker(worker)
        name = w.get("name") or wid[:8]
        if not enable and module in pp.PROTECTED_WORKER:
            return {"target": name, "module": module, "enable": enable, "ok": False,
                    "error": f"{module} cannot be switched off remotely (it carries worker control)"}
        cap = "worker.plugin.enable" if enable else "worker.plugin.disable"
        if cap not in (w.get("caps") or []):
            return {"target": name, "module": module, "enable": enable, "ok": False,
                    "error": f"{name}'s build has no {cap}"}
        try:
            reply = await self._client.call(cap=cap, args={"module": module}, target=wid,
                                            timeout=TOGGLE_TIMEOUT, identity=actor)
        except Exception as err:
            reply = {"ok": False, "error": str(err) or type(err).__name__}
        res = reply.get("result", reply) if reply.get("ok", True) else reply
        res = res if isinstance(res, dict) else {"ok": False, "error": str(res)}
        ok = bool(reply.get("ok", True)) and bool(res.get("ok"))
        self.forget(wid)
        self._journal(cap, name, actor, {"module": module}, {"ok": ok, "result": res,
                                                             **({} if ok else {"error": res.get("error")})})
        store = self._store
        if store is not None:
            try:
                store.record(f"core.worker.plugins.{module}.enabled", "worker", name,
                             old=not enable, new=enable, actor=actor, source="plugins",
                             note="" if ok else f"failed: {res.get('error')}")
            except Exception:
                log.debug("recording the plugin switch failed", exc_info=True)
        return {"target": name, "worker_id": wid, "module": module, "enable": enable, "ok": ok,
                "error": None if ok else (res.get("error") or reply.get("error")), "result": res}

    async def toggle_all(self, module: str, enable: bool, actor: str,
                         workers: list[str] | None = None) -> dict:
        """Switch ``module`` on every online worker where that is allowed
        (``workers`` narrows it), concurrently."""
        m = await self.matrix(refresh=True)
        row = next((r for r in m["rows"] if r["key"] == f"worker:{module}"), None)
        if row is None:
            raise LookupError(f"no worker plugin {module!r}")
        want = "enable" if enable else "disable"
        names = {c["id"]: c["name"] for c in m["columns"][1:]}
        targets = [wid for wid, cell in row["cells"].items()
                   if wid != "hub" and cell.get("toggle") == want
                   and (not workers or wid in workers or names.get(wid) in workers)]
        results = await asyncio.gather(*(self.toggle_worker(wid, module, enable, actor)
                                         for wid in targets))
        return {"ok": all(r["ok"] for r in results), "module": module, "enable": enable,
                "results": list(results),
                "note": None if targets else f"no online worker can {want} {module}"}

    async def reload(self, target: str, module: str, actor: str) -> dict:
        if target in ("hub", "rook", ""):
            res = await self.node.host.reload(module)
            self._journal("hub.plugin.reload", "rook", actor, {"module": module}, res)
            return {"target": "hub", "module": module, "ok": bool(res.get("ok")),
                    "error": res.get("error"), "result": res}
        wid, w = self._worker(target)
        name = w.get("name") or wid[:8]
        if "worker.plugin.reload" not in (w.get("caps") or []):
            return {"target": name, "module": module, "ok": False,
                    "error": f"{name}'s build cannot reload plugins (update it)"}
        try:
            reply = await self._client.call(cap="worker.plugin.reload", args={"module": module},
                                            target=wid, timeout=TOGGLE_TIMEOUT, identity=actor)
        except Exception as err:
            reply = {"ok": False, "error": str(err) or type(err).__name__}
        res = reply.get("result", reply) if reply.get("ok", True) else reply
        res = res if isinstance(res, dict) else {"ok": False, "error": str(res)}
        ok = bool(reply.get("ok", True)) and bool(res.get("ok"))
        self.forget(wid)
        self._journal("worker.plugin.reload", name, actor, {"module": module},
                      {"ok": ok, "result": res})
        return {"target": name, "module": module, "ok": ok,
                "error": None if ok else res.get("error"), "result": res}

    # -- marketplace (preview) ------------------------------------------------
    def catalog_urls(self) -> list[str]:
        svc = self._svc
        if svc is not None:
            try:
                v = svc.resolve(pp.CATALOGS_KEY).get("value")
                return [str(u) for u in v or [] if str(u).strip()]
            except Exception:
                pass
        return []

    async def catalog(self, refresh: bool = False) -> dict:
        urls = self.catalog_urls()
        cats = await asyncio.to_thread(self.catalogs.load, urls, refresh)
        return {"catalogs": cats, "configured": urls, "install": False,
                "install_note": NOT_IMPLEMENTED}

    def install(self, entry_id: str, actor: str) -> dict:
        reply = {"ok": False, "error": NOT_IMPLEMENTED, "code": "not_implemented",
                 "id": str(entry_id or "")}
        self._journal("plugin.marketplace.install", "rook", actor, {"id": entry_id}, reply)
        return reply


def admin_for(node: Any) -> PluginAdmin:
    """The node's one PluginAdmin (its listing cache is shared by the page
    and the ``plugin.marketplace.*`` caps)."""
    admin = getattr(node, "plugin_admin", None)
    if admin is None:
        admin = PluginAdmin(node)
        try:
            node.plugin_admin = admin
        except Exception:
            pass
    return admin
