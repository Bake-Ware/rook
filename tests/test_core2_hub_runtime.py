"""The 0.5 hub process (rook/core2/hub/runtime.py, doors, __main__): a hub
started in-process with a loopback band and a fake worker that enforces with
rook.core2.worker.security, called through the HTTP door.

Every test runs under a hard timeout; HTTP requests run in a thread with
their own socket timeout (the door runs coroutines on the hub's loop)."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import stat
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("nacl")
from nacl.signing import SigningKey  # noqa: E402
pytest.importorskip("jsonschema")

from rook.core2 import journal as journal_mod, wire  # noqa: E402
from rook.core2.builtin import CORE  # noqa: E402
from rook.core2.hub import runtime  # noqa: E402
from rook.core2.hub.__main__ import main  # noqa: E402
from rook.core2.hub.runtime import Config, Hub, Loopback, load_config  # noqa: E402
from rook.core2.worker.security import ReplayStore, TrustStore, WorkerGate  # noqa: E402
from rook.kit.manifest import check, normalize  # noqa: E402

BAND = "e17904e6ed4c664592f64f6f1d02c682"
WID = "3f0a9c1d2e3f40516273849506a7b8c9"
WHY = "Check the runtime end to end."

ECHO = """\
manifest: 1
plugin: echo
version: 1.0.0
doc: Says things back.
uses: ["echo.*"]
caps:
  say:
    doc: Say it back.
    risk: read
    scope: self
    run: scripts/echo.py:say
    args:
      text: { type: str, required: true }
  root:
    doc: Needs administrator rights.
    risk: write
    scope: local
    elevation: upfront
    run: scripts/echo.py:root
  relay:
    doc: Calls echo.say again through the hub.
    risk: read
    scope: self
    run: scripts/echo.py:relay
"""

NOTES = """\
manifest: 1
plugin: notes
version: 1.0.0
doc: Notes kept on the hub.
runs_on: hub
records:
  note:
    fields:
      text: { type: str, required: true }
    search: [text]
    caps: [list, create, search]
caps:
  count:
    doc: How many notes there are.
    risk: read
    scope: multi-user
    run: scripts/notes.py:count
  tell:
    doc: Keep a message as a note.
    risk: write
    scope: multi-user
    run: scripts/notes.py:tell
    args:
      text: { type: str, required: true }
      to: { type: str }
      title: { type: str }
  quit:
    doc: Plugin code that exits.
    risk: read
    scope: self
    run: scripts/notes.py:quit
  relay:
    doc: Counts the notes through the hub (ctx.call).
    risk: read
    scope: self
    run: scripts/notes.py:relay
provides:
  notify.send: notes.tell
hooks:
  on: [ { event: worker.online, run: scripts/notes.py:online } ]
"""

NOTES_PY = """\
SEEN = []

def count(ctx):
    return {"count": len(ctx.records.note.list())}

def tell(ctx, text, to=None, title=None):
    return ctx.records.note.create(text=text)["id"]

def quit(ctx):
    raise SystemExit(3)

def relay(ctx):
    return ctx.call("notes.count")

def online(ctx, event, payload):
    SEEN.append((payload["name"], ctx.call("notes.count")["count"]))
"""


def run(coro, timeout=30):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def plugin(folder: Path, name: str, text: str, scripts: dict) -> Path:
    d = folder / name
    (d / "scripts").mkdir(parents=True)
    (d / "plugin.yaml").write_text(text)
    for f, body in scripts.items():
        (d / "scripts" / f).write_text(body)
    return d


def config(tmp_path: Path, library: Path | None = None, **over) -> Config:
    cfg = Config(data=tmp_path / "data", band=BAND, library=library,
                 users={"bake": {"preset": "owner"}, "kid": {"preset": "child"}},
                 agents={"claude": {"grants": ["*"], "max_risk": "exec"}},
                 doors={"http": {"bind": "127.0.0.1:0"}}, **over)
    return cfg


async def http(hub: Hub, method: str, path: str, body=None, token: str | None = None, raw: bytes | None = None):
    port = hub.doors[0].port

    def go():
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    return await asyncio.to_thread(go)


async def until(pred, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


class FakeWorker:
    """A worker on the loopback band: announces core + echo, verifies every
    ticket with WorkerGate and runs echo's caps inline."""

    def __init__(self, hub: Hub, band: Loopback, tmp: Path, core: str = "2.0", extra: dict | None = None):
        self.hub, self.core = hub, core
        self.key = SigningKey.generate()    # its own key: signs its core.call / core.elevate (13.1)
        folder = plugin(tmp, "echo", ECHO, {"echo.py": "def say(ctx, text): return text\n"})
        self.echo_raw, _ = check(folder)
        self.core_raw, _ = check(CORE)
        # More plugins: {manifest text: {cap: fn(request) -> result}}
        self.extra_raw, self.extra = [], {}
        for text, fns in (extra or {}).items():
            raw, issues = check(plugin(tmp, f"x{len(self.extra_raw)}", text, {}))
            assert raw is not None, [str(i) for i in issues]
            self.extra_raw.append(raw)
            self.extra.update(fns)
        anchor = tmp / "anchor.json"
        anchor.write_text(json.dumps({"hub": hub.hub_id, "root": wire.pub_text(hub.root), "band": BAND}))
        caps = {n: {"risk": m["risk"], "effects": m["effects"], "elevation": m["elevation"]}
                for raw in (self.core_raw, self.echo_raw, *self.extra_raw) for n, m in normalize(raw)["caps"].items()
                if m.get("on") == "worker"}
        self.gate = WorkerGate(WID, "kaiju", TrustStore(anchor, None, WID),
                               ReplayStore(tmp / "replay.txt"), caps, ask=self.ask)
        self.to_hub = band.attach(BAND, self.handle)
        self.pending: dict[str, asyncio.Future] = {}
        self.verified: list[dict] = []
        self.refused: list[dict] = []
        self.args_seen: list[dict] = []
        self.announces = 0
        self.sent: list[dict] = []          # its requests to the hub, as a band member would see them
        self.during_relay = None            # async fn(ticket): runs inside echo.relay, after its sub-call

    def entries(self):
        return sorted(({"plugin": r["plugin"], "version": r["version"], "hash": wire.manifest_hash(r),
                        **({"builtin": True} if r.get("builtin") else {})}
                       for r in (self.core_raw, self.echo_raw, *self.extra_raw)), key=lambda e: e["plugin"])

    async def announce(self, **over):
        e = self.entries()
        key = {"key": wire.pub_text(self.key)} if self.key else {}
        await self.to_hub({"kind": "announce", "worker_id": WID, "name": "kaiju", "core": self.core,
                           **key, "version": "1.0", "manifests": e,
                           "manifests_hash": runtime._entries_hash(e), **over})

    async def heartbeat(self, mhash=None):
        await self.to_hub({"kind": "heartbeat", "worker_id": WID, "core": self.core, "ts": 0,
                           "manifests_hash": mhash or runtime._entries_hash(self.entries())})

    async def ask(self, band, args):
        return await self.request("core.elevate", args)

    async def request(self, cap, args, **proof):
        rid = secrets.token_hex(16)
        fut = self.pending[rid] = asyncio.get_running_loop().create_future()
        req = {"id": rid, "cap": cap, "target": self.hub.node, "args": args}
        if self.key:
            req["proof"] = wire.request_proof(self.key, req, **{
                "worker": WID, "hub": self.hub.hub_id, "band": BAND, "nonce": secrets.token_hex(16),
                "ts": int(time.time()), **proof})
        self.sent.append(req)
        await self.to_hub(req)
        return await fut

    async def handle(self, msg):
        if msg.get("typ") == "rook-announce":
            self.announces += int(self.gate.hub_announce(BAND, msg))
            return
        if "cap" not in msg:
            fut = self.pending.pop(msg.get("id"), None)
            if fut is not None:
                fut.set_result(msg)
            return
        got = self.gate.check(msg, BAND)
        if got is None:
            return
        if isinstance(got, dict):
            self.refused.append(got)
            await self.to_hub(got)
            return
        self.verified.append(got.ticket)
        args = msg.get("args") or {}
        self.args_seen.append(args)

        async def action(lease):
            cap = got.ticket["cap"]
            if cap == "core.manifests":
                return {"manifests": [r for r in (self.core_raw, self.echo_raw, *self.extra_raw)
                                      if not args.get("plugins") or r["plugin"] in args["plugins"]]}
            if cap in self.extra:
                return self.extra[cap](msg)
            if cap == "echo.say":
                return {"said": args["text"], "person": got.ticket.get("person"),
                        "agent": got.ticket.get("agent")}
            if cap == "echo.root":
                return {"lease": lease["id"]}
            if cap == "core.lease.drop":
                return self.gate.drop_leases(got.ticket["hub"], BAND, args)
            if cap == "echo.relay":
                sub = await self.request("core.call", {"ticket": got.ticket, "cap": "echo.say",
                                                       "target": "kaiju", "args": {"text": "inner"},
                                                       "why": "Relay a message."})
                if self.during_relay:
                    await self.during_relay(got.ticket)
                return {"inner": sub}
            raise RuntimeError(cap)
        out = await self.gate.execute(got, action)
        await self.to_hub(out)


