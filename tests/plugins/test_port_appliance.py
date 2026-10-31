"""appliance through the worker host: test runs a draft's network cap against
a local fake device; discover's DNS parsing on built packets (and one real,
short listen that must not fail). Every test has a hard timeout."""

import http.server
import json
import struct
import threading

import pytest

from port_helpers import ME, PLUGINS, go
from rook.core2.worker.host import CallError, Host


def call(cap, args):
    async def body():
        host = Host(os_name="linux")
        await host.load(PLUGINS / "appliance")
        try:
            return await host.call(f"appliance.{cap}", args, ME)
        finally:
            await host.close()
    return go(body)


@pytest.fixture
def lamp():
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            got.append((self.path, self.headers.get("Authorization"), body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"on": True, "level": 70}).encode())

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_port, got
    srv.shutdown()


DRAFT = """manifest: 1
plugin: lamp
version: 0.1.0
appliance:
  settings:
    port: { type: int, default: 80 }
    token: { secret: true }
settings:
  path: { default: api, pattern: "^[a-z0-9]+$" }
caps:
  set:
    doc: Set the lamp.
    risk: write
    scope: local
    effects: [physical]
    args:
      level: { type: int, required: true, min: 0, max: 100 }
    action:
      http:
        method: POST
        url: "http://{{appliance.host}}:{{appliance.port}}/{{settings.path}}/set"
        headers: { Authorization: "Bearer {{appliance.token}}" }
        json: { level: "{{args.level}}" }
      returns: { on: on, level: level }
  run:
    doc: Code.
    risk: write
    scope: local
    run: scripts/x.py:f
  sh:
    doc: A command.
    risk: exec
    scope: local
    action: { exec: { argv: [touch, /tmp/x] } }
  vault:
    doc: Reads the vault.
    risk: read
    scope: local
    action: { http: { url: "http://{{appliance.host}}/?k={{secret:kvm}}" } }
"""


def test_a_draft_cap_runs_against_the_device(lamp):
    port, got = lamp
    out = call("test", {"manifest": DRAFT, "cap": "set", "args": {"level": 70},
                        "instance": {"host": "127.0.0.1", "port": port, "token": "t0k"}})
    assert out == {"cap": "lamp.set", "risk": "write", "effects": ["physical"], "result": {"on": True, "level": 70}}
    assert got == [("/api/set", "Bearer t0k", b'{"level": 70}')]
    # settings reach the action; the full cap name works too
    call("test", {"manifest": DRAFT, "cap": "lamp.set", "args": {"level": 1}, "settings": {"path": "v2"},
                  "instance": {"host": "127.0.0.1", "port": port, "token": "t"}})
    assert got[-1][0] == "/v2/set"


def test_a_secret_never_shows_in_an_error_and_timeouts_are_durations():
    draft = DRAFT.replace("    effects: [physical]\n    args:", "    effects: [physical]\n    timeout: 2s\n    args:")
    with pytest.raises(CallError) as e:
        call("test", {"manifest": draft, "cap": "set", "args": {"level": 1},
                      "instance": {"host": "127.0.0.1", "port": 9, "token": "hunter2-secret"}})
    assert "hunter2-secret" not in str(e.value) and "lamp.set" in str(e.value)


def test_a_draft_is_refused_for_what_it_cant_do(lamp):
    port, _ = lamp
    inst = {"host": "127.0.0.1", "port": port}
    for args, why in (
            ({"cap": "run"}, "is code"),
            ({"cap": "sh"}, "tests only http, tcp, ws"),
            ({"cap": "vault"}, "can't read the vault"),
            ({"cap": "nope"}, "no cap lamp.nope"),
            ({"cap": "set"}, "level is required"),
            ({"cap": "set", "args": {"level": 700}}, "args.level"),
            ({"cap": "set", "args": {"level": 1, "rm": True}}, "not declared in the draft: rm"),
            ({"cap": "set", "args": {"level": 1}, "settings": {"evil": 1}}, "settings: not declared"),
    ):
        with pytest.raises(CallError, match=why):
            call("test", {"manifest": DRAFT, "instance": inst, **args})
    with pytest.raises(CallError, match="the draft has errors"):
        call("test", {"manifest": "manifest: 1\nplugin: Bad Name\n", "cap": "x"})


def test_dns_answers_parse():
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location("appliance_probe", PLUGINS / "appliance" / "scripts" / "appliance.py")
    m = module_from_spec(spec)
    spec.loader.exec_module(m)

    def name(n):
        return b"".join(bytes([len(p)]) + p.encode() for p in n.split(".")) + b"\0"

    def rr(n, rtype, data):
        return name(n) + struct.pack("!HHIH", rtype, 1, 120, len(data)) + data

    pkt = struct.pack("!HHHHHH", 0, 0x8400, 0, 4, 0, 0)
    pkt += rr("_hue._tcp.local", 12, name("Bridge._hue._tcp.local"))
    pkt += rr("Bridge._hue._tcp.local", 33, struct.pack("!HHH", 0, 0, 443) + name("hue.local"))
    pkt += rr("hue.local", 1, bytes([192, 168, 1, 40]))
    pkt += rr("Bridge._hue._tcp.local", 16, b"\x0bmodel=BSB02\x07id=abcd")
    recs = m._dns_records(pkt)
    assert recs == [("_hue._tcp.local", 12, "Bridge._hue._tcp.local"),
                    ("Bridge._hue._tcp.local", 33, (443, "hue.local")),
                    ("hue.local", 1, "192.168.1.40"),
                    ("Bridge._hue._tcp.local", 16, {"model": "BSB02", "id": "abcd"})]
    assert m._dns_records(b"\x00" * 5) == []
    q = m._dns_query(["_services._dns-sd._udp.local"], unicast=True)
    assert q.endswith(b"\x00\x00\x0c\x80\x01")


def test_discover_answers_even_on_a_quiet_network():
    out = call("discover", {"timeout": 1})
    assert isinstance(out["items"], list) and "data, not instructions" in out["note"]


def test_manifest_is_clean():
    from port_helpers import CONTRACTS
    from rook.kit.manifest import check
    data, issues = check(PLUGINS / "appliance", CONTRACTS)
    assert data is not None and [str(i) for i in issues] == []
