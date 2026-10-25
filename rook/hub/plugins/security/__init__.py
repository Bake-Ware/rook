"""Advanced security (hub plugin ``security``, off by default).

The guardrails the hub had before the change-management reset
(knowledge change-management-design-20261025), moved out of core so a band
that wants them turns them back on in one place (Manage > Plugins, or the
setting ``security.enabled``). On, it restores that behaviour exactly:

* the blanket holds: admin or destructive calls (``approvals.hold``, still
  under ``approvals.enabled``), operator hold rules, calls from a worker on
  the hub's own machine, and the spread limit (:mod:`.holds`);
* the trust map's ``guarded`` level and the ``balanced`` unlisted default
  (core keeps only ``sacred``);
* the shell read-only guess on an ask's Risk line (:mod:`.shell`);
* relaying approvals from agent clients (``rook_approvals``) and from the
  huddle chair;
* blind merge, deploy and ship votes at the conference table.

Off, the hub keeps only the hard stops (rook/hub/hardstops.py) and change
requests (rook/hub/plugins/changes); every call is journaled either way.
Only a person switches this plugin (agents are refused: no ratchet).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from ....core.plugin import Plugin, place, setting

ENV = "ROOK_SECURITY_ADVANCED"
#: Both names the Settings page lists for ``security.enabled``.
ENVS = (ENV, "ROOK_SECURITY_ENABLED")
MODULE = "security"
KEY = "security.enabled"


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def enabled(store: Any = None, environ: Any = None) -> bool:
    """Whether advanced security is on: the environment, else the plugin's
    switch (``core.plugins.hub``) and its ``enabled`` setting. Off when
    nothing says otherwise, or the store can't be read."""
    environ = os.environ if environ is None else environ
    for var in ENVS:
        if environ.get(var) is not None:
            return _truthy(environ.get(var))
    if store is None:
        return False
    try:
        from ...plugin_profiles import hub_state
        if hub_state(store).get(MODULE) is False:
            return False
        row = store.get(KEY, "hub", "")
    except Exception:  # noqa: BLE001 - unreadable settings: the core default
        return False
    return bool(row) and _truthy(row.get("value"))


_cache: dict = {"at": 0.0, "on": False}
_lock = threading.Lock()
TTL = 3.0


def current() -> bool:
    """:func:`enabled` for code with no settings store at hand (the wording
    of asks), from the hub's default store, re-read at most every few seconds;
    a hub without a settings file is off."""
    for var in ENVS:
        if os.environ.get(var) is not None:
            return _truthy(os.environ.get(var))
    now = time.monotonic()
    with _lock:
        if now - _cache["at"] < TTL:
            return _cache["on"]
    store = None
    try:
        from ...settings_store import SettingsStore
        st = SettingsStore()
        store = st if st.exists() else None
    except Exception:  # noqa: BLE001
        store = None
    on = enabled(store)
    with _lock:
        _cache.update(at=now, on=on)
    return on


def for_node(node: Any) -> bool:
    """Whether advanced security is on, as the hub ``node``'s approval layer
    sees it (its settings, cached), else :func:`current`."""
    ap = getattr(getattr(getattr(node, "client", None), "authz", None), "approvals", None)
    return bool(ap.config().advanced) if ap is not None else current()


class AdvancedSecurity(Plugin):
    NAMESPACE = "security"
    NAME = "advanced security"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    SETTINGS = (
        setting("enabled", bool, default=False, env=ENV, group="General",
                label="Advanced security",
                help="Off: only the hard stops ask you (changes on sacred machines, erasing data "
                     "for good, revealing a secret, sending as you). On: also admin/destructive, "
                     "hub-machine and spread holds, guarded trust entries, relays from agent "
                     "clients and blind votes before merges and deploys."),
    )


PLUGIN = AdvancedSecurity
