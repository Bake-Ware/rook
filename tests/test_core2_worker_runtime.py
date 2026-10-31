"""The 0.5 worker runtime (rook/core2/worker/runtime.py) end to end: an
in-process worker on the loopback relay, a fake hub node that signs
announces, tickets and leases (rook/core2/wire.py) and pushes bundles built
by the real distribution code over Drop. Every test has a hard timeout."""
from __future__ import annotations

import asyncio
import io
import itertools
import json
import os
import secrets
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

pytest.importorskip("nacl")
pytest.importorskip("telesthete")
from nacl.signing import SigningKey  # noqa: E402

from rook.core2 import CORE_VERSION, wire  # noqa: E402
from rook.core2.hub import distribute as d  # noqa: E402
from rook.core2.transport import FEATURES, LoopbackRelay, Transport, band_of  # noqa: E402
from rook.core2.worker.runtime import Config, Formatter, Worker, load_config  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PSK, PSK2 = "worker-runtime-band-one", "worker-runtime-band-two"
BAND, BAND2 = band_of(PSK), band_of(PSK2)
WID = "3f0a" * 8
ROOT, OP = SigningKey(b"\x11" * 32), SigningKey(b"\x12" * 32)
G_ROOT, G_OP = SigningKey(b"\x21" * 32), SigningKey(b"\x22" * 32)
HOME, GUEST = wire.key_id(bytes(ROOT.verify_key)), wire.key_id(bytes(G_ROOT.verify_key))

ECHO_YAML = """manifest: 1
plugin: echo
version: 1.0.0
uses: [echo.say]
caps:
  say:
    doc: Say it back, with who asked.
    risk: read
    scope: self
    args: { text: { type: str } }
    run: scripts/echo.py:say
  boom:
    doc: Always fails.
    risk: read
    scope: self
    run: scripts/echo.py:boom
  root:
    doc: Needs administrator rights.
    risk: write
    scope: local
    elevation: may
    run: scripts/echo.py:root
  relay:
    doc: Calls another cap through the hub.
    risk: read
    scope: self
    run: scripts/echo.py:relay
"""

ECHO_PY = """
def say(ctx, text=""):
    return {"text": text, "person": ctx.person, "agent": ctx.agent, "why": ctx.why}

def boom(ctx):
    raise RuntimeError("kaboom")

def root(ctx):
    if not ctx.lease:
        raise PermissionError("needs root")
    return {"lease": ctx.lease["id"]}

def relay(ctx):
    return {"inner": ctx.call("echo.say", text="inner")}
"""


def go(body, seconds=20):
    async def main():
        return await asyncio.wait_for(body(), seconds)
    return asyncio.run(main())


