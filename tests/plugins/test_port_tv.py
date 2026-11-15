"""tv (LG webOS) on the hub, through a delegate that serves net.open, net.udp
and the stream caps, against a fake SSAP websocket server on 127.0.0.1."""

import asyncio
import importlib.util
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from port_helpers import PLUGINS, FakeNet, go, on_worker
from rook.kit.ctx import KitError

KEY = "k-1234-paired"
APPS = [{"id": "youtube.leanback.v4", "title": "YouTube"},
        {"id": "netflix", "title": "Netflix"},
        {"id": "com.disney.disneyplus-prod", "title": "Disney+"},
        {"id": "amazon", "title": "Prime Video"},
        {"id": "amazon.music", "title": "Amazon Music"},
        {"id": "musichub", "title": "Music Hub"}]
INPUTS = [{"id": "HDMI_1", "label": "HDMI 1", "appId": "com.webos.app.hdmi1", "connected": True}]

_spec = importlib.util.spec_from_file_location("tv_script", PLUGINS / "tv" / "scripts" / "tv.py")
MOD = importlib.util.module_from_spec(_spec)      # its constants, for the fake
_spec.loader.exec_module(MOD)


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
        self.image_uri = None

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
        if uri == MOD.CAPTURE:
            return {"returnValue": True, "imageUri": self.image_uri or
                    f"https://127.0.0.1:3001/resources/abc/capture.jpg"}
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


def call(tv, cap, args=None, *, key=KEY, mac=None, data=None, up=True, timeout=15, net=None):
    """Run one cap against ``tv`` as the hub does, for the TV's delegate."""
    from websockets.asyncio.server import serve

    async def body():
        server = None
        if up:
            server = await serve(tv.handler, "127.0.0.1", 0)
            tv.port = server.sockets[0].getsockname()[1]
        else:
            tv.port = _free_port()   # nothing listens there
        inst = {"name": "den-tv", "host": "127.0.0.1", "port": tv.port,
                "settings": {"client_key": key, "mac": mac}}
        try:
            return (await asyncio.to_thread(on_worker, "tv", cap, args, appliance=inst, data=data, net=net))[0]
        finally:
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
    with pytest.raises(KitError, match="no appliance instance"):
        on_worker("tv", "tv.status")


# -- reads ----------------------------------------------------------------------

def test_status(tv, tmp_path):
    got = call(tv, "tv.status", data=tmp_path)
    assert got["on"] is True and got["paired"] and got["reachable"]
    assert got["app"] == "YouTube" and got["volume"] == 10 and got["muted"] is False
    assert got["model"] == "OLED55C1" and got["wake_on_lan"] is True and got["screen"] == "on"
    assert got["macs"] == ["aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:01"]
    # learned for power.on
    assert json.loads((tmp_path / "tv" / "macs.json").read_text())
    tv.app = "com.webos.app.hdmi1"
    assert call(tv, "tv.status")["app"] == "HDMI 1"


def test_lists(tv):
    items = call(tv, "tv.app.list")["items"]
    assert [i["title"] for i in items] == ["Amazon Music", "Disney+", "Music Hub", "Netflix", "Prime Video", "YouTube"]
    assert call(tv, "tv.inputs")["items"][0]["id"] == "HDMI_1"


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
    with pytest.raises(KitError, match="bad args"):
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
    call(tv, "tv.hid.key", {"key": "HOME"})
    assert tv.buttons == ["type:button\nname:ENTER\n\n", "type:button\nname:BACK\n\n",
                          "type:button\nname:HOME\n\n"]
    with pytest.raises(KitError, match="no modifier keys"):
        call(tv, "tv.hid.key", {"key": "a", "mods": ["ctrl"]})
    with pytest.raises(KitError, match="unknown button"):
        call(tv, "tv.hid.key", {"key": "f13"})
    with pytest.raises(KitError, match="bad args"):
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
    with pytest.raises(KitError, match="bad args"):
        call(tv, "tv.app.open_url", {"url": "file:///etc/passwd"})
    call(tv, "tv.screen.off")
    call(tv, "tv.screen.on")
    assert [u for u, _ in tv.seen if "turn" in u] == [MOD.SCREEN_OFF, MOD.SCREEN_ON]


