"""tv (LG webOS) against a fake SSAP websocket server on 127.0.0.1.

TODO(castling:worker-host): the host can't run appliance caps yet (no
instance values reach Host.call or ctx; spec 5.7), so ``call`` below does
what it will: the loader's bound cap, the host's arg check, defaults and
``bind``, then the script with ``ctx.appliance`` set. Switch to host.call
once it takes an appliance.
"""

import asyncio
import copy
import json
import socket

import pytest

from port_helpers import ME, PLUGINS, go
from rook.core2.worker.loader import load_folder
from rook.kit.ctx import Ctx, KitError, invoke

KEY = "k-1234-paired"
APPS = [{"id": "youtube.leanback.v4", "title": "YouTube"},
        {"id": "netflix", "title": "Netflix"},
        {"id": "com.disney.disneyplus-prod", "title": "Disney+"},
        {"id": "amazon", "title": "Prime Video"},
        {"id": "amazon.music", "title": "Amazon Music"},
        {"id": "musichub", "title": "Music Hub"}]
INPUTS = [{"id": "HDMI_1", "label": "HDMI 1", "appId": "com.webos.app.hdmi1", "connected": True}]

PLUGIN = load_folder(PLUGINS / "tv", os_name="linux")
MOD = next(iter(PLUGIN.modules.values()))
STOP = PLUGIN.resolve("scripts/tv.py:stop")


class FakeTV:
    """Answers like a webOS TV's SSAP server; records every request."""

    def __init__(self):
        self.seen: list[tuple[str, dict]] = []
        self.buttons: list[str] = []
        self.power = "Active"
        self.app = "youtube.leanback.v4"
        self.volume = 10
        self.muted = False
        self.sound = "tv_speaker"
        self.prompt = "accept"      # accept | deny | ignore
        self.fail: dict[str, str] = {}
        self.port = None

    def answer(self, uri, payload):
        uri = uri.removeprefix("ssap://")
        self.seen.append((uri, payload))
        if uri in self.fail:
            return {"returnValue": False, "errorText": self.fail[uri]}
        if uri == MOD.POWER_STATE:
            return {"returnValue": True, "state": self.power}
        if uri == MOD.FOREGROUND_APP:
            return {"returnValue": True, "appId": self.app}
        if uri == MOD.GET_VOLUME:
            return {"returnValue": True, "volumeStatus": {"volume": self.volume, "muteStatus": self.muted}}
        if uri == MOD.SET_VOLUME:
            self.volume = payload["volume"]
        if uri == MOD.VOLUME_UP:
            self.volume += 1
        if uri == MOD.VOLUME_DOWN:
            self.volume -= 1
        if uri == MOD.SET_MUTE:
            self.muted = payload["mute"]
        if uri == MOD.SOUND_OUTPUT:
            return {"returnValue": True, "soundOutput": self.sound}
        if uri == MOD.LAUNCH_POINTS:
            return {"returnValue": True, "launchPoints": APPS}
        if uri == MOD.INPUTS:
            return {"returnValue": True, "devices": INPUTS}
        if uri == MOD.CONNECTION_INFO:
            return {"returnValue": True, "wiredInfo": {"macAddress": "AA:BB:CC:DD:EE:01"},
                    "wifiInfo": {"macAddress": "aa:bb:cc:dd:ee:02"}}
        if uri == MOD.SYSTEM_INFO:
            return {"returnValue": True, "modelName": "OLED55C1"}
        if uri == MOD.GET_SETTINGS:
            return {"returnValue": True, "settings": {"wolwowlOnOff": "true"}}
        if uri == MOD.POINTER_SOCKET:
            return {"returnValue": True, "socketPath": f"ws://127.0.0.1:{self.port}/pointer"}
        if uri == MOD.POWER_OFF:
            self.power = "Suspend"
        return {"returnValue": True}

    async def handler(self, ws):
        if ws.request.path == "/pointer":
            async for raw in ws:
                self.buttons.append(raw)
            return
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "register":
                presented = msg["payload"].get("client-key")
                if presented == KEY:
                    await ws.send(json.dumps({"type": "registered", "id": msg["id"],
                                              "payload": {"client-key": KEY}}))
                elif presented:
                    await ws.send(json.dumps({"type": "error", "id": msg["id"],
                                              "error": "403 cancelled by user"}))
                else:
                    await ws.send(json.dumps({"type": "response", "id": msg["id"],
                                              "payload": {"pairingType": "PROMPT"}}))
                    if self.prompt == "accept":
                        await ws.send(json.dumps({"type": "registered", "id": msg["id"],
                                                  "payload": {"client-key": "new-key"}}))
                    elif self.prompt == "deny":
                        await ws.send(json.dumps({"type": "error", "id": msg["id"],
                                                  "error": "403 User denied access"}))
                continue
            body = self.answer(msg["uri"], msg.get("payload") or {})
            await ws.send(json.dumps({"type": "response", "id": msg["id"], "payload": body}))