async def until(cond, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


class FakeHub:
    """A hub node on one band: signed announces with its hubkey grant,
    ticketed requests, and answers to ``core.elevate`` and ``core.call``."""

    def __init__(self, relay, psk, root, op):
        self.root, self.op = root, op
        self.hub, self.band, self.node = wire.key_id(bytes(root.verify_key)), band_of(psk), secrets.token_hex(16)
        now = int(time.time())
        self.grant = wire.sign(root, {"typ": "rook-hubkey", "v": 2, "hub": self.hub, "key": wire.pub_text(op),
                                      "bands": [self.band], "iat": now - 60, "exp": now + 86400})
        self.seq = itertools.count(int(time.time()))
        self.issued: dict[str, dict] = {}
        self.got: list[dict] = []
        self.sub_calls: list[dict] = []
        self.replay = wire.ReplayCache()
        self.t = Transport(self.node, [relay.link(psk)], hello=self.announce, beat=self.announce, every=0.05)

    def announce(self, band):
        msg = wire.sign(self.op, {"kind": "announce", "typ": "rook-announce", "v": 2, "worker_id": self.node,
                                  "name": "rook", "hub": self.hub, "band": band, "core": CORE_VERSION,
                                  "ts": int(time.time()), "seq": next(self.seq), "features": FEATURES})
        return {**msg, "hubkey": self.grant}

    async def start(self):
        await self.t.start(self.on_message)

    async def on_message(self, band, msg):
        if isinstance(msg.get("cap"), str):
            asyncio.ensure_future(self.answer(band, msg))
        else:
            self.got.append(msg)

    async def answer(self, band, req):
        args, out = req.get("args") or {}, {"id": req["id"], "from": self.node}
        ticket = args.get("ticket") or {}
        # 13.1: signed by the worker with the key it announced
        key = next((m["key"] for m in reversed(self.announces()) if m.get("key")), None)
        ok, why = wire.verify_request(req.get("proof"), req, hub=self.hub, band=band,
                                      keys={WID: key} if key else {}, now=int(time.time()), replay=self.replay)
        if not ok:
            out.update(ok=False, error=f"denied: worker request: {why}")
        elif self.issued.get(ticket.get("nonce")) != ticket:
            out.update(ok=False, error="denied: no such call in flight")
        elif req["cap"] == "core.elevate":
            now = int(time.time())
            lease = {"typ": "rook-lease", "v": 2, "id": secrets.token_hex(16), "hub": self.hub, "band": band,
                     "worker": ticket["worker"], "session": ticket["session"], "iat": now,
                     "exp": now + wire.LEASE_SECS}
            lease.update({k: ticket[k] for k in ("person", "agent") if k in ticket})
            out.update(ok=True, result={"lease": wire.sign(self.op, lease)})
        elif req["cap"] == "core.call":
            self.sub_calls.append(args)
            out.update(ok=True, result={"cap": args["cap"], "args": args["args"], "parent": ticket["call"]})
        else:
            out.update(ok=False, error=f"unknown capability: {req['cap']}")
        await self.t.send(band, out)

    def request(self, cap, args=None, *, target=WID, iat=None, **ticket):
        cid, now = secrets.token_hex(16), int(time.time()) if iat is None else iat
        t = wire.sign(self.op, {"typ": "rook-ticket", "v": 2, "hub": self.hub, "band": self.band, "call": cid,
                                "nonce": secrets.token_hex(16), "worker": target, "cap": cap,
                                "args_hash": wire.args_hash(args), "person": "person:bake",
                                "agent": "agent:claude-code", "session": "s_1", "why": "Testing the worker.",
                                "iat": now, "exp": now + 60, **ticket})
        self.issued[t["nonce"]] = t
        return {"id": cid, "cap": cap, "target": target, "args": args, "ticket": t}

    async def send(self, req, timeout=10):
        return await self.t.request(self.band, req, timeout)

    async def call(self, cap, args=None, timeout=10, **kw):
        return await self.send(self.request(cap, args, **kw), timeout)

    def announces(self):
        return [m for m in self.got if m.get("kind") == "announce" and m.get("worker_id") == WID]


class BandPush:
    """distribute's transport, over the band: offer the bundle over Drop and
    send ``core.plugin.install`` with its descriptor."""

    def __init__(self, hub: FakeHub):
        self.hub = hub

    async def push(self, worker_id, bundle):
        return await self.push_bytes(Path(bundle).read_bytes(), worker_id)

    async def push_bytes(self, blob, worker_id=WID):
        desc = self.hub.t.offer(self.hub.band, blob, to=worker_id)
        out = await self.hub.call("core.plugin.install", {"bundle": desc}, target=worker_id, timeout=30)
        return out["result"] if out["ok"] else {"ok": False, "reason": out["error"]}


ENV = d.WorkerEnv(worker_id=WID, os="linux", python="3.11", packages={})


def tv(root, version, *, fail=False):
    folder = root / f"src-tv-{version}"
    (folder / "scripts").mkdir(parents=True)
    (folder / "plugin.yaml").write_text(
        f"manifest: 1\nplugin: tv\nversion: {version}\n"
        "caps:\n  ping:\n    doc: Answer.\n    risk: read\n    scope: self\n    run: scripts/main.py:ping\n"
        "check: [tv.ping]\n")
    body = "def ping(ctx):\n    raise ctx.Error('screen not answering')\n" if fail else \
        f"def ping(ctx):\n    return {{'version': {version!r}}}\n"
    (folder / "scripts" / "main.py").write_text(body)
    return folder


@pytest.fixture
def setup(tmp_path):
    echo = tmp_path / "local" / "echo"
    (echo / "scripts").mkdir(parents=True)
    (echo / "plugin.yaml").write_text(ECHO_YAML)
    (echo / "scripts" / "echo.py").write_text(ECHO_PY)
    anchor = tmp_path / "anchor.json"
    anchor.write_text(json.dumps({"hub": HOME, "root": wire.pub_text(ROOT), "band": BAND}))
    relay = LoopbackRelay()
    cfg = Config(data=tmp_path / "data", anchor=anchor, name="kaiju", worker_id=WID,
                 bands=[{"psk": PSK}], plugins=[tmp_path / "local"])

    def worker(**opts):
        return Worker(cfg, make_link=lambda spec: relay.link(spec["psk"]),
                      transport_opts={"every": 0.05, **opts})
    return relay, cfg, worker


async def up(relay, worker, psk=PSK, root=ROOT, op=OP):
    hub = FakeHub(relay, psk, root, op)
    await hub.start()
    await worker.start()
    await until(lambda: (hub.hub, hub.band) in worker.hub_nodes and hub.announces())
    return hub


# -- calls --------------------------------------------------------------------

def test_worker_serves_verified_calls_and_refuses_bad_tickets(setup, caplog):
    relay, cfg, make = setup
    caplog.set_level("INFO", logger="rook.worker")

    async def body():
        w = make()
        hub = await up(relay, w)
        a = hub.announces()[-1]
        assert a["name"] == "kaiju" and a["core"] == CORE_VERSION and a["role"] == "home"
        assert a["ceiling"] == {"grants": ["*"]}
        assert [m["plugin"] for m in a["manifests"]] == ["core", "echo", "stream", "worker"]
        assert a["manifests"][0]["builtin"] is True and a["facts"]["os"]
        await until(lambda: any(m.get("kind") == "heartbeat" and m["manifests_hash"] == a["manifests_hash"]
                                for m in hub.got))

        req = hub.request("echo.say", {"text": "hi"})
        out = await hub.send(req)
        assert out == {"id": req["id"], "from": WID, "ok": True,
                       "result": {"text": "hi", "person": "person:bake", "agent": "agent:claude-code",
                                  "why": "Testing the worker."}}
        # The same request again is a band resend: the transport answers it
        # with the reply it holds and never runs it twice. Once the
        # transport has forgotten it, the gate's replay cache still refuses.
        assert await hub.send(req) == out
        for b in w.transport._bands.values():
            b.inbound.clear()
            b.unacked.clear()
        assert (await hub.send(req))["error"] == "denied by worker: replay"

        old = int(time.time()) - 300
        assert (await hub.call("echo.say", {"text": "x"}, iat=old))["error"] == "denied by worker: expired"
        tampered = hub.request("echo.say", {"text": "hi"})
        tampered["args"] = {"text": "rm -rf"}
        assert (await hub.send(tampered))["error"] == "denied by worker: wrong_args"
        forged = hub.request("echo.say", {"text": "hi"})
        forged["ticket"] = {**forged["ticket"], "person": "person:mallory"}
        assert (await hub.send(forged))["error"] == "denied by worker: bad_signature"
        stranger = FakeHub(relay, PSK, SigningKey(b"\x31" * 32), SigningKey(b"\x32" * 32))
        assert (await hub.send(stranger.request("echo.say", {})))["error"] == "denied by worker: unknown_hub"
        assert (await hub.call("echo.nope"))["error"] == "unknown capability: echo.nope"
        assert (await hub.call("echo.say", {"text": 5}))["error"].startswith("bad args: ")
        secret = {"text": ["hunter2-sk-live-0123"]}          # the hub substituted a secret into args
        assert "hunter2" in (await hub.call("echo.say", secret))["error"]
        logged = "\n".join(Formatter("json").format(r) for r in caplog.records)
        assert "hunter2" not in logged and '"error": "bad args"' in logged
        assert '"error": "denied by worker: replay"' in logged
        with pytest.raises(asyncio.TimeoutError):          # not for this worker: silence
            await hub.call("echo.say", {}, target="ab" * 16, timeout=0.3)

        out = await hub.call("echo.boom")
        assert out["ok"] is False and out["error"] == "echo.boom failed: RuntimeError: kaboom"
        assert (await hub.call("echo.say", {"text": "still here"}))["ok"]

        out = await hub.call("echo.root")
        assert out["ok"] and out["elevated"] == out["result"]["lease"]
        assert list(w.gate.leases) == [out["elevated"]]

        req = hub.request("echo.relay")
        out = await hub.send(req)
        assert out["result"] == {"inner": {"cap": "echo.say", "args": {"text": "inner"}, "parent": req["id"]}}
        assert hub.sub_calls[-1]["ticket"] == req["ticket"] and hub.sub_calls[-1]["target"] == WID

        got = await hub.call("core.trust.get")
        assert got["result"]["anchor"] == {"hub": HOME, "band": BAND, "protected": False}
        got = await hub.call("core.manifests", {"plugins": ["echo"]})
        assert [m["plugin"] for m in got["result"]["manifests"]] == ["echo"]
        assert (await hub.call("core.lease.drop", {"person": "person:bake"}))["result"] == {"dropped": 1}

        await w.stop()
        await hub.t.close()
    go(body)


def test_the_worker_signs_with_its_own_key(setup):
    """Spec 5.8, 13.1: <data>/worker.key, made once and kept; its
    public half in every announce; its core.call carries a proof the hub
    checks against it. Replies stay unsigned."""
    relay, cfg, make = setup

    async def body():
        w = make()
        hub = await up(relay, w)
        key_file = cfg.data / "worker.key"
        assert oct(key_file.stat().st_mode & 0o777) == "0o600"
        pub = wire.pub_text(SigningKey(bytes.fromhex(key_file.read_text().strip())))
        assert hub.announces()[-1]["key"] == pub
        out = await hub.send(hub.request("core.manifests", {"plugins": ["echo"]}))
        assert out["ok"] and "proof" not in out
        out = await hub.send(hub.request("echo.relay"))
        assert out["ok"] and out["result"]["inner"]["cap"] == "echo.say"     # FakeHub checked the proof
        await w.stop()
        await hub.t.close()
        w2 = make()                                # the same key after a restart
        hub2 = await up(relay, w2)
        assert hub2.announces()[-1]["key"] == pub
        await w2.stop()
        await hub2.t.close()
        key_file.write_text("not a key\n")          # never replaced: the hub pinned it
        with pytest.raises(RuntimeError, match="not a worker key"):
            await make().start()
    go(body)


def test_leases_end_when_the_hub_is_lost(setup):
    relay, cfg, make = setup

    async def body():
        w = make(lost_after=0.4)
        hub = await up(relay, w)
        out = await hub.call("echo.root")
        assert out["ok"] and w.gate.leases
        await hub.t.close()                     # the hub goes quiet
        await until(lambda: not w.gate.leases, 5)
        await w.stop()
    go(body)


# -- plugins pushed by the hub -----------------------------------------------------

def test_signed_bundles_install_roll_back_and_survive_a_restart(setup, tmp_path):
    relay, cfg, make = setup

    async def body():
        w = make()
        hub = await up(relay, w)
        push = BandPush(hub)

        async def ship(folder, signer=OP):
            return await d.install(folder, ENV, push, signer=signer, hub=HOME, resolve=lambda r, e: [])

        report = await ship(tv(tmp_path, "1.0.0"))
        assert report["ok"] and report["version"] == "1.0.0" and report["checks"] == ["tv.ping"]
        assert (await hub.call("tv.ping"))["result"] == {"version": "1.0.0"}
        await until(lambda: any(m["plugin"] == "tv" for m in hub.announces()[-1]["manifests"]))

        bad = await ship(tv(tmp_path, "1.1.0", fail=True))
        assert not bad["ok"] and "screen not answering" in bad["reason"] and "(rolled back)" in bad["reason"]
        assert (await hub.call("tv.ping"))["result"] == {"version": "1.0.0"}

        stranger = await ship(tv(tmp_path, "1.2.0"), signer=SigningKey(b"\x41" * 32))
        assert not stranger["ok"] and "not signed by a hub that may approve plugins here" in stranger["reason"]

        plan = d.prepare(tv(tmp_path, "1.3.0"), ENV, signer=OP, hub=HOME, resolve=lambda r, e: [])
        bundle = d.write_bundle(plan, tmp_path / "tv.tar")
        tampered = await push.push_bytes(_swap(bundle.read_bytes(), "scripts/main.py",
                                               b"def ping(ctx):\n    return 'owned'\n"))
        assert not tampered["ok"] and "tampered bundle" in tampered["reason"]
        assert (await hub.call("tv.ping"))["result"] == {"version": "1.0.0"}

        assert (await ship(tv(tmp_path, "1.4.0")))["ok"]
        assert (await hub.call("tv.ping"))["result"] == {"version": "1.4.0"}
        back = await hub.call("core.plugin.rollback", {"plugin": "tv"})
        assert back["ok"] and back["result"]["version"] == "1.0.0"
        assert (await hub.call("tv.ping"))["result"] == {"version": "1.0.0"}
        replayed = hub.request("tv.ping")
        assert (await hub.send(replayed))["ok"]

        await w.stop()
        w2 = make()                              # restart on the same data folder
        await w2.start()
        await until(lambda: (HOME, BAND) in w2.hub_nodes)
        assert (await hub.call("tv.ping"))["result"] == {"version": "1.0.0"}
        assert (await hub.send(replayed))["error"] == "denied by worker: replay"
        assert w2.worker_id == WID and not w2.gate.leases
        await w2.stop()
        await hub.t.close()
    go(body, 40)


def _swap(blob: bytes, name: str, data: bytes) -> bytes:
    """The same tar with one file's bytes replaced (plugin.sig untouched)."""
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(blob)) as src, tarfile.open(fileobj=out, mode="w") as dst:
        for m in src.getmembers():
            body = data if m.name == name else src.extractfile(m).read()
            m.size = len(body)
            dst.addfile(m, io.BytesIO(body))
    return out.getvalue()


