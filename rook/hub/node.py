"""The hub's own band node: hub-placed plugins served as worker ``rook``.

The hub runs the same :class:`rook.core.host.PluginHost` as a worker, with
the ``hub`` role in its facts, so plugins placed with ``is_hub`` load here and
nowhere else. :class:`HubNode` puts that host on the band:

* it announces itself on every band as a worker named ``rook`` (the reserved
  name), with its caps, plugins and facts - so band members and old
  dashboards see it like any worker;
* band requests addressed to it (``target`` = its id, or an open call for a
  cap it owns) are dispatched through the host and answered on the band;
* the MCP bridge's own calls to it (``rook_call(cap, worker="rook")`` and the
  generated tools) short-circuit in-process, never touching the band.

Band-originated calls carry an unauthenticated, self-stamped identity (any
PSK holder can send one), so by default they may only reach caps declared
``risk="read"``; ``ROOK_HUB_BAND_MAX_RISK`` raises that ceiling. Calls from
the MCP bridge are token-attributed and not limited here (the permissions
task adds per-principal rules).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..core import authz as core_authz
from ..core.context import caller_identity
from ..core.facts import NodeFacts, local_facts, wire_facts
from ..core.host import PluginHost
from ..core.plugin import CORE_API_VERSION, CapMeta

log = logging.getLogger("rook.hub.node")

#: Reserved band worker name of the hub node.
HUB_WORKER_NAME = "rook"
BUILTIN_PACKAGE = "rook.hub.plugins"


def _stable_node_id(state_dir: str | None) -> str:
    if state_dir:
        path = Path(state_dir) / "hub_node_id"
        try:
            wid = path.read_text(encoding="utf-8").strip()
            if wid:
                return wid
        except OSError:
            pass
        wid = uuid.uuid4().hex
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(wid + "\n", encoding="utf-8")
        except OSError:
            log.warning("could not persist hub node id; using an ephemeral one")
        return wid
    return uuid.uuid4().hex


class HubNode:
    def __init__(self, state_dir: str | None = None, *,
                 client: Any = None,
                 vault: Any = None,
                 chat: Any = None,
                 on_band_call: Callable[[str, str | None, dict, dict], None] | None = None,
                 package: str | None = BUILTIN_PACKAGE,
                 entry_points: bool = True,
                 build_version: str | None = None,
                 band_max_risk: str | None = None,
                 enrollment: Any = None,
                 settings_store: Any = None) -> None:
        if build_version is None:
            # A deployed hub runs a named release (`rook hub deploy` sets
            # ROOK_RELEASE, e.g. 412.salty.otter: the worker version scheme).
            build_version = os.environ.get("ROOK_RELEASE", "").strip() or None
        if build_version is None:
            try:
                from ..worker._build_info import VERSION as build_version
            except Exception:
                build_version = "0.dev"
        self.worker_id = _stable_node_id(state_dir)
        self.name = HUB_WORKER_NAME
        self.version = build_version
        head = str(build_version).split(".", 1)[0]
        self.build = int(head) if head.isdigit() else 0
        self.client = client
        self._vault = vault
        # The bridge's chat store (rook.band_mcp.chat_rooms), for the chat
        # rooms plugin; None = the plugin opens chat.db in state_dir itself.
        self.chat = chat
        self._on_band_call = on_band_call
        self._state_dir = state_dir
        self.attached = False  # set by attach_hub_node once a band client serves it
        self.journal = None    # the bridge's call journal, set by attach_hub_node
        self.enrollment = enrollment
        # The shared settings store (rook.hub.settings_store). Opened lazily:
        # nothing is created on disk until a value is written or read back.
        if settings_store is None:
            from .settings_store import SettingsStore
            settings_store = SettingsStore()
        self.settings_store = settings_store
        self.settings = None  # SettingsService, set by the settings hub plugin
        # Paths plugins serve on the hub site (rook hub serve hands them over).
        from .site import SiteMounts
        self.site = SiteMounts()
        risk = band_max_risk or os.environ.get("ROOK_HUB_BAND_MAX_RISK", "read")
        self.band_max_risk = core_authz.norm_tier(risk) or "read"
        # Local authority: the hub holds the band's signing key, so its own node
        # carries the hub role without a grant.
        self.facts = NodeFacts(node_id=self.worker_id, name=self.name,
                               roles=frozenset({"is_hub"}), hw=local_facts())
        self.host = PluginHost(facts=self.facts, build_version=build_version,
                               elect_one=lambda _p: True,  # one hub node per band
                               stored_settings=self._stored_settings,
                               secrets=self._secret, cap_caller=self._cap_caller,
                               data_root=(os.path.join(state_dir, "plugins")
                                          if state_dir else None))
        reg = self.host.registry
        reg.register("caps.describe", self._caps_describe, meta=CapMeta(risk="read"))
        # Runtime plugin control on the hub, through the host's boot path
        # (the same as worker.plugin.*). Enable/disable persist in the
        # settings store (core.plugins.hub), like worker.plugin.* do on a
        # worker; the Plugins page uses the same path (set_plugin_enabled).
        reg.register("hub.plugin.enable", self._plugin_enable, meta=CapMeta(risk="admin"))
        reg.register("hub.plugin.disable", self._plugin_disable,
                     meta=CapMeta(risk="admin", tags=("destructive",)))
        reg.register("hub.plugin.reload", self._plugin_reload,
                     meta=CapMeta(risk="admin", tags=("destructive",)))
        # Hub plugins an operator switched off stay off across restarts.
        self._plugin_state = self._stored_plugin_state()
        self.host.load(self.host.discover(package, entry_points=entry_points),
                       disabled={m for m, on in self._plugin_state.items() if not on})
        # bind_host runs for these and for any plugin enabled/reloaded later.
        self.host.set_binder(self._bind_plugin)

    def _bind_plugin(self, p: Any) -> None:
        bind = getattr(p, "bind_host", None)
        if callable(bind):
            bind(self)

    def _caps_describe(self, prefix: str = "") -> dict:
        """Arg schema, docstring and declared risk/limit/fields for every hub cap.

        ``prefix`` (e.g. ``"hub."``) limits it to matching caps."""
        return self.host.registry.describe(prefix)

    @staticmethod
    def _admin_gate(what: str) -> None:
        from .authz import require_hub_admin
        refusal = require_hub_admin(what)
        if refusal:
            raise PermissionError(refusal)

    @staticmethod
    def _caller() -> str:
        return str(caller_identity.get() or "system:rook-hub")

    async def _plugin_enable(self, module: str) -> dict:
        """Load a hub plugin module now (placement, settings, available(), bind_host, start).

        Kept on across restarts. A plugin with an ``enabled`` setting has it
        turned on too (refused when the environment locks it off). Band
        owners and operators only."""
        self._admin_gate(f"hub.plugin.enable {module}")
        return await self.set_plugin_enabled(module, True, self._caller())

    async def _plugin_disable(self, module: str) -> dict:
        """Unload a hub plugin now (its caps go, its stop() runs) and keep it off.

        Refused while another loaded plugin depends on it, and for ``settings``
        (it carries the switches to turn things back on)."""
        self._admin_gate(f"hub.plugin.disable {module}")
        return await self.set_plugin_enabled(module, False, self._caller())

    # -- persisted plugin switches ---------------------------------------
    def _stored_plugin_state(self) -> dict[str, bool]:
        from .plugin_profiles import hub_state
        try:
            return hub_state(self.settings_store)
        except Exception:
            log.exception("reading the hub plugin switches failed; loading every plugin")
            return {}

    def _enabled_setting(self, module: str) -> str | None:
        """``<namespace>.enabled`` when the module's plugin declares that switch."""
        p = self.host.loaded(module)
        cls = type(p) if p is not None else None
        if cls is None:
            cand = self.host.candidates.get(module)
            try:
                obj = cand.load() if cand is not None else None
            except Exception:
                return None
            cls = obj if isinstance(obj, type) else (type(obj) if obj is not None else None)
        if cls is None or not getattr(cls, "NAMESPACE", ""):
            return None
        if any(getattr(s, "name", "") == "enabled" for s in getattr(cls, "SETTINGS", ())):
            return f"{cls.NAMESPACE}.enabled"
        return None

    def _turn_on_setting(self, module: str, actor: str) -> str | None:
        """Turn on the plugin's own ``enabled`` setting, if it has one and it
        is off. Returns a refusal when the environment locks it off."""
        key = self._enabled_setting(module)
        if key is None:
            return None
        svc = self.settings
        if svc is not None:
            try:
                eff = svc.resolve(key)
            except Exception:
                eff = None
            if eff is not None:
                if eff.get("value"):
                    return None
                if eff.get("locked"):
                    return (f"{eff.get('env') or 'the environment'} keeps {key} off in the MCP "
                            "environment; remove it there to turn this plugin on")
                svc.set(key, True, scope="hub", actor=actor, source="plugins",
                        note="turned on with the plugin")
                return None
        row = self.settings_store.get(key, "hub", "")
        if not row or row.get("value") is not True:
            self.settings_store.set(key, "hub", "", value=True, actor=actor, source="plugins",
                                    note="turned on with the plugin")
        return None

    async def set_plugin_enabled(self, module: str, enable: bool, actor: str,
                                 *, persist: bool = True) -> dict:
        """Switch one hub plugin now and keep the choice (``core.plugins.hub``).

        On: its ``enabled`` setting (if any) is turned on, then it loads through
        the boot path; a plugin that still cannot load (a missing token) stays
        recorded as on and loads once it can. Off: it unloads and stays off at
        start, whatever its settings say. Precedence: rook.hub.plugin_profiles."""
        from .plugin_profiles import PROTECTED_HUB, set_hub_state
        module = str(module or "").strip()
        if module not in self.host.candidates:
            return {"ok": False, "module": module, "error": f"no such hub plugin module: {module}"}
        if not enable and module in PROTECTED_HUB:
            return {"ok": False, "module": module,
                    "error": f"refusing to disable {module} (it re-enables plugins)"}
        if enable:
            refusal = self._turn_on_setting(module, actor)
            if refusal:
                return {"ok": False, "module": module, "error": refusal}
            res = await self.host.enable(module)
        else:
            if self.host.loaded(module) is None:
                res = {"ok": True, "module": module, "note": "was not loaded"}
                self.host._mark(module, "disabled", "disabled by operator")
            else:
                res = await self.host.disable(module)
                if not res.get("ok"):
                    return res   # a dependent still needs it: nothing changes
        if persist:
            try:
                self._plugin_state = set_hub_state(
                    self.settings_store, {module: bool(enable)}, actor=actor,
                    note="" if res.get("ok") else f"did not load: {res.get('error')}")
            except Exception as err:
                log.exception("persisting the hub plugin switch for %s failed", module)
                res = {**res, "persist_error": f"{type(err).__name__}: {err}"}
            res = {**res, "persisted": True}
        return res

    async def sync_plugin_state(self, actor: str = "system:rook-hub") -> list[dict]:
        """Apply stored switches changed by another process (the dashboard's
        setup wizard, an edit on the Settings page) since this node last
        applied them. Returns what it did."""
        stored = self._stored_plugin_state()
        done = []
        for module in sorted(set(stored) | set(self._plugin_state)):
            want, had = stored.get(module), self._plugin_state.get(module)
            if want is None or want == had or module not in self.host.candidates:
                continue
            loaded = self.host.loaded(module) is not None
            if want and not loaded:
                done.append(await self.set_plugin_enabled(module, True, actor, persist=False))
            elif not want and loaded:
                done.append(await self.set_plugin_enabled(module, False, actor, persist=False))
        self._plugin_state = stored
        return done

    async def _plugin_reload(self, module: str) -> dict:
        """Restart one hub plugin with its current settings (stop, load again, start)."""
        self._admin_gate(f"hub.plugin.reload {module}")
        return await self.host.reload(module)

    # -- settings / secrets / resources ----------------------------------
    def _stored_settings(self, namespace: str) -> dict:
        """Operator-stored hub-scope values for a plugin: the settings store,
        over the wave-1 ``hub_plugin_settings.json`` beside the other hub
        stores (read-only, kept so existing files keep working)."""
        out: dict = {}
        if self._state_dir:
            try:
                data = json.loads((Path(self._state_dir) / "hub_plugin_settings.json")
                                  .read_text(encoding="utf-8"))
                ns = data.get(namespace) if isinstance(data, dict) else None
                if isinstance(ns, dict):
                    out.update(ns)
            except (OSError, ValueError):
                pass
        try:
            out.update(self.settings_store.namespace_values(namespace))
        except Exception:
            log.exception("reading stored settings for %s failed", namespace)
        return out

    def _secret(self, key: str) -> str | None:
        if self._vault is None:
            return None
        try:
            return self._vault.get(key, "system:rook-hub", via="plugin setting")
        except KeyError:
            return None

    async def _cap_caller(self, cap: str, args: dict, target: str, timeout: float) -> Any:
        """Resolve a ``cap://<worker|any>/<cap>`` resource and call it."""
        if self.client is None:
            raise RuntimeError("hub node has no band client")
        roster = self.client.workers
        if target == "any":
            holders = sorted((w.get("name") or wid, wid) for wid, w in roster.items()
                             if cap in w.get("caps", []) and wid != self.worker_id)
            if not holders:
                raise LookupError(f"no live worker has {cap!r}")
            wid = holders[0][1]
        elif target in roster:
            wid = target
        else:
            named = [wid for wid, w in roster.items()
                     if (w.get("name") or "").lower() == target.lower()]
            if len(named) != 1:
                raise LookupError(f"cannot resolve worker {target!r} for {cap!r}")
            wid = named[0]
        reply = await self.client.call(cap=cap, args=args, target=wid, timeout=timeout,
                                       identity="system:rook-hub")
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error") or f"{cap} failed")
        return reply.get("result")

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        await self.host.start()

    async def stop(self) -> None:
        await self.host.stop()

    # -- band presence ---------------------------------------------------
    def caps(self) -> list[str]:
        return self.host.registry.list()

    def has(self, cap: str) -> bool:
        return self.host.registry.has(cap)

    def announce_msg(self) -> dict:
        msg = {
            "kind": "announce",
            "worker_id": self.worker_id,
            "name": self.name,
            "description": "Rook hub: hub-placed plugins",
            "caps": self.caps(),
            "plugins": [p.NAMESPACE for p in self.host.plugins],
            "version": self.version,
            "build": self.build,
            "core_api": CORE_API_VERSION,
            "facts": wire_facts(self.facts.hw),
            "roles": sorted(self.facts.roles),
        }
        tiers = self.host.tiers()
        if tiers:
            msg["tiers"] = tiers
        hb = self.host.heartbeats()
        if hb:
            msg["hb"] = hb
        return msg

    def entry(self) -> dict:
        """Roster entry, same shape as a worker's (see BandClient)."""
        m = self.announce_msg()
        return {"worker_id": self.worker_id, "name": self.name,
                "description": m["description"], "caps": m["caps"],
                "plugins": m["plugins"], "hb": m.get("hb", {}),
                "version": self.version, "build": self.build, "app_release": {},
                "facts": m["facts"], "roles": m["roles"], "local": True,
                "last_seen": time.time()}

    # -- dispatch --------------------------------------------------------
    def band_allowed(self, cap: str) -> bool:
        """Whether an (unauthenticated) band call may reach ``cap``: its
        effective tier (declared, floored by the built-in table) is at most
        the band ceiling."""
        meta = self.host.registry.meta(cap)
        tier = core_authz.effective_tier(cap, getattr(meta, "risk", None))
        return core_authz.tier_rank(tier) <= core_authz.tier_rank(self.band_max_risk)

    async def dispatch(self, cap: str, args: Any, identity: str | None = None,
                       source: str = "local") -> dict:
        """Run a cap; returns the reply body (``ok`` + ``result``/``error``).
        ``source="band"`` applies the band risk ceiling and journals the call
        (MCP-originated calls are journaled by rook_call)."""
        if source == "band" and self.has(cap) and not self.band_allowed(cap):
            body = {"ok": False, "error": (
                f"{cap} is not callable over the band on the hub (risk above "
                f"{self.band_max_risk!r}); call it through the MCP bridge")}
        elif source == "band":
            # Any PSK holder can send this: no authenticated principal until
            # device-signed calls (permissions.md 1); the envelope identity
            # stays a display breadcrumb.
            from .authz import BAND_UNAUTHENTICATED, current_principal
            tok = current_principal.set(BAND_UNAUTHENTICATED)
            try:
                body = await self.host.dispatch(cap, args, identity)
            finally:
                current_principal.reset(tok)
        else:
            body = await self.host.dispatch(cap, args, identity)
        if source == "band" and self._on_band_call is not None:
            try:
                self._on_band_call(cap, identity, args if isinstance(args, dict) else {}, body)
            except Exception:
                log.exception("journaling band call to hub failed")
        return body

    async def invoke(self, cap: str, args: dict | None = None, identity: str | None = None) -> Any:
        """In-process call for the MCP bridge's plugin tools: runs the cap
        through the registry (so core's limit/fields contract applies) and
        returns its result, raising the handler's own exception instead of
        building a reply body."""
        tok = caller_identity.set(identity)
        try:
            return await self.host.registry.call(cap, **(args or {}))
        finally:
            caller_identity.reset(tok)

    def guidance_defaults(self) -> dict[str, str]:
        """Guidance slots declared by the loaded plugins, as guidance-store
        keys: a slot that already names a kind (``tool:``, ``cap:``,
        ``server``, ``hygiene``) is used as is; a bare cap name or prefix
        becomes ``cap:<slot>`` (a tip on ``rook_call`` replies for that cap)."""
        out: dict[str, str] = {}
        for p in self.host.plugins:
            for slot, text in (p.GUIDANCE or {}).items():
                key = slot if (slot in ("server", "hygiene") or slot.startswith(("tool:", "cap:"))) \
                    else "cap:" + slot
                # An empty default only declares the slot (knowledge declares
                # tool:rook_knowledge so it is editable): another plugin's
                # text for the same slot (manual's hint) wins over it.
                if text or key not in out:
                    out[key] = text if not out.get(key) else out[key]
        return out

    def plugin(self, namespace: str) -> Any:
        return self.host.plugin(namespace)

    def wants(self, msg: dict) -> bool:
        """Whether a band request is for this node."""
        cap = msg.get("cap")
        target = msg.get("target")
        if target:
            return target == self.worker_id
        return bool(cap) and self.has(cap)