@pytest.fixture
def tv():
    return FakeTV()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(tv, cap, args=None, *, key=KEY, mac=None, data=None, up=True, timeout=15):
    """Run one cap against ``tv`` as the host will: arg check, defaults,
    bind, then the script with the instance on ctx.appliance."""
    from websockets.asyncio.server import serve

    c = PLUGIN.caps[cap]
    args = dict(args or {})
    errors = list(c.validator.iter_errors(args))
    if errors:
        raise ValueError(f"bad args: {errors[0].message}")
    for k, spec in c.spec.get("args", {}).items():
        if k not in args and "default" in spec:
            args[k] = copy.deepcopy(spec["default"])
    args.update(c.bind)

    async def body():
        server = None
        if up:
            server = await serve(tv.handler, "127.0.0.1", 0)
            tv.port = server.sockets[0].getsockname()[1]
        else:
            tv.port = _free_port()   # nothing listens there
        ctx = Ctx(PLUGIN.manifest, ME, cap=cap, data_dir=data)
        ctx.appliance = {"name": "den-tv", "host": "127.0.0.1", "port": tv.port,
                         "client_key": key, "mac": mac}
        try:
            return await invoke(c.func, ctx, args)
        finally:
            await STOP(Ctx(PLUGIN.manifest, ME))
            if server is not None:
                server.close()
                await asyncio.wait_for(server.wait_closed(), 5)
    return go(body, timeout)


def sent(tv, uri):
    return [p for u, p in tv.seen if u == uri]


# -- pairing --------------------------------------------------------------------

def test_pair_with_the_prompt(tv):
    assert call(tv, "tv.pair", {"timeout": 10}, key=None) == \
        {"paired": True, "settings": {"client_key": "new-key"}}


def test_pair_with_a_key_from_elsewhere(tv):
    assert call(tv, "tv.pair", {"key": KEY}, key=None)["settings"] == {"client_key": KEY}


def test_pair_refused(tv):
    tv.prompt = "deny"
    with pytest.raises(KitError, match="pairing refused"):
        call(tv, "tv.pair", {"timeout": 10}, key=None)


def test_pair_nobody_answers(tv):
    tv.prompt = "ignore"
    with pytest.raises(KitError, match="nobody accepted"):
        call(tv, "tv.pair", {"timeout": 10}, key=None, timeout=20)


def test_no_key_or_a_bad_key_says_pair(tv):
    with pytest.raises(KitError, match="not paired: run tv.pair"):
        call(tv, "tv.app.list", key=None)
    with pytest.raises(KitError, match="not paired: run tv.pair"):
        call(tv, "tv.app.list", key="stale-key")
    assert call(tv, "tv.status", key=None)["paired"] is False


def test_unreachable_is_a_clean_error(tv):
    with pytest.raises(KitError, match="off or unreachable.*tv.power.on") as e:
        call(tv, "tv.app.list", up=False)
    assert e.value.retry
    got = call(tv, "tv.status", up=False)
    assert got["on"] is False and got["reachable"] is False


def test_no_instance(tv):
    async def body():
        ctx = Ctx(PLUGIN.manifest, ME, cap="tv.status")
        return await invoke(PLUGIN.caps["tv.status"].func, ctx, {})
    with pytest.raises(KitError, match="no appliance instance"):
        go(body)


# -- reads ----------------------------------------------------------------------