# -- several bands -------------------------------------------------------------------

def test_guest_band_joins_through_the_home_hub_with_its_own_ceiling(setup):
    relay, cfg, make = setup

    def membership(role, iat, ceiling=None):
        doc = {"typ": "rook-membership", "v": 2, "serial": secrets.token_hex(16), "worker": WID, "hub": GUEST,
               "root": wire.pub_text(G_ROOT), "band": BAND2, "role": role, "by": "person:bake", "iat": iat}
        if ceiling is not None:
            doc["ceiling"] = ceiling
        return wire.sign(ROOT, doc)

    async def body():
        w = make()
        home = await up(relay, w)
        guest = FakeHub(relay, PSK2, G_ROOT, G_OP)
        await guest.start()
        now = int(time.time())
        out = await home.call("core.trust.set", {"membership": membership("guest", now, {"grants": ["echo.say"]}),
                                                 "connect": {"psk": PSK2}})
        assert out["ok"], out
        await until(lambda: (GUEST, BAND2) in w.hub_nodes and guest.announces())
        assert set(w.transport.bands) == {BAND, BAND2}
        a = guest.announces()[-1]
        assert a["role"] == "guest" and a["ceiling"] == {"grants": ["echo.say"]}
        assert json.loads((cfg.data / "bands.json").read_text()) == {BAND2: {"psk": PSK2}}

        said = await guest.call("echo.say", {"text": "hello guest"})
        assert said["ok"] and said["result"]["text"] == "hello guest"
        assert (await guest.call("echo.boom"))["error"] == "denied by worker: outside_ceiling"
        refused = await guest.call("core.trust.set", {"membership": membership("guest", now + 1, {"grants": ["*"]})})
        assert refused["error"].startswith("denied by worker: ")
        # a guest ticket for the home band is not good there
        cross = guest.request("echo.say", {"text": "x"})
        assert (await home.send(cross))["error"] == "denied by worker: unknown_hub"

        out = await home.call("core.trust.set", {"membership": membership("removed", now + 2)})
        assert out["ok"], out
        await until(lambda: w.transport.bands == [BAND])
        assert json.loads((cfg.data / "bands.json").read_text()) == {}
        await w.stop()
        await home.t.close()
        await guest.t.close()
    go(body)


