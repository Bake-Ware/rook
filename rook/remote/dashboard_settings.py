"""The dashboard's side of the settings framework (docs/design/settings.md).

The dashboard reads three hub settings at start and keeps them current:
the installer domain, the relay address workers dial and the primary band
label. Their sources, highest first::

    environment / flag  >  settings store (Settings page)  >  setup.json  >  default

Before this, ``setup.json`` silently beat the environment after the first
start (settings P1). Now the environment wins, and a conflict (the variable
hides a stored or file value) is logged at start and shown on the Settings
page. The band key is not here: it lives in the enrollment database and the
environment only seeds the first band.

The dashboard also reports what its environment sets to the store's runtime
table, because the MCP process (which serves the Settings API) cannot read
another process's environment.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterable

log = logging.getLogger("rook.remote.dashboard_settings")

#: attribute -> (settings key, setup.json field, env var, flag)
FIELDS = {
    "domain": ("core.hub.domain", "pyz_domain", "ROOK_DOMAIN", "--domain"),
    "hub_public": ("core.hub.public_relay", "hub_public", "ROOK_HUB_PUBLIC", "--hub-public"),
    "band_name": ("core.hub.band_name", "band_name", "ROOK_BAND_NAME", "--band-name"),
}


def explicit_sources(argv: Iterable[str], environ: Any = None) -> dict[str, str]:
    """Which of :data:`FIELDS` the command line or environment set explicitly
    (as opposed to argparse defaults): ``{attr: "flag --x" | "env ROOK_X"}``."""
    environ = environ if environ is not None else os.environ
    argv = list(argv)
    out = {}
    for attr, (_key, _file, var, flag) in FIELDS.items():
        if any(a == flag or a.startswith(flag + "=") for a in argv):
            out[attr] = f"flag {flag}"
        elif environ.get(var) is not None:
            out[attr] = f"env {var}"
    return out


def open_store():
    try:
        from ..hub.settings_store import SettingsStore
        return SettingsStore()
    except Exception:
        log.warning("settings store unavailable", exc_info=True)
        return None


def stored_values(store) -> dict[str, Any]:
    """``{attr: value}`` stored on the Settings page (hub scope)."""
    out: dict[str, Any] = {}
    if store is None:
        return out
    try:
        for attr, (key, *_rest) in FIELDS.items():
            row = store.get(key, "hub", "")
            if row is not None and row.get("value") not in (None, ""):
                out[attr] = row["value"]
    except Exception:
        log.warning("reading the settings store failed", exc_info=True)
    return out


def resolve(current: dict[str, Any], explicit: dict[str, str] | None,
            setup: dict[str, str], stored: dict[str, Any]) -> tuple[dict, dict, list]:
    """Effective values, their sources, and conflicts.

    ``current``: the constructor values (flag/env/default). ``explicit``:
    :func:`explicit_sources`, or ``None`` for callers that predate it (they
    keep the old order, setup.json over the constructor, below the store).
    """
    values, sources, conflicts = {}, {}, []
    for attr, (key, field, var, flag) in FIELDS.items():
        filed = setup.get(field) or None
        saved = stored.get(attr)
        if explicit is not None and attr in explicit:
            values[attr], sources[attr] = current.get(attr), explicit[attr]
            hidden = [(src, v) for src, v in (("stored", saved), ("setup.json", filed))
                      if v not in (None, "") and v != current.get(attr)]
            if hidden:
                src, v = hidden[0]
                shown = "Settings page" if src == "stored" else src
                conflicts.append({"key": key, "env": explicit[attr].split(" ", 1)[1],
                                  "hidden": src, "note": (
                                      f"{explicit[attr]} = {current.get(attr)!r} wins over the "
                                      f"{shown} value {v!r}; remove it from the dashboard's "
                                      f"environment or command line to use that value.")})
        elif saved not in (None, ""):
            values[attr], sources[attr] = saved, "stored"
        elif filed:
            values[attr], sources[attr] = filed, "setup.json"
        else:
            values[attr], sources[attr] = current.get(attr), "default"
    return values, sources, conflicts


def chat_db_path(store, legacy: str) -> str:
    """ROOK_CHAT_DB, else the chat database the MCP reported, else ``legacy``
    (settings P4: both processes serve the same rooms)."""
    explicit = os.environ.get("ROOK_CHAT_DB")
    if explicit:
        return explicit
    try:
        rep = store.runtime("mcp") if store is not None else {}
        path = (rep.get("stores") or {}).get("chat_db")
        if path and os.path.exists(path):
            return path
    except Exception:
        log.debug("no MCP runtime report for the chat database", exc_info=True)
    return legacy


def flag_values(argv: Iterable[str], flag: str) -> str | None:
    """The value given to ``flag`` on a command line, if any."""
    argv = list(argv)
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def report(store, *, explicit: dict | None, sources: dict, conflicts: list,
           chat_db: str, values: dict | None = None, started_at: float | None = None,
           argv: Iterable[str] = ()) -> None:
    if store is None:
        return
    try:
        from ..hub.settings_schema import Schema
        from ..hub.settings_service import SettingsService
        from ..core.settings import MASK, fingerprint
        schema = Schema(worker_package=None, hub_package=None)
        svc = SettingsService(store, schema, process="dashboard")
        env = svc.env_report("dashboard")
        argv = list(argv)
        for e in schema:
            if e.owner != "dashboard" or not e.setting.flag:
                continue
            raw = flag_values(argv, e.setting.flag)
            if raw is not None:
                env[e.key] = {"env": e.setting.flag,
                              "value": (MASK + ":" + fingerprint(raw)) if e.setting.secret else raw}
        # Flags count as locks too (they are in argv, not the environment).
        for attr, src in (explicit or {}).items():
            if src.startswith("flag "):
                key = FIELDS[attr][0]
                env.setdefault(key, {"env": src.split(" ", 1)[1],
                                     "value": (values or {}).get(attr)})
        store.report_runtime("dashboard", {
            "started_at": started_at or time.time(), "env": env, "sources": sources,
            "conflicts": conflicts, "stores": {"chat_db": chat_db,
                                               "settings_db": str(store.path)}})
    except Exception:
        log.warning("could not write the dashboard runtime report", exc_info=True)