class Capture(BaseHTTPRequestHandler):
    """The TV's own web server, where it leaves the capture."""
    body = b"\xff\xd8\xff\xe0 fake jpeg"

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.fetched.append(self.path)
        if self.path.startswith("/moved"):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.server.body)))
        self.end_headers()
        self.wfile.write(self.server.body)


@pytest.fixture
def web():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    srv.fetched, srv.body = [], Capture.body
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_screen_capture(tv, web):
    port = web.server_address[1]
    tv.image_uri = f"http://127.0.0.1:{port}/resources/abc/capture.jpg"
    got = call(tv, "tv.screen.capture")
    assert __import__("base64").b64decode(got["data"]) == web.body and got["mime"] == "image/jpeg"
    assert sent(tv, MOD.CAPTURE) == [{"path": "/tmp/capture.jpg", "method": "DISPLAY", "format": "JPG"}]
    assert web.fetched == ["/resources/abc/capture.jpg"]
    tv.image_uri = "http://169.254.169.254/latest/meta-data"     # only ever the TV itself
    with pytest.raises(KitError, match="no capture address of its own"):
        call(tv, "tv.screen.capture")
    tv.image_uri = f"http://127.0.0.1:{port}/moved"               # never a redirect
    with pytest.raises(KitError, match="HTTP 302"):
        call(tv, "tv.screen.capture")
    tv.image_uri, web.body = f"http://127.0.0.1:{port}/x.jpg", b"<html>"
    with pytest.raises(KitError, match="not a JPEG"):
        call(tv, "tv.screen.capture")


def test_power_off(tv):
    assert call(tv, "tv.power.off") == {"ok": True}
    assert sent(tv, MOD.POWER_OFF) == [{}]
    tv.seen.clear()
    assert call(tv, "tv.power.off")["already_off"]          # network standby
    assert not sent(tv, MOD.POWER_OFF)
    assert call(tv, "tv.power.off", up=False)["already_off"]


def wol(net):
    return [(data, to) for data, to in net.sent if to and to.endswith(":9")]


def test_power_toggle(tv):
    got = call(tv, "tv.power.toggle")
    assert got["ok"] and got["on"] is False and tv.power == "Suspend"      # it was on: off
    with pytest.raises(KitError):                                          # nothing answers: on needs a MAC
        call(tv, "tv.power.toggle", {"wait": 0}, up=False)


def test_power_on(tv):
    assert call(tv, "tv.power.on")["already_on"]
    with pytest.raises(KitError, match="MAC address unknown"):
        call(tv, "tv.power.on", {"wait": 0}, up=False)
    net = FakeNet()
    got = call(tv, "tv.power.on", {"wait": 0}, up=False, mac="AA:BB:CC:DD:EE:FF", net=net)
    assert got["macs"] == ["aa:bb:cc:dd:ee:ff"]
    packets = wol(net)
    assert {to for _, to in packets} <= {"255.255.255.255:9", "127.0.0.1:9"} and "127.0.0.1:9" in {t for _, t in packets}
    assert packets[0][0] == b"\xff" * 6 + bytes.fromhex("aabbccddeeff") * 16
    tv.power = "Suspend"
    with pytest.raises(KitError, match="did not come on within 1 s"):
        call(tv, "tv.power.on", {"wait": 1}, mac="AA:BB:CC:DD:EE:FF")


def test_power_on_learned_macs(tv, tmp_path):
    call(tv, "tv.status", data=tmp_path)
    got = call(tv, "tv.power.on", {"wait": 0}, up=False, data=tmp_path)
    assert got["macs"] == ["aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:01"]


def test_each_call_closes_its_connection(tv):
    net = FakeNet()
    call(tv, "tv.media.play", net=net)
    assert net.streams and all(st["closed"] for st in net.streams.values())


def test_arp_table():
    table = ("IP address       HW type     Flags       HW address            Mask     Device\n"
             "192.168.1.20     0x1         0x2         a4:b1:c2:d3:e4:f5     *        eth0\n"
             "192.168.1.21     0x1         0x0         00:00:00:00:00:00     *        eth0\n")
    assert MOD.arp_mac("192.168.1.20", table) == "a4:b1:c2:d3:e4:f5"
    assert MOD.arp_mac("192.168.1.21", table) is None