@pytest.fixture
def hubs(tmp_path):
    """(start, stop) helpers around one hub with a loopback band."""
    state = {}

    async def start(cfg=None, worker=True, extra=None):
        band = Loopback()
        hub = Hub(cfg or config(tmp_path), transport=band)
        await hub.start()
        state["hub"] = hub
        token = (hub.data / "owner.token").read_text().strip() if (hub.data / "owner.token").exists() else None
        w = FakeWorker(hub, band, tmp_path / f"w{len(state)}", extra=extra) if worker else None
        return hub, w, token
    return start


def test_call_through_the_door_reaches_a_worker_and_is_journaled(hubs):
    async def go():
        hub, w, token = await hubs()
        try:
            await w.announce()
            await until(lambda: "echo.say" in hub.dispatcher.workers.get(WID, runtime.Worker("", "", "", {}, {})).caps)
            status, out = await http(hub, "POST", "/call", {"worker": "kaiju", "cap": "echo.say",
                                                             "args": {"text": "hello"}, "why": WHY}, token)
            assert status == 200 and out["ok"], out
            assert out["result"] == {"said": "hello", "person": "person:bake", "agent": None}
            # the worker verified the ticket: bound to this call, cap, args and caller
            t = w.verified[-1]
            assert (t["cap"], t["worker"], t["person"], t["why"]) == ("echo.say", WID, "person:bake", WHY)
            assert t["args_hash"] == wire.args_hash({"text": "hello"}) and t["call"] == out["id"]
            # echo isn't in the library: the hub learned it with core.manifests
            assert any(v["cap"] == "core.manifests" for v in w.verified)
            status, j = await http(hub, "GET", f"/journal?call={out['id']}", token=token)
            entry = j["entries"][0]
            assert (entry["decision"], entry["ok"], entry["ticket"]) == ("allow", True, [t["nonce"]])
            assert entry["chain"] == "general" and entry["hash"] == journal_mod.entry_hash(entry)
            chain = list(hub.journal.entries("general"))
            assert journal_mod.first_break(chain) is None and len(chain) >= 3
            assert hub.journal.check() == []
        finally:
            await hub.stop()
    run(go())


def test_door_auth_and_bad_requests(hubs):
    async def go():
        hub, _, token = await hubs(worker=False)
        try:
            assert (await http(hub, "GET", "/health"))[0] == 200
            assert (await http(hub, "GET", "/workers"))[0] == 401
            assert (await http(hub, "GET", "/workers", token="nope"))[0] == 401
            assert (await http(hub, "POST", "/call", raw=b"{not json", token=token))[0] == 400
            assert (await http(hub, "POST", "/call", raw=b'{"x": NaN}', token=token))[0] == 400
            assert (await http(hub, "POST", "/call", raw=b"[1]", token=token))[0] == 400
            assert (await http(hub, "GET", "/nowhere", token=token))[0] == 404
            assert (await http(hub, "GET", "/call", token=token))[0] == 405
            status, out = await http(hub, "POST", "/call", {"worker": "rook"}, token)
            assert status == 200 and "worker and cap" in out["error"]
            _, out = await http(hub, "POST", "/call", {"worker": "nobody", "cap": "x.y", "why": WHY}, token)
            assert out == {"id": out["id"], "ok": False, "error": "unknown worker: nobody"}
        finally:
            await hub.stop()
    run(go())



def test_an_unread_body_never_becomes_the_next_request(hubs):
    # GET /health answers 200 without reading its body: on a kept-alive
    # connection that body was parsed as a second request (smuggling
    # behind a shared proxy connection).
    import socket

    async def go():
        hub, _, _ = await hubs(worker=False)
        try:
            inner = b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n"
            req = b"GET /health HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n%s" % (len(inner), inner)

            def talk():
                with socket.create_connection(("127.0.0.1", hub.doors[0].port), timeout=5) as c:
                    c.sendall(req)
                    got = b""
                    try:
                        while chunk := c.recv(65536):
                            got += chunk
                    except socket.timeout:
                        pass
                    return got
            return await asyncio.to_thread(talk)
        finally:
            await hub.stop()
    got = run(go())
    assert got.count(b"HTTP/1.1 200") == 1, got

def test_why_rights_and_holds(hubs):
    async def go():
        hub, w, token = await hubs()
        try:
            await w.announce()
            await until(lambda: WID in hub.known and hub.known[WID].problem is None
                        and "echo.say" in hub.dispatcher.workers[WID].caps)
            _, out = await http(hub, "POST", "/call", {"worker": "kaiju", "cap": "echo.say",
                                                       "args": {"text": "x"}, "why": "  "}, token)
            assert out["error"] == "denied: why required"
            kid = hub.add_token(runtime.Caller(person="person:kid"))
            _, out = await http(hub, "POST", "/call", {"worker": "kaiju", "cap": "echo.say",
                                                       "args": {"text": "x"}, "why": WHY}, kid)
            assert out["error"].startswith("denied: person:")
            # an agent token with exec ceiling: root always runs elevated (upfront), so it
            # counts as admin at dispatch, past claude; an elevate the worker asks for
            # outside the agent's rights is refused and journaled too
            claude = hub.add_token(runtime.Caller(agent="agent:claude"))
            _, out = await http(hub, "POST", "/call", {"worker": "kaiju", "cap": "echo.root", "why": WHY}, claude)
            assert out == {"id": out["id"], "ok": False, "error": "denied: agent: risk"}, out
            # the owner may elevate: the worker gets a lease, journaled with the call
            _, out = await http(hub, "POST", "/call", {"worker": "kaiju", "cap": "echo.root", "why": WHY}, token)
            assert out["ok"] and out["elevated"] == out["result"]["lease"], out
            assert hub.journal.query(call=out["id"])[0]["lease"] == out["elevated"]
            # a sub-call through core.call runs as the parent's caller, in its session
            _, out = await http(hub, "POST", "/call", {"worker": "kaiju", "cap": "echo.relay", "why": WHY}, token)
            assert out["ok"] and out["result"]["inner"]["result"]["said"] == "inner", out
            inner = hub.journal.query(cap="echo.say", limit=1)[0]
            assert inner["person"] == "person:bake" and inner["why"] == "Relay a message."
        finally:
            await hub.stop()
    run(go())



