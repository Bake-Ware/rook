"""devkit: devices defined by asking (rook/worker/plugins/devkit.py). Agent-
written definitions become device workers; recipes run against local fakes."""

import asyncio
import base64
import json
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rook.core.devices import device_worker_id
from rook.core.pluginkit import ArgError, CapError
from rook.worker.plugins import devkit
from rook.worker.plugins.devkit import DefinitionError, normalize

TOKEN = "tok-s3cr3t-value"


# -- a fake lock on HTTP ---------------------------------------------------------

class _Lock(BaseHTTPRequestHandler):
    state = {"locked": True, "battery": 87}
    seen: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", headers=()):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        return self.headers.get("Authorization") in (f"Bearer {TOKEN}",
                                                     "Basic " + base64.b64encode(
                                                         f"admin:{TOKEN}".encode()).decode())

    def do_GET(self):
        type(self).seen.append(("GET", self.path, dict(self.headers)))
        if self.path.startswith("/api/state") and type(self).state is None:
            return self._send(500, {"error": "jammed"})
        if self.path.startswith("/api/state"):
            return self._send(200, {"state": dict(type(self).state), "fw": "1.2"})
        if self.path == "/text":
            return self._send(200, b"temp=21.5C ok", "text/plain")
        if self.path == "/moved":
            return self._send(302, b"", headers=[("Location", "http://example.invalid/")])
        if self.path == "/echo-token":
            return self._send(200, {"you_sent": self.headers.get("Authorization")})
        self._send(404, {"error": "no such thing"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        type(self).seen.append(("POST", self.path, body))
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        if self.path == "/api/lock":
            type(self).state["locked"] = bool(body.get("locked"))
            return self._send(200, {"ok": True, "locked": type(self).state["locked"]})
        if self.path == "/api/level":
            return self._send(200, {"level": body.get("level"), "type": type(body.get("level")).__name__})
        self._send(404, {})


@pytest.fixture
def lock():
    _Lock.state = {"locked": True, "battery": 87}
    _Lock.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Lock)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def lock_def(host, **extra):
    d = {
        "name": "front-door", "type": "lock", "model": "Demo Lock",
        "vars": {"host": host},
        "caps": {
            "state": {"doc": "Locked or not, and the battery.",
                      "http": {"url": "http://{host}/api/state", "extract": {
                          "locked": "state.locked", "battery": "state.battery"}}},
            "lock": {"doc": "Lock the door.",
                     "http": {"method": "POST", "url": "http://{host}/api/lock",
                              "headers": {"Authorization": "Bearer {secret:front-door.token}"},
                              "body": {"locked": True}, "extract": "locked"}},
            "unlock": {"doc": "Unlock the door.", "risk": "exec",
                       "http": {"method": "POST", "url": "http://{host}/api/lock",
                                "headers": {"Authorization": "Bearer {secret:front-door.token}"},
                                "body": {"locked": False}, "extract": "locked"}},
        },
        "state": {"cap": "state", "every": 30},
    }
    d.update(extra)
    return d


# -- definitions ---------------------------------------------------------------

def test_a_definition_is_normalized_with_honest_risk():
    spec = normalize(lock_def("192.0.2.20"))
    assert spec["key"] == "front-door" and spec["type"] == "lock"
    caps = spec["caps"]
    assert caps["state"]["risk"] == "read" and caps["state"]["tags"] == []
    # A POST is at least write and acts on a thing: physical, whatever was declared.
    assert caps["lock"]["risk"] == "write" and caps["lock"]["tags"] == ["physical"]
    assert caps["unlock"]["risk"] == "exec" and "physical" in caps["unlock"]["tags"]
    assert spec["state"] == {"cap": "state", "every": 30}
    assert devkit.secrets_of(spec) == ["front-door.token"]


def test_declared_risk_never_goes_under_the_recipe_floor():
    d = {"name": "x", "type": "plug", "caps": {
        "on": {"risk": "read", "http": {"method": "PUT", "url": "http://h/on"}},
        "blink": {"risk": "read", "udp": {"host": "h", "port": 9, "send": "blink"}},
        "cli": {"risk": "write", "argv": {"argv": ["true"]}},
        "snapshot": {"risk": "read", "tags": ["sensitive"], "http": {"url": "http://h/cam"}},
    }}
    caps = normalize(d)["caps"]
    assert caps["on"]["risk"] == "write" and caps["blink"]["risk"] == "write"
    assert caps["cli"]["risk"] == "exec"
    assert caps["snapshot"] == {"http": caps["snapshot"]["http"], "args": {}, "risk": "read",
                            "tags": ["sensitive"]}


@pytest.mark.parametrize("bad, why", [
    ({"headers": {"Authorization": "Bearer abc123"}}, "credential"),
    ({"query": {"api_key": "abc123"}}, "credential"),
    ({"body": {"user": {"password": "hunter22"}}}, "credential"),
    ({"url": "http://admin:pw@h/x"}, "no user or password in the url"),
    ({"url": "ftp://h/x"}, "http://"),
    ({"url": "http://h/{nope}"}, "neither its args nor"),
    ({"auth": {"type": "basic", "user": "admin", "password": "plain"}}, "credential"),
])
def test_bad_recipes_are_refused_with_the_reason(bad, why):
    http = {"url": "http://h/x", **bad}
    with pytest.raises(DefinitionError) as e:
        normalize({"name": "x", "type": "plug", "caps": {"go": {"http": http}}})
    assert why in str(e.value)


def test_a_body_key_merely_called_key_is_not_a_credential():
    spec = normalize({"name": "remote", "type": "remote", "caps": {
        "press": {"args": {"key": {"type": "str", "choices": ["POWER", "HOME"]}},
                  "http": {"method": "POST", "url": "http://h/key", "body": {"key": "{key}"}}}}})
    assert spec["caps"]["press"]["args"]["key"]["choices"] == ["POWER", "HOME"]


def test_definition_level_problems():
    with pytest.raises(DefinitionError) as e:
        normalize({"name": "bad/name", "type": "info", "caps": {}})
    msg = str(e.value)
    assert "name:" in msg and "type:" in msg and "caps:" in msg
    # The hub fills {{secret:...}} in before the worker sees it: refuse the form outright.
    with pytest.raises(DefinitionError, match="single braces"):
        normalize({"name": "x", "type": "lock", "caps": {"s": {"http": {
            "url": "http://h/", "headers": {"X-Token": "{{secret:t}}"}}}}})
    # State must be a read that needs no args.
    with pytest.raises(DefinitionError, match="state: lock must be a read"):
        normalize(lock_def("h", state={"cap": "lock"}))
    with pytest.raises(DefinitionError, match="no .secret"):
        normalize({"name": "x", "type": "plug", "caps": {"s": {"argv": {
            "argv": ["tool", "--pw", "{secret:t}"]}}}})
    with pytest.raises(DefinitionError, match="same name as a var"):
        normalize({"name": "x", "type": "plug", "vars": {"host": "h"}, "caps": {"s": {
            "args": {"host": "str"}, "http": {"url": "http://{host}/"}}}})
    with pytest.raises(DefinitionError, match="constants only"):
        normalize({"name": "x", "type": "plug", "vars": {"host": "{secret:t}"},
                   "caps": {"s": {"http": {"url": "http://{host}/"}}}})


def test_call_args_are_checked_and_typed():
    spec = {"type": "int", "min": 0, "max": 100}
    assert devkit.coerce_arg("level", spec, "40") == 40
    for bad in (101, "x", True, 1.5):
        with pytest.raises(ArgError):
            devkit.coerce_arg("level", spec, bad)
    assert devkit.coerce_arg("on", {"type": "bool"}, "on") is True
    with pytest.raises(ArgError):
        devkit.coerce_arg("name", {"type": "str"}, "two\nlines")


# -- running recipes -------------------------------------------------------------

@pytest.mark.asyncio
async def test_http_recipes_fill_extract_and_report(lock):
    spec = normalize(lock_def(lock))
    secrets = {"front-door.token": TOKEN}
    res = await devkit.run_cap(spec, "state", {}, {})
    assert res["ok"] and res["status"] == 200 and res["value"] == {"locked": True, "battery": 87}
    res = await devkit.run_cap(spec, "unlock", {}, secrets)
    assert res["ok"] and res["value"] is False and _Lock.state["locked"] is False
    assert _Lock.seen[-1] == ("POST", "/api/lock", {"locked": False})
    bad = await devkit.run_cap(spec, "lock", {}, {"front-door.token": "wrong"})
    assert not bad["ok"] and bad["status"] == 401 and bad["body"] == {"error": "bad token"}


@pytest.mark.asyncio
async def test_typed_args_quoting_text_extract_basic_auth_and_no_redirects(lock):
    spec = normalize({"name": "misc", "type": "thing", "vars": {"host": lock}, "caps": {
        "level": {"args": {"level": {"type": "int", "min": 0, "max": 100}},
                  "http": {"method": "POST", "url": "http://{host}/api/level",
                           "auth": {"type": "basic", "user": "admin",
                                    "password": "{secret:pw}"},
                           "body": {"level": "{level}"}}},
        "temp": {"http": {"url": "http://{host}/text", "extract": "re:temp=([0-9.]+)"}},
        "path": {"args": {"room": "str"}, "http": {"url": "http://{host}/api/state/{room}"}},
        "moved": {"http": {"url": "http://{host}/moved"}},
    }})
    res = await devkit.run_cap(spec, "level", {"level": 40}, {"pw": TOKEN})
    assert res["ok"] and res["body"] == {"level": 40, "type": "int"}
    assert (await devkit.run_cap(spec, "temp", {}, {}))["value"] == "21.5"
    await devkit.run_cap(spec, "path", {"room": "../../x?y"}, {})
    assert _Lock.seen[-1][1] == "/api/state/..%2F..%2Fx%3Fy"
    moved = await devkit.run_cap(spec, "moved", {}, {})
    assert not moved["ok"] and moved["status"] == 302
    with pytest.raises(ArgError, match="missing arg"):
        await devkit.run_cap(spec, "level", {}, {"pw": TOKEN})
    with pytest.raises(ArgError, match="unknown args"):
        await devkit.run_cap(spec, "temp", {"x": 1}, {})


@pytest.mark.asyncio
async def test_unreachable_device_is_an_outcome_not_a_crash():
    spec = normalize({"name": "gone", "type": "plug", "caps": {
        "s": {"http": {"url": "http://127.0.0.1:9/x", "timeout": 2}}}})
    res = await devkit.run_cap(spec, "s", {}, {})
    assert res["ok"] is False and res["reachable"] is False and res["error"]


@pytest.mark.asyncio
async def test_tcp_and_udp_recipes():
    async def echo(reader, writer):
        data = await reader.read(100)
        writer.write(b"ACK " + data)
        await writer.drain()
        writer.close()
    srv = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    class Udp(asyncio.DatagramProtocol):
        def connection_made(self, t):
            self.t = t

        def datagram_received(self, data, addr):
            self.t.sendto(b'{"power": "' + data + b'"}', addr)
    loop = asyncio.get_running_loop()
    ut, _ = await loop.create_datagram_endpoint(Udp, local_addr=("127.0.0.1", 0))
    uport = ut.get_extra_info("sockname")[1]
    try:
        spec = normalize({"name": "amp", "type": "amp", "vars": {"host": "127.0.0.1"}, "caps": {
            "vol": {"args": {"v": {"type": "int", "min": 0, "max": 80}},
                    "tcp": {"host": "{host}", "port": port, "send": "MV{v}\r"}},
            "raw": {"tcp": {"host": "{host}", "port": port, "send_hex": "00ff"}},
            "power": {"args": {"p": {"type": "str", "choices": ["on", "off"]}},
                      "udp": {"host": "{host}", "port": uport, "send": "{p}", "reply": True,
                              "extract": "power"}},
        }})
        assert spec["caps"]["vol"]["risk"] == "write"
        res = await devkit.run_cap(spec, "vol", {"v": 35}, {})
        assert res["ok"] and res["reply"] == "ACK MV35\r"
        raw = await devkit.run_cap(spec, "raw", {}, {})
        assert raw["reply_hex"] == b"ACK \x00\xff".hex()
        res = await devkit.run_cap(spec, "power", {"p": "on"}, {})
        assert res["ok"] and res["value"] == "on"
    finally:
        srv.close()
        ut.close()


@pytest.mark.asyncio
async def test_argv_recipes_run_without_a_shell():
    spec = normalize({"name": "cli", "type": "plug", "caps": {
        "say": {"args": {"word": "str"},
                "argv": {"argv": [sys.executable, "-c",
                                  "import json,sys,os; print(json.dumps({'w': sys.argv[1], "
                                  "'pw': os.environ['PW']}))", "{word}"],
                         "env": {"PW": "{secret:pw}"}, "extract": "w"}}}})
    assert spec["caps"]["say"]["risk"] == "exec"
    res = await devkit.run_cap(spec, "say", {"word": "hi; rm -rf /"}, {"pw": TOKEN})
    assert res["ok"] and res["value"] == "hi; rm -rf /" and res["stdout"]["pw"] == TOKEN
    assert devkit.mask(res, {"pw": TOKEN})["stdout"]["pw"] == "{secret:pw}"
    with pytest.raises(ArgError, match="may not start with '-'"):
        await devkit.run_cap(spec, "say", {"word": "--help"}, {"pw": TOKEN})


# -- the plugin through a real worker --------------------------------------------

class _Transport:
    band_id = bytes(16)

    def __init__(self):
        self.sent = []

    async def send(self, data, route=0):
        self.sent.append(json.loads(data))


@pytest.fixture
def worker(tmp_path, monkeypatch):
    from rook.worker import admin, audit, core
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "worker_id")
    monkeypatch.setattr(admin, "_PLUGIN_STATE", tmp_path / "plugins.json")
    monkeypatch.setattr(admin, "_CUSTOM_STATE", tmp_path / "custom_caps.json")
    monkeypatch.setattr(audit, "_AUDIT_DIR", tmp_path)
    monkeypatch.setattr(audit, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    w = core.Worker(_Transport(), enabled=["devkit"], name="hub-box")
    w.host._data_root = str(tmp_path / "plugins")
    granted = {"front-door.token": TOKEN}
    w.secret_asks = []

    async def request(cap, args=None, target=None, timeout=10.0):
        assert cap == "settings.worker_secret" and args["worker_id"] == w.worker_id
        w.secret_asks.append(list(args["names"]))
        names = args["names"]
        return {"ok": True, "result": {"secrets": {n: granted[n] for n in names if n in granted},
                                       "missing": [n for n in names if n not in granted]}}
    w.request = request
    return w


def _plugin(w):
    return next(p for p in w.plugins if p.NAMESPACE == "devkit")


async def _call(w, dotpath, target=None, **args):
    msg = {"id": "m1", "cap": dotpath, "args": args}
    if target:
        msg["target"] = target
    before = len(w.transport.sent)
    await w._on_message(json.dumps(msg).encode(), ())
    return [m for m in w.transport.sent[before:] if m.get("id") == "m1"][0]


@pytest.mark.asyncio
async def test_define_call_state_and_remove_through_a_worker(worker, lock, tmp_path):
    w = worker
    await w.host.start()
    try:
        reply = await _call(w, "devkit.define", definition=lock_def(lock))
        assert reply["ok"], reply
        res = reply["result"]
        dev_id = device_worker_id(w.worker_id, "devkit", "front-door")
        assert res["worker_id"] == dev_id and res["updated"] is False
        assert res["caps"] == {"lock.state": "read", "lock.lock": "write", "lock.unlock": "exec"}
        assert res["secrets_ready"] is True and res["reachable"] is True
        await asyncio.sleep(0)                       # the re-announce runs
        ann = [m for m in w.transport.sent if m.get("worker_id") == dev_id][-1]
        assert ann["name"] == "front-door" and ann["delegate"] == w.worker_id
        assert ann["device"] == {"type": "lock", "driver": "devkit", "model": "Demo Lock"}
        assert ann["tiers"] == {"lock.state": "read", "lock.lock": "write", "lock.unlock": "exec"}
        assert ann["hb"] == {"lock": {"on": True, "locked": True, "battery": 87}}
        own = [m for m in w.transport.sent if m.get("worker_id") == w.worker_id][-1]
        assert own["devices"] == [dev_id]

        # The device answers in its own name; the secret came from the hub, once.
        reply = await _call(w, "lock.unlock", target=dev_id)
        assert reply == {"id": "m1", "from": dev_id, "ok": True,
                         "result": {"ok": True, "value": False}}
        assert w.secret_asks == [["front-door.token"]]
        described = (await _call(w, "caps.describe", target=dev_id))["result"]
        assert described["lock.unlock"]["doc"] == "Unlock the door."
        assert described["lock.unlock"]["risk"] == "exec"
        assert described["lock.unlock"]["tags"] == ["physical"]

        # A failing device call is a clear failure.
        _Lock.state = None                            # the fake now answers 500
        reply = await _call(w, "lock.state", target=dev_id)
        assert reply["ok"] is False and "front-door" in reply["error"]
        _Lock.state = {"locked": False, "battery": 80}

        # Listed, then removed: off the band at once, kept in retired.json.
        items = (await _call(w, "devkit.list"))["result"]["items"]
        assert [i["name"] for i in items] == ["front-door"] and items[0]["secrets"] == ["front-door.token"]
        reply = await _call(w, "devkit.remove", device="Front-Door")
        assert reply["ok"] and reply["result"]["removed"] == "front-door"
        await asyncio.sleep(0)
        own = [m for m in w.transport.sent if m.get("worker_id") == w.worker_id][-1]
        assert "devices" not in own
        retired = json.loads((tmp_path / "plugins" / "devkit" / "retired.json").read_text())
        assert retired[-1]["why"] == "removed" and retired[-1]["definition"]["name"] == "front-door"
    finally:
        await w.host.stop()


@pytest.mark.asyncio
async def test_definitions_survive_a_restart(worker, lock, tmp_path):
    from rook.worker import core
    await worker.host.start()
    try:
        assert (await _call(worker, "devkit.define", definition=json.dumps(lock_def(lock))))["ok"]
    finally:
        await worker.host.stop()
    stored = json.loads((tmp_path / "plugins" / "devkit" / "devices.json").read_text())
    assert list(stored) == ["front-door"] and TOKEN not in json.dumps(stored)
    w2 = core.Worker(_Transport(), enabled=["devkit"], name="hub-box")
    w2.host._data_root = str(tmp_path / "plugins")
    await w2.host.start()
    try:
        assert [d.name for d in _plugin(w2).devices()] == ["front-door"]
        await w2.announce()
        dev_id = device_worker_id(w2.worker_id, "devkit", "front-door")
        assert dev_id == device_worker_id(worker.worker_id, "devkit", "front-door")
        assert any(m.get("worker_id") == dev_id for m in w2.transport.sent)
    finally:
        await w2.host.stop()


@pytest.mark.asyncio
async def test_test_runs_a_draft_and_secrets_must_be_granted(worker, lock):
    w = worker
    await w.host.start()
    try:
        draft = lock_def(lock)
        reply = await _call(w, "devkit.test", definition=draft, cap="lock.state")
        assert reply["ok"] and reply["result"]["value"] == {"locked": True, "battery": 87}
        assert reply["result"]["cap"] == "lock.state" and reply["result"]["risk"] == "read"
        assert _plugin(w).devices() == []             # a draft is never saved
        # A draft's vars are the caller's: a cap using a secret is tested only once defined.
        draft["caps"]["get_echo"] = {"http": {"url": "http://{host}/echo-token", "headers": {
            "Authorization": "Bearer {secret:front-door.token}"}}}
        reply = await _call(w, "devkit.test", definition=draft, cap="get_echo")
        assert reply["ok"] is False and "define the device first" in reply["error"]
        assert w.secret_asks == []
        # A device that echoes the credential back never shows it.
        assert (await _call(w, "devkit.define", definition=draft))["ok"]
        reply = await _call(w, "devkit.test", device="front-door", cap="get_echo")
        assert TOKEN not in json.dumps(reply)
        assert reply["result"]["body"]["you_sent"] == "Bearer {secret:front-door.token}"
        # Not granted to this worker: defined anyway, with a note how to grant it.
        draft["caps"]["lock"]["http"]["headers"]["Authorization"] = "Bearer {secret:other}"
        res = (await _call(w, "devkit.define", definition=draft))["result"]
        assert res["secrets_ready"] is False and "devkit.secrets" in res["note"]
        reply = await _call(w, "devkit.test", device="front-door", cap="lock")
        assert reply["ok"] is False and "not available" in reply["error"]
        # Bad definitions say every reason.
        reply = await _call(w, "devkit.define", definition={"name": "x", "type": "caps"})
        assert reply["ok"] is False and "type:" in reply["error"] and "caps:" in reply["error"]
    finally:
        await w.host.stop()


@pytest.mark.asyncio
async def test_replacing_a_definition_keeps_the_id_and_retires_the_old(worker, lock, tmp_path):
    w = worker
    await w.host.start()
    try:
        await _call(w, "devkit.define", definition=lock_def(lock))
        renamed = lock_def(lock, name="Front Door", key="front-door")
        res = (await _call(w, "devkit.define", definition=renamed))["result"]
        assert res["updated"] is True and res["name"] == "Front Door"
        assert res["worker_id"] == device_worker_id(w.worker_id, "devkit", "front-door")
        retired = json.loads((tmp_path / "plugins" / "devkit" / "retired.json").read_text())
        assert retired[-1]["why"] == "replaced" and retired[-1]["definition"]["name"] == "front-door"
        clash = await _call(w, "devkit.define", definition=lock_def(lock, name="front door",
                                                                   key="other"))
        assert clash["ok"] is False and "already called" in clash["error"]
    finally:
        await w.host.stop()


def test_device_caps_carry_scope_and_meta():
    spec = normalize(lock_def("192.0.2.20"))
    dev = devkit.RecipeDevice(devkit.DevkitPlugin(), spec)
    caps = dev.caps()
    assert sorted(caps) == ["lock.lock", "lock.state", "lock.unlock"]
    from rook.core.plugin import validate_caps
    assert validate_caps(caps) == []
    assert caps["lock.unlock"]._rook_cap_meta.risk == "exec"
    assert dev.heartbeat() is None                    # not checked yet


# -- discovery -----------------------------------------------------------------------

def _name(n):
    return b"".join(bytes([len(p)]) + p.encode() for p in n.split(".")) + b"\0"


def _rr(name, rtype, rdata):
    return _name(name) + struct.pack("!HHIH", rtype, 1, 120, len(rdata)) + rdata


def test_mdns_records_are_parsed():
    inst = "Front Door._hap._tcp.local"
    pkt = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 3)
    pkt += _rr("_hap._tcp.local", 12, _name(inst))
    pkt += _rr(inst, 33, struct.pack("!HHH", 0, 0, 5001) + _name("lock.local"))
    pkt += _rr(inst, 16, b"\x07md=Demo\x04sf=1")
    pkt += _rr("lock.local", 1, bytes([192, 0, 2, 20]))
    recs = devkit._dns_records(pkt)
    assert (("_hap._tcp.local", 12, inst) in recs and (inst, 33, (5001, "lock.local")) in recs
            and (inst, 16, {"md": "Demo", "sf": "1"}) in recs
            and ("lock.local", 1, "192.0.2.20") in recs)


