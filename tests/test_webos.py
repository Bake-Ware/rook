"""The webOS TV delegate plugin against a fake TV control API (SSAP)."""

import asyncio
import json
import os
import stat
import time

import pytest
import pytest_asyncio
import websockets

from rook.core.pluginkit import CapError
from rook.worker.plugins import _webos_ssap as ssap
from rook.worker.plugins import webos


class FakeTV:
    """Enough of a webOS TV's SSAP server for the driver: registration with
    prompt or key, requests, subscriptions and the pointer-input socket."""

    def __init__(self):
        self.keys = {"k1"}
        self.accept_prompt = True
        self.calls = []
        self.buttons = []
        self.subs = []          # (ws, id, uri)
        self.port = 0
        self.app = "youtube.leanback.v4"
        self.power = "Active"
        self.volume = {"volume": 12, "muteStatus": False}
        self.deny_apps = False
        self.drop_at_once = False
        self.drop_on = None       # a uri the TV drops the connection on, unanswered
        self.registers = 0
        self.wol = "true"
        self.sound_output = "tv_speaker"
        self.ime_open = True
        self.stall = set()        # uris the TV never answers

    def payload(self, uri, body):
        uri = uri.removeprefix("ssap://")
        if uri == ssap.LAUNCH_POINTS and self.deny_apps:
            return {"returnValue": False, "errorText": "Denied method call"}
        if uri == ssap.LAUNCH_POINTS:
            return {"launchPoints": [{"id": "youtube.leanback.v4", "title": "YouTube"},
                                     {"id": "netflix", "title": "Netflix"},
                                     {"id": "com.webos.app.livetv", "title": "Live TV"}]}
        if uri == ssap.INPUTS:
            return {"devices": [{"id": "HDMI_1", "label": "HDMI 1", "appId": "com.webos.app.hdmi1",
                                 "connected": True},
                                {"id": "HDMI_2", "label": "HDMI 2", "appId": "com.webos.app.hdmi2",
                                 "connected": False}]}
        if uri == ssap.CONNECTION_INFO:
            return {"wifiInfo": {"macAddress": "aa:bb:cc:dd:ee:ff"}}
        if uri == ssap.GET_SETTINGS:
            return {"settings": {"wolwowlOnOff": self.wol}}
        if uri == ssap.SOUND_OUTPUT:
            return {"soundOutput": self.sound_output}
        if uri in (ssap.INSERT_TEXT, ssap.ENTER_KEY) and not self.ime_open:
            return {"returnValue": False, "errorText": "no input field"}
        if uri == ssap.SYSTEM_INFO:
            return {"modelName": "OLED55CXAUA"}
        if uri == ssap.FOREGROUND_APP:
            return {"appId": self.app}
        if uri == ssap.POWER_STATE:
            return {"state": self.power}
        if uri == "audio/getVolume":
            return {"volumeStatus": dict(self.volume)}
        if uri == ssap.SWITCH_INPUT:
            self.app = "com.webos.app." + body["inputId"].lower().replace("_", "")
            return {}
        if uri == ssap.LAUNCH:
            self.app = body["id"]
            return {}
        if uri == ssap.POINTER_SOCKET:
            return {"socketPath": f"ws://127.0.0.1:{self.port}/pointer"}
        if uri == "denied/thing":
            return {"returnValue": False, "errorText": "Denied method call"}
        return {}

    async def push(self):
        for ws, mid, uri in list(self.subs):
            try:
                await ws.send(json.dumps({"type": "response", "id": mid,
                                          "payload": self.payload(uri, {})}))
            except websockets.ConnectionClosed:
                self.subs.remove((ws, mid, uri))

    async def handler(self, ws):
        if ws.request.path == "/pointer":
            async for raw in ws:
                self.buttons.append(raw)
            return
        if self.drop_at_once:
            self.registers += 1
            raw = json.loads(await ws.recv())
            await ws.send(json.dumps({"type": "registered", "id": raw.get("id"),
                                      "payload": {"client-key": "k1"}}))
            await ws.close()
            return
        async for raw in ws:
            msg = json.loads(raw)
            kind, mid = msg.get("type"), msg.get("id")
            if kind == "register":
                key = (msg.get("payload") or {}).get("client-key")
                if key:
                    if key in self.keys:
                        await ws.send(json.dumps({"type": "registered", "id": mid,
                                                  "payload": {"client-key": key}}))
                    else:
                        await ws.send(json.dumps({"type": "error", "id": mid,
                                                  "error": "403 access denied"}))
                        await ws.close()
                    continue
                await ws.send(json.dumps({"type": "response", "id": mid,
                                          "payload": {"pairingType": "PROMPT"}}))
                await asyncio.sleep(0.05)
                if self.accept_prompt:
                    self.keys.add("k-new")
                    await ws.send(json.dumps({"type": "registered", "id": mid,
                                              "payload": {"client-key": "k-new"}}))
                else:
                    await ws.send(json.dumps({"type": "error", "id": mid,
                                              "error": "403 cancelled"}))
                continue
            uri, body = msg.get("uri", ""), msg.get("payload") or {}
            self.calls.append((uri.removeprefix("ssap://"), body))
            if uri.removeprefix("ssap://") in self.stall and kind == "request":
                continue
            if self.drop_on and uri.removeprefix("ssap://") == self.drop_on:
                self.drop_on = None
                await ws.close()
                return
            if kind == "subscribe":
                self.subs.append((ws, mid, uri))
            await ws.send(json.dumps({"type": "response", "id": mid,
                                      "payload": {"returnValue": True, **self.payload(uri, body)}}))