def test_ending_a_session_drops_its_leases_on_the_worker(hubs):
    async def go():
        hub, w, token = await hubs()
        try:
            await w.announce()
            await until(lambda: "echo.root" in hub.dispatcher.workers.get(WID, runtime.Worker("", "", "", {}, {})).caps)
            _, s = await http(hub, "POST", "/session", {"caps": ["*"]}, token)
            _, out = await http(hub, "POST", "/call", {"worker": "kaiju", "cap": "echo.root", "why": WHY,
                                                       "session": s["session"]}, token)
            assert out["ok"] and out["elevated"] in w.gate.leases, out
            _, end = await http(hub, "POST", "/session/end", {"session": s["session"]}, token)
            assert end["ok"]
            await until(lambda: not w.gate.leases and hub.journal.query(cap="core.lease.drop", limit=1))
            assert hub.journal.query(cap="core.lease.drop", limit=1)[0]["agent"] == "system:hub"
        finally:
            await hub.stop()
    run(go())

def test_a_session_names_only_a_project_its_caller_may_see(hubs, tmp_path):
    # Naming a project opens its journal to the session's agent (11.4): the
    # hub checks access at session start (review H9).
    import shutil
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copytree(Path(runtime.__file__).parents[2] / "plugins" / "projects", lib / "projects")

    async def go():
        hub, _, token = await hubs(config(tmp_path, library=lib), worker=False)
        try:
            _, out = await http(hub, "POST", "/call", {"worker": "rook", "cap": "project.create", "why": WHY,
                                                       "args": {"title": "Castling", "slug": "castling"}}, token)
            assert out["ok"], out
            pid = out["result"]["id"]
            claude = hub.add_token(runtime.Caller(agent="agent:claude"))
            kid = hub.add_token(runtime.Caller(person="person:kid"))
            for project in ("castling", pid):
                _, out = await http(hub, "POST", "/session", {"caps": ["*"], "project": project}, claude)
                assert out["ok"], out
            _, out = await http(hub, "POST", "/session", {"caps": ["*"], "project": "secret-plans"}, claude)
            assert out == {"ok": False, "error": "denied: project: no project secret-plans"}
            _, out = await http(hub, "POST", "/session", {"caps": ["*"], "project": "castling"}, kid)
            assert out["error"] == "denied: project: you may not read projects"
            _, out = await http(hub, "POST", "/session", {"caps": ["*"], "project": ["castling"]}, claude)
            assert out["error"] == "denied: project: a project id or slug"
            # owners read the whole journal anyway: any project
            _, out = await http(hub, "POST", "/session", {"caps": ["*"], "project": "secret-plans"}, token)
            assert out["ok"], out
            # so an agent's journal view holds only projects it was let into
            _, out = await http(hub, "GET", "/journal?project=secret-plans", token=claude)
            assert out["ok"] and out["entries"] == []
        finally:
            await hub.stop()
    run(go())

    async def bare():      # no workbench: no projects to name
        hub, _, _ = await hubs(config(tmp_path / "bare"), worker=False)
        try:
            claude = hub.add_token(runtime.Caller(agent="agent:claude"))
            _, out = await http(hub, "POST", "/session", {"caps": ["*"], "project": "castling"}, claude)
            assert out["error"] == "denied: project: no project castling (no projects on this hub)"
            _, out = await http(hub, "POST", "/session", {"caps": ["*"]}, claude)
            assert out["ok"]
        finally:
            await hub.stop()
    run(bare())


def test_holds_wait_for_the_person(hubs, tmp_path):
    async def go():
        hub, w, token = await hubs(config(tmp_path, sacred=["kaiju"]))
        try:
            await w.announce()
            await until(lambda: "echo.root" in hub.dispatcher.workers.get(WID, runtime.Worker("", "", "", {}, {})).caps)
            _, out = await http(hub, "POST", "/call", {"worker": "kaiju", "cap": "echo.root", "why": WHY}, token)
            assert out["held"] and "sacred machine" in out["error"]
            _, holds = await http(hub, "GET", "/holds", token=token)
            assert [h["hold"] for h in holds["holds"]] == [out["hold"]]
            claude = hub.add_token(runtime.Caller(agent="agent:claude"))
            _, refused = await http(hub, "POST", f"/holds/{out['hold']}", {"decision": "approve"}, claude)
            assert "may not release" in refused["error"]
            _, done = await http(hub, "POST", f"/holds/{out['hold']}", {"decision": "approve"}, token)
            assert done["ok"] and done["result"]["lease"], done
            assert [(e["kind"], e["decision"]) for e in hub.journal.query(call=out["id"])] == [
                ("call", "released"), ("elevation", "allow"), ("call", "held")]
        finally:
            await hub.stop()
    run(go())


def test_first_run_state_survives_a_restart(hubs, tmp_path):
    async def go():
        hub, w, token = await hubs()
        hub_id, node = hub.hub_id, hub.node
        await w.announce()
        await until(lambda: "echo.say" in hub.dispatcher.workers.get(WID, runtime.Worker("", "", "", {}, {})).caps)
        await hub.stop()
        data = tmp_path / "data"
        for f in ("keys/root.key", "keys/op.key", "owner.token", "tokens.json", "hub.json", "workers.json"):
            assert stat.S_IMODE(os.stat(data / f).st_mode) == 0o600, f
        assert token not in (data / "tokens.json").read_text()

        hub = Hub(config(tmp_path), transport=Loopback())
        await hub.start()
        try:
            assert (hub.hub_id, hub.node) == (hub_id, node)
            status, out = await http(hub, "GET", "/caps?worker=kaiju", token=token)
            assert status == 200
            kaiju = out["workers"]["kaiju"]
            assert kaiju["online"] is False and kaiju["caps"]["echo.say"]["risk"] == "read"
            _, ws = await http(hub, "GET", "/workers", token=token)
            assert {x["name"]: x["online"] for x in ws["workers"]} == {"rook": True, "kaiju": False}
            assert hub.journal.check() == []
        finally:
            await hub.stop()
    run(go())


def test_presence_and_manifest_refresh(hubs, tmp_path):
    now = [time.time()]      # the worker checks tickets on the real clock

    async def go():
        hub, w, token = await hubs(worker=False)
        await hub.stop()
        hub = Hub(config(tmp_path), transport=(band := Loopback()), clock=lambda: now[0])
        await hub.start()
        w = FakeWorker(hub, band, tmp_path / "w")
        try:
            await w.announce()
            await until(lambda: WID in hub.known and hub.known[WID].online)
            now[0] += 91
            hub._liveness()
            assert hub.known[WID].online is False
            now[0] = time.time()     # back in step, or the worker refuses tickets from the future
            fetches = sum(t["cap"] == "core.manifests" for t in w.verified)
            await w.heartbeat()
            await until(lambda: hub.known[WID].online)
            assert sum(t["cap"] == "core.manifests" for t in w.verified) == fetches   # nothing changed
            await w.heartbeat(mhash="0" * 64)
            await until(lambda: sum(t["cap"] == "core.manifests" for t in w.verified) == fetches + 1)
            await until(lambda: hub.known[WID].problem is not None)
            assert hub.known[WID].problem == "manifests changed while the hub fetched them"
            await w.heartbeat(mhash="0" * 64)           # same hash again: not asked again
            await asyncio.sleep(0.3)
            assert sum(t["cap"] == "core.manifests" for t in w.verified) == fetches + 1
            # an incompatible core: shown, never called
            await w.announce(core="3.0")
            await until(lambda: hub.known[WID].core is None)
            assert hub.known[WID].problem.startswith("incompatible")
            _, out = await http(hub, "POST", "/call", {"worker": "kaiju", "cap": "echo.say",
                                                       "args": {"text": "x"}, "why": WHY}, token)
            assert out["error"] == "unknown worker: kaiju"
        finally:
            await hub.stop()
    run(go())


