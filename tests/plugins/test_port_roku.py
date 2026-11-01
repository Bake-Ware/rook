"""roku (ECP) against a fake Roku on 127.0.0.1: XML queries, keypresses,
launches; the instance's port setting points the caps at it."""

import copy
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from port_helpers import ME, PLUGINS, go
from rook.core2.worker.loader import load_folder
from rook.kit.actions import appliance_values, run_action
from rook.kit.ctx import Ctx, KitError, invoke

PLUGIN = load_folder(PLUGINS / "roku", os_name="linux")

APPS = ('<apps><app id="12" type="appl" version="5.1">Netflix</app>'
        '<app id="837" type="appl" version="2.0">YouTube</app>'
        '<app id="13" type="appl" version="14.0">Prime Video</app>'
        '<app id="tvinput.hdmi2" type="tvin" version="1.0">Xbox</app></apps>')


class FakeRoku(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _answer(self):
        srv = self.server
        url = urlsplit(self.path)
        path = unquote(url.path)
        srv.seen.append((self.command, path, {k: v[0] for k, v in parse_qs(url.query).items()}))
        if srv.limited:
            return self._send(403, "ECP command not allowed in limited mode")
        if self.command == "GET" and path == "/query/device-info":
            return self._send(200, f"<device-info><user-device-name>Den Roku</user-device-name>"
                                   f"<friendly-model-name>TCL Roku TV</friendly-model-name>"
                                   f"<is-tv>true</is-tv><power-mode>{srv.power}</power-mode></device-info>")
        if self.command == "GET" and path == "/query/apps":
            return self._send(200, APPS)
        if self.command == "GET" and path == "/query/active-app":
            return self._send(200, '<active-app><app id="837" type="appl">YouTube</app></active-app>')
        if self.command == "GET" and path == "/query/media-player":
            return self._send(200, f'<player error="false" state="{srv.player}"/>')
        if self.command == "POST" and path.startswith("/keypress/"):
            key = path.removeprefix("/keypress/")
            srv.keys.append(key)
            if key == "PowerOn":
                srv.power = "PowerOn"
            if key == "PowerOff":
                srv.power = "Ready"
            if key == "Play":
                srv.player = {"play": "pause", "pause": "play"}.get(srv.player, srv.player)
            return self._send(200, "")
        if self.command == "POST" and path.startswith("/launch/"):
            return self._send(200, "")
        return self._send(404, "")

    do_GET = do_POST = _answer

    def _send(self, code, text):
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def roku():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeRoku)
    srv.seen, srv.keys, srv.power, srv.player, srv.limited = [], [], "PowerOn", "play", False
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(roku, cap, args=None, *, up=True):
    c = PLUGIN.caps[cap]
    args = dict(args or {})
    errors = list(c.validator.iter_errors(args))
    if errors:
        raise ValueError(f"bad args: {errors[0].message}")
    for k, spec in c.spec.get("args", {}).items():
        if k not in args and "default" in spec:
            args[k] = copy.deepcopy(spec["default"])
    args.update(c.bind)
    port = roku.server_address[1] if up else 9   # discard: nothing answers
    appliance = appliance_values({"name": "den-roku", "host": "127.0.0.1", "port": 8060}, {"port": port})

    async def body():
        if c.action is not None:
            return await run_action(c.action, args, appliance=appliance, worker={"os": "linux"},
                                    timeout=c.timeout, cap=c.spec)
        ctx = Ctx(PLUGIN.manifest, ME, cap=cap)
        ctx.appliance = appliance
        return await invoke(c.func, ctx, args)
    return go(body)


def test_status(roku):
    assert call(roku, "roku.status") == {
        "name": "den-roku", "host": "127.0.0.1", "reachable": True, "power": "PowerOn", "on": True,
        "is_tv": True, "app": "YouTube", "app_id": "837", "player": "play",
        "device_name": "Den Roku", "model": "TCL Roku TV"}


def test_status_unreachable_and_limited(roku):
    got = call(roku, "roku.status", up=False)
    assert got["reachable"] is False and got["on"] is False
    roku.limited = True
    got = call(roku, "roku.status")
    assert got["reachable"] is False and "Control by mobile apps" in got["error"]


def test_apps_and_launch(roku):
    assert [a["title"] for a in call(roku, "roku.app.list")["items"]] == ["Netflix", "Prime Video", "Xbox", "YouTube"]
    got = call(roku, "roku.app.launch", {"app": "youtube", "params": {"contentId": "abc", "mediaType": "movie"}})
    assert got == {"ok": True, "app_id": "837", "app": "YouTube"}
    assert roku.seen[-1] == ("POST", "/launch/837", {"contentId": "abc", "mediaType": "movie"})
    with pytest.raises(KitError, match="no channel matches"):
        call(roku, "roku.app.launch", {"app": "hulu"})


def test_keys_and_inputs(roku):
    call(roku, "roku.hid.key", {"key": "ok"})
    call(roku, "roku.hid.key", {"key": "escape"})
    call(roku, "roku.hid.key", {"key": "FindRemote"})
    call(roku, "roku.set_input", {"input": "hdmi2"})
    assert roku.keys == ["Select", "Back", "FindRemote", "InputHDMI2"]
    for bad in ({"key": "Lit_a"}, {"key": "../launch/12"}, {"key": "home", "mods": ["ctrl"]}):
        with pytest.raises(ValueError, match="bad args"):
            call(roku, "roku.hid.key", bad)


def test_type(roku):
    assert call(roku, "roku.hid.type", {"text": "a b&", "enter": True}) == {"ok": True, "typed": 4, "enter": True}
    assert roku.keys == ["Lit_a", "Lit_ ", "Lit_b", "Lit_&", "Enter"]


def test_sound(roku):
    with pytest.raises(KitError, match="no volume level"):
        call(roku, "roku.sound.set", {"level": 20})
    got = call(roku, "roku.sound.set", {"step": -2, "mute": True})
    assert got["stepped"] == -2 and "toggles" in got["note"]
    assert roku.keys == ["VolumeDown", "VolumeDown", "VolumeMute"]


def test_media(roku):
    assert call(roku, "roku.media.play")["unchanged"]
    assert call(roku, "roku.media.pause") == {"ok": True, "state": "pause"}
    assert call(roku, "roku.media.pause")["unchanged"]
    assert call(roku, "roku.media.play") == {"ok": True, "state": "play"}
    roku.player = "none"
    with pytest.raises(KitError, match="nothing to play"):
        call(roku, "roku.media.play")
    call(roku, "roku.media.seek", {"by": -10})
    call(roku, "roku.media.seek")
    assert roku.keys == ["Play", "Play", "Rev", "Fwd"]


def test_power(roku):
    assert call(roku, "roku.power.on")["already_on"]
    assert call(roku, "roku.power.off") == {"ok": True}
    assert call(roku, "roku.power.off")["already_off"]
    got = call(roku, "roku.power.on", {"wait": 5})
    assert got["on"] and roku.keys == ["PowerOff", "PowerOn"]
    assert call(roku, "roku.power.off", up=False)["already_off"]
    with pytest.raises(KitError, match="Fast TV start"):
        call(roku, "roku.power.on", up=False)


def test_no_instance():
    ctx = Ctx(PLUGIN.manifest, ME, cap="roku.status")
    with pytest.raises(KitError, match="no appliance instance"):
        go(lambda: invoke(PLUGIN.caps["roku.status"].func, ctx, {}))