def attach_hub_node(client: Any, state_dir: str | None, *, vault: Any = None,
                    journal: Any = None, enrollment: Any = None,
                    chat: Any = None, settings_store: Any = None) -> "HubNode | None":
    """Build the hub node and put it on ``client``'s bands. Used by the MCP
    bridge's ``build_server``. Never raises: a broken plugin host leaves the
    bridge running without hub caps. ``ROOK_HUB_PLUGINS=0`` disables it.

    ``node.attached`` says whether the client could take it (test doubles
    without ``attach_local`` cannot); generated MCP tools need it attached.
    """
    if os.environ.get("ROOK_HUB_PLUGINS", "1") == "0":
        return None

    holder: dict = {}

    def journal_band_call(cap: str, identity: str | None, args: dict, reply: dict) -> None:
        if journal is None:
            return
        node = holder.get("node")
        meta = node.host.registry.meta(cap) if node is not None else None
        if "sensitive" in (getattr(meta, "tags", ()) or ()) and reply.get("ok"):
            reply = {"ok": True, "result": "[sensitive: not journaled]"}
        journal.record(cap=cap, worker=HUB_WORKER_NAME,
                       identity=identity or "band:anonymous", args=args, reply=reply)

    try:
        node = HubNode(state_dir, client=client, vault=vault, on_band_call=journal_band_call,
                       enrollment=enrollment, chat=chat, settings_store=settings_store)
        holder["node"] = node
        # Hub plugins that act as their own identity (the home agent) journal
        # what they do themselves.
        node.journal = journal
    except Exception:
        log.exception("hub plugin host failed to start; hub caps unavailable")
        return None
    attach = getattr(client, "attach_local", None)
    node.attached = False
    if callable(attach):
        try:
            attach(node)
            node.attached = True
        except Exception:
            log.exception("could not attach the hub node to the band client")
    return node
