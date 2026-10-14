"""Band-wide plugin choices: presets, defaults for new workers, the hub's own set.

Three stored values (the hub settings store, ``settings.db``, shared by the
MCP server and the dashboard; keys declared in ``settings_schema._CORE``):

* ``core.plugins.hub`` (hub scope): ``{module: bool}``, the hub plugins an
  operator switched on or off from the Plugins page (or ``hub.plugin.*``).
  ``False`` keeps a module from loading at start; ``True`` records that it was
  turned on (its ``enabled`` setting, if it has one, was turned on with it).
* ``core.plugins.worker_defaults`` (band scope): ``{module: bool}``, what a
  newly enrolled worker on that band turns on or off on its first start. A
  pairing code carries it as the worker's *plugin profile*.
* ``core.plugins.catalogs`` (hub scope): marketplace catalog URLs (preview).

A plugin profile is ``{"enable": [modules], "disable": [modules]}``; a module
in neither list keeps the worker's built-in behaviour (load where available).

Hub precedence, highest first (docs/design/plugins.md, "Switching plugins"):

1. an environment variable for the plugin's ``enabled`` setting (it locks it);
2. ``core.plugins.hub`` = ``False``: not loaded, whatever its settings say;
3. the plugin's own ``enabled`` setting, for plugins that have one;
4. ``available()`` (a missing token or backend keeps it unloaded).

Stdlib only: the dashboard imports this too (pairing codes, the setup wizard).
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Iterable

log = logging.getLogger("rook.hub.plugin_profiles")

HUB_KEY = "core.plugins.hub"
WORKER_DEFAULTS_KEY = "core.plugins.worker_defaults"
CATALOGS_KEY = "core.plugins.catalogs"

#: Worker modules nobody switches off remotely: they carry worker.restart,
#: config pushes and enrollment, the ways back in (the Settings worker page's
#: rule, plus enrollment), and ``info`` (cheap, read-only, the dashboard reads it).
PROTECTED_WORKER = frozenset({"selfupdate", "config", "enrollment", "info"})
#: Hub modules that stay on: settings carries the switches to turn things back
#: on, policy the permission rules, info what agents read first (hub.info).
PROTECTED_HUB = frozenset({"settings", "policy", "info"})
#: Listed in a preset (turned on with it), but never turned off by unticking
#: that preset: worker ``info`` (cheap, read-only, the dashboard reads it),
#: hub ``notify`` (the watchdog and plugins call notify.send and fall back
#: when it reports no channel, not when the cap is missing) and hub ``tasks``
#: (on by default; it needs knowledge anyway, so it idles without it).
KEEP_ON = frozenset({"info", "notify", "tasks"})

#: "What do you want Rook to do?" Module names are real plugin modules: the
#: stock worker plugins (rook/worker/plugins), the hub plugins
#: (rook/hub/plugins) and the Android app's native ones (``*_android``,
#: ``hid_a11y``, ``screen``). ``high_risk`` names what deserves a second look;
#: ``tiers`` are the risk tiers its caps declare (kept literal for the
#: dashboard, which does not import plugins; a test recomputes them);
#: ``default`` is what a hub does without a choice (knowledge is off until
#: turned on; everything else loads where it can).
PRESETS: tuple[dict, ...] = (
    {"id": "remote", "default": True, "tiers": ("read", "exec"), "title": "Remote control",
     "summary": "Run commands, move files and read system info on your machines.",
     "hub": [], "worker": ["shell", "file_ops", "proc", "info"],
     "high_risk": ["Runs shell commands (exec)"]},
    {"id": "agents", "default": True, "tiers": ("read", "write", "exec"), "title": "Agent sessions",
     "summary": "Start, watch and resume Claude Code, Codex and terminal sessions; agents check in "
                "with you in huddles.",
     "hub": ["huddle"], "worker": ["sessions", "session_mirror", "session_shim", "terminals", "work",
                           "claude_history", "codex_history", "wake", "hermes"],
     "high_risk": ["Starts agent sessions and terminals (exec)"]},
    {"id": "knowledge", "default": False, "tiers": ("read", "write"), "title": "Knowledge & tasks",
     "summary": "A shared knowledge base and task deck for your agents.",
     "hub": ["knowledge", "tasks"], "worker": ["memory"], "high_risk": []},
    {"id": "home", "default": True, "tiers": ("read", "write"), "title": "Home & voice agent",
     "summary": "The home agent, what it remembers, its persona in your harnesses, and speech "
                "on phones.",
     "hub": ["home", "memory"], "worker": ["persona", "speak_android"], "high_risk": []},
    {"id": "alerts", "default": True, "tiers": ("read", "write", "exec"), "title": "Phone & chat alerts",
     "summary": "Notifications on phones, Telegram and Discord.",
     "hub": ["notify", "telegram", "discord"], "worker": ["notify_android"],
     "high_risk": ["Posts phone notifications (exec on Android)"]},
    {"id": "hardware", "default": True, "tiers": ("read", "write", "exec"), "title": "Hardware",
     "summary": "Keyboard and mouse, screens, cameras, HDMI-CEC, PiKVM and USB dongles.",
     "hub": [], "worker": ["hid", "hid_a11y", "cec", "pikvm", "dongle", "camera", "screenshot",
                           "screen"],
     "high_risk": ["Keyboard and mouse control (hid)", "Screen capture", "Camera"]},
)
PRESET_IDS = tuple(p["id"] for p in PRESETS)

#: Hub plugins whose settings schema has an ``enabled`` switch, by module
#: (kept literal so the dashboard need not import hub plugins; a test checks
#: it against :func:`enabled_setting_keys`).
HUB_ENABLED_SETTINGS = {"discord": "discord.enabled", "home": "home.enabled", "huddle": "huddle.enabled",
                        "jobs": "job.enabled", "knowledge": "knowledge.enabled",
                        "memory": "memory.enabled",
                        "tasks": "task.enabled", "telegram": "telegram.enabled"}

#: Risk tiers of the Android app's plugins, which the hub cannot import (they
#: ship in the APK). Used for preset summaries only; the matrix reads the tiers
#: a live Android worker announces.
ANDROID_RISK = {"speak_android": ("read", "write"), "notify_android": ("exec",),
                "hid_a11y": ("read", "exec"), "screen": ("read",)}


# -- profiles ------------------------------------------------------------------

def _modules(v: Any) -> list[str]:
    if not isinstance(v, (list, tuple, set, frozenset)):
        return []
    out = []
    for m in v:
        m = str(m or "").strip()
        if m and len(m) <= 64 and m.replace("_", "").replace("-", "").isalnum() and m not in out:
            out.append(m)
    return out


def normalize_profile(profile: Any) -> dict | None:
    """``{"enable": [...], "disable": [...]}`` with valid module names only,
    protected modules dropped from ``disable`` and ``enable`` winning over a
    duplicate in ``disable``; ``None`` when it switches nothing."""
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except ValueError:
            return None
    if not isinstance(profile, dict):
        return None
    enable = _modules(profile.get("enable"))
    disable = [m for m in _modules(profile.get("disable"))
               if m not in enable and m not in PROTECTED_WORKER]
    if not enable and not disable:
        return None
    return {"enable": sorted(enable), "disable": sorted(disable)}


def profile_from_defaults(defaults: Any) -> dict | None:
    """A worker profile from a band's ``{module: bool}`` defaults."""
    if not isinstance(defaults, dict):
        return None
    return normalize_profile({"enable": [m for m, v in defaults.items() if v is True],
                              "disable": [m for m, v in defaults.items() if v is False]})


