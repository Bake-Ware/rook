"""``settings.*``: the hub's settings store, as caps on worker ``rook``.

One schema-driven store for hub, band, worker and user settings
(docs/design/settings.md). The dashboard's Settings area uses the same
service through ``/settings/account-api``; agents and services use these caps.

* ``settings.describe`` / ``settings.get`` / ``settings.history``: read.
  Secret values are never returned (a fingerprint shows that one changed).
* ``settings.set`` / ``settings.reset`` / ``settings.apply_worker``: admin.
* ``settings.fetch``: a service (voice, decision engine) reads its own
  settings, secrets included, with a token listed in
  ``core.settings.service_readers``. Tagged sensitive: the reply is not
  journaled.
* ``settings.worker_secret``: a worker fetches, at use, a vault secret that a
  stored setting assigns to it (sent as a ``{{secret:…}}`` reference in its
  config push, never written to its disk). Sensitive, band-callable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from ...core import context
from ...core.plugin import Plugin, capability, place

log = logging.getLogger("rook.hub.plugins.settings")


def _actor() -> str:
    ident = context.caller_identity.get()
    return str(ident) if ident else "system:rook-hub"


def _admin_gate(what: str) -> None:
    """Writes are hub administration: band owners and operator-role tokens
    only (docs/design/permissions.md; ``rook.hub.authz.require_hub_admin``
    once the permissions module is present). Enforced regardless of the
    policy mode."""
    try:
        from ..authz import require_hub_admin
    except ImportError:
        return
    refusal = require_hub_admin(what)
    if refusal:
        raise PermissionError(refusal)


def _principal() -> dict | None:
    try:
        from ...band_mcp import attribution
        att = attribution.current.get()
    except Exception:
        return None
    if att is None:
        return None
    return {"kind": att.kind, "label": att.label, "agent_id": att.agent_id,
            "verified": att.verified}


class HubSettings(Plugin):
    NAMESPACE = "settings"
    NAME = "settings"
    PLACEMENT = place("is_hub", run="one")
    SKILL = ("### settings\n"
             "Hub, band, worker and user settings with their source "
             "(default / hub / band / worker / user / file / env) on worker `rook`: "
             "`settings.get(key=…)` or `settings.get(prefix=\"core.\", scope=\"hub\")`, "
             "`settings.history`, `settings.describe`. Writes (`settings.set`, "
             "`settings.reset`, `settings.apply_worker`) are admin actions: ask the user "
             "first. A key set by an environment variable is locked; the reply says which.\n")

    #: How often this process looks for settings another hub process saved.
    WATCH_S = 3.0

    def __init__(self) -> None:
        super().__init__()
        self.svc = None
        self._watch: asyncio.Task | None = None
        self._seen: dict[str, dict] = {}

    def bind_host(self, node) -> None:
        from ..settings_schema import Schema
        from ..settings_service import SettingsService
        schema = Schema(hub_plugins=list(node.host.plugins))
        self.svc = SettingsService(node.settings_store, schema, vault=node._vault, node=node,
                                   enrollment=getattr(node, "enrollment", None))

        def refresh(entry) -> None:
            if entry.origin != "hub-plugin":
                return
            # This process applies its own writes here; the store watch must not
            # apply (and reload) them a second time.
            with contextlib.suppress(Exception):
                self._seen[entry.namespace] = self.svc.store.namespace_values(entry.namespace)
            # live: the next read sees it; reload: restart that plugin with it.
            node.host.settings_changed([entry.namespace] if entry.setting.apply == "reload"
                                       else [])
            # A plugin that stayed unloaded for want of a setting (an engine
            # choice, an address) gets another try with it.
            retry = getattr(node.host, "retry_unavailable", None)
            if retry is not None:
                retry(entry.namespace)
        self.svc.on_change(refresh)
        node.settings = self.svc

    async def start(self) -> None:
        """Record what this process's environment sets, so the dashboard can
        show env locks and conflicts for keys it cannot read itself."""
        if self.svc is None:
            return
        node = self.svc.node
        self.svc.flags = dict(getattr(node, "startup_flags", {}) or {})
        import os
        state = getattr(node, "_state_dir", None) or ""
        chat = os.environ.get("ROOK_CHAT_DB") or (os.path.join(state, "chat.db") if state else "")
        report = {"started_at": self.svc.started_at, "env": self.svc.env_report("mcp"),
                  "stores": {"dir": state, "chat_db": chat,
                             "settings_db": str(self.svc.store.path)},
                  "conflicts": list(getattr(node, "startup_conflicts", []) or [])}
        try:
            self.svc.store.report_runtime("mcp", report)
        except Exception:
            log.warning("could not write the MCP runtime report to %s", self.svc.store.path,
                        exc_info=True)
        self._seen = self._snapshot()
        self._watch = asyncio.get_running_loop().create_task(self._watch_store())

    async def stop(self) -> None:
        if self._watch is not None:
            self._watch.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._watch
            self._watch = None

    # -- changes another process saved (the dashboard's Settings and Home agent pages) --
    def _snapshot(self) -> dict[str, dict]:
        node = self.svc.node if self.svc is not None else None
        host = getattr(node, "host", None)
        out = {}
        for p in list(getattr(host, "plugins", []) or []):
            if p.SETTINGS:
                with contextlib.suppress(Exception):
                    out[p.NAMESPACE] = self.svc.store.namespace_values(p.NAMESPACE)
        return out

    def sync_from_store(self) -> dict:
        """Apply what changed in the store since the last look: refresh every
        plugin's view (``live`` settings take effect at once) and reload the
        plugins whose changed settings say ``reload``. The dashboard runs in
        another process and writes the same store, so without this a value
        saved there reached the house agent only after a restart."""
        node = self.svc.node
        now = self._snapshot()
        changed: dict[str, set] = {}
        for ns in set(now) | set(self._seen):
            old, new = self._seen.get(ns) or {}, now.get(ns) or {}
            keys = {k for k in set(old) | set(new) if old.get(k) != new.get(k)}
            if keys:
                changed[ns] = keys
        self._seen = now
        if not changed:
            return {"changed": {}}
        reload_ns = []
        for p in list(node.host.plugins):
            keys = changed.get(p.NAMESPACE)
            if keys and any(s.name in keys and s.apply == "reload" for s in p.SETTINGS):
                reload_ns.append(p.NAMESPACE)
        out = node.host.settings_changed(reload_ns)
        log.info("settings saved elsewhere, applied here: %s",
                 {ns: sorted(k)[:8] for ns, k in changed.items()})
        return {"changed": {ns: sorted(k) for ns, k in changed.items()}, **out}

    async def _watch_store(self) -> None:
        mark = None
        while True:
            await asyncio.sleep(self.WATCH_S)
            try:
                now = await asyncio.to_thread(self.svc.store.marker)
                if mark is not None and now != mark:
                    self.sync_from_store()
                mark = now
            except Exception:  # noqa: BLE001 - keep watching
                log.debug("settings store watch failed", exc_info=True)

    def _svc(self):
        if self.svc is None:
            raise RuntimeError("settings service not bound")
        return self.svc

    @capability("describe", risk="read", limit=100,
                fields=["key", "label", "scope", "type", "default", "env", "apply"])
    def describe(self, prefix: str = "") -> list[dict]:
        """The settings schema: key, type, scope, default, env names, apply mode.

        ``prefix`` (``"core."``, ``"voice."``) narrows it. Secrets have no default shown."""
        return [e.describe() for e in self._svc().schema if e.key.startswith(prefix or "")]

    @capability("get", risk="read", limit=100,
                fields=["key", "value", "source", "locked", "env", "pending", "conflict"])
    def get(self, key: str = "", prefix: str = "", scope: str = "hub",
            target: str = "") -> Any:
        """Effective value of a setting, where it came from and what it hides.

        ``key`` for one; else every key under ``prefix`` that applies at
        ``scope`` (hub, band, worker, user) for ``target`` (band id, worker
        name, user id). ``source`` is default|hub|band|worker|user|file|env;
        ``locked`` means an environment variable wins. Secrets show ``***``."""
        svc = self._svc()
        ctx = svc._ctx(scope, target) if scope != "hub" else {}
        if key:
            return svc.resolve(key, ctx)
        out = []
        for e in svc.schema:
            if e.key.startswith(prefix or "") and svc.schema.applies_at(e, scope):
                out.append(svc._resolve(e, ctx))
        return out

    @capability("set", risk="admin")
    def set(self, key: str, value: Any, scope: str = "", target: str = "", note: str = "",
            dry_run: bool = False) -> dict:
        """Store a setting (validated, attributed, in history). Secrets go to the vault.

        ``scope`` defaults to the key's home scope; ``target`` names the band
        id, worker or user. ``dry_run=true`` returns the effective change
        without saving. Worker settings then need ``settings.apply_worker``."""
        if not dry_run:
            _admin_gate(f"settings.set {key}")
        return self._svc().set(key, value, scope=scope or None, target=target, actor=_actor(),
                               note=note, source="mcp", dry_run=dry_run)

    @capability("reset", risk="admin")
    def reset(self, key: str, scope: str = "", target: str = "", note: str = "") -> dict:
        """Remove a stored value so the key inherits again (in history)."""
        _admin_gate(f"settings.reset {key}")
        return self._svc().reset(key, scope=scope or None, target=target, actor=_actor(),
                                 note=note, source="mcp")

    @capability("history", risk="read", limit=20)
    def history(self, key: str = "", scope: str = "", target: str = "") -> list[dict]:
        """Attributed changes, newest first (secrets as fingerprints).

        ``key`` ending in ``.`` matches a prefix (``"voice."``)."""
        return self._svc().store.history(key=key or None, scope=scope or None,
                                         target=target if (target or scope) else None, limit=200)

    @capability("apply_worker", risk="admin")
    async def apply_worker(self, worker: str, confirm_within: float = 120.0) -> dict:
        """Push a worker's stored settings to it (commit-confirmed restart).

        Runs in the background; ``settings.get(scope="worker", target=…)`` and
        the dashboard show the result. Secrets go as ``{{secret:…}}``
        references the worker fetches at use."""
        _admin_gate(f"settings.apply_worker {worker}")
        return await self._svc().apply_worker(worker, _actor(), confirm_within=confirm_within)

    @capability("fetch", risk="read", tags=("sensitive",))
    def fetch(self, namespace: str) -> dict:
        """A service's own settings, secrets included, for its scoped token.

        Only tokens listed in ``core.settings.service_readers[namespace]`` may
        call it; the reply is not journaled."""
        return self._svc().fetch(namespace, _principal())

    @capability("report", risk="write")
    def report(self, namespace: str, env: dict | None = None) -> dict:
        """A service reports which of its settings its environment sets.

        ``env`` maps setting name to the variable that set it (no values)."""
        return self._svc().report_service(namespace, _principal(), env or {})

    @capability("worker_secret", risk="read", tags=("sensitive",))
    def worker_secret(self, worker_id: str, names: list) -> dict:
        """Vault secrets a stored setting assigns to this worker (fetch at use)."""
        return self._svc().worker_secrets(worker_id, list(names or []))


PLUGIN = HubSettings
