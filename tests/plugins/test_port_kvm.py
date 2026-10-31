"""kvm (PiKVM) against a local fake kvmd over HTTPS with a self-signed
certificate, like a real PiKVM.

TODO(castling:worker-host): the host can't run appliance caps yet (no
instance values reach Host.call or ctx; spec 5.7), so ``call`` below does
what it will: the loader's bound cap, the host's arg check and defaults, then
the action with the instance's appliance values, or the script with
``ctx.appliance`` set. Switch to host.call once it takes an appliance.
"""

import base64
import copy
import datetime
import ipaddress
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from port_helpers import ME, PLUGINS, go
from rook.core2.worker.loader import load_folder
from rook.kit.actions import ActionError, Secret, appliance_values, run_action
from rook.kit.ctx import Ctx, KitError, invoke

PASSWORD = "s3cr3t-kvm-pw"
JPEG = b"\xff\xd8\xff\xe0thumb\xff\xd9"


def _cert(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pikvm")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    crt, pem = tmp_path / "kvm.crt", tmp_path / "kvm.key"
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    return crt, pem


class FakeKvmd(BaseHTTPRequestHandler):
    """Answers like kvmd: JSON {ok, result}; records every request."""

    def log_message(self, *a):
        pass

    def _answer(self):
        srv = self.server
        url = urlsplit(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        srv.seen.append({"method": self.command, "path": url.path,
                         "query": {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()},
                         "body": body, "auth": self.headers.get("Authorization"),
                         "type": self.headers.get("Content-Type")})
        want = "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != want:
            return self._send(401, {"ok": False, "result": {"error": "UnauthorizedError"}})
        if (self.command, url.path) == ("GET", "/api/streamer/snapshot"):
            if srv.no_video:
                return self._send(200, {"ok": False, "result": {"error": "UnavailableError"}})
            return self._send(200, JPEG, "image/jpeg")
        if (self.command, url.path) == ("GET", "/api/streamer"):
            source = {"online": True, "resolution": {"width": 1920, "height": 1080}}
            return self._send(200, {"ok": True, "result": {"streamer": None if srv.no_video else {"source": source}}})
        if (self.command, url.path) == ("GET", "/api/atx"):
            return self._send(200, {"ok": True, "result": {"enabled": True, "busy": False,
                                                           "leds": {"power": True, "hdd": False}}})
        if url.path == "/api/moved":               # a redirect to another host
            self.send_response(307)
            self.send_header("Location", srv.elsewhere + "/api/atx")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if (self.command, url.path) == ("GET", "/api/log") and "follow=1" in (url.query or ""):
            self.send_response(200)          # a follow stream: lines until the client goes
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                for _ in range(2000):
                    self.wfile.write(b"kvmd log line\n")
                    self.wfile.flush()
                    __import__("time").sleep(0.005)
            except OSError:
                pass
            return None
        if (self.command, url.path) == ("GET", "/api/log"):
            return self._send(200, b"kvmd log line\n", "text/plain")
        if self.command == "POST" and url.path.startswith(("/api/hid/", "/api/atx/")):
            return self._send(200, {"ok": True, "result": {}})
        return self._send(404, {"ok": False, "result": {"error": "HTTPNotFound"}})

    do_GET = do_POST = _answer

    def _send(self, code, body, ctype="application/json"):
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def kvmd(tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeKvmd)
    srv.seen, srv.no_video = [], False
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(*_cert(tmp_path))
    srv.socket = tls.wrap_socket(srv.socket, server_side=True)
    srv.timeout = 5
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    srv.instance = {"name": "rack-kvm", "host": f"127.0.0.1:{srv.server_address[1]}"}
    yield srv
    srv.shutdown()
    srv.server_close()


PLUGIN = load_folder(PLUGINS / "kvm", os_name="linux")


def call(kvmd, cap, args=None, *, password=PASSWORD, **settings):
    c = PLUGIN.caps[cap]
    args = dict(args or {})
    errors = list(c.validator.iter_errors(args))
    if errors:
        raise ValueError(f"bad args: {errors[0].message}")
    for key, spec in c.spec.get("args", {}).items():
        if key not in args and "default" in spec:
            args[key] = copy.deepcopy(spec["default"])
    decls = PLUGIN.manifest["appliance"]["settings"]
    values = {k: d.get("default") for k, d in decls.items()}
    values.update(settings, password=Secret(password))
    appliance = appliance_values(kvmd.instance, values)

    async def body():
        if c.action is not None:
            return await run_action(c.action, args, appliance=appliance, worker={"os": "linux"},
                                    timeout=c.timeout, cap=c.spec)
        ctx = Ctx(PLUGIN.manifest, ME, cap=cap)
        ctx.appliance = appliance
        return await invoke(c.func, ctx, args)
    return go(body)


def last(kvmd, n=1):
    return kvmd.seen[-n:] if n > 1 else kvmd.seen[-1]


def test_screen_capture(kvmd):
    res = call(kvmd, "kvm.screen.capture")
    assert base64.b64decode(res["data"]) == JPEG and res["mime"] == "image/jpeg"
    # Full size by default: its pixels are what hid.click's x and y mean.
    assert last(kvmd)["query"] == {"save": "0", "load": "0", "preview": "false"}
    call(kvmd, "pikvm.snap", {"preview": True, "quality": 70})
    assert last(kvmd)["query"] == {"save": "0", "load": "0", "preview": "true", "quality": "70"}


def test_no_video_is_an_error(kvmd):
    kvmd.no_video = True
    with pytest.raises(ActionError, match="UnavailableError"):
        call(kvmd, "kvm.screen.capture")


def test_type_and_enter(kvmd):
    call(kvmd, "kvm.hid.type", {"text": "echo hi"})
    assert last(kvmd)["body"] == b"echo hi"
    assert last(kvmd)["query"] == {"limit": "0", "slow": "false"}
    call(kvmd, "kvm.hid.type", {"text": "ls ~", "enter": True, "slow": True})
    seen = last(kvmd)
    assert (seen["path"], seen["body"], seen["query"]["slow"]) == ("/api/hid/print", b"ls ~\n", "true")
    assert seen["type"] == "text/plain; charset=utf-8"


def test_key_names_and_mods(kvmd):
    call(kvmd, "kvm.hid.key", {"key": "delete", "mods": ["ctrl", "alt"]})
    assert last(kvmd)["path"] == "/api/hid/events/send_shortcut"
    assert last(kvmd)["query"] == {"keys": "ControlLeft,AltLeft,Delete"}
    call(kvmd, "kvm.hid.key", {"key": "c"})
    assert last(kvmd)["query"] == {"keys": ",KeyC"}   # kvmd drops the empty item
    call(kvmd, "pikvm.key", {"key": "NumpadEnter", "mods": ["meta"]})   # raw kvmd names pass through
    assert last(kvmd)["query"] == {"keys": "MetaLeft,NumpadEnter"}
    call(kvmd, "kvm.hid.key", {"key": "7"})
    assert last(kvmd)["query"] == {"keys": ",Digit7"}


@pytest.mark.parametrize("args", [{"key": "a,Delete"}, {"key": "a b"}, {"key": ""},
                                  {"key": "a", "mods": ["hyper"]}, {"key": "a", "mods": "ctrl"}])
def test_key_cant_add_keys(kvmd, args):
    with pytest.raises(ValueError, match="bad args"):
        call(kvmd, "kvm.hid.key", args)
    assert kvmd.seen == []


def test_move(kvmd):
    call(kvmd, "kvm.hid.move", {"x": -32768, "y": 100})
    assert last(kvmd)["query"] == {"to_x": "-32768", "to_y": "100"}
    with pytest.raises(ValueError):
        call(kvmd, "kvm.hid.move", {"x": 40000, "y": 0})


def test_click_without_and_with_a_pixel(kvmd):
    assert call(kvmd, "kvm.hid.click") == {"button": "left", "x": None, "y": None}
    assert [(s["path"], s["query"]) for s in kvmd.seen] == [
        ("/api/hid/events/send_mouse_button", {"button": "left"})]
    kvmd.seen.clear()
    call(kvmd, "pikvm.mouse.click", {"button": "right", "x": 1919, "y": 0})
    assert [(s["method"], s["path"], s["query"]) for s in kvmd.seen] == [
        ("GET", "/api/streamer", {}),
        ("POST", "/api/hid/events/send_mouse_move", {"to_x": "32767", "to_y": "-32768"}),
        ("POST", "/api/hid/events/send_mouse_button", {"button": "right"})]
    kvmd.seen.clear()
    call(kvmd, "kvm.hid.click", {"x": 0, "y": 1079})
    assert kvmd.seen[1]["query"] == {"to_x": "-32768", "to_y": "32767"}


def test_click_refusals(kvmd):
    with pytest.raises(KitError, match="x and y together"):
        call(kvmd, "kvm.hid.click", {"x": 5})
    with pytest.raises(KitError, match="outside the 1920-pixel screen"):
        call(kvmd, "kvm.hid.click", {"x": 1920, "y": 5})
    kvmd.no_video = True
    with pytest.raises(KitError, match="no video source"):
        call(kvmd, "kvm.hid.click", {"x": 5, "y": 5})
    assert not [s for s in kvmd.seen if s["path"].endswith("send_mouse_button")]


def test_power(kvmd):
    call(kvmd, "kvm.power.on")
    call(kvmd, "kvm.power.off")
    call(kvmd, "kvm.power.off", {"force": True})
    call(kvmd, "kvm.power.reset")
    assert [s["query"]["action"] for s in kvmd.seen] == ["on", "off", "off_hard", "reset_hard"]
    assert {s["path"] for s in kvmd.seen} == {"/api/atx/power"}
    assert call(kvmd, "kvm.power.status") == {"power": True, "hdd": False, "busy": False}


def test_raw_api(kvmd):
    assert call(kvmd, "kvm.api_get", {"path": "/api/atx"})["json"]["result"]["leds"]["power"] is True
    assert call(kvmd, "kvm.api_get", {"path": "/api/log"}) == {"status": 200, "text": "kvmd log line\n"}
    assert call(kvmd, "kvm.api_get", {"path": "/api/nope"})["status"] == 404   # passed back, not raised
    res = call(kvmd, "pikvm.api.post", {"path": "/api/hid/events/send_key",
                                        "query": {"key": "KeyA", "finish": True}})
    assert res == {"status": 200, "json": {"ok": True, "result": {}}}
    assert last(kvmd)["query"] == {"key": "KeyA", "finish": "true"}
    call(kvmd, "kvm.api_post", {"path": "/api/hid/print", "body_b64": base64.b64encode(b"x").decode(),
                                "body_type": "text/plain"})
    assert (last(kvmd)["body"], last(kvmd)["type"]) == (b"x", "text/plain")
    shot = call(kvmd, "kvm.api_get", {"path": "/api/streamer/snapshot"})   # binary: a file
    assert (shot.data, shot.mime) == (JPEG, "image/jpeg")


@pytest.mark.parametrize("cap,args,match", [
    ("kvm.api_get", {"path": "/etc/passwd"}, "bad args"),
    ("kvm.api_get", {"path": "/api/../x"}, "can't contain .."),
    ("kvm.api_get", {"path": "/api/x?y=1"}, "bad args"),
    ("kvm.api_get", {"path": "/api/x", "query": ["a"]}, "object of strings"),
    ("kvm.api_get", {"path": "/api/x", "query": {"a": {"b": 1}}}, "object of strings"),
    ("kvm.api_post", {"path": "/api/x", "body": "a", "body_b64": "YQ=="}, "not both"),
    ("kvm.api_post", {"path": "/api/x", "body_b64": "!!"}, "not base64"),
    ("kvm.api_post", {"path": "/api/x", "body": "a", "body_type": "text/plain\r\nX-Evil: 1"}, "bad args"),
])
def test_raw_api_refusals(kvmd, cap, args, match):
    with pytest.raises((ValueError, KitError), match=match):
        call(kvmd, cap, args)
    assert kvmd.seen == []


def test_wrong_password_never_shows_it(kvmd):
    with pytest.raises(ActionError) as e:
        call(kvmd, "kvm.power.status", password="not-" + PASSWORD)
    assert "401" in str(e.value) and PASSWORD not in str(e.value)
    with pytest.raises(KitError) as e:
        call(kvmd, "kvm.hid.click", {"x": 1, "y": 1}, password="not-" + PASSWORD)
    assert "401" in e.value.message and PASSWORD not in e.value.message


def test_verify_tls_checks_the_certificate(kvmd):
    with pytest.raises(ActionError, match="(?i)certificate"):
        call(kvmd, "kvm.power.status", verify_tls=True)
    with pytest.raises(KitError, match="(?i)certificate"):
        call(kvmd, "kvm.hid.click", verify_tls=True)


def test_no_instance(kvmd):
    kvmd.instance = {"name": "rack-kvm"}
    with pytest.raises(ActionError, match="appliance.host"):
        call(kvmd, "kvm.power.status")
    with pytest.raises(KitError, match="no appliance instance"):
        call(kvmd, "kvm.hid.click")


def test_a_follow_stream_is_cut_off_not_read_forever(kvmd, monkeypatch):
    # Review K2: kvm.api_get with a follow query read forever.
    script = vars(__import__("sys").modules[PLUGIN.caps["kvm.api_get"].func.__module__])
    monkeypatch.setitem(script, "READ_SECS", 1)
    t0 = __import__("time").monotonic()
    with pytest.raises(KitError, match="kept sending"):
        call(kvmd, "kvm.api_get", {"path": "/api/log", "query": {"follow": "1"}})
    assert __import__("time").monotonic() - t0 < 8
    monkeypatch.setitem(script, "READ_MAX", 100)
    with pytest.raises(KitError, match="larger than"):
        call(kvmd, "kvm.api_get", {"path": "/api/log", "query": {"follow": "1"}})


def test_a_redirect_never_carries_the_password_elsewhere(kvmd):
    # Review K9: urllib re-sends the Authorization header on a redirect, to any host.
    seen = []

    class Elsewhere(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
    other = ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere)
    threading.Thread(target=other.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    kvmd.elsewhere = f"http://127.0.0.1:{other.server_address[1]}"
    try:
        with pytest.raises(KitError, match="307"):
            call(kvmd, "kvm.api_get", {"path": "/api/moved"})
        assert seen == []
    finally:
        other.shutdown()
        other.server_close()