def test_status(tv, tmp_path):
    got = call(tv, "tv.status", data=tmp_path)
    assert got["on"] is True and got["paired"] and got["reachable"]
    assert got["app"] == "YouTube" and got["volume"] == 10 and got["muted"] is False
    assert got["model"] == "OLED55C1" and got["wake_on_lan"] is True and got["screen"] == "on"
    assert got["macs"] == ["aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:01"]
    # learned for power.on
    assert json.loads((tmp_path / "macs.json").read_text())
    tv.app = "com.webos.app.hdmi1"
    assert call(tv, "tv.status")["app"] == "HDMI 1"


def test_lists(tv):
    items = call(tv, "tv.app.list")["items"]
    assert [i["title"] for i in items] == ["Amazon Music", "Disney+", "Music Hub", "Netflix", "Prime Video", "YouTube"]
    assert call(tv, "tv.inputs")["items"][0]["id"] == "HDMI_1"
    assert call(tv, "tv.apps")["items"]   # old name


# -- writes ---------------------------------------------------------------------

def test_launch_by_name(tv):
    assert call(tv, "tv.app.launch", {"app": "disney plus"})["app_id"] == "com.disney.disneyplus-prod"
    call(tv, "tv.app.launch", {"app": "YouTube", "params": {"contentTarget": "https://www.youtube.com/tv?v=x"}})
    assert sent(tv, MOD.LAUNCH)[-1] == {"id": "youtube.leanback.v4",
                                        "params": {"contentTarget": "https://www.youtube.com/tv?v=x"}}


def test_launch_refusals(tv):
    with pytest.raises(KitError, match="matches several apps"):
        call(tv, "tv.app.launch", {"app": "usic"})
    with pytest.raises(KitError, match="is an input, not an app"):
        call(tv, "tv.app.launch", {"app": "hdmi 1"})
    with pytest.raises(KitError, match="no app matches"):
        call(tv, "tv.app.launch", {"app": "kodi"})
    assert not sent(tv, MOD.LAUNCH)


def test_set_input(tv):
    assert call(tv, "tv.set_input", {"input": "hdmi 1"})["input"] == "HDMI_1"
    assert sent(tv, MOD.SWITCH_INPUT) == [{"inputId": "HDMI_1"}]


def test_notification(tv):
    call(tv, "tv.notification.post", {"text": "Dinner", "title": "Rook"})
    assert sent(tv, MOD.TOAST) == [{"message": "Rook: Dinner"}]


def test_sound(tv):
    assert call(tv, "tv.sound.set", {"level": 30})["volume"] == 30
    assert call(tv, "tv.sound.set", {"step": -2})["volume"] == 28
    assert call(tv, "tv.sound.set", {"mute": True})["muted"] is True
    tv.sound = "external_arc"
    assert "external_arc" in call(tv, "tv.sound.set", {"step": 1})["note"]
    with pytest.raises(KitError, match="level or step"):
        call(tv, "tv.sound.set", {"level": 3, "step": 1})
    with pytest.raises(KitError, match="give level, step or mute"):
        call(tv, "tv.sound.set", {})
    with pytest.raises(ValueError, match="bad args"):
        call(tv, "tv.sound.set", {"level": 101})


def test_media(tv):
    assert call(tv, "tv.media.play")["action"] == "play"
    call(tv, "tv.media.seek", {"by": -10})
    call(tv, "tv.media.seek")
    assert [u for u, _ in tv.seen if u.startswith("media.controls")] == \
        ["media.controls/play", "media.controls/rewind", "media.controls/fastForward"]


def test_buttons(tv):
    call(tv, "tv.hid.key", {"key": "enter"})
    call(tv, "tv.hid.key", {"key": "escape"})
    call(tv, "tv.button", {"key": "HOME"})
    assert tv.buttons == ["type:button\nname:ENTER\n\n", "type:button\nname:BACK\n\n",
                          "type:button\nname:HOME\n\n"]
    with pytest.raises(KitError, match="no modifier keys"):
        call(tv, "tv.hid.key", {"key": "a", "mods": ["ctrl"]})
    with pytest.raises(KitError, match="unknown button"):
        call(tv, "tv.hid.key", {"key": "f13"})
    with pytest.raises(ValueError, match="bad args"):
        call(tv, "tv.hid.key", {"key": "HOME\nname:POWER"})