def profile_id(profile: dict | None) -> str:
    """Stable short id of a profile (what a worker records once applied)."""
    if not profile:
        return ""
    raw = json.dumps(profile, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# -- the store -------------------------------------------------------------------

def _store(store: Any = None) -> Any:
    if store is not None:
        return store
    from .settings_store import SettingsStore
    return SettingsStore()


def _value(store: Any, key: str, scope: str, target: str = "") -> Any:
    try:
        row = store.get(key, scope, target)
    except Exception:
        log.warning("reading %s (%s %s) failed", key, scope, target, exc_info=True)
        return None
    return row.get("value") if row else None


def _bool_map(v: Any) -> dict[str, bool]:
    if not isinstance(v, dict):
        return {}
    return {str(k): bool(x) for k, x in v.items() if isinstance(x, bool) and _modules([k])}


def hub_state(store: Any = None) -> dict[str, bool]:
    """``core.plugins.hub``: ``{module: bool}`` (protected modules never off)."""
    st = _bool_map(_value(_store(store), HUB_KEY, "hub"))
    return {m: v for m, v in st.items() if v or m not in PROTECTED_HUB}


def hub_disabled(store: Any = None) -> set[str]:
    return {m for m, v in hub_state(store).items() if not v}


def set_hub_state(store: Any, changes: dict[str, bool], *, actor: str,
                  note: str = "", source: str = "plugins") -> dict[str, bool]:
    """Merge ``changes`` into the hub's stored set (one history entry)."""
    store = _store(store)
    cur = hub_state(store)
    new = dict(cur)
    for m, v in changes.items():
        if m in PROTECTED_HUB and not v:
            continue
        new[str(m)] = bool(v)
    if new != cur:
        store.set(HUB_KEY, "hub", "", value=new, actor=actor, note=note, source=source)
    return new


def worker_defaults(band_id: str, store: Any = None) -> dict[str, bool]:
    if not band_id:
        return {}
    return _bool_map(_value(_store(store), WORKER_DEFAULTS_KEY, "band", band_id))


def set_worker_defaults(store: Any, band_id: str, changes: dict[str, Any], *, actor: str,
                        note: str = "", source: str = "plugins") -> dict[str, bool]:
    """Merge ``changes`` (``None`` removes a module: back to built-in) into a
    band's defaults for new workers."""
    if not band_id:
        raise ValueError("a band is required")
    store = _store(store)
    cur = worker_defaults(band_id, store)
    new = dict(cur)
    for m, v in changes.items():
        if not _modules([m]):
            raise ValueError(f"not a plugin module name: {m!r}")
        if v is None:
            new.pop(m, None)
        elif m in PROTECTED_WORKER and not v:
            raise ValueError(f"{m} cannot be turned off (it carries worker control)")
        else:
            new[m] = bool(v)
    if new != cur:
        store.set(WORKER_DEFAULTS_KEY, "band", band_id, value=new, actor=actor, note=note,
                  source=source)
    return new


def band_profile(band_id: str, store: Any = None) -> dict | None:
    """The plugin profile a new worker on ``band_id`` gets (its band defaults)."""
    return profile_from_defaults(worker_defaults(band_id, store))


# -- presets -----------------------------------------------------------------------

def preset(pid: str) -> dict:
    for p in PRESETS:
        if p["id"] == pid:
            return p
    raise ValueError(f"unknown preset {pid!r}; use one of {', '.join(PRESET_IDS)}")


def _switchable(p: dict, side: str) -> list[str]:
    """A preset's modules that unticking it switches off."""
    protected = PROTECTED_HUB if side == "hub" else PROTECTED_WORKER
    return [m for m in p[side] if m not in KEEP_ON and m not in protected]


def preset_states(hub: dict[str, bool] | None = None, worker: dict[str, bool] | None = None,
                  enabled: dict[str, bool] | None = None) -> dict[str, bool | None]:
    """What the operator has chosen for each preset, from stored choices only
    (never from what happens to be loaded): ``True`` when every stored value
    for its switchable modules is on, ``False`` when every one is off,
    ``None`` when nothing is stored (built-in behaviour) or the values are
    mixed (switched one by one). ``hub``: :func:`hub_state`; ``worker``: a
    band's :func:`worker_defaults`; ``enabled``: hub module -> its effective
    ``enabled`` setting, where one was set (a stored choice too)."""
    hub, worker, enabled = hub or {}, worker or {}, enabled or {}
    out: dict[str, bool | None] = {}
    for p in PRESETS:
        vals = [worker[m] for m in _switchable(p, "worker") if m in worker]
        for m in _switchable(p, "hub"):
            if m in hub:
                vals.append(hub[m])
            elif m in enabled:
                vals.append(enabled[m])
        out[p["id"]] = (True if vals and all(vals) else
                        False if vals and not any(vals) else None)
    return out


def preset_selected(states: dict[str, bool | None]) -> dict[str, bool]:
    """Checkbox state per preset: the stored choice, else its built-in default."""
    return {p["id"]: (p["default"] if states.get(p["id"]) is None else bool(states[p["id"]]))
            for p in PRESETS}


def preset_changes(selected: Iterable[str], states: dict[str, bool | None] | None = None
                   ) -> tuple[dict[str, bool], dict[str, bool]]:
    """``(hub, worker)`` ``{module: bool}`` for ticking exactly ``selected``,
    given the current ``states`` (:func:`preset_states`; none = nothing
    stored). Only a preset whose box changed from what it shows
    (:func:`preset_selected`) writes anything: ticked, its modules on;
    unticked, its switchable modules off (never :data:`KEEP_ON`). A preset
    left as it was writes nothing, so saving the default selection changes
    no default. A module turned on by one changed preset stays on."""
    chosen = {preset(p)["id"] for p in dict.fromkeys(selected or ())}
    shown = preset_selected(states or {})
    hub: dict[str, bool] = {}
    worker: dict[str, bool] = {}
    for p in PRESETS:
        want = p["id"] in chosen
        if want == shown[p["id"]]:
            continue
        for side, out in (("hub", hub), ("worker", worker)):
            for m in (p[side] if want else _switchable(p, side)):
                out[m] = out.get(m, False) or want
    return hub, worker


def stored_preset_states(store: Any, band_id: str | None,
                         hub_enabled_settings: dict[str, str] | None = None
                         ) -> dict[str, bool | None]:
    """:func:`preset_states` from the settings store alone (stored values,
    no environment): what the dashboard process can see without the hub."""
    store = _store(store)
    keys = HUB_ENABLED_SETTINGS if hub_enabled_settings is None else hub_enabled_settings
    enabled = {}
    for module, key in keys.items():
        row = store.get(key, "hub", "")
        if row and isinstance(row.get("value"), bool):
            enabled[module] = row["value"]
    return preset_states(hub_state(store), worker_defaults(band_id, store) if band_id else {},
                         enabled)


def apply_presets_to_store(store: Any, selected: Iterable[str], band_id: str | None, *,
                           actor: str, hub_enabled_settings: dict[str, str] | None = None,
                           source: str = "presets") -> dict:
    """Store a preset choice: the hub's set, the ``enabled`` setting of chosen
    hub plugins that have one (``hub_enabled_settings``: module -> setting
    key), and ``band_id``'s defaults for new workers. Only presets whose box
    changed write anything (:func:`preset_changes`). Does not touch a running
    hub (the hub node applies stored changes on its next sync; see
    :meth:`rook.hub.node.HubNode.sync_plugin_state`). Used where the hub node
    is not at hand (the dashboard's /setup wizard)."""
    store = _store(store)
    selected = list(dict.fromkeys(selected or ()))
    keys = HUB_ENABLED_SETTINGS if hub_enabled_settings is None else hub_enabled_settings
    states = stored_preset_states(store, band_id, keys)
    hub, worker = preset_changes(selected, states)
    note = "preset: " + (", ".join(selected) or "none")
    set_hub_state(store, hub, actor=actor, note=note, source=source)
    for module, key in keys.items():
        if hub.get(module):
            row = store.get(key, "hub", "")
            if not row or row.get("value") is not True:
                store.set(key, "hub", "", value=True, actor=actor, note=note, source=source)
    defaults = set_worker_defaults(store, band_id, worker, actor=actor, note=note,
                                   source=source) if band_id else {}
    return {"presets": selected, "hub": hub, "worker_defaults": defaults}


def enabled_setting_keys() -> dict[str, str]:
    """``{hub module: "<namespace>.enabled"}`` for built-in hub plugins whose
    settings schema has an ``enabled`` switch (knowledge, tasks, home, ...)."""
    import importlib
    import pkgutil
    out: dict[str, str] = {}
    try:
        pkg = importlib.import_module("rook.hub.plugins")
    except Exception:
        return out
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        try:
            obj = getattr(importlib.import_module(f"rook.hub.plugins.{info.name}"), "PLUGIN", None)
        except Exception:
            continue
        cls = obj if isinstance(obj, type) else type(obj) if obj is not None else None
        if cls is None or not getattr(cls, "NAMESPACE", ""):
            continue
        if any(getattr(s, "name", "") == "enabled" for s in getattr(cls, "SETTINGS", ())):
            out[info.name] = f"{cls.NAMESPACE}.enabled"
    return out


def setup_presets_html(states: dict[str, bool | None] | None = None) -> str:
    """The preset checkboxes for the /setup wizard (escaped HTML), ticked as
    the stored choices show them (:func:`preset_selected`), so re-running
    the wizard and saving it as shown reverses nothing chosen elsewhere."""
    ticked = preset_selected(states or {})
    from html import escape
    rows = ['<fieldset class="presets"><legend>What do you want Rook to do?</legend>',
            '<input type="hidden" name="presets_shown" value="1">',
            '<small>Applies to the hub and to workers you add later; change it any time on '
            'Manage &gt; Plugins. The ticks show the current choice (or what Rook does by '
            'default): only the groups you change are saved.</small>']
    for p in PRESETS:
        risk = ", ".join(p["tiers"])
        high = "".join(f'<span class="risk-high">{escape(h)}</span>' for h in p["high_risk"])
        rows.append(
            f'<label class="preset"><input type="checkbox" name="preset" value="{escape(p["id"])}"'
            f'{" checked" if ticked[p["id"]] else ""}> <b>{escape(p["title"])}</b> '
            f'<small>{escape(p["summary"])} Risk: {escape(risk)}.</small>{high}</label>')
    rows.append("</fieldset>")
    return "\n".join(rows)
