"""The webOS TV delegate plugin against a fake TV control API (SSAP)."""

import asyncio
import json
import os
import stat

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

    def payload(self, uri, body):
        uri = uri.removeprefix("ssap://")
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
            await ws.send(json.dumps({"type": "response", "id": mid,
                                      "payload": self.payload(uri, {})}))

    async def handler(self, ws):
        if ws.request.path == "/pointer":
            async for raw in ws:
                self.buttons.append(raw)
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
    for bad in ("office-tv", "=192.0.2.1", "a=b@zz", "x=1,X=2", "bad/name=1"):
        with pytest.raises(ValueError):
            webos.parse_tvs(bad)


def test_plugin_is_off_without_tvs(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBOS_TVS", raising=False)
    p = webos.WebOSPlugin()
    p.__dict__["_data_root"] = str(tmp_path)
    assert not p.available() and "webos.tvs" in p.unavailable_reason()
    monkeypatch.setenv("WEBOS_TVS", "nonsense")
    assert not p.available()


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
        await _until(lambda: dev.state.get("power") == "Active" and dev.state.get("volume") == 12)
        assert dev.heartbeat() == {"on": True, "app": "YouTube", "volume": 12, "muted": False}
        assert dev.info() == {"type": "tv", "driver": "webos", "model": "OLED55CXAUA"}
        st = await dev._status()
        assert st["on"] and st["reachable"] and st["mac"] == "aa:bb:cc:dd:ee:ff"
        # Subscriptions keep it current: the TV goes to HDMI 1, screen off, muted.
        tv.app, tv.power, tv.volume = "com.webos.app.hdmi1", "Screen Off", {"volume": 5, "muteStatus": True}
        await tv.push()
        await _until(lambda: dev.state.get("app") == "HDMI 1" and dev.state.get("muted"))
        assert dev.heartbeat() == {"on": True, "app": "HDMI 1", "volume": 5, "muted": True,
                                   "screen": "off"}
        assert json.loads((tmp_path / "webos" / "macs.json").read_text()) == \
            {"office-tv": "aa:bb:cc:dd:ee:ff"}
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
        assert (await dev._power_on()) == {"ok": True, "already_on": True}
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
        monkeypatch.setattr(webos, "_send_wol", lambda pkt, host: sent.append((pkt, host)) or [host])
        r = await dev._power_on()
        assert r == {"ok": True, "mac": "aa:bb:cc:dd:ee:ff", "sent_to": ["127.0.0.1"]}
        assert sent[0][0] == webos.magic_packet("aa:bb:cc:dd:ee:ff")
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
