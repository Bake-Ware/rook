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

    def __init__(self, hub: Hub, band: Loopback, tmp: Path, core: str = "2.0"):
        self.hub, self.core = hub, core
        folder = plugin(tmp, "echo", ECHO, {"echo.py": "def say(ctx, text): return text\n"})
        self.echo_raw, _ = check(folder)
        self.core_raw, _ = check(CORE)
        anchor = tmp / "anchor.json"
        anchor.write_text(json.dumps({"hub": hub.hub_id, "root": wire.pub_text(hub.root), "band": BAND}))
        caps = {n: {"risk": m["risk"], "effects": m["effects"], "elevation": m["elevation"]}
                for raw in (self.core_raw, self.echo_raw) for n, m in normalize(raw)["caps"].items()
                if m.get("on") == "worker"}
        self.gate = WorkerGate(WID, "kaiju", TrustStore(anchor, None, WID),
                               ReplayStore(tmp / "replay.txt"), caps, ask=self.ask)
        self.to_hub = band.attach(BAND, self.handle)
        self.pending: dict[str, asyncio.Future] = {}
        self.verified: list[dict] = []
        self.refused: list[dict] = []
        self.announces = 0

    def entries(self):
        return sorted(({"plugin": r["plugin"], "version": r["version"], "hash": wire.manifest_hash(r),
                        **({"builtin": True} if r.get("builtin") else {})}
                       for r in (self.core_raw, self.echo_raw)), key=lambda e: e["plugin"])

    async def announce(self, **over):
        e = self.entries()
        await self.to_hub({"kind": "announce", "worker_id": WID, "name": "kaiju", "core": self.core,
                           "version": "1.0", "manifests": e,
                           "manifests_hash": runtime._entries_hash(e), **over})

    async def heartbeat(self, mhash=None):
        await self.to_hub({"kind": "heartbeat", "worker_id": WID, "core": self.core, "ts": 0,
                           "manifests_hash": mhash or runtime._entries_hash(self.entries())})

    async def ask(self, band, args):
        return await self.request("core.elevate", args)

    async def request(self, cap, args):
        rid = secrets.token_hex(16)
        fut = self.pending[rid] = asyncio.get_running_loop().create_future()
        await self.to_hub({"id": rid, "cap": cap, "target": self.hub.node, "args": args})
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

        async def action(lease):
            cap = got.ticket["cap"]
            if cap == "core.manifests":
                return {"manifests": [r for r in (self.core_raw, self.echo_raw)
                                      if not args.get("plugins") or r["plugin"] in args["plugins"]]}
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
                return {"inner": sub}
            raise RuntimeError(cap)
        await self.to_hub(await self.gate.execute(got, action))


@pytest.fixture
def hubs(tmp_path):
    """(start, stop) helpers around one hub with a loopback band."""
    state = {}

    async def start(cfg=None, worker=True):
        band = Loopback()
        hub = Hub(cfg or config(tmp_path), transport=band)
        await hub.start()
        state["hub"] = hub
        token = (hub.data / "owner.token").read_text().strip() if (hub.data / "owner.token").exists() else None
        w = FakeWorker(hub, band, tmp_path / f"w{len(state)}") if worker else None
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


def test_hmac_and_token_routes(hubs, tmp_path, monkeypatch):
    import hashlib
    import hmac as hmac_
    lib = tmp_path / "lib"
    plugin(lib, "bot", BOT, {"bot.py": "def hook(ctx, request):\n    return {'got': request['body'].decode()}\n"})
    monkeypatch.setenv("ROOK_SECRET_BOT_KEY", "k3y")

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
            monkeypatch.delenv("ROOK_SECRET_BOT_KEY")      # an unset secret refuses everything
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