def test_type(tv):
    got = call(tv, "tv.hid.type", {"text": "cats", "enter": True, "app": "youtube"})
    assert got == {"ok": True, "typed": 4, "enter": True, "app": "YouTube"}
    assert sent(tv, MOD.INSERT_TEXT) == [{"text": "cats", "replace": False}]
    assert sent(tv, MOD.ENTER_KEY) == [{}]
    with pytest.raises(KitError, match="YouTube is in front, not 'netflix'; nothing typed"):
        call(tv, "tv.hid.type", {"text": "x", "app": "netflix"})
    tv.fail[MOD.INSERT_TEXT] = "no text input"
    with pytest.raises(KitError, match="A text field must be open"):
        call(tv, "tv.hid.type", {"text": "x"})


def test_open_url_and_screen(tv):
    call(tv, "tv.app.open_url", {"url": "https://example.com"})
    with pytest.raises(ValueError, match="bad args"):
        call(tv, "tv.app.open_url", {"url": "file:///etc/passwd"})
    call(tv, "tv.screen.off")
    call(tv, "tv.screen.on")
    assert [u for u, _ in tv.seen if "turn" in u] == [MOD.SCREEN_OFF, MOD.SCREEN_ON]


def test_power_off(tv):
    assert call(tv, "tv.power.off") == {"ok": True}
    assert sent(tv, MOD.POWER_OFF) == [{}]
    tv.seen.clear()
    assert call(tv, "tv.power.off")["already_off"]          # network standby
    assert not sent(tv, MOD.POWER_OFF)
    assert call(tv, "tv.power.off", up=False)["already_off"]


def test_power_on(tv, monkeypatch):
    packets = []
    monkeypatch.setattr(MOD, "_send_wol", lambda macs, host: packets.append(macs) or ["255.255.255.255"])
    monkeypatch.setattr(MOD, "arp_mac", lambda host: None)
    assert call(tv, "tv.power.on")["already_on"]
    with pytest.raises(KitError, match="MAC address unknown"):
        call(tv, "tv.power.on", {"wait": 0}, up=False)
    got = call(tv, "tv.power.on", {"wait": 0}, up=False, mac="AA:BB:CC:DD:EE:FF")
    assert got["ok"] and got["macs"] == ["aa:bb:cc:dd:ee:ff"] and packets
    tv.power = "Suspend"
    with pytest.raises(KitError, match="did not come on within 1 s"):
        call(tv, "tv.power.on", {"wait": 1}, mac="AA:BB:CC:DD:EE:FF")


def test_power_on_learned_macs(tv, monkeypatch, tmp_path):
    monkeypatch.setattr(MOD, "_send_wol", lambda macs, host: ["255.255.255.255"])
    monkeypatch.setattr(MOD, "arp_mac", lambda host: None)
    call(tv, "tv.status", data=tmp_path)
    got = call(tv, "tv.power.on", {"wait": 0}, up=False, data=tmp_path)
    assert got["macs"] == ["aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:01"]


def test_connection_is_reused(tv):
    async def body():
        from websockets.asyncio.server import serve
        server = await serve(tv.handler, "127.0.0.1", 0)
        tv.port = server.sockets[0].getsockname()[1]
        ctx = Ctx(PLUGIN.manifest, ME, cap="tv.media.play")
        ctx.appliance = {"name": "den-tv", "host": "127.0.0.1", "port": tv.port, "client_key": KEY}
        try:
            await invoke(PLUGIN.caps["tv.media.play"].func, ctx, {"action": "play"})
            first = MOD._clients[f"127.0.0.1:{tv.port}"]
            await invoke(PLUGIN.caps["tv.media.play"].func, ctx, {"action": "pause"})
            assert MOD._clients[f"127.0.0.1:{tv.port}"] is first
        finally:
            await STOP(Ctx(PLUGIN.manifest, ME))
            server.close()
            await server.wait_closed()
        assert not MOD._clients
    go(body)