def test_hub_plugins_records_contracts_and_hooks(hubs, tmp_path):
    lib = tmp_path / "lib"
    plugin(lib, "notes", NOTES, {"notes.py": NOTES_PY})
    plugin(lib, "broken", "manifest: 1\nplugin: broken\n", {})

    async def go():
        hub, w, token = await hubs(config(tmp_path, library=lib))
        try:
            library = [n for n, p in hub.plugins.plugins.items() if not p.manifest.get("builtin")]
            assert library == ["notes"]       # broken was refused, the hub runs
            assert hub._installed()["notes"] == "1.0.0"     # what requires.plugins is checked against
            # Only defined contracts become grantable names (a plugin can't make one up).
            assert hub._contracted({"risk": "read", "contracts": ["notify.send", "made.up"]})["contracts"] == ["notify.send"]
            _, out = await http(hub, "POST", "/call", {"worker": "rook", "cap": "notes.note.create",
                                                       "args": {"text": "milk"}, "why": WHY}, token)
            assert out["ok"] and out["result"]["text"] == "milk", out
            _, out = await http(hub, "POST", "/call", {"worker": "rook", "cap": "notify.send",
                                                       "args": {"text": "eggs"}, "why": WHY}, token)
            assert out["ok"], out
            # A session for notify.send alone still reaches its provider (spec 8.2).
            _, s = await http(hub, "POST", "/session", {"caps": ["notify.send"]}, token)
            _, out = await http(hub, "POST", "/call", {"worker": "rook", "cap": "notify.send", "session": s["session"],
                                                       "args": {"text": "jam"}, "why": WHY}, token)
            assert out["ok"], out
            told = hub.journal.query(cap="notes.tell", limit=1)[0]
            assert told["contract"] == "notify.send" and told["session"] == s["session"]
            assert [e["cap"] for e in hub.journal.query(cap="notes.*")] == ["notes.tell", "notes.tell",
                                                                          "notes.note.create"]
            _, out = await http(hub, "POST", "/call", {"worker": "rook", "cap": "notes.count", "why": WHY}, token)
            assert out["result"] == {"count": 3}
            _, out = await http(hub, "POST", "/call", {"worker": "rook", "cap": "notes.note.create",
                                                       "args": {"txt": "x"}, "why": WHY}, token)
            assert out["error"].startswith("bad args"), out
            _, out = await http(hub, "POST", "/call", {"worker": "rook", "cap": "notes.quit", "why": WHY}, token)
            assert out["error"] == "notes.quit failed: SystemExit: 3" and hub.started
            _, caps = await http(hub, "GET", "/caps?worker=rook", token=token)
            assert caps["workers"]["rook"]["caps"]["notify.send"]["risk"] == "write"
            await w.announce()
            mod = next(m for m in hub.plugins._modules["notes"].values())
            await until(lambda: mod.SEEN == [("kaiju", 3)])     # on: worker.online hook ran, and called a cap
            hooked = hub.journal.query(cap="notes.count", limit=1)[0]
            assert (hooked["agent"], hooked["person"], hooked["ok"]) == ("system:plugin.notes", None, True)
        finally:
            await hub.stop()
    run(go())


def test_hub_caps_carry_the_parent_calls_layers_and_door(hubs, tmp_path):
    # A page that declared notes.relay only must not reach notes.count through
    # it, and closing the door the call came in by stops its sub-calls too
    # (review H5 hub caps, H10).
    lib = tmp_path / "lib"
    plugin(lib, "notes", NOTES, {"notes.py": NOTES_PY})
    bake = runtime.Caller(person="person:bake")

    async def go():
        hub, _, _ = await hubs(config(tmp_path, library=lib), worker=False)
        try:
            body = {"worker": "rook", "cap": "notes.relay", "why": WHY}
            out = await hub.call(bake, body, "web")
            assert out["ok"] and out["result"] == {"count": 0}, out
            out = await hub.call(bake, body, "web", layers=[["page", {"grants": ["notes.relay"]}]])
            assert "denied: page: grants" in out["error"], out
            inner = hub.journal.query(cap="notes.count", limit=1)[0]
            assert inner["error"] == "denied: page: grants"
            seen, dispatch = [], hub.dispatcher.dispatch

            async def spy(caller, call):
                seen.append((call.cap, call.door, [n for n, _ in call.layers]))
                return await dispatch(caller, call)
            hub.dispatcher.dispatch = spy
            out = await hub.call(bake, body, "mcp", layers=[["page", {"grants": ["notes.*"]}]])
            assert out["ok"], out
            assert seen == [("notes.relay", "mcp", ["page"]), ("notes.count", "mcp", ["page", "uses"])]
            # a hook (its own component session) is not narrowed by a call in flight
            assert (await hub.call_as(runtime.KitCaller(plugin="notes"), "notes.count", None))["ok"]
        finally:
            await hub.stop()
    run(go())


def test_a_token_taken_out_of_the_store_or_a_removed_agent_stops_working(hubs):
    # Review D9: reload only added tokens, and a deleted agent's token still
    # answered /workers and /caps.
    async def go():
        hub, _, owner = await hubs(worker=False)
        try:
            kid = hub.add_token(runtime.Caller(person="person:kid"))
            claude = hub.add_token(runtime.Caller(agent="agent:claude"))
            for t in (kid, claude):
                assert (await http(hub, "GET", "/workers", token=t))[0] == 200
            path = hub.data / "tokens.json"
            stored = json.loads(path.read_text())
            del stored[runtime.digest(kid)]
            path.write_text(json.dumps(stored))
            os.utime(path, (time.time() + 5, time.time() + 5))      # a new mtime, whatever the clock's grain
            assert (await http(hub, "GET", "/workers", token=kid))[0] == 401
            del hub.identities.agents["claude"]
            assert (await http(hub, "GET", "/workers", token=claude))[0] == 401
            assert (await http(hub, "GET", "/workers", token=owner))[0] == 200
        finally:
            await hub.stop()
    run(go())


def test_config_file_and_token_command(tmp_path, capsys):
    path = tmp_path / "hub.yaml"
    path.write_text(textwrap.dedent(f"""\
        data: ./d
        band: {BAND}
        users: {{ bake: {{ preset: owner }} }}
        agents: {{ claude: {{ grants: ["*"] }} }}
    """))
    cfg = load_config(path)
    assert cfg.data == tmp_path / "d" and cfg.doors == {"http": {"bind": "127.0.0.1:8740"}}
    assert main(["--config", str(path), "token", "--person", "bake", "--agent", "claude"]) == 0
    token = capsys.readouterr().out.strip()
    stored = json.loads((tmp_path / "d" / "tokens.json").read_text())
    assert token not in json.dumps(stored)
    assert list(stored.values())[0]["agent"] == "agent:claude"
    assert main(["--config", str(path), "token", "--person", "nobody"]) == 2

    path.write_text(f"data: ./d\nband: {BAND}\nusers: {{ kid: {{ preset: child }} }}\n")
    with pytest.raises(ValueError, match="owner"):
        load_config(path)
    path.write_text(f"data: ./d\nband: {BAND}\nusers: {{ bake: {{ preset: owner }} }}\nbogus: 1\n")
    with pytest.raises(ValueError, match="unknown keys: bogus"):
        load_config(path)
    assert main(["--config", str(path)]) == 2


def test_hub_announce_is_signed_and_accepted(hubs):
    async def go():
        hub, w, _ = await hubs()
        try:
            await w.announce()
            await until(lambda: w.announces >= 1)
            assert w.gate.lost(hub.hub_id, BAND) is False
        finally:
            await hub.stop()
    run(go())


