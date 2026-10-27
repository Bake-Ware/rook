"""The Home Assistant delegate plugin against a fake HA WebSocket API.

The fake follows HA 2026.7's real message shapes (auth handshake, registry
lists, get_states, call_service with a target, state_changed events), with
a small house modelled on a real one: a light, a thermostat, a power strip,
a doorbell camera with a siren, a fridge, a lock, and HA's own services."""

import asyncio
import json

import pytest
import pytest_asyncio
import websockets

from rook.core.pluginkit import CapError
from rook.worker.plugins import _hass_ws as hass
from rook.worker.plugins import homeassistant as ha

TOKEN = "tok-1"


def _ent(eid, device, name=None, category=None, disabled=None):
    return {"entity_id": eid, "device_id": device, "name": None, "original_name": name,
            "entity_category": category, "disabled_by": disabled, "hidden_by": None,
            "area_id": None, "platform": "x"}


class FakeHA:
    def __init__(self):
        self.calls = []
        self.subs = []          # (ws, id, event_type)
        self.connections = 0
        self.fail_services = set()
        self.devices = [
            {"id": "dev-bed", "name": "bed lights", "name_by_user": None, "area_id": "bedroom_1",
             "manufacturer": None, "model": None, "entry_type": None, "disabled_by": None},
            {"id": "dev-thermo", "name": "Thermostat", "name_by_user": None,
             "area_id": "living_room", "manufacturer": "Resideo",
             "model": "Honeywell Home Thermostat", "entry_type": None, "disabled_by": None},
            {"id": "dev-strip", "name": "ninjas tank", "name_by_user": None, "area_id": None,
             "manufacturer": "Tuya", "model": "Smart Power Strip", "entry_type": None,
             "disabled_by": None},
            {"id": "dev-door", "name": "Front Door", "name_by_user": None, "area_id": None,
             "manufacturer": "Ring", "model": "Doorbell Wired", "entry_type": None,
             "disabled_by": None},
            {"id": "dev-fridge", "name": "Family Hub", "name_by_user": None, "area_id": "office",
             "manufacturer": "Samsung Electronics", "model": "25K_REF_LCD_FHUB10.0",
             "entry_type": None, "disabled_by": None},
            {"id": "dev-lock", "name": "Back door lock", "name_by_user": None, "area_id": None,
             "manufacturer": "Yale", "model": "Assure", "entry_type": None, "disabled_by": None},
            {"id": "dev-assist", "name": "assist microphone", "name_by_user": None,
             "area_id": "kitchen", "manufacturer": None, "model": None, "entry_type": "service",
             "disabled_by": None},
            {"id": "dev-spk1", "name": "Jr's room speaker", "name_by_user": None, "area_id": None,
             "manufacturer": "Google Inc.", "model": "Google Home", "entry_type": None,
             "disabled_by": None},
            {"id": "dev-spk2", "name": "Jr's room speaker", "name_by_user": None, "area_id": None,
             "manufacturer": "Google Inc.", "model": "Google Home", "entry_type": None,
             "disabled_by": None},
        ]
        self.entities = [
            _ent("light.bedroom_1_bed_lights", "dev-bed", "bed lights"),
            _ent("climate.thermostat", "dev-thermo", None),
            _ent("sensor.thermostat_humidity", "dev-thermo", "Humidity"),
            _ent("switch.ninjas_tank_socket_1", "dev-strip", "Socket 1"),
            _ent("switch.ninjas_tank_socket_2", "dev-strip", "Socket 2"),
            _ent("switch.ninjas_tank_child_lock", "dev-strip", "Child lock", category="config"),
            _ent("select.ninjas_tank_power_on_behavior", "dev-strip", "Power on behavior",
                 category="config"),
            _ent("camera.front_door", "dev-door", None),
            _ent("switch.front_door_motion_detection", "dev-door", "Motion detection"),
            _ent("siren.front_door_siren", "dev-door", "Siren"),
            _ent("number.front_door_volume", "dev-door", "Volume", category="config"),
            _ent("number.office_family_hub_fridge_temperature", "dev-fridge",
                 "Fridge temperature"),
            _ent("button.office_family_hub_reset_water_filter", "dev-fridge",
                 "Reset water filter", category="config"),
            _ent("switch.office_family_hub_power_cool", "dev-fridge", "Power cool"),
            _ent("binary_sensor.office_family_hub_fridge_door", "dev-fridge", "Fridge door"),
            _ent("sensor.ninjas_tank_power", "dev-strip", "Power"),
            _ent("camera.front_door_live_view", "dev-door", "Live view"),
            _ent("lock.back_door", "dev-lock", None),
            _ent("switch.assist_microphone_mute", "dev-assist", "Mute"),
            _ent("media_player.jr_s_room_speaker", "dev-spk1", None),
            _ent("media_player.jr_s_room_speaker_2", "dev-spk2", None),
            _ent("light.gone", "dev-bed", "old", disabled="user"),
        ]
        self.areas = [{"area_id": "bedroom_1", "name": "Bedroom 1"},
                      {"area_id": "living_room", "name": "Living Room"},
                      {"area_id": "office", "name": "office"},
                      {"area_id": "kitchen", "name": "Kitchen"}]
        self.states = {}
        for eid, state, attrs in (
                ("light.bedroom_1_bed_lights", "off", {"friendly_name": "bed lights",
                                                       "supported_color_modes": ["rgb"]}),
                ("climate.thermostat", "heat", {"friendly_name": "Thermostat",
                                                "current_temperature": 68, "temperature": 70,
                                                "hvac_modes": ["off", "heat", "cool"],
                                                "min_temp": 50, "max_temp": 90}),
                ("sensor.thermostat_humidity", "41", {"unit_of_measurement": "%"}),
                ("switch.ninjas_tank_socket_1", "on", {}),
                ("switch.ninjas_tank_socket_2", "off", {}),
                ("switch.ninjas_tank_child_lock", "off", {}),
                ("select.ninjas_tank_power_on_behavior", "on",
                 {"options": ["off", "on", "memory"]}),
                ("camera.front_door", "idle", {}),
                ("switch.front_door_motion_detection", "on", {}),
                ("siren.front_door_siren", "off", {}),
                ("number.front_door_volume", "8.0", {"min": 0, "max": 11, "step": 1}),
                ("number.office_family_hub_fridge_temperature", "40",
                 {"min": 34, "max": 44, "step": 1, "unit_of_measurement": "°F"}),
                ("button.office_family_hub_reset_water_filter", "unknown", {}),
                ("switch.office_family_hub_power_cool", "off", {}),
                ("binary_sensor.office_family_hub_fridge_door", "off", {"device_class": "door"}),
                ("sensor.ninjas_tank_power", "12.5", {"device_class": "power",
                                                      "unit_of_measurement": "W"}),
                ("camera.front_door_live_view", "idle", {}),
                ("lock.back_door", "locked", {}),
                ("switch.assist_microphone_mute", "off", {}),
                ("media_player.jr_s_room_speaker", "unavailable", {}),
                ("media_player.jr_s_room_speaker_2", "off", {}),
                ("scene.office_lights_full", "unknown", {"friendly_name": "office lights full"}),
                ("scene.bed_lights_off", "unknown", {"friendly_name": "bed lights off"})):
            self.set_state(eid, state, attrs)
        self.stamp = 0

    def set_state(self, eid, state, attrs=None):
        old = self.states.get(eid)
        a = dict(old["attributes"]) if old else {}
        a.update(attrs or {})
        self.stamp = getattr(self, "stamp", 0) + 1
        self.states[eid] = {"entity_id": eid, "state": state, "attributes": a,
                            "last_updated": f"t{self.stamp}"}
        return old, self.states[eid]

    async def emit(self, event_type, data):
        for ws, mid, et in list(self.subs):
            if et == event_type:
                await ws.send(json.dumps({"id": mid, "type": "event",
                                          "event": {"event_type": et, "data": data}}))

    async def service(self, domain, service, ids, data):
        if f"{domain}.{service}" in self.fail_services:
            return {"code": "not_found", "message": f"Service {domain}.{service} not found."}
        for eid in ids:
            dom = eid.split(".")[0]
            new = None
            if service == "turn_on":
                new = ("on", {"brightness": round(data.get("brightness_pct", 100) * 255 / 100)}
                       if dom == "light" else {})
            elif service == "turn_off":
                new = ("off", {})
            elif service == "set_value":
                new = (str(data["value"]), {})
            elif service == "select_option":
                new = (data["option"], {})
            elif service == "set_temperature":
                new = (self.states[eid]["state"], {"temperature": data["temperature"]})
            elif service == "set_hvac_mode":
                new = (data["hvac_mode"], {})
            elif service == "press":
                new = ("2026-10-27T15:00:00", {})
            if new is not None:
                old, cur = self.set_state(eid, *new)
                await self.emit("state_changed", {"entity_id": eid, "old_state": old,
                                                  "new_state": cur})
        return None

    async def handler(self, ws):
        self.connections += 1
        await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.7.2"}))
        auth = json.loads(await ws.recv())
        if auth.get("access_token") != TOKEN:
            await ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token"}))
            return
        await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.7.2"}))
        async for raw in ws:
            msg = json.loads(raw)
            mid, kind = msg["id"], msg["type"]
            result, error = None, None
            if kind == "subscribe_events":
                self.subs.append((ws, mid, msg["event_type"]))
            elif kind == "get_config":
                result = {"version": "2026.7.2", "unit_system": {"temperature": "°F"}}
            elif kind == "get_states":
                result = list(self.states.values())
            elif kind == "config/device_registry/list":
                result = self.devices
            elif kind == "config/entity_registry/list":
                result = self.entities
            elif kind == "config/area_registry/list":
                result = self.areas
            elif kind == "call_service":
                ids = msg["target"]["entity_id"]
                self.calls.append((msg["domain"], msg["service"], ids,
                                   msg.get("service_data") or {}))
                error = await self.service(msg["domain"], msg["service"], ids,
                                           msg.get("service_data") or {})
                result = {"context": {"id": "c"}}
            else:
                error = {"code": "unknown_command", "message": "Unknown command."}
            out = {"id": mid, "type": "result", "success": error is None}
            out["result" if error is None else "error"] = result if error is None else error
            await ws.send(json.dumps(out))