@pytest_asyncio.fixture
async def tv(monkeypatch):
    fake = FakeTV()
    async with websockets.serve(fake.handler, "127.0.0.1", 0) as server:
        fake.port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(ssap, "PORT", fake.port)
        monkeypatch.setattr(ssap, "TLS_PORT", 1)   # nothing listens there
        monkeypatch.setattr(webos, "arp_mac", lambda host: None)
        yield fake


def _plugin(tmp_path, monkeypatch, spec="office-tv=127.0.0.1", key="k1"):
    monkeypatch.setenv("WEBOS_TVS", spec)
    p = webos.WebOSPlugin()
    p.__dict__["_data_root"] = str(tmp_path)
    if key:
        (tmp_path / "webos").mkdir(exist_ok=True)
        (tmp_path / "webos" / "keys.json").write_text(json.dumps({"office-tv": key}))
    return p


async def _until(cond, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


# -- config ------------------------------------------------------------------

def test_tv_list_parsing():
    assert webos.parse_tvs("office-tv=192.0.2.10, den=tv.example.com@AA-BB-CC-DD-EE-FF\n") == [
        ("office-tv", "192.0.2.10", None), ("den", "tv.example.com", "aa:bb:cc:dd:ee:ff")]
    assert webos.parse_tvs("") == []
    for bad in ("office-tv", "=192.0.2.1", "a=b@zz", "x=1,X=2", "bad/name=1",
                "office-tv=@aa:bb:cc:dd:ee:ff"):
        with pytest.raises(ValueError):
            webos.parse_tvs(bad)


def test_plugin_is_off_without_tvs(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBOS_TVS", raising=False)
    p = webos.WebOSPlugin()
    p.__dict__["_data_root"] = str(tmp_path)
    assert not p.available() and "webos.tvs" in p.unavailable_reason()
    monkeypatch.setenv("WEBOS_TVS", "nonsense")
    assert not p.available()


def test_plugin_is_off_without_websockets(tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    monkeypatch.setattr(ssap, "supported", lambda: False)
    assert not p.available() and "websockets" in p.unavailable_reason()


def test_magic_packet():
    pkt = webos.magic_packet("aa:bb:cc:dd:ee:ff")
    assert pkt[:6] == b"\xff" * 6 and pkt[6:] == bytes.fromhex("aabbccddeeff") * 16


# -- live state --------------------------------------------------------------

@pytest.mark.asyncio
async def test_tv_state_heartbeat_and_status(tv, tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    await p.start()
    try:
        [dev] = p.devices()
        assert dev.name == "office-tv" and dev.NAMESPACE == "tv"
        await _until(lambda: dev.state.get("power") == "Active" and dev.state.get("volume") == 12
                     and dev.state.get("app") == "YouTube")
        assert dev.heartbeat() == {"on": True, "app": "YouTube", "volume": 12, "muted": False}
        assert dev.info() == {"type": "tv", "driver": "webos", "model": "OLED55CXAUA"}
        st = await dev._status()
        assert st["on"] and st["reachable"] and st["macs"] == ["aa:bb:cc:dd:ee:ff"]
        assert st["wake_on_lan"] is True and st["sound_output"] == "tv_speaker"
        # Subscriptions keep it current: the TV goes to HDMI 1, screen off, muted.
        tv.app, tv.power, tv.volume = "com.webos.app.hdmi1", "Screen Off", {"volume": 5, "muteStatus": True}
        await tv.push()
        await _until(lambda: dev.state.get("app") == "HDMI 1" and dev.state.get("muted"))
        assert dev.heartbeat() == {"on": True, "app": "HDMI 1", "volume": 5, "muted": True,
                                   "screen": "off"}
        assert json.loads((tmp_path / "webos" / "macs.json").read_text()) == \
            {"office-tv": ["aa:bb:cc:dd:ee:ff"]}
        listed = await p._list()
        assert listed["items"][0]["app"] == "HDMI 1" and listed["items"][0]["paired"]
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_tv_caps(tv, tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    await p.start()
    [dev] = p.devices()
    try:
        await _until(lambda: dev.state.get("reachable"))
        assert await dev._toast("hello") == {"ok": True}
        assert (await dev._launch("netflix"))["app_id"] == "netflix"
        r = await dev._launch("YouTube", params={"contentTarget": "https://www.youtube.com/tv?v=x"})
        assert r == {"ok": True, "app_id": "youtube.leanback.v4", "app": "YouTube"}
        with pytest.raises(CapError, match="no app matches"):
            await dev._launch("spotify")
        assert (await dev._input("hdmi 1"))["input"] == "HDMI_1"
        assert (await dev._input("HDMI_2"))["label"] == "HDMI 2"
        with pytest.raises(CapError, match="no input matches"):
            await dev._input("hdmi 9")
        await dev._volume(level=30)
        await dev._volume(step=-2)
        with pytest.raises(TypeError):
            await dev._volume(level=30, step=1)
        with pytest.raises(TypeError):
            await dev._volume(level=101)
        await dev._mute(on=True)
        await dev._media("pause")
        with pytest.raises(TypeError):
            await dev._media("explode")
        await dev._open_url("https://example.com/")
        with pytest.raises(TypeError):
            await dev._open_url("file:///etc/passwd")
        await dev._screen(on=False)
        await dev._power_off()
        await dev._button("home")
        await _until(lambda: tv.buttons)
        assert tv.buttons == ["type:button\nname:HOME\n\n"]
        sent = [u for u, _ in tv.calls]
        for uri in (ssap.TOAST, ssap.LAUNCH, ssap.SWITCH_INPUT, ssap.SET_VOLUME, ssap.VOLUME_DOWN,
                    ssap.SET_MUTE, ssap.MEDIA["pause"], ssap.OPEN_URL, ssap.SCREEN_OFF,
                    ssap.POWER_OFF):
            assert uri in sent, uri
        assert sent.count(ssap.VOLUME_DOWN) == 2
        assert ("system.launcher/launch", {"id": "youtube.leanback.v4",
                "params": {"contentTarget": "https://www.youtube.com/tv?v=x"}}) in tv.calls
        apps = (await dev._apps())["items"]
        assert [a["title"] for a in apps] == ["Live TV", "Netflix", "YouTube"]
        assert [i["label"] for i in (await dev._inputs())["items"]] == ["HDMI 1", "HDMI 2"]
        assert (await dev._power_on()) == {"ok": True, "on": True, "already_on": True}
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_a_refused_request_is_a_clear_failure(tv, tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    await p.start()
    [dev] = p.devices()
    try:
        await _until(lambda: dev.state.get("reachable"))
        with pytest.raises(CapError, match="office-tv: Denied method call"):
            await dev._req("denied/thing")
    finally:
        await p.stop()


# -- pairing -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_unpaired_tv_reports_it_and_pairs_by_prompt(tv, tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch, key=None)
    await p.start()
    [dev] = p.devices()
    try:
        await _until(lambda: dev.state.get("error"))
        assert dev.heartbeat() == {"on": False, "paired": False}
        with pytest.raises(CapError, match="not paired: run tv.pair"):
            await dev._toast("hi")
        tv.accept_prompt = False
        with pytest.raises(CapError, match="pairing refused"):
            await dev._pair(timeout=10)
        tv.accept_prompt = True
        assert await dev._pair(timeout=10) == {"ok": True, "paired": True}
        keys = tmp_path / "webos" / "keys.json"
        assert json.loads(keys.read_text()) == {"office-tv": "k-new"}
        assert stat.S_IMODE(os.stat(keys).st_mode) == 0o600
        assert await dev._toast("hi") == {"ok": True}
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_pair_with_a_known_key_and_a_rejected_one(tv, tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch, key="stale")
    await p.start()
    [dev] = p.devices()
    try:
        await _until(lambda: dev.state.get("paired") is False)
        assert await dev._pair(key="k1") == {"ok": True, "paired": True}
        assert json.loads((tmp_path / "webos" / "keys.json").read_text()) == {"office-tv": "k1"}
        await _until(lambda: dev.state.get("reachable"))
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_unreachable_tv_is_off_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ssap, "PORT", 1)
    monkeypatch.setattr(ssap, "TLS_PORT", 1)
    p = _plugin(tmp_path, monkeypatch, spec="office-tv=127.0.0.1@aa:bb:cc:dd:ee:ff")
    await p.start()
    [dev] = p.devices()
    try:
        await _until(lambda: dev.state.get("error"))
        assert dev.heartbeat() == {"on": False}
        with pytest.raises(CapError, match="office-tv is off or unreachable"):
            await dev._toast("hi")
        sent = []
        monkeypatch.setattr(webos, "_send_wol", lambda macs, host: sent.append((macs, host)) or [host])
        r = await dev._power_on(wait=0)
        assert r == {"ok": True, "macs": ["aa:bb:cc:dd:ee:ff"], "sent_to": ["127.0.0.1"]}
        assert sent == [(["aa:bb:cc:dd:ee:ff"], "127.0.0.1")]
        # Waiting for a TV that never answers: a clear failure, and it kept knocking.
        monkeypatch.setattr(webos, "WAKE_EVERY", 0.2)
        with pytest.raises(CapError, match="did not come on within 1 s"):
            await dev._power_on(wait=1)
        assert len(sent) >= 3
        assert await dev._power_off() == {"ok": True, "already_off": True}
        # Off since before the delegate learned its Wi-Fi MAC: the ARP table still has it.
        monkeypatch.setattr(webos, "arp_mac", lambda host: "02:00:5e:10:00:02")
        sent.clear()
        await dev._power_on(wait=0)
        assert sent == [(["02:00:5e:10:00:02", "aa:bb:cc:dd:ee:ff"], "127.0.0.1")]
        assert json.loads((tmp_path / "webos" / "macs.json").read_text())["office-tv"][0] == \
            "02:00:5e:10:00:02"
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_tvs_become_device_workers_of_the_delegate(tv, tmp_path, monkeypatch):
    """The plugin through a real Worker: announced as a device, called by id."""
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
    monkeypatch.setenv("WEBOS_TVS", "office-tv=127.0.0.1")
    (tmp_path / "plugins" / "webos").mkdir(parents=True)
    (tmp_path / "plugins" / "webos" / "keys.json").write_text(json.dumps({"office-tv": "k1"}))
    w = core.Worker(T(), enabled=["webos"], name="hub-box")
    w.host._data_root = str(tmp_path / "plugins")
    await w.host.start()
    try:
        tv_id = device_worker_id(w.worker_id, "webos", "office-tv")
        dev = w.plugins[[p.NAMESPACE for p in w.plugins].index("webos")].devices()[0]
        await _until(lambda: dev.state.get("volume") == 12)
        await w.announce()
        ann = [m for m in w.transport.sent if m.get("worker_id") == tv_id][-1]
        assert ann["name"] == "office-tv" and ann["delegate"] == w.worker_id
        assert ann["hb"]["tv"]["on"] is True and "tv.launch" in ann["caps"]
        await w._on_message(json.dumps({"id": "c1", "cap": "tv.toast", "target": tv_id,
                                        "args": {"message": "hi"}}).encode(), ())
        assert w.transport.sent[-1] == {"id": "c1", "from": tv_id, "ok": True,
                                        "result": {"ok": True}}
    finally:
        await w.host.stop()


@pytest.mark.asyncio
async def test_state_stays_live_when_a_catalogue_fails(tv, tmp_path, monkeypatch):
    """Review #5: subscriptions come first, so a refused app list leaves the
    TV on and its app known (by id)."""
    tv.deny_apps = True
    p = _plugin(tmp_path, monkeypatch)
    await p.start()
    [dev] = p.devices()
    try:
        await _until(lambda: dev.state.get("power") == "Active")
        assert dev.heartbeat()["on"] is True and dev.heartbeat()["app"] == "youtube.leanback.v4"
        assert (await dev._power_on()) == {"ok": True, "on": True, "already_on": True}
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_pairing_does_not_hold_up_the_live_connection(tv, tmp_path, monkeypatch):
    """Review #8: a pairing in progress or refused leaves the paired
    connection serving."""
    p = _plugin(tmp_path, monkeypatch)
    await p.start()
    [dev] = p.devices()
    try:
        await _until(lambda: dev.state.get("reachable"))
        tv.accept_prompt = False
        pairing = asyncio.ensure_future(dev._pair(timeout=10))
        await asyncio.sleep(0.01)
        assert await dev._toast("still here") == {"ok": True}   # not blocked by the prompt
        with pytest.raises(CapError, match="pairing refused"):
            await pairing
        assert dev.state["reachable"] and await dev._toast("and after") == {"ok": True}
        assert json.loads((tmp_path / "webos" / "keys.json").read_text()) == {"office-tv": "k1"}
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_a_stopped_tv_does_not_reconnect(tv, tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    await p.start()
    [dev] = p.devices()
    await _until(lambda: dev.state.get("reachable"))
    await p.stop()
    with pytest.raises(CapError, match="off or unreachable"):
        await dev._toast("hi")
    assert dev.client is None


@pytest.mark.asyncio
async def test_a_connection_that_drops_at_once_backs_off(tv, tmp_path, monkeypatch):
    """Review #6: no tight reconnect loop when the TV keeps dropping us."""
    p = _plugin(tmp_path, monkeypatch)
    tv.drop_at_once = True
    monkeypatch.setattr(webos, "RETRY_MIN", 0.2)
    await p.start()
    try:
        await asyncio.sleep(1.0)
        assert 1 <= tv.registers <= 4, tv.registers
    finally:
        await p.stop()


# -- round two: wake, waiting, typing, sleep, sturdier requests ---------------

def test_arp_mac_reads_the_kernel_table(tmp_path):
    t = tmp_path / "arp"
    t.write_text("IP address       HW type     Flags       HW address            Mask     Device\n"
                 "192.0.2.98       0x1         0x2         02:00:5E:10:00:02     *        eth0\n"
                 "192.0.2.99       0x1         0x0         00:00:00:00:00:00     *        eth0\n")
    assert webos.arp_mac("192.0.2.98", str(t)) == "02:00:5e:10:00:02"
    assert webos.arp_mac("192.0.2.99", str(t)) is None   # incomplete entry
    assert webos.arp_mac("192.0.2.1", str(t)) is None
    assert webos.arp_mac("192.0.2.98", str(tmp_path / "none")) is None


@pytest.mark.asyncio
async def test_wake_goes_to_every_mac_the_live_one_first(tv, tmp_path, monkeypatch):
    """The CX TVs report a wired MAC while on Wi-Fi; the first version woke
    only the wired one, so tv.power_on never reached them."""
    monkeypatch.setattr(webos, "arp_mac", lambda host: "02:00:5e:10:00:02")
    orig = tv.payload

    def payload(uri, body):
        if uri.removeprefix("ssap://") == ssap.CONNECTION_INFO:
            return {"wiredInfo": {"macAddress": "02:00:5E:10:00:01"},
                    "wifiInfo": {"macAddress": "02:00:5E:10:00:02"},
                    "p2pInfo": {"macAddress": "06:00:5E:10:00:02"}}
        return orig(uri, body)
    tv.payload = payload
    p = _plugin(tmp_path, monkeypatch)
    # A macs.json from the first version: one string, the wired MAC.
    (tmp_path / "webos" / "macs.json").write_text(json.dumps({"office-tv": "02:00:5e:10:00:01"}))
    await p.start()
    [dev] = p.devices()
    try:
        assert dev.macs == ["02:00:5e:10:00:01"]
        await _until(lambda: len(dev.macs) == 2)
        assert dev.macs == ["02:00:5e:10:00:02", "02:00:5e:10:00:01"]
        assert json.loads((tmp_path / "webos" / "macs.json").read_text()) == \
            {"office-tv": ["02:00:5e:10:00:02", "02:00:5e:10:00:01"]}
        assert (await p._list())["items"][0]["macs"] == dev.macs
    finally:
        await p.stop()


def test_send_wol_sends_one_packet_per_mac(monkeypatch):
    sent = []

    class S:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setsockopt(self, *a):
            pass

        def sendto(self, pkt, addr):
            sent.append((pkt, addr))
    monkeypatch.setattr(webos.socket, "socket", S)
    assert webos._send_wol(["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"], "192.0.2.5") == \
        ["255.255.255.255", "192.0.2.5"]
    assert len(sent) == 4
    assert sent[0] == (webos.magic_packet("aa:bb:cc:dd:ee:ff"), ("255.255.255.255", 9))
    assert sent[1] == (webos.magic_packet("11:22:33:44:55:66"), ("255.255.255.255", 9))


@pytest.mark.asyncio
async def test_power_on_waits_until_the_tv_is_on(tv, tmp_path, monkeypatch):
    tv.power = "Active Standby"   # answering, but off
    p = _plugin(tmp_path, monkeypatch, spec="office-tv=127.0.0.1@aa:bb:cc:dd:ee:ff")
    monkeypatch.setattr(webos, "WAKE_EVERY", 0.1)
    await p.start()
    [dev] = p.devices()
    try:
        await _until(lambda: dev.state.get("power") == "Active Standby")
        assert dev.heartbeat()["on"] is False
        knocks = []

        def wol(macs, host):
            knocks.append(macs)
            if len(knocks) == 2:   # it wakes on the second packet
                tv.power = "Active"
                asyncio.run_coroutine_threadsafe(tv.push(), loop)
            return ["255.255.255.255"]
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(webos, "_send_wol", wol)
        r = await dev._power_on(wait=5)
        assert r["ok"] and r["on"] and r["macs"] == ["aa:bb:cc:dd:ee:ff"] and len(knocks) >= 2
        # Stays off: the error says why, from the TV's own setting.
        tv.power, tv.wol = "Active Standby", "false"
        await tv.push()
        await _until(lambda: dev.state.get("on") is False)
        dev.state["wake_on_lan"] = False
        monkeypatch.setattr(webos, "_send_wol", lambda macs, host: [host])
        with pytest.raises(CapError, match="Turn on via Wi-Fi setting is off"):
            await dev._power_on(wait=1)
        with pytest.raises(TypeError):
            await dev._power_on(wait=600)
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_app_names_as_people_say_them(tv, tmp_path, monkeypatch):
    orig = tv.payload
    extra = []

    def payload(uri, body):
        if uri.removeprefix("ssap://") == ssap.LAUNCH_POINTS:
            return {"launchPoints": [{"id": "youtube.leanback.v4", "title": "YouTube"},
                                     {"id": "youtube.leanback.kids.v4", "title": "YouTube Kids"},
                                     {"id": "com.disney.disneyplus-prod", "title": "Disney+"},
                                     {"id": "amazon", "title": "Prime Video"}] + extra}
        return orig(uri, body)
    tv.payload = payload
    p = _plugin(tmp_path, monkeypatch)
    await p.start()
    [dev] = p.devices()
    try:
        await _until(lambda: len(dev.apps) == 4)
        assert (await dev._launch("you tube"))["app_id"] == "youtube.leanback.v4"
        assert (await dev._launch("youtube kids"))["app_id"] == "youtube.leanback.kids.v4"
        assert (await dev._launch("Disney Plus"))["app_id"] == "com.disney.disneyplus-prod"
        assert (await dev._launch("amazon"))["app_id"] == "amazon"
        assert (await dev._launch("prime"))["app_id"] == "amazon"
        with pytest.raises(CapError, match="is an input, not an app: use tv.input"):
            await dev._launch("HDMI 2")
        # Installed after we connected: a miss re-reads the app list once.
        extra.append({"id": "spotify-beehive", "title": "Spotify - Music and Podcasts"})
        assert (await dev._launch("spotify"))["app_id"] == "spotify-beehive"
        with pytest.raises(CapError, match="no app matches"):
            await dev._launch("tidal")
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_type_text_into_the_open_field(tv, tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    await p.start()
    [dev] = p.devices()
    try:
        await _until(lambda: dev.state.get("reachable"))
        await _until(lambda: dev.state.get("app") == "YouTube")
        assert await dev._type("bluey", enter=True) == {"ok": True, "typed": 5, "enter": True,
                                                        "app": "YouTube"}
        assert (ssap.INSERT_TEXT, {"text": "bluey", "replace": False}) in tv.calls
        assert (ssap.ENTER_KEY, {}) in tv.calls
        await dev._type("x", replace=True)
        assert (ssap.INSERT_TEXT, {"text": "x", "replace": True}) in tv.calls
        with pytest.raises(TypeError):
            await dev._type("")
        # app= guards against typing into whatever happens to be in front.
        assert (await dev._type("x", app="youtube"))["app"] == "YouTube"
        n = len(tv.calls)
        with pytest.raises(CapError, match="YouTube is in front, not 'netflix'; nothing typed"):
            await dev._type("secret", app="netflix")
        assert len(tv.calls) == n
        tv.ime_open = False
        with pytest.raises(CapError, match="A text field must be open on the TV"):
            await dev._type("bluey")
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_a_dropped_connection_is_retried_once(tv, tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    await p.start()
    [dev] = p.devices()
    try:
        await _until(lambda: dev.state.get("reachable"))
        tv.drop_on = ssap.TOAST   # standby or a Wi-Fi blip, as the request goes out
        assert await dev._toast("hi") == {"ok": True}
        assert [u for u, _ in tv.calls].count(ssap.TOAST) == 2
        await _until(lambda: dev.state.get("reachable") and dev.client and dev.client.connected)
        assert await dev._toast("again") == {"ok": True}
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_power_off_that_drops_the_connection_is_success(tv, tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    await p.start()
    [dev] = p.devices()
    try:
        await _until(lambda: dev.state.get("reachable"))
        tv.drop_on = ssap.POWER_OFF
        assert await dev._power_off() == {"ok": True}
        assert [u for u, _ in tv.calls].count(ssap.POWER_OFF) == 1   # never sent twice
        # Answering from network standby is off already: nothing is sent.
        await _until(lambda: dev.state.get("reachable"))
        tv.power = "Active Standby"
        await tv.push()
        await _until(lambda: dev.state.get("on") is False)
        assert await dev._power_off() == {"ok": True, "already_off": True}
        assert [u for u, _ in tv.calls].count(ssap.POWER_OFF) == 1
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_volume_notes_an_external_speaker(tv, tmp_path, monkeypatch):
    tv.sound_output = "external_optical"
    p = _plugin(tmp_path, monkeypatch)
    await p.start()
    [dev] = p.devices()
    try:
        await _until(lambda: dev.state.get("sound_output") == "external_optical")
        r = await dev._volume(step=1)
        assert "external_optical" in r["note"]
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_sleep_timer(tv, tmp_path, monkeypatch):
    monkeypatch.setattr(webos, "SLEEP_TICK", 0.05)
    p = _plugin(tmp_path, monkeypatch)
    await p.start()
    [dev] = p.devices()
    try:
        await _until(lambda: dev.state.get("reachable"))
        r = await dev._sleep(30)
        assert r["ok"] and r["in_minutes"] == 30
        st = await dev._status()
        assert st["sleep_in_minutes"] == 30 and st["sleep_at"] == r["sleep_at"]
        assert dev.heartbeat()["sleep_at"] == r["sleep_at"]   # visible on the band
        assert await dev._sleep(0) == {"ok": True, "cancelled": True}
        assert "sleep_at" not in await dev._status()
        with pytest.raises(TypeError):
            await dev._sleep(721)
        # A timer that came due switches the TV off...
        p.set_sleep(dev, int(time.time()) - 1)
        await _until(lambda: ssap.POWER_OFF in [u for u, _ in tv.calls])
        assert p.sleep_at(dev) is None
        # ...but one missed by a long way (the delegate was down) is dropped.
        tv.calls.clear()
        p.set_sleep(dev, int(time.time()) - 3600)
        await _until(lambda: p.sleep_at(dev) is None)
        await asyncio.sleep(0.1)
        assert ssap.POWER_OFF not in [u for u, _ in tv.calls]
        # Switched off with the remote: the timer is cleared, not left to fire later.
        await dev._sleep(30)
        tv.power = "Active Standby"
        await tv.push()
        await _until(lambda: p.sleep_at(dev) is None)
        assert "sleep_at" not in dev.heartbeat()
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_sleep_timer_survives_a_restart(tv, tmp_path, monkeypatch):
    p = _plugin(tmp_path, monkeypatch)
    await p.start()
    [dev] = p.devices()
    at = (await dev._sleep(5))["sleep_at"]
    await p.stop()
    p2 = _plugin(tmp_path, monkeypatch)
    await p2.start()
    try:
        assert p2.sleep_at(p2.devices()[0]) == at
    finally:
        await p2.stop()


@pytest.mark.asyncio
async def test_power_on_asks_the_tv_before_saying_already_on(tv, tmp_path, monkeypatch):
    """A connection that died without closing still looks on: power_on asks."""
    p = _plugin(tmp_path, monkeypatch, spec="office-tv=127.0.0.1@aa:bb:cc:dd:ee:ff")
    await p.start()
    [dev] = p.devices()
    try:
        await _until(lambda: dev.state.get("on") and dev.state.get("reachable"))
        tv.stall.add(ssap.POWER_STATE)
        sent = []
        monkeypatch.setattr(webos, "_send_wol", lambda macs, host: sent.append(macs) or [host])
        r = await dev._power_on(wait=0)
        assert "already_on" not in r and sent == [["aa:bb:cc:dd:ee:ff"]]
    finally:
        await p.stop()