def test_a_failed_start_leaves_nothing_open(tmp_path):
    async def go():
        hub = Hub(config(tmp_path, library=tmp_path / "missing"), transport=Loopback())
        with pytest.raises(ValueError, match="not a folder"):
            await hub.start()
        hub = Hub(config(tmp_path), transport=Loopback())   # the same data folder starts fine
        await hub.start()
        await hub.stop()
    run(go())


def test_announce_facts_are_bounded(hubs):
    async def go():
        hub, w, _ = await hubs()
        try:
            await w.announce(facts={"os": "linux", "is_hub": True, "grants": ["*"]})
            await until(lambda: WID in hub.known)
            assert hub.known[WID].facts == {"os": "linux"}
            await w.announce(facts={"blob": "x" * 2000})
            await until(lambda: hub.known[WID].facts == {})
        finally:
            await hub.stop()
    run(go())


def test_only_late_replies_to_our_own_calls_are_journaled(hubs):
    async def go():
        hub, w, token = await hubs()
        try:
            await w.announce()
            await until(lambda: "echo.say" in hub.dispatcher.workers.get(WID, runtime.Worker("", "", "", {}, {})).caps)
            hub._late["a" * 32] = WID
            await w.to_hub({"id": "b" * 32, "from": WID, "ok": True, "result": 1})      # never asked
            await w.to_hub({"id": "a" * 32, "from": "c" * 32, "ok": True, "result": 1})  # wrong worker
            await w.to_hub({"id": "a" * 32, "from": WID, "ok": False, "error": "slow"})
            await until(lambda: hub.journal.query(kind="late_reply"))
            await asyncio.sleep(0.1)
            assert [(e["call"], e["error"]) for e in hub.journal.query(kind="late_reply")] == [("a" * 32, "slow")]
        finally:
            await hub.stop()
    run(go())


def test_a_call_the_hub_gives_up_on_stops_being_resent(hubs):
    async def go():
        hub, w, _ = await hubs()
        forgotten = []
        hub.transport.forget = lambda band, rid: forgotten.append((band, rid))
        try:
            req = {"id": "d" * 32, "cap": "echo.say", "target": "e" * 32, "args": {}}     # nobody answers
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(hub._send(hub.band, req), 0.1)      # dispatch's timeout
            assert forgotten == [(hub.band, "d" * 32)] and hub._late["d" * 32] == "e" * 32
        finally:
            await hub.stop()
    run(go())


BOT = """\
manifest: 1
plugin: bot
version: 1.0.0
runs_on: hub
settings:
  token: { type: str }
routes:
  signed: { path: /signed, methods: [POST], run: scripts/bot.py:hook, auth: { hmac: "{{secret:BOT_KEY}}" } }
  tg: { path: /tg, methods: [POST], run: scripts/bot.py:hook,
        auth: { token: "{{settings.token}}", header: X-Telegram-Bot-Api-Secret-Token } }
  bearer: { path: /bearer, methods: [POST], run: scripts/bot.py:hook, auth: { token: "{{settings.token}}" } }
  bare: { path: /bare, methods: [POST], run: scripts/bot.py:hook, auth: token }
"""


