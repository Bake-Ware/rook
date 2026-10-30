"""Host.handle through the real worker gate (security.py): signed tickets in,
replies out, the gate's cap table following hot loads, elevation re-runs."""

import asyncio
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("nacl")
from nacl.signing import SigningKey  # noqa: E402

from rook.core2 import wire  # noqa: E402
from rook.core2.worker import security as sec  # noqa: E402
from rook.core2.worker.host import Host  # noqa: E402

VEC = Path(__file__).resolve().parents[2] / "conformance" / "core-v2" / "vectors"
NOW = 1793300000
_vec = {n: json.loads((VEC / f"{n}.json").read_text()) for n in ("signatures", "tickets")}
KEYS = {k["name"]: SigningKey(bytes.fromhex(k["seed"])) for k in _vec["signatures"]["keys"]}
ROOT, OP = KEYS["home-root"], KEYS["home-op"]
HOME = wire.key_id(bytes(ROOT.verify_key))
WORKER = _vec["tickets"]["worker"]["worker_id"]
BAND = _vec["tickets"]["worker"]["trust"][HOME]["band"]

CORE = {"manifest": 1, "plugin": "core", "version": "2.0.0", "builtin": True,
        "caps": {"manifests": {"doc": "Manifests.", "risk": "read", "scope": "self"},
                 "lease.drop": {"doc": "Drop leases.", "risk": "write", "scope": "self"}}}

FS_YAML = """manifest: 1
plugin: fs
version: 1.0.0
caps:
  echo:
    doc: Echo with the caller.
    risk: read
    scope: self
    args: { text: { type: str } }
    run: scripts/fs.py:echo
  write:
    doc: Write somewhere protected.
    risk: write
    scope: local
    elevation: may
    run: scripts/fs.py:write
  plain:
    doc: Same, but never elevates.
    risk: write
    scope: local
    run: scripts/fs.py:write
"""

FS_PY = """
def echo(ctx, text=""):
    return {"text": text, "person": ctx.person, "agent": ctx.agent, "why": ctx.why}

def write(ctx):
    if not ctx.lease:
        raise PermissionError("needs root")
    return {"lease": ctx.lease["id"]}
"""


def go(body, timeout=15):
    async def main():
        return await asyncio.wait_for(body(), timeout)
    return asyncio.run(main())


@pytest.fixture
def fs(tmp_path):
    d = tmp_path / "fs"
    (d / "scripts").mkdir(parents=True)
    (d / "plugin.yaml").write_text(FS_YAML)
    (d / "scripts" / "fs.py").write_text(FS_PY)
    return d


def make_host(tmp_path, ask=None):
    anchor = tmp_path / "anchor.json"
    anchor.write_text(json.dumps({"hub": HOME, "root": wire.pub_text(ROOT), "band": BAND}))
    trust = sec.TrustStore(anchor, tmp_path / "memberships.json", WORKER)
    gate = sec.WorkerGate(WORKER, "kaiju", trust, sec.ReplayStore(tmp_path / "nonces", NOW), {}, ask)
    grant = wire.sign(ROOT, {"typ": "rook-hubkey", "v": 2, "hub": HOME, "key": wire.pub_text(OP),
                             "bands": [BAND], "iat": NOW - 60, "exp": NOW + 86400})
    assert gate.add_hubkey(BAND, grant, NOW)
    host = Host(os_name="linux", gate=gate)
    host.add_builtin(CORE, {})
    return host


def request(cap, args=None):
    t = wire.sign(OP, {"typ": "rook-ticket", "v": 2, "hub": HOME, "band": BAND, "call": os.urandom(16).hex(),
                       "nonce": os.urandom(16).hex(), "worker": WORKER, "cap": cap,
                       "args_hash": wire.args_hash(args), "person": "person:bake",
                       "agent": "agent:claude-code", "session": "s_1", "why": "testing handle",
                       "iat": NOW, "exp": NOW + 60})
    return {"id": t["call"], "cap": cap, "target": WORKER, "args": args, "ticket": t}


def test_handle_runs_verified_calls_and_follows_hot_loads(tmp_path, fs):
    async def body():
        host = make_host(tmp_path)
        assert host.gate.caps["core.manifests"]["risk"] == "read"
        req = request("fs.echo", {"text": "hi"})
        assert (await host.handle(req, BAND, NOW))["error"] == "unknown capability: fs.echo"

        await host.load(fs)
        req = request("fs.echo", {"text": "hi"})
        out = await host.handle(req, BAND, NOW)
        assert out == {"id": req["id"], "from": WORKER, "ok": True,
                       "result": {"text": "hi", "person": "person:bake", "agent": "agent:claude-code",
                                  "why": "testing handle"}}
        assert (await host.handle(req, BAND, NOW))["error"] == "denied by worker: replay"
        assert (await host.handle(request("fs.echo", {"text": 1}), BAND, NOW))["error"].startswith("bad args: ")
        assert await host.handle({**request("fs.echo"), "target": "someone-else"}, BAND, NOW) is None

        got = await host.handle(request("core.manifests"), BAND, NOW)
        assert [m["plugin"] for m in got["result"]["manifests"]] == ["core", "fs"]

        await host.unload("fs")
        assert "fs.echo" not in host.gate.caps
        assert (await host.handle(request("fs.echo"), BAND, NOW))["error"] == "unknown capability: fs.echo"
        await host.close()
    go(body)


def test_handle_elevates_may_caps_and_refuses_the_rest(tmp_path, fs):
    asked = []

    async def ask(band, args):
        asked.append(args["reason"])
        t = args["ticket"]
        lease = wire.sign(OP, {"typ": "rook-lease", "v": 2, "id": "ab" * 16, "hub": t["hub"],
                               "band": t["band"], "worker": t["worker"], "person": t.get("person"),
                               "agent": t.get("agent"), "session": t["session"], "iat": NOW,
                               "exp": NOW + wire.LEASE_SECS})
        return {"ok": True, "result": {"lease": lease}}

    async def body():
        host = make_host(tmp_path, ask)
        await host.load(fs)
        req = request("fs.write")
        out = await host.handle(req, BAND, NOW)
        assert out == {"id": req["id"], "from": WORKER, "ok": True,
                       "result": {"lease": "ab" * 16}, "elevated": "ab" * 16}
        assert asked == ["access_denied"]

        out = await host.handle(request("fs.plain"), BAND, NOW)
        assert out["ok"] is False and out["error"] == "fs.plain failed: access denied: needs root"
        assert asked == ["access_denied"]   # elevation: never doesn't ask

        dropped = await host.handle(request("core.lease.drop", {"lease": "ab" * 16}), BAND, NOW)
        assert dropped["result"] == {"dropped": 1}
        await host.close()
    go(body)