def test_query_packets_ask_for_ptr_records():
    q = devkit._dns_query(["_services._dns-sd._udp.local"])
    assert struct.unpack("!HHHHHH", q[:12])[2] == 1
    assert q.endswith(b"\x05local\x00" + struct.pack("!HH", 12, 1))
    # From a random port, the question asks for a unicast answer (QU).
    assert devkit._dns_query(["a.local"], unicast=True).endswith(struct.pack("!HH", 12, 0x8001))


def test_errors_are_cap_errors():
    assert issubclass(ArgError, CapError)


# -- review round 1 (A70): more edges ----------------------------------------------

def test_get_caps_named_for_an_action_are_actions():
    caps = normalize({"name": "relay", "type": "plug", "caps": {
        "turn_on": {"risk": "read", "http": {"url": "http://h/relay/0?turn=on"}},
        "lock": {"http": {"url": "http://h/lock"}},
        "set_level": {"http": {"url": "http://h/level"}},
        "lock_state": {"http": {"url": "http://h/lock/state"}},
        "power_usage": {"http": {"method": "HEAD", "url": "http://h/meter"}},
        "ping": {"tcp": {"host": "h", "port": 1, "send": "x"}},
        "cli": {"argv": {"argv": ["true"]}},
    }})["caps"]
    for name in ("turn_on", "lock", "set_level"):
        assert caps[name]["risk"] == "write" and caps[name]["tags"] == ["physical"], name
    assert caps["lock_state"]["risk"] == "read" and caps["power_usage"]["risk"] == "read"
    # Only a GET named as a reading is a read: open_garage, relay, power_cycle are actions.
    more = normalize({"name": "relay", "type": "plug", "caps": {n: {"risk": "read", "http": {
        "url": "http://h/x"}} for n in ("open_garage", "lock_door", "power_cycle", "run_scene",
                                       "relay", "close_valve", "get_status", "is_on",
                                       "door_status")}})["caps"]
    assert {n: c["risk"] for n, c in more.items()} == {
        "open_garage": "write", "lock_door": "write", "power_cycle": "write",
        "run_scene": "write", "relay": "write", "close_valve": "write",
        "get_status": "read", "is_on": "read", "door_status": "read"}
    assert caps["ping"]["tags"] == ["physical"] and caps["cli"]["tags"] == ["physical"]