def test_hmac_and_token_routes(hubs, tmp_path):
    import hashlib
    import hmac as hmac_
    lib = tmp_path / "lib"
    plugin(lib, "bot", BOT, {"bot.py": "def hook(ctx, request):\n    return {'got': request['body'].decode()}\n"})

    async def post(hub, path, body: bytes, headers: dict):
        def go():
            req = urllib.request.Request(f"http://127.0.0.1:{hub.doors[0].port}/p/bot{path}", data=body,
                                         method="POST", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        return await asyncio.to_thread(go)

    async def go():
        hub, _, _ = await hubs(config(tmp_path, library=lib, settings={"bot": {"token": "t0k"}}), worker=False)
        try:
            hub.vault.set("BOT_KEY", "k3y", by="person:bake")
            body = b'{"ref": "main"}'
            sig = "sha256=" + hmac_.new(b"k3y", body, hashlib.sha256).hexdigest()
            assert await post(hub, "/signed", body, {"X-Hub-Signature-256": sig}) == (200, {"got": body.decode()})
            assert (await post(hub, "/signed", body + b" ", {"X-Hub-Signature-256": sig}))[0] == 401
            assert (await post(hub, "/signed", body, {}))[0] == 401
            assert (await post(hub, "/signed", body, {"X-Hub-Signature-256": "sha256=\u00e9\u00e9"}))[0] == 401
            assert (await post(hub, "/tg", body, {"X-Telegram-Bot-Api-Secret-Token": "t0k"}))[0] == 200
            assert (await post(hub, "/tg", body, {"X-Telegram-Bot-Api-Secret-Token": "nope"}))[0] == 401
            assert (await post(hub, "/bearer", body, {"Authorization": "Bearer t0k"}))[0] == 200
            assert (await post(hub, "/bearer", body, {"Authorization": "Bearer t0"}))[0] == 401
            assert (await post(hub, "/bare", body, {}))[0] == 501
            hub.vault.delete("BOT_KEY")                     # an unset secret refuses everything
            assert (await post(hub, "/signed", body, {"X-Hub-Signature-256": sig}))[0] == 401
        finally:
            await hub.stop()
    run(go())


def test_hourly_check_runs_off_the_loop_and_prunes_sessions(hubs):
    async def go():
        hub, _, _ = await hubs(worker=False)
        try:
            ended = hub.dispatcher.start_session(runtime.Caller("person:bake"), ["*"])
            hub.dispatcher.end_session(ended.id)
            walked = []

            def slow_check():          # a long journal: seconds of walking
                time.sleep(0.5)
                walked.append(True)
                return []
            hub.journal.check = slow_check
            ticks = 0

            async def tick():
                nonlocal ticks
                while not walked:
                    ticks += 1
                    await asyncio.sleep(0.02)
            await asyncio.gather(hub._hourly(), tick())
            assert walked and ticks > 10          # the loop kept running while the chains were walked
            assert ended.id not in hub.dispatcher.sessions
        finally:
            await hub.stop()
    run(go())


# -- workers sign their requests (review H2) ----------------------------------------

class Attacker:
    """Another band member: holds the PSK, hears everything the hub sends
    (tickets included) and sends what it likes."""

    def __init__(self, band: Loopback):
        self.heard: list[dict] = []
        self.to_hub = band.attach(BAND, self.hear)
        self.key = SigningKey.generate()

    async def hear(self, msg):
        self.heard.append(msg)

    async def ask(self, req, timeout=5.0):
        start = len(self.heard)        # a replayed id was answered once already
        await self.to_hub(req)
        for _ in range(int(timeout / 0.02)):
            got = next((m for m in self.heard[start:] if m.get("id") == req["id"] and "ok" in m), None)
            if got:
                return got
            await asyncio.sleep(0.02)
        raise AssertionError("no reply")


def evil_call(hub, ticket, text="evil"):
    return {"id": secrets.token_hex(16), "cap": "core.call", "target": hub.node,
            "args": {"ticket": ticket, "cap": "echo.say", "target": "kaiju", "args": {"text": text},
                     "why": "Not the worker."}}


def test_h2_an_in_flight_ticket_cant_be_replayed_by_another_band_member(hubs):
    """castling/review.md H2: a PSK holder reads the ticket of a call in
    flight and sends its own core.call with it. Before worker keys the hub ran
    as the parent's person; now every attempt is refused."""
    async def go():
        hub, w, token = await hubs()
        evil = Attacker(hub.transport)
        tries = {}

        async def during(ticket):
            # 1. the bare bearer ticket, as in 2.0
            tries["bare"] = await evil.ask(evil_call(hub, ticket))
            tries["bare again"] = await evil.ask(evil_call(hub, ticket))
            # 2. signed with its own key, claiming to be the worker
            req = evil_call(hub, ticket)
            req["proof"] = wire.request_proof(evil.key, req, worker=WID, hub=hub.hub_id, band=BAND,
                                              nonce=secrets.token_hex(16), ts=int(time.time()))
            tries["forged"] = await evil.ask(req)
            # 3. as a machine of its own (its key pinned at its announce), with the worker's ticket
            other = "ab" * 16
            await evil.to_hub({"kind": "announce", "worker_id": other, "name": "evil", "core": "2.0",
                               "key": wire.pub_text(evil.key), "manifests": [],
                               "manifests_hash": runtime._entries_hash([])})
            await until(lambda: hub.worker_key(other))
            req = evil_call(hub, ticket)
            req["proof"] = wire.request_proof(evil.key, req, worker=other, hub=hub.hub_id, band=BAND,
                                              nonce=secrets.token_hex(16), ts=int(time.time()))
            tries["other worker"] = await evil.ask(req)
            # 4. the worker's own signed sub-call, sent again
            tries["replay"] = await evil.ask(w.sent[-1])
            # 5. the worker's proof on a request of its own
            req = evil_call(hub, ticket)
            req["proof"] = w.sent[-1]["proof"]
            tries["moved proof"] = await evil.ask(req)
        w.during_relay = during
        try:
            await w.announce()
            await until(lambda: "echo.relay" in hub.dispatcher.workers.get(WID, runtime.Worker("", "", "", {}, {})).caps)
            assert hub.worker_key(WID) == wire.pub_text(w.key)
            _, out = await http(hub, "POST", "/call", {"worker": "kaiju", "cap": "echo.relay", "why": WHY}, token)
            assert out["ok"] and out["result"]["inner"]["result"]["said"] == "inner", out
            errors = {k: (v["ok"], v["error"]) for k, v in tries.items()}
            assert errors == {
                "bare": (False, "denied: worker request: no_proof"),
                "bare again": (False, "denied: worker request: no_proof"),
                "forged": (False, "denied: worker request: unknown_key"),
                "other worker": (False, "denied: worker request: wrong_worker"),
                "replay": (False, "denied: worker request: replay"),
                "moved proof": (False, "denied: worker request: wrong_call"),
            }, errors
            # nothing ran for the attacker: only the worker's own sub-call reached it
            said = [t for t in w.verified if t["cap"] == "echo.say"]
            assert len(said) == 1
            # the real sub-call is journaled with the call it ran inside
            inner = hub.journal.query(cap="echo.say", limit=1)[0]
            assert inner["parent"] == out["id"] and inner["ok"]
        finally:
            await hub.stop()
    run(go())


def test_vault_references_reach_only_the_worker(hubs, tmp_path):
    cfg = Config(data=tmp_path / "data", band=BAND, users={"bake": {"preset": "owner"}, "ann": {"preset": "adult"}},
                 agents={"claude": {"grants": ["*"], "max_risk": "exec"}}, doors={"http": {"bind": "127.0.0.1:0"}})

    async def go():
        hub, w, token = await hubs(cfg)
        claude = hub.add_token(runtime.Caller(person="person:bake", agent="agent:claude"))
        ann = hub.add_token(runtime.Caller(person="person:ann"))
        try:
            await w.announce()
            await until(lambda: "echo.say" in hub.dispatcher.workers.get(WID, runtime.Worker("", "", "", {}, {})).caps)
            # secret.set: the value is sealed, journaled as [redacted]
            _, out = await http(hub, "POST", "/call", {"worker": "rook", "cap": "secret.set", "why": WHY, "args": {
                "name": "gh", "value": "ghp_v4lue", "description": "{{secret:gh}} stays text on the hub"}}, token)
            assert out["ok"] and out["result"]["name"] == "gh", out
            entry = hub.journal.query(call=out["id"])[0]
            assert entry["args"]["value"] == "[redacted]" and entry["args_hash"] == wire.args_hash(entry["args"])
            assert hub.vault.list()[0]["description"] == "{{secret:gh}} stays text on the hub"
            _, out = await http(hub, "POST", "/call", {"worker": "rook", "cap": "secret.list", "why": WHY}, claude)
            assert [s["name"] for s in out["result"]["secrets"]] == ["gh"] and "ghp_v4lue" not in json.dumps(out)

            # A reference: the worker gets the value; the caller and the journal never do.
            _, out = await http(hub, "POST", "/call", {"worker": "kaiju", "cap": "echo.say", "why": WHY,
                                                       "args": {"text": "Bearer {{secret:gh}}"}}, claude)
            assert out["ok"] and out["result"]["said"] == "Bearer [redacted]", out
            assert w.args_seen[-1] == {"text": "Bearer ghp_v4lue"}
            entry = hub.journal.query(call=out["id"])[0]
            assert entry["args"] == {"text": "Bearer {{secret:gh}}"} and "ghp_v4lue" not in json.dumps(entry)
            assert w.verified[-1]["args_hash"] == wire.args_hash({"text": "Bearer ghp_v4lue"})

            # Not an owner, an unknown name: refused before anything is sent.
            sent = len(w.args_seen)
            _, out = await http(hub, "POST", "/call", {"worker": "kaiju", "cap": "echo.say", "why": WHY,
                                                       "args": {"text": "{{secret:gh}}"}}, ann)
            assert not out["ok"] and "only an owner" in out["error"], out
            _, out = await http(hub, "POST", "/call", {"worker": "kaiju", "cap": "echo.say", "why": WHY,
                                                       "args": {"text": "{{secret:nope}}"}}, token)
            assert out["error"] == "secret: nope is not in the vault"
            _, out = await http(hub, "POST", "/call", {"worker": "rook", "cap": "secret.list", "why": WHY}, ann)
            assert not out["ok"] and "only an owner" in out["error"], out
            assert len(w.args_seen) == sent

            # reveal and delete are hard stops: only the person, there, answers.
            _, out = await http(hub, "POST", "/call", {"worker": "rook", "cap": "secret.reveal", "why": WHY,
                                                       "args": {"name": "gh"}}, claude)
            assert out["held"] and "reveals a secret" in out["error"], out
            _, refused = await http(hub, "POST", f"/holds/{out['hold']}", {"decision": "approve"}, claude)
            assert not refused["ok"]
            _, done = await http(hub, "POST", f"/holds/{out['hold']}", {"decision": "approve"}, token)
            assert done["ok"] and done["result"] == {"name": "gh", "value": "ghp_v4lue"}, done
            _, out = await http(hub, "POST", "/call", {"worker": "rook", "cap": "secret.delete", "why": WHY,
                                                       "args": {"name": "gh"}}, token)
            assert out["held"] and "destructive" in out["error"], out
            assert hub.vault.names() == ["gh"]
            _, done = await http(hub, "POST", f"/holds/{out['hold']}", {"decision": "approve"}, token)
            assert done["ok"] and hub.vault.names() == []
        finally:
            await hub.stop()
    run(go())


def test_worker_keys_are_pinned(hubs, tmp_path):
    async def go():
        hub, w, token = await hubs()
        try:
            # enrollment recorded a key: an announce with another one is refused
            enrolled = SigningKey.generate()
            hub.enroll.enrolled[WID] = {"name": "kaiju", "owner": "person:bake", "key": wire.pub_text(enrolled)}
            await w.announce()
            await asyncio.sleep(0.2)
            assert WID not in hub.known
            w.key = enrolled
            await w.announce()
            await until(lambda: WID in hub.known)
            pinned = hub.journal.query(kind="worker_key", viewer={"owner": True}, limit=5)
            assert [(e["worker"], e["via"], e["key"]) for e in pinned] == \
                [(WID, "enrollment", wire.key_id(wire.pub_text(enrolled)))]
            # once pinned, another key is refused: the name and manifests stay the worker's
            w.key = SigningKey.generate()
            await w.announce(name="impostor", manifests=[], manifests_hash=runtime._entries_hash([]))
            await asyncio.sleep(0.2)
            assert hub.known[WID].name == "kaiju" and hub.known[WID].key == wire.pub_text(enrolled)
            # an announce with no key (older code) doesn't unpin it
            w.key = None
            await w.announce()
            await asyncio.sleep(0.1)
            assert hub.worker_key(WID) == wire.pub_text(enrolled)
            assert (hub.data / "workers.json").read_text().count(wire.pub_text(enrolled)) == 1
            bad = {"kind": "announce", "worker_id": "cd" * 16, "name": "x", "core": "2.0", "key": "ed25519:nope",
                   "manifests": [], "manifests_hash": runtime._entries_hash([])}
            await w.to_hub(bad)
            await asyncio.sleep(0.1)
            assert "cd" * 16 not in hub.known
        finally:
            await hub.stop()
    run(go())


def test_keep_alive_requests_do_not_wait_for_delayed_acks(hubs):
    """The door writes headers and body separately; with Nagle on, each reply
    on a reused connection waited ~40 ms for the client's delayed ACK."""
    import http.client
    import statistics

    async def go():
        hub, _, _ = await hubs(worker=False)
        try:
            def timed():
                c = http.client.HTTPConnection("127.0.0.1", hub.doors[0].port, timeout=10)
                ms = []
                for _ in range(40):
                    t = time.monotonic()
                    c.request("GET", "/health")
                    c.getresponse().read()
                    ms.append((time.monotonic() - t) * 1000)
                c.close()
                return statistics.median(ms[5:])
            assert await asyncio.to_thread(timed) < 20
        finally:
            await hub.stop()
    run(go())


def test_the_vault_survives_a_restart_and_a_foreign_one_stays_locked(hubs, tmp_path):
    async def go():
        hub, _, _ = await hubs(worker=False)
        hub.vault.set("k", "v", by="person:bake")
        await hub.stop()
        hub, _, _ = await hubs(worker=False)
        try:
            assert hub.vault.get("k") == "v"
        finally:
            await hub.stop()
        other = tmp_path / "other"
        (other / "data").mkdir(parents=True)
        (other / "data" / "vault.db").write_bytes((tmp_path / "data" / "vault.db").read_bytes())
        hub, _, token = await hubs(config(other), worker=False)
        try:                              # the hub runs; the vault waits for recover-vault
            assert hub.vault.locked and hub.vault.names() == ["k"]
            _, out = await http(hub, "POST", "/call", {"worker": "rook", "cap": "secret.set", "why": WHY,
                                                       "args": {"name": "x", "value": "y"}}, token)
            assert not out["ok"] and "locked" in out["error"], out
        finally:
            await hub.stop()
    run(go())


def test_sub_calls_per_call_are_capped(hubs, monkeypatch):
    from rook.core2.hub import dispatch as dispatch_mod
    monkeypatch.setattr(dispatch_mod, "SUB_CALLS", 1)

    async def go():
        hub, w, token = await hubs()
        more = []

        async def again(ticket):
            for _ in range(2):
                more.append(await w.request("core.call", {"ticket": ticket, "cap": "echo.say", "target": "kaiju",
                                                          "args": {"text": "more"}, "why": "Once more."}))
        w.during_relay = again
        try:
            await w.announce()
            await until(lambda: "echo.relay" in hub.dispatcher.workers.get(WID, runtime.Worker("", "", "", {}, {})).caps)
            _, out = await http(hub, "POST", "/call", {"worker": "kaiju", "cap": "echo.relay", "why": WHY}, token)
            assert out["ok"] and out["result"]["inner"]["ok"]
            assert [m["error"] for m in more] == ["denied: core.call: more than 1 sub-calls in one call"] * 2
            limit = hub.journal.query(kind="sub_call_limit", viewer={"owner": True}, limit=5)
            assert len(limit) == 1 and limit[0]["call"] == out["id"]
            assert hub.dispatcher.sub_calls == {}       # forgotten when the call ends
        finally:
            await hub.stop()
    run(go())


CREW = """\
manifest: 1
plugin: crew
version: 1.0.0
doc: Runs agents of its own (like the agents runtime).
runs_on: hub
uses: [notify.send]
caps:
  hire:
    doc: Make or change one of its agents.
    risk: admin
    scope: multi-user
    run: scripts/crew.py:hire
    args:
      name: { type: str, required: true }
      grants: { type: list, items: { type: str } }
  count:
    doc: Something an agent may be granted.
    risk: read
    scope: self
    run: scripts/crew.py:count
  token:
    doc: A turn token (tests only hand it back).
    risk: read
    scope: self
    run: scripts/crew.py:token
    args:
      name: { type: str, required: true }
  think:
    doc: A turn as the agent through one provider of a contract.
    risk: write
    scope: self
    run: scripts/crew.py:think
    args:
      name: { type: str, required: true }
      provider: { type: str, required: true }
"""

CREW_PY = """\
from rook.kit.ctx import Caller

def hire(ctx, name, grants=None):
    ctx.run_agent(name, grants)
    return {"ok": True}

def count(ctx):
    return {"person": ctx.person, "agent": ctx.agent}

def token(ctx, name):
    return ctx.turn_token(name, 60)

async def think(ctx, name, provider):
    who = Caller(person=ctx.person, agent=f"agent:{name}", band=ctx.band, why=f"{name} thinks")
    return await ctx.call_as(who, "notify.send", {"text": "from " + name}, provider=provider)
"""


def test_rook_run_agents_have_their_own_identity_and_turn_tokens(hubs, tmp_path):
    # Drift #4 (castling-protocol): agents and people each have their own
    # identity; acting for a person = what both may; alone = its own grants.
    lib = tmp_path / "lib"
    plugin(lib, "notes", NOTES, {"notes.py": NOTES_PY})
    plugin(lib, "crew", CREW, {"crew.py": CREW_PY})
    now = [time.time()]

    async def go():
        hub = Hub(config(tmp_path, library=lib), transport=Loopback(), clock=lambda: now[0])
        await hub.start()
        owner = (hub.data / "owner.token").read_text().strip()

        async def call(token, cap, **args):
            return (await http(hub, "POST", "/call", {"worker": "rook", "cap": cap, "args": args, "why": WHY},
                               token))[1]
        try:
            out = await call(owner, "crew.hire", name="scribe", grants=["crew.count", "notify.send"])
            assert out["ok"], out
            assert hub.identities.agents["scribe"] == {"grants": ["crew.count", "notify.send"]}
            assert hub.identities.run_by == {"scribe": "crew"}
            note = hub.journal.query(kind="agent", limit=1)[0]
            assert (note["by"], note["agent"], note["grants"]) == ("system:plugin.crew", "agent:scribe",
                                                                   ["crew.count", "notify.send"])
            # Never a hub.yaml agent's name, never malformed grants.
            for name, grants, error in (("claude", ["*"], "configured in hub.yaml"), ("x", [""], "malformed")):
                out = await call(owner, "crew.hire", name=name, grants=grants)
                assert error in out["error"], out
            assert hub.identities.agents["claude"]["max_risk"] == "exec"
            # Only the plugin that runs it mints its turn tokens.
            with pytest.raises(runtime.KitError, match="doesn't run agent scribe"):
                hub.turn_token("notes", "person:bake", "scribe", 60)
            # A turn token: the agent, for the person whose call it came from.
            token = (await call(owner, "crew.token", name="scribe"))["result"]
            assert hub.authenticate(token) == runtime.Caller("person:bake", "agent:scribe")
            out = await call(token, "crew.count")
            assert out["result"] == {"person": "person:bake", "agent": "agent:scribe"}, out
            entry = hub.journal.query(cap="crew.count", limit=1)[0]
            assert (entry["person"], entry["agent"]) == ("person:bake", "agent:scribe")
            out = await call(token, "crew.hire", name="scribe", grants=["*"])     # its own grants hold
            assert out["error"] == "denied: agent: grants", out
            # For a child, only what both may (the child's preset refuses crew.count).
            kid = hub.turn_token("crew", "person:kid", "scribe", 60)
            assert (await call(kid, "crew.count"))["error"] == "denied: person: grants"
            # Alone: its own grants only.
            alone = hub.turn_token("crew", None, "scribe", 60)
            assert (await call(alone, "crew.count"))["result"] == {"person": None, "agent": "agent:scribe"}
            # A contract through one chosen provider: rights match the contract.
            out = await call(owner, "crew.think", name="scribe", provider="notes.tell")
            assert out["ok"], out
            told = hub.journal.query(cap="notes.tell", limit=1)[0]
            assert (told["contract"], told["agent"], told["person"]) == ("notify.send", "agent:scribe",
                                                                         "person:bake")
            out = await call(owner, "crew.think", name="scribe", provider="notes.count")
            assert "doesn't provide notify.send" in out["error"], out
            # Tokens end with the turn, expire, and die with the agent.
            hub.end_turn_token(kid)
            assert hub.authenticate(kid) is None
            assert (await http(hub, "GET", "/workers", token=alone))[0] == 200
            now[0] += 61
            assert hub.authenticate(alone) is None and hub.authenticate(token) is None
            now[0] -= 61
            assert hub.authenticate(token) is not None
            out = await call(owner, "crew.hire", name="scribe")          # no grants: archived
            assert out["ok"] and "scribe" not in hub.identities.agents
            assert hub.authenticate(token) is None
            assert (await http(hub, "GET", "/workers", token=token))[0] == 401


LAMP = """\
manifest: 1
plugin: lamp
version: 1.0.0
doc: A lamp on the LAN.
settings:
  cloud: { secret: true, where: hub, doc: The vendor cloud token. }
appliance:
  settings:
    key: { secret: true, doc: Written by lamp.pair. }
    level: { type: int, default: 50 }
  pair: lamp.pair
caps:
  pair:
    doc: Pair with it.
    risk: admin
    scope: local
    effects: [persists]
    run: scripts/lamp.py:pair
  on:
    doc: Switch it on.
    risk: write
    scope: local
    effects: [physical]
    run: scripts/lamp.py:on
"""


def test_appliances_are_onboarded_called_by_name_and_carry_their_settings(hubs, tmp_path):
    """castling-workers-appliances (Bake): "The agent only targets a name ... the
    hub looks up the appliance's profile, which records its proxy (delegate)
    route"; castling-manifest-appliance: "Each instance gets its own name and
    is called like a worker"; castling-enrollment: "Secrets: referenced only
    by vault name; the hub sends the value with each call; never written to
    worker disk." """
    seen = []

    def pair(msg):
        seen.append(msg)
        return {"paired": True, "settings": {"key": "pair-k3y"}}

    def on(msg):
        seen.append(msg)
        return {"carry": msg.get("carry")}

    cfg = Config(data=tmp_path / "data", band=BAND, users={"bake": {"preset": "owner"}, "ann": {"preset": "adult"}},
                 agents={"claude": {"grants": ["*"], "max_risk": "admin"}}, doors={"http": {"bind": "127.0.0.1:0"}})

    async def go():
        hub, w, token = await hubs(cfg, extra={LAMP: {"lamp.pair": pair, "lamp.on": on}})
        ann = hub.add_token(runtime.Caller(person="person:ann"))
        try:
            await w.announce()
            await until(lambda: "lamp.on" in hub.dispatcher.workers.get(WID, runtime.Worker("", "", "", {}, {})).caps)

            async def rc(cap, args=None, tok=token, worker="rook"):
                return (await http(hub, "POST", "/call", {"worker": worker, "cap": cap, "why": WHY,
                                                          "args": args or {}}, tok))[1]

            assert (await rc("secret.set", {"name": "lamp-cloud", "value": "cl0ud-v4lue"}))["ok"]
            out = await rc("setting.set", {"plugin": "lamp", "key": "cloud", "value": "{{secret:lamp-cloud}}"})
            assert out["ok"] and out["result"]["value"] == "lamp-cloud", out
            out = await rc("setting.set", {"plugin": "lamp", "key": "cloud", "value": "not a name!"})
            assert not out["ok"] and "vault name" in out["error"]

            out = await rc("appliance.add", {"name": "den-lamp", "plugin": "echo", "worker": "kaiju"})
            assert not out["ok"] and "no appliance section" in out["error"]
            out = await rc("appliance.add", {"name": "den-lamp", "plugin": "lamp", "worker": "kaiju"}, tok=ann)
            assert not out["ok"] and "only an owner" in out["error"]
            out = await rc("appliance.add", {"name": "den-lamp", "plugin": "lamp", "worker": "kaiju",
                                             "host": "192.168.1.9", "settings": {"level": 80}})
            assert out["ok"] and out["result"]["added"] and out["result"]["paired"] == {"paired": True}, out
            assert "pair-k3y" not in json.dumps(out)
            assert seen[0]["cap"] == "lamp.pair" and seen[0]["appliance"] == "den-lamp"
            assert hub.vault.get("appliance.den-lamp.key") == "pair-k3y"
            rec = hub.appliances.get("den-lamp")
            assert rec["delegates"] == [WID] and rec["settings"] == {"level": 80, "key": "appliance.den-lamp.key"}

            # Called by name, like a worker: the delegate gets the instance and settings, resolved.
            out = await rc("lamp.on", worker="den-lamp")
            assert out["ok"], out
            req = seen[-1]
            assert req["target"] == WID and req["appliance"] == "den-lamp"
            assert req["carry"] == {"settings": {"cloud": "cl0ud-v4lue"},
                                    "instance": {"name": "den-lamp", "host": "192.168.1.9",
                                                 "settings": {"level": 80, "key": "pair-k3y"}}}
            t = w.verified[-1]
            assert t["appliance"] == "den-lamp" and t["carry_hash"] == wire.args_hash(req["carry"])
            # values never come back to the caller nor reach the journal
            assert "cl0ud-v4lue" not in json.dumps(out) and "pair-k3y" not in json.dumps(out)
            assert out["result"]["carry"]["settings"]["cloud"] == "[redacted]"
            entry = hub.journal.query(call=out["id"])[0]
            assert entry["appliance"] == "den-lamp" and entry["worker"] == WID
            assert "cl0ud-v4lue" not in json.dumps(hub.journal.query()) and "pair-k3y" not in json.dumps(hub.journal.query())

            # Only its plugin's caps; the delegate's own caps stay its own.
            out = await rc("echo.say", {"text": "hi"}, worker="den-lamp")
            assert out["error"] == "unknown capability: echo.say"
            # Listing shows vault names, never values.
            out = await rc("appliance.list")
            [a] = out["result"]["appliances"]
            assert a["delegates"] == [{"id": WID, "name": "kaiju", "online": True}]
            assert "pair-k3y" not in json.dumps(out)

            # A secret that left the vault fails the call before anything is sent.
            hub.vault.delete("lamp-cloud")
            n = len(seen)
            out = await rc("lamp.on", worker="den-lamp")
            assert not out["ok"] and "lamp-cloud is not in the vault" in out["error"], out
            assert len(seen) == n

            out = await rc("appliance.remove", {"name": "den-lamp"})
            assert out["ok"]
            assert (await rc("lamp.on", worker="den-lamp"))["error"] == "unknown worker: den-lamp"
            assert "pair-k3y" not in (hub.data / "appliances.json").read_text()
        finally:
            await hub.stop()
    run(go())