# -- config and the command ------------------------------------------------------------

def test_load_config_resolves_paths_and_refuses_mistakes(tmp_path):
    path = tmp_path / "worker.yaml"
    path.write_text("data: ./state\nanchor: anchor.json\nname: kaiju\nplugins: [plugins]\n"
                    "bands: [{psk_env: ROOK_PSK, relay: 'relay:7474'}]\n")
    cfg = load_config(path)
    assert cfg.data == tmp_path / "state" and cfg.anchor == tmp_path / "anchor.json"
    assert cfg.plugins == [tmp_path / "plugins"] and cfg.bands[0]["psk_env"] == "ROOK_PSK"
    for bad, why in (("data: x\n", "anchor is required"), ("data: x\nanchor: a\nband: 1\n", "unknown keys: band"),
                     ("data: x\nanchor: a\nname: rook\n", "name must be"),
                     ("data: x\nanchor: a\nworker_id: abc\n", "worker_id must be 32 hex")):
        path.write_text(bad)
        with pytest.raises(ValueError, match=why):
            load_config(path)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM")
def test_the_command_starts_and_stops_cleanly(tmp_path):
    anchor = tmp_path / "anchor.json"
    anchor.write_text(json.dumps({"hub": HOME, "root": wire.pub_text(ROOT), "band": BAND}))
    (tmp_path / "worker.yaml").write_text("data: ./state\nanchor: anchor.json\nname: kaiju\nlog: {format: json}\n")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO), os.environ.get("PYTHONPATH", "")])}
    p = subprocess.Popen([sys.executable, "-m", "rook.core2.worker", "--config", str(tmp_path / "worker.yaml")],
                         cwd=tmp_path, env=env, stderr=subprocess.PIPE, text=True)
    try:
        lines = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = p.stderr.readline()
            if not line:
                break
            lines.append(line)
            if "worker started" in line:
                break
        assert any("worker started" in x for x in lines), "".join(lines)
        started = json.loads(next(x for x in lines if "worker started" in x))
        assert started["name"] == "kaiju" and len(started["worker"]) == 32
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=30)
        assert p.returncode == 0, err
        assert "worker stopped" in err
        assert (tmp_path / "state" / "worker_id").read_text().strip() == started["worker"]
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(10)

    (tmp_path / "bad.yaml").write_text("data: x\n")
    r = subprocess.run([sys.executable, "-m", "rook.core2.worker", "--config", str(tmp_path / "bad.yaml")],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "anchor is required" in r.stderr


def test_a_failed_start_closes_what_it_opened(setup):
    relay, cfg, make = setup

    def broken(spec):
        raise RuntimeError("no relay here")

    async def body():
        w = Worker(cfg, make_link=broken)
        with pytest.raises(RuntimeError, match="no relay here"):
            await w.start()
        assert not w.started and w.host.plugins == {}       # echo was loaded, then closed
        await w.stop()                                       # a no-op, not a crash
    go(body)


# -- the real hub ----------------------------------------------------------------------

def test_the_real_hub_drives_the_worker_over_the_transport(setup, tmp_path):
    """rook.core2.hub.runtime.Hub and this worker on one loopback band: the
    hub learns the worker from its announce (fetching the echo manifest with
    core.manifests), dispatches ticketed calls, grants elevation and runs a
    plugin's ctx.call as a sub-call."""
    from rook.core2.hub.identity import Caller as HubCaller
    from rook.core2.hub.runtime import Config as HubConfig, Hub

    relay, cfg, make = setup
    keys = tmp_path / "hub" / "keys"
    keys.mkdir(parents=True)
    (keys / "root.key").write_text(bytes(ROOT).hex())
    (keys / "op.key").write_text(bytes(OP).hex())
    node = secrets.token_hex(16)
    (tmp_path / "hub" / "hub.json").write_text(json.dumps({"node": node}))
    hub_cfg = HubConfig(data=tmp_path / "hub", band=BAND, users={"bake": {"preset": "owner"}}, doors={})
    bake = HubCaller(person="person:bake")

    async def body():
        w = make()
        await w.start()
        hub = Hub(hub_cfg, transport=Transport(node, [relay.link(PSK)]))
        await hub.start()                 # its first announce is hub contact; the worker announces back
        await until(lambda: "echo.say" in getattr(hub.dispatcher.workers.get(WID), "caps", {}), 10)
        assert hub.known[WID].name == "kaiju" and hub.known[WID].problem is None

        async def call(cap, args=None):
            return await hub.call(bake, {"worker": "kaiju", "cap": cap, "args": args,
                                         "why": "Checking the worker runtime against the hub."}, "test")

        said = await call("echo.say", {"text": "hi"})
        assert said["ok"] and said["result"]["person"] == "person:bake", said
        boom = await call("echo.boom")
        assert not boom["ok"] and boom["error"] == "echo.boom failed: RuntimeError: kaboom"
        root = await call("echo.root")
        assert root["ok"] and root["elevated"] == root["result"]["lease"], root
        relayed = await call("echo.relay")
        assert relayed["ok"] and relayed["result"]["inner"]["text"] == "inner", relayed

        await hub.stop()
        await w.stop()
    go(body, 30)


# -- the worker's own caps: worker.* and stream.* ------------------------------

def test_worker_caps_ping_status_log_restart(setup):
    relay, cfg, make = setup

    async def body():
        w = make()
        restarted = asyncio.Event()
        w.reexec = restarted.set
        hub = await up(relay, w)
        pong = await hub.call("worker.ping")
        assert pong["ok"] and pong["result"]["pong"] is True, pong
        st = (await hub.call("worker.status"))["result"]
        assert st["name"] == "kaiju" and st["worker_id"] == WID and st["core"] == CORE_VERSION
        assert st["band"] == {"connected": True, "hub_contact": True}
        assert st["plugins"] == ["echo@1.0.0"] and st["streams_open"] == 0

        await hub.call("echo.say", {"text": "hi"})
        await hub.call("echo.boom")
        await hub.call("core.manifests")                  # not worth a line
        got = (await hub.call("worker.log", {"cap": "echo."}))["result"]["entries"]
        assert [(e["cap"], e["ok"], e["person"]) for e in got] == [
            ("echo.say", True, "person:bake"), ("echo.boom", False, "person:bake")]
        assert got[1]["error"].endswith("kaboom") and got[0]["why"] == "Testing the worker."
        failed = (await hub.call("worker.log", {"failed": True}))["result"]["entries"]
        assert [e["cap"] for e in failed] == ["echo.boom"]
        assert (await hub.call("worker.log", {"person": "person:nobody"}))["result"]["entries"] == []
        assert (await hub.call("info.ping"))["ok"]            # the 0.4 name is an alias

        out = await hub.call("worker.restart")
        assert out["ok"] and out["result"] == {"restarting": True}
        await asyncio.wait_for(restarted.wait(), 10)
        assert not w.started                                  # stopped cleanly before the re-exec
    go(body)


def test_ran_log_rotates_and_reads_both_files(tmp_path):
    from rook.core2.worker.builtins import RanLog
    ran = RanLog(tmp_path / "ran.jsonl", max_bytes=100)
    for i in range(12):
        ran.add({"at": i, "cap": f"x.{i}", "ok": i % 2 == 0})
    assert (tmp_path / "ran.jsonl.1").exists()
    got = ran.read(limit=500)
    assert [e["at"] for e in got] == sorted(e["at"] for e in got) and got[-1]["at"] == 11
    assert len(got) < 12                                      # the oldest went with the rotation
    assert [e["at"] for e in ran.read(limit=2)] == [10, 11]
    assert all(not e["ok"] for e in ran.read(failed=True)) and ran.read(since=11)[0]["at"] == 11


@pytest.mark.skipif(sys.platform == "win32", reason="the shell's PTY is POSIX")
def test_stream_caps_drive_a_shell_terminal(setup):
    relay, cfg, make = setup
    cfg.plugins.append(REPO / "rook" / "plugins" / "shell")

    async def body():
        w = make()
        hub = await up(relay, w)
        opened = await hub.call("shell.open", {"argv": ["/bin/sh"], "pty": True})
        assert opened["ok"], opened
        sid = opened["result"]["stream"]
        mine = (await hub.call("stream.list", {"kind": "terminal"}))["result"]["streams"]
        assert [s["id"] for s in mine] == [sid] and "participants" not in mine[0]

        wrote = await hub.call("stream.write", {"stream": sid, "body": {"text": "echo castl''ing\r"}})
        assert wrote["ok"], wrote
        seen, after = b"", 0
        for _ in range(50):
            got = (await hub.call("stream.read", {"stream": sid, "after": after, "wait": 1}))["result"]
            after = got["next"]
            seen += b"".join(__import__("base64").b64decode(e["b64"]) for e in got["events"] if "b64" in e)
            if b"castling" in seen:
                break
        assert b"castling" in seen

        # Someone else's stream doesn't exist for them.
        other = await hub.call("stream.read", {"stream": sid}, person="person:mallory")
        assert not other["ok"] and "no stream" in other["error"]
        assert (await hub.call("stream.list", {}, person="person:mallory"))["result"]["streams"] == []
        assert not (await hub.call("stream.close", {"stream": sid}, person="person:mallory"))["ok"]

        closed = await hub.call("stream.close", {"stream": sid})
        assert closed["ok"] and closed["result"]["closed"], closed
        end = (await hub.call("stream.read", {"stream": sid, "after": after}))["result"]
        assert end["closed"] and end["events"][-1]["data"]["state"] == "closed"
        assert (await hub.call("worker.status"))["result"]["streams_open"] == 0
        await w.stop()
    go(body, 30)


@pytest.mark.skipif(sys.platform == "win32", reason="the shell's PTY is POSIX")
def test_worker_and_stream_caps_keep_bands_apart(setup):
    """A guest band's hub sees its own calls, plugins and streams only (spec
    9.4): person:bake on the guest hub is not the home hub's person:bake."""
    relay, cfg, make = setup
    cfg.plugins.append(REPO / "rook" / "plugins" / "shell")

    async def body():
        w = make()
        home = await up(relay, w)
        guest = FakeHub(relay, PSK2, G_ROOT, G_OP)
        await guest.start()
        now = int(time.time())
        doc = wire.sign(ROOT, {"typ": "rook-membership", "v": 2, "serial": secrets.token_hex(16), "worker": WID,
                               "hub": GUEST, "root": wire.pub_text(G_ROOT), "band": BAND2, "role": "guest",
                               "by": "person:bake", "iat": now,
                               "ceiling": {"grants": ["echo.say", "worker.*", "stream.*"]}})
        assert (await home.call("core.trust.set", {"membership": doc, "connect": {"psk": PSK2}}))["ok"]
        await until(lambda: (GUEST, BAND2) in w.hub_nodes and guest.announces())

        opened = await home.call("shell.open", {"argv": ["cat"], "pty": False})
        sid = opened["result"]["stream"]
        await home.call("echo.say", {"text": "home"})
        await guest.call("echo.say", {"text": "guest"})

        log = (await guest.call("worker.log"))["result"]["entries"]
        assert [(e["cap"], e["band"]) for e in log] == [("echo.say", BAND2)]
        assert {e["cap"] for e in (await home.call("worker.log"))["result"]["entries"]} >= {"shell.open", "echo.say"}
        st = (await guest.call("worker.status"))["result"]
        assert st["plugins"] == ["echo@1.0.0"] and st["streams_open"] == 0      # no shell: outside its ceiling
        assert "shell.exec" not in st["unavailable"]
        assert (await home.call("worker.status"))["result"]["streams_open"] == 1
        # the same person name on another hub is someone else
        assert (await guest.call("stream.list"))["result"]["streams"] == []
        stolen = await guest.call("stream.read", {"stream": sid})
        assert not stolen["ok"] and "no stream" in stolen["error"]
        assert (await home.call("stream.close", {"stream": sid}))["ok"]
        await w.stop()
        await home.t.close()
        await guest.t.close()
    go(body, 30)