@pytest.mark.parametrize("cap", [
    {"args": {"ip": "str"}, "http": {"url": "http://{ip}/x"}},
    {"args": {"port": "int"}, "http": {"url": "http://h:{port}/x"}},
    {"args": {"ip": "str"}, "tcp": {"host": "{ip}", "port": 23, "send": "x"}},
    {"args": {"p": "int"}, "udp": {"host": "h", "port": "{p}", "send": "x"}},
    {"http": {"url": "http://{secret:h}/x"}},
])
def test_a_caller_never_picks_the_host(cap):
    with pytest.raises(DefinitionError, match="vars only"):
        normalize({"name": "x", "type": "plug", "caps": {"go": cap}})


@pytest.mark.parametrize("typ", sorted(devkit.RESERVED_TYPES))
def test_reserved_types_are_refused(typ):
    with pytest.raises(DefinitionError, match="type:"):
        normalize({"name": "x", "type": typ, "caps": {"s": {"http": {"url": "http://h/"}}}})


def test_more_credential_refusals():
    for cap in ({"argv": {"argv": ["tool"], "env": {"TOOL_PASSWORD": "hunter22"}}},
                {"http": {"url": "http://h/", "body": "{{secret:t}}"}}):
        with pytest.raises(DefinitionError):
            normalize({"name": "x", "type": "plug", "caps": {"s": cap}})