@pytest_asyncio.fixture
async def fake():
    f = FakeHA()
    async with websockets.serve(f.handler, "127.0.0.1", 0) as server:
        f.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        yield f


def _plugin(tmp_path, monkeypatch, fake, devices="bed-lights=bed lights, Thermostat, "
            "ninjas tank, Front Door, fridge=Family Hub, dev-lock", token=TOKEN):
    monkeypatch.setenv("HASS_URL", fake.url if fake else "http://127.0.0.1:1")
    monkeypatch.setenv("HASS_TOKEN", token)
    monkeypatch.setenv("HASS_DEVICES", devices)
    monkeypatch.setattr(ha, "SETTLE_SECS", 1.0)
    p = ha.HomeAssistantPlugin()
    p.__dict__["_data_root"] = str(tmp_path)
    return p


async def _until(cond, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


def _dev(p, name):
    return next(d for d in p.devices() if d.name == name)


# -- config ------------------------------------------------------------------

def test_device_list_parsing_and_names():
    assert ha.parse_devices("bed-lights=bed lights, Thermostat\n") == [
        ("bed-lights", "bed lights"), (None, "Thermostat")]
    assert ha.parse_devices("") == []
    for bad in ("x=", "bad/name=lamp"):
        with pytest.raises(ValueError):
            ha.parse_devices(bad)
    assert ha.slug("Jr's room speaker") == "jrs-room-speaker"
    assert ha.slug("game terminal ") == "game-terminal"
    assert hass.ws_url("https://ha.local:8123/") == "wss://ha.local:8123/api/websocket"
    assert hass.ws_url("192.0.2.5:8123") == "ws://192.0.2.5:8123/api/websocket"


def test_plugin_is_off_without_url_or_token(tmp_path, monkeypatch):
    monkeypatch.delenv("HASS_URL", raising=False)
    monkeypatch.delenv("HASS_TOKEN", raising=False)
    p = ha.HomeAssistantPlugin()
    p.__dict__["_data_root"] = str(tmp_path)
    assert not p.available() and "homeassistant.url" in p.unavailable_reason()
    monkeypatch.setenv("HASS_URL", "http://192.0.2.5:8123")
    assert not p.available() and "token" in p.unavailable_reason()
    monkeypatch.setenv("HASS_TOKEN", "x")
    assert p.available()


def test_caps_declare_scope_and_risk():
    caps = ha.HADevice.declared_caps()
    assert sorted(caps) == ["ha.off", "ha.on", "ha.service", "ha.set", "ha.status"]
    risks = {k: v._rook_cap_meta.risk for k, v in caps.items()}
    assert risks == {"ha.status": "read", "ha.on": "write", "ha.off": "write",
                     "ha.set": "write", "ha.service": "admin"}


# -- the band devices ----------------------------------------------------------

@pytest.mark.asyncio
async def test_devices_heartbeats_and_status(fake, tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch, fake)
    await p.start()
    try:
        await _until(lambda: p.connected and len(p.devices()) == 6)
        names = sorted(d.name for d in p.devices())
        assert names == ["back-door-lock", "bed-lights", "fridge", "front-door",
                         "ninjas-tank", "thermostat"]
        bed, thermo, strip = _dev(p, "bed-lights"), _dev(p, "thermostat"), _dev(p, "ninjas-tank")
        assert bed.info() == {"type": "light", "driver": "homeassistant"}
        assert thermo.info() == {"type": "climate", "driver": "homeassistant",
                                 "model": "Honeywell Home Thermostat"}
        assert _dev(p, "front-door").TYPE == "camera" and _dev(p, "back-door-lock").TYPE == "lock"
        assert _dev(p, "front-door").heartbeat() == {"on": True, "text": "idle"}   # two cameras, one state
        assert bed.heartbeat() == {"on": False, "text": "off"}
        assert thermo.heartbeat() == {"on": True, "text": "heat · 68°F · set 70°F"}
        assert strip.heartbeat() == {"on": True, "text": "1 of 2 on"}
        # A fridge whose only main controls are switches is a device, not a
        # switch that reads "off"; a power strip with a power meter is a switch.
        fridge = _dev(p, "fridge")
        assert fridge.TYPE == "device" and fridge.heartbeat() == {"on": True, "text": "available"}
        assert strip.TYPE == "switch"
        assert "in Living Room" in thermo.description
        st = await thermo._status()
        assert st["area"] == "Living Room" and st["available"] is True
        assert st["entities"][0] == {"entity": "climate.thermostat", "name": "Thermostat",
                                     "state": "heat", "current_temperature": 68,
                                     "temperature": 70, "hvac_modes": ["off", "heat", "cool"]}
        assert st["entities"][1]["name"] == "Humidity"
        sst = await strip._status()
        assert {"entity": "switch.ninjas_tank_child_lock", "name": "Child lock",
                "state": "off", "category": "config"} in sst["entities"]
        # Disabled entities are left out.
        assert all(e["entity"] != "light.gone" for e in (await bed._status())["entities"])
        # State follows HA's events.
        old, cur = fake.set_state("light.bedroom_1_bed_lights", "on", {"brightness": 128})
        await fake.emit("state_changed", {"entity_id": "light.bedroom_1_bed_lights",
                                          "old_state": old, "new_state": cur})
        await _until(lambda: bed.heartbeat()["on"])
        assert bed.heartbeat() == {"on": True, "text": "on · 50%"}
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_switching(fake, tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch, fake)
    await p.start()
    try:
        await _until(lambda: p.connected and len(p.devices()) == 6)
        bed, strip, door = _dev(p, "bed-lights"), _dev(p, "ninjas-tank"), _dev(p, "front-door")
        r = await bed._on(brightness=30, color="#ff8000")
        assert fake.calls[-1] == ("light", "turn_on", ["light.bedroom_1_bed_lights"],
                                  {"brightness_pct": 30, "rgb_color": [255, 128, 0]})
        assert r["ok"] and r["entities"][0]["state"] == "on"
        assert r["entities"][0]["brightness_pct"] == 30
        await bed._on(color="warm white")
        assert fake.calls[-1][3] == {"color_name": "warm white"}
        await bed._off()
        assert fake.calls[-1][:3] == ("light", "turn_off", ["light.bedroom_1_bed_lights"])
        with pytest.raises(TypeError):
            await bed._on(brightness=0)
        # Several sockets: name one, or all; never a guess.
        with pytest.raises(CapError, match="several things that can be switched off"):
            await strip._off()
        await strip._off(entity="socket 2")
        assert fake.calls[-1][2] == ["switch.ninjas_tank_socket_2"]
        await strip._on(entity="all")
        assert fake.calls[-1][2] == ["switch.ninjas_tank_socket_1", "switch.ninjas_tank_socket_2"]
        with pytest.raises(TypeError, match="lights only"):
            await strip._on(entity="socket 1", brightness=50)
        # A doorbell: "on" without a name is its one plain switch, never the siren.
        n = len(fake.calls)
        await door._on()
        assert fake.calls[n][2] == ["switch.front_door_motion_detection"]
        await door._on(entity="siren")   # only when named
        assert fake.calls[-1][:3] == ("siren", "turn_on", ["siren.front_door_siren"])
        with pytest.raises(CapError, match="no entity 'garage'"):
            await door._on(entity="garage")
        # A lock only through ha.service, the admin cap.
        lock = _dev(p, "back-door-lock")
        with pytest.raises(CapError, match="nothing to be switched on"):
            await lock._on()
        with pytest.raises(CapError, match="use ha.service"):
            await lock._off(entity="lock.back_door")
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_set_values(fake, tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch, fake)
    await p.start()
    try:
        await _until(lambda: p.connected and len(p.devices()) == 6)
        thermo, fridge, strip = _dev(p, "thermostat"), _dev(p, "fridge"), _dev(p, "ninjas-tank")
        r = await thermo._set(72)
        assert fake.calls[-1] == ("climate", "set_temperature", ["climate.thermostat"],
                                  {"temperature": 72})
        assert r["service"] == "climate.set_temperature" and r["entities"][0]["temperature"] == 72
        await thermo._set("Cool")
        assert fake.calls[-1][1:] == ("set_hvac_mode", ["climate.thermostat"],
                                      {"hvac_mode": "cool"})
        with pytest.raises(TypeError, match="mode must be one of"):
            await thermo._set("party")
        await fridge._set(38)
        assert fake.calls[-1][:2] == ("number", "set_value")
        with pytest.raises(TypeError, match="from 34 to 44"):
            await fridge._set(50)
        await strip._set("memory", entity="power on behavior")
        assert fake.calls[-1] == ("select", "select_option",
                                  ["select.ninjas_tank_power_on_behavior"], {"option": "memory"})
        with pytest.raises(CapError, match="nothing to set"):
            await strip._set(1)
        with pytest.raises(TypeError, match="one entity at a time"):
            await strip._set(1, entity="all")
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_service_escape_hatch(fake, tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch, fake)
    await p.start()
    try:
        await _until(lambda: p.connected and len(p.devices()) == 6)
        fridge, lock = _dev(p, "fridge"), _dev(p, "back-door-lock")
        r = await fridge._service("button.press", entity="reset water filter")
        assert fake.calls[-1] == ("button", "press",
                                  ["button.office_family_hub_reset_water_filter"], {})
        assert r["ok"]
        await lock._service("lock.unlock", data={"code": "1234"})
        assert fake.calls[-1] == ("lock", "unlock", ["lock.back_door"], {"code": "1234"})
        with pytest.raises(TypeError, match="must not name a target"):
            await lock._service("lock.unlock", data={"entity_id": "lock.front"})
        with pytest.raises(TypeError, match="not a light entity"):
            await fridge._service("light.turn_on", entity="power cool")
        with pytest.raises(CapError, match="no vacuum entity"):
            await fridge._service("vacuum.start")
        with pytest.raises(TypeError):
            await fridge._service("not a service")
        fake.fail_services.add("button.press")
        with pytest.raises(CapError, match="button.press failed: Service button.press not found"):
            await fridge._service("button.press")
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_listing_devices_and_scenes(fake, tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch, fake, devices="Jr's room speaker, bed lights")
    await p.start()
    try:
        await _until(lambda: p.connected)
        # An HA name shared by two devices is ambiguous: it needs the id.
        assert [d.name for d in p.devices()] == ["bed-lights"]
        listed = await p._devices_cap()
        assert listed["connected"] and listed["version"] == "2026.7.2"
        ids = [i["id"] for i in listed["items"]]
        assert "dev-assist" not in ids          # HA's own services stay out...
        assert "dev-assist" in [i["id"] for i in (await p._devices_cap(all=True))["items"]]
        bed = next(i for i in listed["items"] if i["id"] == "dev-bed")
        assert bed == {"id": "dev-bed", "ha_name": "bed lights", "area": "Bedroom 1",
                       "model": None, "controls": ["light"], "available": True,
                       "on_band_as": "bed-lights"}
        spk = [i for i in listed["items"] if i["ha_name"] == "Jr's room speaker"]
        assert [i["available"] for i in spk] == [True, False]   # available first
        assert [i["id"] for i in (await p._devices_cap(query="living room"))["items"]] == \
            ["dev-thermo"]
        scenes = await p._scenes()
        assert {"scene": "scene.office_lights_full", "name": "office lights full"} in scenes["items"]
        assert (await p._scene("Office Lights Full"))["scene"] == "scene.office_lights_full"
        assert fake.calls[-1] == ("scene", "turn_on", ["scene.office_lights_full"], {})
        with pytest.raises(CapError, match="no scene matches"):
            await p._scene("disco")
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_registry_changes_follow_and_devices_survive_ha_down(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(ha, "RELOAD_DELAY", 0.05)
    p = _plugin(tmp_path, monkeypatch, fake, devices="bed lights")
    await p.start()
    try:
        await _until(lambda: p.connected and p.devices())
        [bed] = p.devices()
        fake.devices[0]["name_by_user"] = "Bed Lights 2"
        fake.entities.append(_ent("switch.bed_lights_night_mode", "dev-bed", "Night mode"))
        fake.set_state("switch.bed_lights_night_mode", "off")
        await fake.emit("entity_registry_updated", {"action": "create"})
        # Renamed in HA: matched by the old name no more, so it leaves the band.
        await _until(lambda: not p.devices())
    finally:
        await p.stop()
    # Restarted while HA is down: still listed from the last registry, as unavailable.
    p2 = _plugin(tmp_path, monkeypatch, None, devices="bed-lights=dev-bed")
    (tmp_path / "homeassistant" / "devices.json").write_text(json.dumps(
        {"dev-bed": {"ha_name": "bed lights", "entities": [
            {"id": "light.bedroom_1_bed_lights", "name": "bed lights", "category": None}]}}))
    await p2.start()
    try:
        [bed] = p2.devices()
        assert bed.name == "bed-lights" and bed.TYPE == "light"
        assert bed.heartbeat() == {"on": False, "available": False, "text": "unavailable"}
        with pytest.raises(CapError, match="Home Assistant is unreachable"):
            await bed._on()
        st = await bed._status()
        assert st["available"] is False and st["error"]
    finally:
        await p2.stop()


@pytest.mark.asyncio
async def test_a_bad_token_is_a_clear_failure(fake, tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch, fake, token="wrong")
    await p.start()
    try:
        await _until(lambda: p.error)
        assert "refused the access token" in p.error
        with pytest.raises(CapError, match="refused the access token"):
            await p._scenes()
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_ha_devices_become_device_workers_of_the_delegate(fake, tmp_path, monkeypatch):
    """Through a real Worker: announced as devices, called by id."""
    from rook.core.devices import device_worker_id
    from rook.worker import admin, audit, core

    class T:
        band_id = bytes(16)

        def __init__(self):
            self.sent = []

        async def send(self, data, route=0):
            self.sent.append(json.loads(data))
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "worker_id")
    monkeypatch.setattr(admin, "_PLUGIN_STATE", tmp_path / "plugins.json")
    monkeypatch.setattr(admin, "_CUSTOM_STATE", tmp_path / "custom_caps.json")
    monkeypatch.setattr(audit, "_AUDIT_DIR", tmp_path)
    monkeypatch.setattr(audit, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setenv("HASS_URL", fake.url)
    monkeypatch.setenv("HASS_TOKEN", TOKEN)
    monkeypatch.setenv("HASS_DEVICES", "bed-lights=bed lights")
    w = core.Worker(T(), enabled=["homeassistant"], name="janitor")
    w.host._data_root = str(tmp_path / "plugins")
    await w.host.start()
    try:
        plug = w.plugins[[p.NAMESPACE for p in w.plugins].index("homeassistant")]
        await _until(lambda: plug.connected and plug.devices())
        dev_id = device_worker_id(w.worker_id, "homeassistant", "dev-bed")
        await w.announce()
        ann = [m for m in w.transport.sent if m.get("worker_id") == dev_id][-1]
        assert ann["name"] == "bed-lights" and ann["delegate"] == w.worker_id
        assert ann["device"] == {"type": "light", "driver": "homeassistant"}
        assert ann["hb"] == {"ha": {"on": False, "text": "off"}}
        assert ann["tiers"]["ha.service"] == "admin" and "ha.on" in ann["caps"]
        await w._on_message(json.dumps({"id": "c1", "cap": "ha.on", "target": dev_id,
                                        "args": {"brightness": 40}}).encode(), ())
        reply = w.transport.sent[-1]
        assert reply["id"] == "c1" and reply["from"] == dev_id and reply["ok"], reply
        assert fake.calls[-1][3] == {"brightness_pct": 40}
    finally:
        await w.host.stop()
