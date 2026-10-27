"""homeassistant.* and ha.* — Home Assistant devices as device workers.

This worker becomes the *delegate* for the Home Assistant devices listed in
``homeassistant.devices``: each shows on the band as a worker of its own (a
lamp, a smart plug, a thermostat, a fridge), with ``ha.*`` caps and a live
heartbeat ``hb.ha = {on, text}``. One band device per HA *device*, not per
entity: a thermostat with a temperature sensor and a fan mode is one worker,
and its entities are what its caps act on. See ``docs/design/devices.md``.

It keeps one connection to Home Assistant's WebSocket API, reads the device,
entity and area registries and every state once, then follows
``state_changed`` events, so heartbeats are current without polling. When
HA is down the devices stay listed (from the last registry seen) as
unavailable.

Config (settings, or env):
    HASS_URL       Home Assistant's address, e.g. ``https://192.0.2.20:8123``.
                   Blank leaves the plugin off.
    HASS_TOKEN     A long-lived access token (HA: your profile > Security).
    HASS_INSECURE  Skip TLS verification (HA OS's self-signed certificate).
    HASS_DEVICES   Which HA devices go on the band, comma-separated: an HA
                   device's name (or id), optionally as ``band-name=HA name``:
                   ``bed-lights=bed lights, Thermostat``. ``homeassistant.devices``
                   lists them. Blank puts none on the band.

Capabilities (on each device):
    ha.status                       read
    ha.on, ha.off, ha.set           write (physical)
    ha.service                      admin (any HA service on its entities:
                                    locks, alarms, valves, garage doors and
                                    gates go only this way)
And on this worker: ``homeassistant.devices`` (HA's devices, for picking),
``homeassistant.scenes`` and ``homeassistant.scene`` (admin; scenes belong
to no device).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from ...core.aiocompat import timeout as aio_timeout
from ...core.db import atomic_write_json
from ...core.devices import Device, valid_device_name
from ...core.pluginkit import ArgError, fail, require_str
from ..plugin import Plugin, capability, setting
from . import _hass_ws as hass

log = logging.getLogger("rook.worker.homeassistant")

RETRY_MIN, RETRY_MAX = 5.0, 60.0
STABLE_SECS = 60.0
RELOAD_DELAY = 2.0     # registry changes come in bursts; reload once they settle
SETTLE_SECS = 2.0      # how long ha.on/off/set wait for HA to report the new state

# A device's kind, from the first of these domains it has a main entity in;
# else "switch" when all it has are switches and power meters (a plug, a power
# strip), else "device": a fridge's ice-maker switch doesn't make it a switch.
KINDS = (("climate", "climate"), ("light", "light"), ("fan", "fan"), ("cover", "cover"),
         ("lock", "lock"), ("vacuum", "vacuum"), ("media_player", "media"),
         ("humidifier", "humidifier"), ("water_heater", "water_heater"),
         ("camera", "camera"), ("alarm_control_panel", "alarm"), ("valve", "valve"))
CONTROL = {d for d, _ in KINDS} | {"switch", "siren", "number", "select", "button",
                                   "input_boolean", "text", "input_number", "input_select",
                                   "input_text"}
# What ha.on / ha.off switch when no entity is named.
SWITCHABLE = {"light", "switch", "fan", "media_player", "climate", "humidifier",
              "water_heater", "input_boolean"}
# Switched only when named: a siren on a camera is not what "turn it on" means.
NAMED_ONLY = {"siren", "cover"}
# Security devices: only through ha.service, which is an admin cap.
GUARDED = {"lock", "alarm_control_panel", "valve"}
# Covers that are a way into the house count as security devices too.
GUARDED_COVERS = {"garage", "gate", "door"}
SETTABLE = {"number", "input_number", "select", "input_select", "climate", "cover", "fan",
            "media_player", "light", "text", "input_text", "water_heater", "humidifier"}
OFF_STATES = {"off", "unavailable", "unknown", "standby", "closed", "locked", "docked", ""}
# State attributes worth an agent's tokens, by domain.
ATTRS = {
    "light": ("brightness", "color_mode", "color_temp_kelvin", "rgb_color"),
    "climate": ("current_temperature", "temperature", "target_temp_low", "target_temp_high",
                "hvac_action", "hvac_modes", "fan_mode", "preset_mode"),
    "media_player": ("media_title", "media_artist", "app_name", "source", "volume_level",
                     "is_volume_muted"),
    "cover": ("current_position",),
    "fan": ("percentage", "preset_mode"),
    "number": ("min", "max", "step", "unit_of_measurement"),
    "input_number": ("min", "max", "step", "unit_of_measurement"),
    "select": ("options",),
    "input_select": ("options",),
    "sensor": ("unit_of_measurement", "device_class"),
    "binary_sensor": ("device_class",),
    "humidifier": ("humidity", "mode"),
    "water_heater": ("temperature", "operation_mode"),
}


PLUG_SENSORS = {"power", "energy", "voltage", "current", "power_factor"}


def _plug_like(e: dict) -> bool:
    """An entity a smart plug has: a switch, or a power meter."""
    dom = e["id"].split(".")[0]
    return dom in ("switch", "input_boolean") or (dom == "sensor" and e.get("dc") in PLUG_SENSORS)


def _guarded(e: dict) -> bool:
    """A lock, an alarm panel, a valve (a water main) or a garage door, gate or
    door: only through ha.service, the admin cap."""
    dom = e["id"].split(".")[0]
    return dom in GUARDED or (dom == "cover" and e.get("dc") in GUARDED_COVERS)


def slug(name: str) -> str:
    """A band name from an HA name: ``"Sam's desk lamp"`` -> ``sams-desk-lamp``."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower().replace("'", "")).strip("-")
    return s[:63] or "device"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def parse_devices(spec: str) -> list[tuple[str | None, str]]:
    """``"bed-lights=bed lights, Thermostat"`` -> ``[("bed-lights", "bed lights"),
    (None, "Thermostat")]``. A band name, when given, must be a valid one."""
    out: list[tuple[str | None, str]] = []
    for part in (spec or "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, ref = part.partition("=")
        if sep:
            name, ref = name.strip(), ref.strip()
            if not ref or not valid_device_name(name):
                raise ValueError(f"bad device entry {part!r}: expected band-name=HA device")
            out.append((name, ref))
        else:
            out.append((None, part))
    return out


class HADevice(Device):
    """One Home Assistant device: its entities, switched and set through HA."""

    NAMESPACE = "ha"
    TYPE = "device"
    DRIVER = "homeassistant"

    def __init__(self, plugin: "HomeAssistantPlugin", ha_id: str, name: str,
                 meta: dict) -> None:
        super().__init__(key=ha_id, name=name, description="")
        self.plugin = plugin
        self.ha_id = ha_id
        self.update(name, meta)

    def update(self, name: str, meta: dict) -> None:
        """Take a (re)read registry entry: ``{ha_name, model, manufacturer,
        area, entities: [{id, name, category}]}``."""
        self.name = name
        self.meta = meta
        self.entities: list[dict] = list(meta.get("entities") or [])
        mains = [e for e in self.entities if not e.get("category")
                 and e["id"].split(".")[0] in CONTROL]
        doms = {e["id"].split(".")[0] for e in mains}
        self.TYPE = next((k for d, k in KINDS if d in doms), "device")
        self.primary = next((d for d, _ in KINDS if d in doms), None)
        if self.primary is None and doms and doms <= {"switch", "input_boolean"} \
                and all(_plug_like(e) for e in self.entities if not e.get("category")):
            self.TYPE, self.primary = "switch", "switch"
        self.mains = [e for e in mains if self.primary and
                      e["id"].split(".")[0] in ({"switch", "input_boolean"}
                                                if self.primary == "switch" else {self.primary})]
        where = f" in {meta['area']}" if meta.get("area") else ""
        what = " ".join(x for x in (meta.get("manufacturer"), meta.get("model")) if x)
        self.description = (f"Home Assistant device {meta.get('ha_name') or name!r}{where}"
                            + (f" ({what})" if what else ""))

    # -- device protocol ---------------------------------------------------
    def info(self) -> dict:
        out = {"type": self.TYPE, "driver": self.DRIVER}
        if self.meta.get("model"):
            out["model"] = str(self.meta["model"])[:64]
        return out

    def _state(self, eid: str) -> dict:
        return self.plugin.states.get(eid) or {}

    def available(self) -> bool:
        if not self.plugin.connected:
            return False
        look = self.mains or self.entities
        return any(self._state(e["id"]).get("state") not in (None, "unavailable")
                   for e in look)

    def heartbeat(self) -> dict | None:
        if not self.available():
            return {"on": False, "available": False, "text": "unavailable"}
        on = [e for e in self.mains if self._state(e["id"]).get("state") not in OFF_STATES]
        # A device with no main switch (a fridge) is "on" while it answers.
        return {"on": bool(on) or not self.mains, "text": self._summary(on)}

    def _summary(self, on: list[dict]) -> str:
        if not self.mains:
            return "available"
        if len(self.mains) > 1 and self.primary in ("switch", "light", "fan"):
            return f"{len(on)} of {len(self.mains)} on"
        st = self._state(self.mains[0]["id"])
        state, a = str(st.get("state") or "unknown"), st.get("attributes") or {}
        unit = self.plugin.temp_unit
        if self.primary == "light" and state == "on" and a.get("brightness") is not None:
            return f"on · {round(a['brightness'] * 100 / 255)}%"
        if self.primary == "climate":
            parts = [state]
            if a.get("current_temperature") is not None:
                parts.append(f"{a['current_temperature']}{unit}")
            if a.get("temperature") is not None:
                parts.append(f"set {a['temperature']}{unit}")
            return " · ".join(parts)
        if self.primary == "media_player":
            what = a.get("media_title") or a.get("app_name") or a.get("source")
            return " · ".join(str(x) for x in (state, what) if x)
        if self.primary == "cover" and a.get("current_position") is not None:
            return f"{state} · {a['current_position']}%"
        return state

    # -- entities ----------------------------------------------------------
    def _entity_view(self, e: dict) -> dict:
        st = self._state(e["id"])
        out: dict[str, Any] = {"entity": e["id"], "name": e.get("name") or e["id"],
                               "state": st.get("state", "unknown")}
        if e.get("category"):
            out["category"] = e["category"]
        a = st.get("attributes") or {}
        for k in ATTRS.get(e["id"].split(".")[0], ()):
            if a.get(k) is not None:
                v = a[k]
                if k == "brightness":
                    k, v = "brightness_pct", round(v * 100 / 255)
                out[k] = v
        return out

    def _resolve(self, entity: str) -> dict:
        """One of this device's entities, by id, the part after the dot, or name."""
        entity = require_str(entity, "entity", max_len=255)
        for e in self.entities:
            if entity == e["id"]:
                return e
        want = _norm(entity)
        hits = [e for e in self.entities
                if want in (_norm(e["id"]), _norm(e["id"].split(".", 1)[1]), _norm(e.get("name")))]
        if not hits:
            hits = [e for e in self.entities if want and want in _norm(e.get("name"))]
        if len(hits) == 1:
            return hits[0]
        if hits:
            fail(f"{entity!r} matches several of {self.name}'s entities: "
                 + ", ".join(e["id"] for e in hits[:10]))
        fail(f"{self.name} has no entity {entity!r}; ha.status lists them")

    def _targets(self, entity: str | None, allowed: set[str], verb: str) -> list[dict]:
        if entity is not None and entity != "all":
            e = self._resolve(entity)
            dom = e["id"].split(".")[0]
            if _guarded(e):
                fail(f"{e['id']} is a {e.get('dc') or dom}: use ha.service (an admin "
                     "action) for it")
            if dom not in allowed:
                fail(f"{e['id']} can't be {verb} ({dom}); ha.service calls any of its services")
            return [e]
        cands = [e for e in self.entities if not e.get("category") and not _guarded(e)
                 and e["id"].split(".")[0] in allowed - NAMED_ONLY]
        if not cands:
            fail(f"{self.name} has nothing to be {verb}; ha.status lists its entities")
        if len(cands) > 1 and entity != "all":
            fail(f"{self.name} has several things that can be {verb}: "
                 + ", ".join(e["id"] for e in cands[:10])
                 + '. Name one with entity=, or entity="all"')
        return cands

    async def _call(self, domain: str, service: str, ids: list[str],
                    data: dict | None = None) -> None:
        await self.plugin.call_service(domain, service, ids, data)

    async def _settled(self, ids: list[str], before: dict[str, Any]) -> list[dict]:
        """The entities' views once HA reports a change (or ``SETTLE_SECS`` on)."""
        end = time.monotonic() + SETTLE_SECS
        while time.monotonic() < end:
            if all(self._state(i).get("last_updated") != before.get(i) for i in ids):
                break
            await asyncio.sleep(0.05)
        by_id = {e["id"]: e for e in self.entities}
        return [self._entity_view(by_id[i]) for i in ids if i in by_id]

    def _stamp(self, ids: list[str]) -> dict[str, Any]:
        return {i: self._state(i).get("last_updated") for i in ids}

    # -- caps --------------------------------------------------------------
    @capability("status", risk="read", tags=("physical",), scope="multi-machine")
    async def _status(self) -> dict:
        """The device and every entity's state: ``{name, kind, area, model,
        available, entities: [{entity, name, state, …}]}``. Config and
        diagnostic entities carry ``category``."""
        m = self.meta
        out: dict[str, Any] = {"name": self.name, "kind": self.TYPE,
                               "ha_name": m.get("ha_name"), "available": self.available()}
        for k in ("area", "manufacturer", "model"):
            if m.get(k):
                out[k] = m[k]
        if not self.plugin.connected:
            out["error"] = self.plugin.error or "not connected to Home Assistant"
        out["entities"] = [self._entity_view(e) for e in self.entities]
        return out

    @capability("on", risk="write", tags=("physical",), scope="multi-machine")
    async def _on(self, entity: str | None = None, brightness: int | None = None,
                  color: str | None = None, kelvin: int | None = None) -> dict:
        """Switch on (a light, plug, fan, media player…). ``entity`` picks one
        when the device has several (``"all"`` for every one). Lights also take
        ``brightness`` (1-100 %), ``color`` (a name like ``"warm white"``/``"red"``,
        or ``#rrggbb``) and ``kelvin`` (2000-6500)."""
        await self.plugin.live()
        targets = self._targets(entity, SWITCHABLE | NAMED_ONLY, "switched on")
        data: dict[str, Any] = {}
        if brightness is not None or color is not None or kelvin is not None:
            if any(t["id"].split(".")[0] != "light" for t in targets):
                raise ArgError("brightness, color and kelvin are for lights only")
            if brightness is not None:
                if isinstance(brightness, bool) or not isinstance(brightness, int) \
                        or not 1 <= brightness <= 100:
                    raise ArgError("brightness must be an integer 1-100 (percent)")
                data["brightness_pct"] = brightness
            if color is not None:
                color = require_str(color, "color", max_len=40)
                if re.match(r"^#?[0-9a-fA-F]{6}$", color):
                    h = color.lstrip("#")
                    data["rgb_color"] = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
                else:
                    data["color_name"] = color.lower()
            if kelvin is not None:
                if isinstance(kelvin, bool) or not isinstance(kelvin, int) \
                        or not 1500 <= kelvin <= 9000:
                    raise ArgError("kelvin must be an integer 1500-9000")
                data["color_temp_kelvin"] = kelvin
        return await self._switch(targets, "turn_on", data)

    @capability("off", risk="write", tags=("physical",), scope="multi-machine")
    async def _off(self, entity: str | None = None) -> dict:
        """Switch off. ``entity`` picks one when the device has several
        (``"all"`` for every one)."""
        await self.plugin.live()
        return await self._switch(self._targets(entity, SWITCHABLE | NAMED_ONLY, "switched off"),
                                  "turn_off", {})

    async def _switch(self, targets: list[dict], service: str, data: dict) -> dict:
        ids = [t["id"] for t in targets]
        before = self._stamp(ids)
        by_dom: dict[str, list[str]] = {}
        for i in ids:
            by_dom.setdefault(i.split(".")[0], []).append(i)
        for dom, group in by_dom.items():
            svc = service
            if dom == "cover":
                svc = "open_cover" if service == "turn_on" else "close_cover"
            elif dom == "valve":
                svc = "open_valve" if service == "turn_on" else "close_valve"
            await self._call(dom, svc, group, data or None)
        return {"ok": True, "entities": await self._settled(ids, before)}

    @capability("set", risk="write", tags=("physical",), scope="multi-machine")
    async def _set(self, value: Any, entity: str | None = None) -> dict:
        """Set a value on one entity; what it means depends on the entity:
        a number or text field its value, a select its option, a thermostat
        its target temperature (a number) or mode (``"heat"``, ``"off"``…),
        a light its brightness, a fan its speed, a cover its position, a
        media player its volume (those four 0-100). ``entity`` may be left
        out when the device has only one such thing."""
        if entity == "all":
            raise ArgError("ha.set sets one entity at a time")
        await self.plugin.live()
        [e] = self._targets(entity, SETTABLE, "set") if entity is not None else \
            [self._settable_default()]
        dom, eid = e["id"].split(".")[0], e["id"]
        a = self._state(eid).get("attributes") or {}

        def pct() -> int:
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not 0 <= value <= 100:
                raise ArgError(f"{eid}: value must be a number 0-100")
            return round(value)

        def num() -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ArgError(f"{eid}: value must be a number")
            lo, hi = a.get("min"), a.get("max")
            if (lo is not None and value < lo) or (hi is not None and value > hi):
                raise ArgError(f"{eid}: value must be from {lo} to {hi}")
            return value

        if dom in ("number", "input_number"):
            call = (dom, "set_value", {"value": num()})
        elif dom in ("select", "input_select"):
            opts = [str(o) for o in a.get("options") or []]
            pick = next((o for o in opts if _norm(o) == _norm(value)), None)
            if pick is None:
                raise ArgError(f"{eid}: value must be one of {', '.join(opts)}")
            call = (dom, "select_option", {"option": pick})
        elif dom in ("text", "input_text"):
            call = (dom, "set_value", {"value": require_str(value, "value", max_len=255,
                                                            empty=True)})
        elif dom == "climate":
            if isinstance(value, str):
                modes = [str(m) for m in a.get("hvac_modes") or []]
                if modes and value.lower() not in modes:
                    raise ArgError(f"{eid}: mode must be one of {', '.join(modes)}")
                call = (dom, "set_hvac_mode", {"hvac_mode": value.lower()})
            else:
                call = (dom, "set_temperature", {"temperature": num()})
        elif dom == "water_heater":
            call = (dom, "set_temperature", {"temperature": num()})
        elif dom == "humidifier":
            call = (dom, "set_humidity", {"humidity": pct()})
        elif dom == "light":
            call = (dom, "turn_on", {"brightness_pct": pct()})
        elif dom == "fan":
            call = (dom, "set_percentage", {"percentage": pct()})
        elif dom == "cover":
            call = (dom, "set_cover_position", {"position": pct()})
        else:   # media_player
            call = (dom, "volume_set", {"volume_level": pct() / 100})
        before = self._stamp([eid])
        await self._call(call[0], call[1], [eid], call[2])
        return {"ok": True, "service": f"{call[0]}.{call[1]}",
                "entities": await self._settled([eid], before)}

    def _settable_default(self) -> dict:
        cands = [e for e in self.entities if not e.get("category") and not _guarded(e)
                 and e["id"].split(".")[0] in SETTABLE]
        if len(cands) == 1:
            return cands[0]
        if not cands:
            fail(f"{self.name} has nothing to set; ha.status lists its entities "
                 "(settings in category config need entity=)")
        fail(f"{self.name} has several things that can be set: "
             + ", ".join(e["id"] for e in cands[:10]) + ". Name one with entity=")

    @capability("service", risk="admin", tags=("physical",), scope="multi-machine")
    async def _service(self, service: str, entity: str | None = None,
                       data: dict | None = None) -> dict:
        """Call any Home Assistant service on this device's entities, e.g.
        ``service="button.press", entity="reset water filter"``,
        ``"media_player.media_pause"``, ``"lock.unlock"``. Without ``entity``
        it goes to every entity of the service's domain. ``data`` is the
        service's own fields; the target is always this device's entities."""
        await self.plugin.live()
        service = require_str(service, "service", max_len=100)
        dom, dot, svc = service.partition(".")
        if not dot or not re.match(r"^[a-z0-9_]+$", dom) or not re.match(r"^[a-z0-9_]+$", svc):
            raise ArgError('service must look like "domain.service", e.g. "button.press"')
        if data is not None and not isinstance(data, dict):
            raise ArgError("data must be an object")
        data = dict(data or {})
        if any(k in data for k in ("entity_id", "device_id", "area_id", "target")):
            raise ArgError("data must not name a target: ha.service always acts on this device")
        if entity is not None:
            ids = [self._resolve(entity)["id"]]
            # The service's domain must be the entity's own: a domain-wide
            # one ("homeassistant.restart") would act on all of HA.
            if ids[0].split(".")[0] != dom:
                raise ArgError(f"{ids[0]} is not a {dom} entity")
        else:
            ids = [e["id"] for e in self.entities if e["id"].split(".")[0] == dom]
            if not ids:
                fail(f"{self.name} has no {dom} entity; ha.status lists them")
        before = self._stamp(ids)
        await self._call(dom, svc, ids, data or None)
        return {"ok": True, "service": service, "entities": await self._settled(ids, before)}


class HomeAssistantPlugin(Plugin):
    NAMESPACE = "homeassistant"
    SETTINGS = (
        setting("url", str, default="", scope="worker", env="HASS_URL", apply="reload",
                label="Home Assistant URL",
                help="e.g. https://192.0.2.20:8123. Blank leaves homeassistant.* off."),
        setting("token", str, secret=True, scope="worker", env="HASS_TOKEN", apply="reload",
                label="Access token",
                help="A long-lived access token (HA: your profile > Security)."),
        setting("insecure", bool, default=False, scope="worker", env="HASS_INSECURE",
                apply="reload", label="Skip TLS verification",
                help="For HA's own self-signed certificate (HA OS's default)."),
        setting("devices", str, default="", scope="worker", env="HASS_DEVICES",
                apply="reload", label="Devices",
                help="HA devices to put on the band, comma-separated: an HA device's "
                     "name or id, or band-name=HA name. homeassistant.devices lists them."),
    )

    def __init__(self) -> None:
        super().__init__()
        self.client: hass.HAClient | None = None
        self.states: dict[str, dict] = {}
        self.registry: dict[str, dict] = {}     # HA device id -> meta
        self.version: str | None = None
        self.temp_unit = "°"
        self.error: str | None = None
        self._devices: dict[str, HADevice] = {}
        # Each configured entry, once matched: {entry: {id, name}}, so a rename
        # in HA neither drops the device nor changes its band name.
        self._bindings: dict[str, dict] = {}
        self.not_found: list[str] = []
        self._task: asyncio.Task | None = None
        self._reload: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._stopped = False

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.connected

    def _cfg(self) -> tuple[str, str, bool]:
        s = self.settings
        return (str(s.get("url") or "").strip(), str(s.get("token") or ""),
                bool(s.get("insecure", False)))

    def _spec(self) -> list[tuple[str | None, str]]:
        try:
            return parse_devices(self.settings.get("devices") or "")
        except ValueError as e:
            log.warning("homeassistant.devices: %s", e)
            return []

    def available(self) -> bool:
        url, token, _ = self._cfg()
        return bool(url and token) and _ws_supported()

    def unavailable_reason(self) -> str:
        if not _ws_supported():
            return "Needs the Python websockets package (pip install websockets)."
        url, token, _ = self._cfg()
        if not url:
            return "No Home Assistant configured: set homeassistant.url."
        return "No access token: set homeassistant.token (HASS_TOKEN)."

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self._stopped = False
        try:
            with open(self.data_dir / "devices.json", encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, ValueError):
            cached = {}
        try:
            with open(self.data_dir / "bindings.json", encoding="utf-8") as f:
                bound = json.load(f)
            self._bindings = {k: v for k, v in bound.items()
                              if isinstance(v, dict) and isinstance(v.get("id"), str)}
        except (OSError, ValueError, AttributeError):
            self._bindings = {}
        if isinstance(cached, dict):
            # Listed (as unavailable) before HA answers, or while it is down.
            self.registry = {k: v for k, v in cached.items() if isinstance(v, dict)}
            self._match(cached=True)
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        for task in (self._task, self._reload):
            if task is not None:
                task.cancel()
                await asyncio.wait({task}, timeout=5)
        self._task = self._reload = None
        if self.client is not None:
            await self.client.close()
            self.client = None

    def devices(self) -> list[Device]:
        return list(self._devices.values())

    async def _run(self) -> None:
        delay = RETRY_MIN
        while not self._stopped:
            client = None
            try:
                client = await self._connect()
            except asyncio.CancelledError:
                raise
            except hass.AuthFailed as e:
                self.error = f"Home Assistant refused the access token ({e})"
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
            if client is not None:
                since = time.monotonic()
                await client.closed.wait()
                if self.connected:
                    continue   # a cap already reconnected: follow that connection
                self.error = "connection to Home Assistant closed"
                if time.monotonic() - since >= STABLE_SECS:
                    delay = RETRY_MIN
                    continue
            self._wake.clear()
            try:
                async with aio_timeout(delay):
                    await self._wake.wait()
            except (TimeoutError, asyncio.TimeoutError):
                pass
            delay = min(delay * 2, RETRY_MAX)

    async def _connect(self) -> hass.HAClient:
        """The live client, connecting (and reading everything) if needed."""
        async with self._lock:
            if self._stopped:
                raise hass.HAError("the homeassistant plugin stopped")
            if self.connected:
                return self.client
            return await self._open()

    async def _open(self) -> hass.HAClient:
        url, token, insecure = self._cfg()
        client = hass.HAClient(url, token, insecure=insecure)
        await client.connect()
        try:
            # Follow changes first, so nothing between the read and the
            # subscription is missed.
            await client.subscribe("state_changed", self._on_state)
            for ev in ("device_registry_updated", "entity_registry_updated",
                       "area_registry_updated"):
                try:
                    await client.subscribe(ev, self._on_registry)
                except hass.HAError:
                    pass   # not allowed for this user: renames show after a reconnect
            await self._load(client)
        except BaseException:
            await client.close()
            raise
        if self.client is not None and self.client is not client:
            await self.client.close()
        self.client, self.version, self.error = client, client.version, None
        return client

    async def _load(self, c: hass.HAClient) -> None:
        cfg = await c.command("get_config") or {}
        self.temp_unit = ((cfg.get("unit_system") or {}).get("temperature")) or "°"
        self.states = {s["entity_id"]: s for s in await c.command("get_states") or []
                       if isinstance(s, dict) and s.get("entity_id")}
        await self._load_registry(c)

    async def _load_registry(self, c: hass.HAClient) -> None:
        devs = await c.command("config/device_registry/list") or []
        ents = await c.command("config/entity_registry/list") or []
        try:
            areas = {a["area_id"]: a.get("name") for a in
                     await c.command("config/area_registry/list") or []}
        except hass.HAError:
            areas = {}
        by_dev: dict[str, list[dict]] = {}
        for e in ents:
            if e.get("device_id") and not e.get("disabled_by"):
                by_dev.setdefault(e["device_id"], []).append(e)
        reg: dict[str, dict] = {}
        for d in devs:
            if not d.get("id") or d.get("disabled_by"):
                continue
            ha_name = d.get("name_by_user") or d.get("name") or d["id"]
            area = d.get("area_id")
            entities = []
            for e in sorted(by_dev.get(d["id"], []), key=lambda e: e["entity_id"]):
                st = self.states.get(e["entity_id"]) or {}
                name = (e.get("name") or e.get("original_name")
                        or (st.get("attributes") or {}).get("friendly_name") or "")
                ent = {"id": e["entity_id"], "name": _short(name, ha_name),
                       "category": e.get("entity_category")}
                dc = (e.get("device_class") or e.get("original_device_class")
                      or (st.get("attributes") or {}).get("device_class"))
                if dc:
                    ent["dc"] = str(dc)
                entities.append(ent)
                area = area or e.get("area_id")
            reg[d["id"]] = {"ha_name": ha_name, "model": d.get("model"),
                            "manufacturer": d.get("manufacturer"),
                            "area": areas.get(area) if area else None,
                            "service": d.get("entry_type") == "service",
                            "entities": entities}
        self.registry = reg
        self._match()

    def _match(self, cached: bool = False) -> None:
        """(Re)build the band devices from the config and the registry,
        keeping each device object while it stays, so the delegate's device
        table keeps its entry. ``cached``: the registry is the last one saved
        (only the configured devices), so a miss is no news and not saved."""
        by_name: dict[str, list[str]] = {}
        for k, m in self.registry.items():
            by_name.setdefault(_norm(m.get("ha_name")), []).append(k)
        fresh: dict[str, HADevice] = {}
        used: set[str] = set()
        bindings: dict[str, dict] = {}
        missing: list[str] = []
        for band_name, ref in self._spec():
            entry = f"{band_name}={ref}" if band_name else ref
            bound = self._bindings.get(entry)
            if bound and bound["id"] in self.registry:
                ids = [bound["id"]]
            elif ref in self.registry:
                ids = [ref]
            else:
                ids = by_name.get(_norm(ref), [])
            if len(ids) != 1:
                if bound:
                    bindings[entry] = bound   # gone for now; HA may bring it back
                if cached:
                    continue
                missing.append(ref)
                log.warning("homeassistant.devices: %r %s", ref,
                            "matches several HA devices; use its id" if ids
                            else "is not an HA device")
                continue
            ha_id = ids[0]
            meta = self.registry[ha_id]
            name = band_name or (bound["name"] if bound and bound["id"] == ha_id
                                 and bound.get("name") else slug(meta.get("ha_name") or ref))
            bindings[entry] = {"id": ha_id, "name": name}
            if not valid_device_name(name) or name.lower() in used or ha_id in fresh:
                log.warning("homeassistant.devices: %r left out (bad or repeated name)", ref)
                continue
            used.add(name.lower())
            dev = self._devices.get(ha_id)
            if dev is None:
                dev = HADevice(self, ha_id, name, meta)
            else:
                dev.update(name, meta)
            fresh[ha_id] = dev
        self._devices = fresh
        if cached:
            return
        self.not_found = missing
        self._bindings = bindings
        cache = {k: self.registry[k] for k in fresh}
        try:
            atomic_write_json(self.data_dir / "devices.json", cache, indent=1)
            atomic_write_json(self.data_dir / "bindings.json", bindings, indent=1)
        except OSError:
            log.debug("could not cache the HA devices", exc_info=True)

    def _on_state(self, event: dict) -> None:
        data = event.get("data") or {}
        eid, new = data.get("entity_id"), data.get("new_state")
        if not eid:
            return
        if new is None:
            self.states.pop(eid, None)
        else:
            self.states[eid] = new

    def _on_registry(self, event: dict) -> None:
        if self._reload is None or self._reload.done():
            self._reload = asyncio.get_running_loop().create_task(self._reload_later())

    async def _reload_later(self) -> None:
        await asyncio.sleep(RELOAD_DELAY)
        c = self.client
        if c is not None and c.connected:
            try:
                await self._load_registry(c)
            except Exception:
                log.debug("re-reading the HA registries failed", exc_info=True)

    # -- calls -------------------------------------------------------------
    async def live(self) -> hass.HAClient:
        """The live connection, or a clear failure."""
        if self.connected:
            return self.client
        try:
            return await self._connect()
        except hass.AuthFailed as e:
            fail(f"Home Assistant refused the access token ({e}); check homeassistant.token")
        except Exception as e:
            fail(f"Home Assistant is unreachable ({type(e).__name__}: {e})")

    async def call_service(self, domain: str, service: str, ids: list[str],
                           data: dict | None = None) -> None:
        c = await self.live()
        fields: dict[str, Any] = {"domain": domain, "service": service,
                                  "target": {"entity_id": ids}}
        if data:
            fields["service_data"] = data
        try:
            await c.command("call_service", **fields)
        except hass.HAError as e:
            fail(f"Home Assistant: {domain}.{service} failed: {e}")
        except (TimeoutError, asyncio.TimeoutError):
            fail(f"Home Assistant did not answer {domain}.{service} in time; "
                 "it may still happen, check ha.status")

    # -- caps on the delegate ------------------------------------------------
    @capability("devices", risk="read", scope="multi-machine")
    async def _devices_cap(self, query: str = "", all: bool = False) -> dict:
        """Home Assistant's devices, for picking which go on the band
        (homeassistant.devices setting): ``{connected, version, items: [{id,
        ha_name, area, model, controls, available, on_band_as}], not_found}``
        (``not_found``: configured entries no single HA device matches). Leaves out
        HA's own services and devices with nothing to control unless ``all``."""
        if not self.connected:
            try:
                await self._connect()
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
        q = _norm(query)
        on_band = {k: d.name for k, d in self._devices.items()}
        items = []
        for k, m in self.registry.items():
            doms = sorted({e["id"].split(".")[0] for e in m.get("entities") or []
                           if not e.get("category") and e["id"].split(".")[0] in CONTROL})
            if not all and (m.get("service") or not doms):
                continue
            if q and q not in _norm(m.get("ha_name")) and q not in _norm(m.get("area")) \
                    and q not in _norm(m.get("model")):
                continue
            avail = any((self.states.get(e["id"]) or {}).get("state") not in (None, "unavailable")
                        for e in m.get("entities") or [])
            items.append({"id": k, "ha_name": m.get("ha_name"), "area": m.get("area"),
                          "model": m.get("model"), "controls": doms, "available": avail,
                          "on_band_as": on_band.get(k)})
        items.sort(key=lambda i: (not i["available"], str(i["ha_name"]).lower()))
        out: dict[str, Any] = {"connected": self.connected, "version": self.version,
                               "items": items}
        if self.not_found:
            out["not_found"] = list(self.not_found)   # configured, but no (single) HA device
        if self.error and not self.connected:
            out["error"] = self.error
        return out

    @capability("scenes", risk="read", scope="multi-machine")
    async def _scenes(self) -> dict:
        """Home Assistant's scenes: ``{items: [{scene, name}]}``."""
        await self.live()
        return {"items": [{"scene": k, "name": (s.get("attributes") or {}).get("friendly_name") or k}
                          for k, s in sorted(self.states.items()) if k.startswith("scene.")]}

    @capability("scene", risk="admin", tags=("physical",), scope="multi-machine")
    async def _scene(self, name: str) -> dict:
        """Turn on a scene by id or name (``"office lights full"``). Admin: a
        scene can do anything HA can, unlocking a door included."""
        await self.live()
        name = require_str(name, "name", max_len=200)
        scenes = {k: (s.get("attributes") or {}).get("friendly_name") or k
                  for k, s in self.states.items() if k.startswith("scene.")}
        want = _norm(name)
        hits = [k for k, n in scenes.items() if name == k or want in (_norm(n), _norm(k[6:]))]
        if not hits:
            hits = [k for k, n in scenes.items() if want and want in _norm(n)]
        if len(hits) != 1:
            fail((f"{name!r} matches several scenes: " + ", ".join(hits[:10])) if hits
                 else f"no scene matches {name!r}; homeassistant.scenes lists them")
        await self.call_service("scene", "turn_on", hits)
        return {"ok": True, "scene": hits[0], "name": scenes[hits[0]]}


def _short(name: str, device: str) -> str:
    """An entity's name without its device's name in front:
    ``"Front Door Motion detection"`` on ``Front Door`` -> ``"Motion detection"``."""
    if device and name.lower().startswith(device.lower() + " "):
        return name[len(device) + 1:]
    return name


def _ws_supported() -> bool:
    import importlib.util
    return importlib.util.find_spec("websockets") is not None


PLUGIN = HomeAssistantPlugin