@pytest.mark.asyncio
async def test_a_secret_spanning_lines_never_reaches_a_header(lock):
    spec = normalize(lock_def(lock))
    with pytest.raises(CapError, match="span lines"):
        await devkit.run_cap(spec, "lock", {}, {"front-door.token": "a\r\nX-Evil: 1"})


@pytest.mark.asyncio
async def test_long_replies_are_capped(monkeypatch):
    big = b"x" * (devkit.MAX_REPLY + 10)
    monkeypatch.setattr(devkit, "_http_blocking", lambda *a: (200, "OK", {}, big))
    spec = normalize({"name": "x", "type": "plug", "caps": {"s": {"http": {"url": "http://h/"}}}})
    res = await devkit.run_cap(spec, "s", {}, {})
    assert res["truncated"] is True and len(res["body"]) == devkit.MAX_TEXT + 1


@pytest.mark.asyncio
async def test_argv_programs_get_a_small_environment(monkeypatch):
    monkeypatch.setenv("ROOK_PSK", "band-key-do-not-leak")
    spec = normalize({"name": "cli", "type": "plug", "caps": {"env": {"argv": {"argv": [
        sys.executable, "-c", "import json,os; print(json.dumps(sorted(os.environ)))"],
        "env": {"MINE": "1"}}}}})
    res = await devkit.run_cap(spec, "env", {}, {})
    assert "ROOK_PSK" not in res["stdout"] and "MINE" in res["stdout"]


def test_masking_covers_url_encoded_copies():
    out = devkit.mask({"a": "k=ab%2Fcd%3D", "b": "ab/cd="}, {"t": "ab/cd="})
    assert out == {"a": "k={secret:t}", "b": "{secret:t}"}


@pytest.mark.asyncio
async def test_files_are_private_and_management_tiers_hold(worker, lock, tmp_path):
    import stat
    await worker.host.start()
    try:
        await _call(worker, "devkit.define", definition=lock_def(lock))
        await _call(worker, "devkit.remove", device="front-door")
        for f in ("devices.json", "retired.json"):
            mode = stat.S_IMODE((tmp_path / "plugins" / "devkit" / f).stat().st_mode)
            assert mode == 0o600, (f, oct(mode))
        described = (await _call(worker, "caps.describe", prefix="devkit."))["result"]
        assert {c: described[c]["risk"] for c in described} == {
            "devkit.define": "admin", "devkit.remove": "admin", "devkit.test": "exec",
            "devkit.list": "read", "devkit.discover": "read"}
    finally:
        await worker.host.stop()
