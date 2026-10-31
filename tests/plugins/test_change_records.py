"""Change records (rook/plugins/changes) on a real 0.5 hub, in process with a
loopback band, driven through the HTTP door: the ceremony each trust level
asks for, standard changes run, tested and rolled back by the hub (a failed
test rolls back on its own), risky ones held for one owner card, reverts,
steps running as their author, recovery after a restart, and plugin pushes
refused without the plugin.

A ``probe`` hub plugin is the machine being changed: ``probe.set`` and
``probe.get`` keep values in its data dir, so the tests can see what a change
did and what its rollback undid. ``tell`` provides ``notify.send``. Every
test body runs under a hard timeout; HTTP requests have their own socket
timeout."""

import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from port_helpers import PLUGINS  # noqa: E402
from rook.core2.hub.identity import Caller  # noqa: E402
from rook.core2.hub.runtime import Config, Hub, Loopback  # noqa: E402
from rook.kit.ctx import KitError, load_run  # noqa: E402
from rook.kit.ctx import Caller as KitCaller  # noqa: E402
from rook.kit.manifest import check  # noqa: E402

BAND = "e17904e6ed4c664592f64f6f1d02c682"
WHY = "Check the change records plugin."
mod = sys.modules[load_run(PLUGINS / "changes", "scripts/changes.py:clean").__module__]

PROBE = """\
manifest: 1
plugin: probe
version: 1.0.0
doc: The machine the change tests change.
runs_on: hub
caps:
  set:
    doc: Set a value.
    risk: write
    scope: self
    on: hub
    run: scripts/probe.py:set_
    args: { key: { type: str, required: true }, value: { type: str, required: true } }
  get:
    doc: Read a value.
    risk: read
    scope: self
    on: hub
    run: scripts/probe.py:get
    args: { key: { type: str, required: true } }
  fail:
    doc: Answer ok false, like shell.exec with a non-zero exit.
    risk: write
    scope: self
    on: hub
    run: scripts/probe.py:fail
  slow:
    doc: Take a while.
    risk: write
    scope: self
    on: hub
    timeout: 60s
    run: scripts/probe.py:slow
    args: { key: { type: str, required: true }, value: { type: str, required: true } }
  wipe:
    doc: A hard stop of its own.
    risk: write
    scope: self
    effects: [destructive]
    on: hub
    run: scripts/probe.py:wipe
"""

PROBE_PY = """\
import asyncio, json

def _load(ctx):
    f = ctx.data / "state.json"
    return json.loads(f.read_text()) if f.exists() else {}

def set_(ctx, key, value):
    s = _load(ctx)
    s[key] = value
    s.setdefault("_by", []).append([key, value, ctx.person, ctx.agent, ctx.why])
    (ctx.data / "state.json").write_text(json.dumps(s))
    return {"ok": True, "key": key, "value": value}

def get(ctx, key):
    return {"value": _load(ctx).get(key)}

def fail(ctx):
    return {"ok": False, "code": 1, "stderr": "nope"}

def wipe(ctx):
    s = _load(ctx)
    s["wiped"] = True
    (ctx.data / "state.json").write_text(json.dumps(s))
    return {"wiped": True}

async def slow(ctx, key, value):
    set_(ctx, key, value)
    await asyncio.sleep(30)
    return {"ok": True}
"""

TELL = """\
manifest: 1
plugin: tell
version: 1.0.0
doc: A notify.send provider that writes to a file.
runs_on: hub
caps:
  send:
    doc: Write the message down.
    risk: write
    scope: self
    on: hub
    run: scripts/tell.py:send
    args:
      text: { type: str, required: true }
      to: { type: str }
      title: { type: str }
provides:
  notify.send: tell.send
"""

TELL_PY = """\
import json

def send(ctx, text, to=None, title=None):
    with open(ctx.data / "sent.jsonl", "a") as f:
        f.write(json.dumps({"text": text, "to": to, "title": title}) + "\\n")
    return {"sent": True}
"""

FULL = {"risk": "The probe value changes for everyone reading it.",
        "plan": "Set x to 2 with probe.set, then read it back; set it to 1 to undo."}


def plugin(lib: Path, name: str, text: str, scripts: dict) -> None:
    (lib / name / "scripts").mkdir(parents=True)
    (lib / name / "plugin.yaml").write_text(text)
    for f, body in scripts.items():
        (lib / name / "scripts" / f).write_text(body)


@pytest.fixture
def cfg(tmp_path):
    lib = tmp_path / "lib"
    plugin(lib, "probe", PROBE, {"probe.py": PROBE_PY})
    plugin(lib, "tell", TELL, {"tell.py": TELL_PY})
    for name in ("changes", "projects"):
        (lib / name).symlink_to(PLUGINS / name, target_is_directory=True)
    return Config(data=tmp_path / "data", band=BAND, library=lib,
                  users={"bake": {"preset": "owner"}, "amy": {"preset": "adult"}, "pat": {"preset": "adult"}},
                  agents={"helper": {"grants": ["change.*", "probe.get"]}},
                  settings={"change": {"rejoin": "1s"}},
                  contracts={"notify.send": "tell.send"}, doors={"http": {"bind": "127.0.0.1:0"}})


class Door:
    def __init__(self, hub: Hub, token: str):
        self.hub, self.token = hub, token

    async def request(self, method: str, path: str, body: dict | None = None):
        def go():
            req = urllib.request.Request(f"http://127.0.0.1:{self.hub.doors[0].port}{path}", method=method,
                                         data=None if body is None else json.dumps(body).encode())
            req.add_header("Authorization", f"Bearer {self.token}")
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                return json.loads(e.read())
        return await asyncio.to_thread(go)

    async def call(self, cap: str, **args):
        return await self.request("POST", "/call", {"worker": "rook", "cap": cap, "args": args, "why": WHY})

    async def ok(self, cap: str, **args):
        out = await self.call(cap, **args)
        assert out["ok"], out
        return out["result"]


class Rig:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.events: list = []

    async def start(self) -> "Rig":
        self.hub = Hub(self.cfg, transport=Loopback())
        self.hub.listeners.append(lambda e, p, s: self.events.append((e, p)))
        await self.hub.start()
        self.bake = Door(self.hub, (self.hub.data / "owner.token").read_text().strip())
        self.amy = Door(self.hub, self.hub.add_token(Caller(person="person:amy"), label="amy"))
        self.pat = Door(self.hub, self.hub.add_token(Caller(person="person:pat"), label="pat"))
        self.helper = Door(self.hub, self.hub.add_token(Caller(agent="agent:helper"), label="helper"))
        return self

    async def stop(self) -> None:
        await self.hub.stop()

    def state(self) -> dict:
        f = self.hub.data / "plugins" / "probe" / "state.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def sent(self) -> list[dict]:
        f = self.hub.data / "plugins" / "tell" / "sent.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []

    def change(self, cid: str) -> dict:
        return self.hub.records.type("change.change").get(KitCaller(band=BAND, owner=True), cid)

    def calls(self, cap: str) -> list[dict]:
        return list(reversed(self.hub.journal.query(cap=cap, limit=500)))


def S(cap, **args):
    return {"worker": "rook", "cap": f"probe.{cap}", "args": args}


def change(**kw):
    out = {"what": "Set the probe's x to 2", "why": "The x we need is 2.", **FULL,
            "scripts": [S("set", key="x", value="2")],
            "tests": [{**S("get", key="x"), "expect": '"2"'}],
            "rollback": S("set", key="x", value="1"), **kw}
    return {k: v for k, v in out.items() if v is not None}


async def until(pred, timeout=10.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = pred()
        if got:
            return got
        await asyncio.sleep(0.03)
    raise AssertionError(f"{what} not met in {timeout}s")


def run(body, timeout=60):
    async def main():
        return await asyncio.wait_for(body(), timeout)
    return asyncio.run(main())


def ended(rig, cid):
    return lambda: rig.change(cid)["state"] in mod.TERMINAL and rig.change(cid)


def on_rig(cfg, body, timeout=60):
    async def main():
        rig = await Rig(cfg).start()
        try:
            await body(rig)
        finally:
            await rig.stop()
    run(main, timeout)


# -- the record, without a hub ---------------------------------------------------------------

def test_manifest_is_clean():
    raw, issues = check(PLUGINS / "changes")
    assert raw is not None and not issues, [str(i) for i in issues]


def test_what_each_level_asks_for():
    rec = mod.clean(change())
    assert rec["machines"] == ["rook"] and rec["caps"] == ["probe.set", "probe.get"]
    assert mod.ceremony(rec, "normal") == mod.ceremony(rec, "sacred") == []
    bare = mod.clean({"what": "Set x", "why": "Because.", "scripts": [S("set", key="x", value="2")]})
    assert mod.ceremony(bare, "normal") == ["risk", "plan", "tests", "rollback"]
    assert mod.ceremony(bare, "ship-it") == []
    # a risky change may have no way back; atomic scripts test themselves
    assert mod.ceremony({**bare, **FULL, "kind": "risky"}, "normal") == ["tests"]
    atomic = mod.clean({**change(), "tests": None, "scripts": [{**S("set", key="x", value="2"), "atomic": True}]})
    assert mod.ceremony(atomic, "normal") == []
    assert mod.ceremony({**rec, "risk": "n/a"}, "normal") == ["risk"]
    for args, msg in ((change(scripts=[{"worker": "rook", "run": "make deploy"}]), "unknown run"),
                      (change(scripts=[{"worker": "rook", "cap": "Shell exec"}]), "a cap name"),
                      (change(tests=[{**S("get", key="x"), "atomic": True}]), "unknown atomic"),
                      (change(scripts=None), "go with scripts"),
                      (change(why="  "), "what and why"),
                      (change(scripts=[S("set", key="x", value="2")] * 21), "at most 20")):
        with pytest.raises(KitError, match=msg):
            mod.clean(args)


# -- on the hub ------------------------------------------------------------------------------

def test_a_standard_change_runs_as_its_author_and_is_kept(cfg):
    async def body(rig):
        rec = await rig.amy.ok("change.submit", **change(), wait=True)
        assert rec["state"] == "kept" and rec["outcome"] == "tests passed", rec
        assert rec["creator"] == rec["person"] == "person:amy" and rec["level"] == "normal"
        assert [(e["phase"], e["ok"]) for e in rec["log"]] == [("script", True), ("test", True)]
        assert rig.state()["x"] == "2"
        key, value, person, agent, why = rig.state()["_by"][0]
        assert (person, agent) == ("person:amy", None) and why == f"The x we need is 2. [change {rec['id']}]"
        entry = rig.calls("probe.set")[0]
        assert entry["person"] == "person:amy" and entry["why"] == why and entry["decision"] == "allow"
        # the record is the audit trail: band-wide and searchable
        assert [r["id"] for r in await rig.bake.ok("change.search", query="probe x")] == [rec["id"]]
        assert [r["id"] for r in await rig.pat.ok("change.list", where={"state": "kept"})] == [rec["id"]]
        assert not (await rig.amy.call("change.update", id=rec["id"], state="kept"))["ok"]   # only the plugin writes
        assert rig.sent() == []
    on_rig(cfg, body)


def test_a_failed_test_rolls_back_by_itself(cfg):
    async def body(rig):
        await rig.bake.ok("probe.set", key="x", value="1")
        rec = await rig.amy.ok("change.submit", **change(tests=[{**S("get", key="x"), "expect": '"3"'}]), wait=True)
        assert rec["state"] == "rolled_back", rec
        assert rec["outcome"].startswith("test 1 on rook failed (the result does not contain")
        assert [(e["phase"], e["ok"]) for e in rec["log"]] == [
            ("script", True), ("test", False), ("rollback", True), ("after", False)]
        assert rig.state()["x"] == "1"                      # the rollback put it back
        told = await until(rig.sent, what="the owners told")
        assert "rolled back" in told[0]["text"] and rec["id"] in told[0]["text"]
    on_rig(cfg, body)


def test_a_failed_script_rolls_back_unless_nothing_changed(cfg):
    async def body(rig):
        # the second script fails after the first changed things: roll back
        two = await rig.amy.ok("change.submit", **change(scripts=[S("set", key="x", value="2"), S("fail")]), wait=True)
        assert two["state"] == "rolled_back" and two["outcome"].startswith("script 2 on rook failed"), two
        assert rig.state()["x"] == "1"
        # a first script that fails may have run halfway: roll back too
        one = await rig.amy.ok("change.submit", **change(scripts=[S("fail")]), wait=True)
        assert one["state"] == "rolled_back", one
        # unless it is atomic (undoes itself), or never ran: then nothing changed
        atomic = await rig.amy.ok("change.submit", **change(scripts=[{**S("fail"), "atomic": True}]), wait=True)
        assert atomic["state"] == "refused" and atomic["outcome"].startswith("nothing changed: script 1"), atomic
        assert [e["phase"] for e in atomic["log"]] == ["script"]
        # helper may call change.* but not probe.set: dispatch refuses the step as helper
        denied = await rig.helper.ok("change.submit", **change(), wait=True)
        assert denied["state"] == "refused" and "denied" in denied["outcome"], denied
        assert [e["phase"] for e in denied["log"]] == ["script"] and not denied["log"][0]["ran"]
        # a failing check changes nothing at all
        checked = await rig.amy.ok("change.submit", **change(checks=[S("fail")]), wait=True)
        assert checked["state"] == "refused" and checked["outcome"].startswith("nothing changed: check 1"), checked
        assert rig.sent() and all("A change did not stick" == m["title"] for m in rig.sent())
    on_rig(cfg, body)


def test_a_narrow_session_stays_narrow_through_a_change(cfg):
    async def body(rig):
        s = await rig.amy.request("POST", "/session", {"caps": ["change.*", "probe.get"]})
        out = await rig.amy.request("POST", "/call", {"worker": "rook", "cap": "change.submit", "why": WHY,
                                                      "session": s["session"], "args": {**change(), "wait": True}})
        rec = out["result"]
        assert rec["state"] == "refused" and "denied: session" in rec["log"][0]["error"], rec
        assert rig.state() == {}
        # her own door session allows probe.set: the same change runs
        assert (await rig.amy.ok("change.submit", **change(), wait=True))["state"] == "kept"
    on_rig(cfg, body)


def test_ceremony_follows_the_projects_trust_level(cfg):
    async def body(rig):
        bare = {"what": "Set x", "why": "Because x.", "scripts": [S("set", key="x", value="5")]}
        out = await rig.amy.call("change.submit", **bare)
        assert not out["ok"] and "the record needs risk, plan, tests, rollback (the project is normal)" in out["error"]
        quick = await rig.bake.ok("project.create", title="Quick", slug="quick")
        careful = await rig.bake.ok("project.create", title="Careful", slug="careful")
        await rig.bake.ok("project.trust.create", project=quick["id"], level="ship-it")
        await rig.bake.ok("project.trust.create", project=careful["id"], level="sacred")
        # ship-it: what and why are enough, and it runs
        rec = await rig.amy.ok("change.submit", **bare, project="quick", wait=True)
        assert rec["state"] == "kept" and rec["level"] == "ship-it" and rig.state()["x"] == "5", rec
        # a change made by hand: just the record
        note = await rig.amy.ok("change.submit", what="Rebooted the router", why="It hung.", project="quick")
        assert note["state"] == "noted" and note["outcome"] == "recorded" and note["machines"] == []
        # sacred: the full record, and even a standard change waits for the card
        sac = await rig.amy.ok("change.submit", **change(), project=careful["id"])
        assert sac["state"] == "asked" and sac["level"] == "sacred" and sac["hold"].startswith("h_"), sac
        out = await rig.amy.call("change.submit", **change(), project="nowhere")
        assert not out["ok"] and "no project nowhere" in out["error"]
    on_rig(cfg, body)


def test_a_risky_change_waits_for_one_owner_card(cfg):
    async def body(rig):
        rec = await rig.amy.ok("change.submit", **change(kind="risky", rollback=None), wait=True)
        assert rec["state"] == "asked" and rig.state() == {}, rec
        hold = rec["hold"]
        card = next(p for e, p in rig.events if e == "hold.created" and p["hold"] == hold)
        assert card["cap"] == "change.apply" and card["person"] is None and "destructive" in card["reasons"]
        assert card["why"] == "Change Set the probe's x to 2: The x we need is 2."
        # amy wrote it, but only an owner answers it
        assert not (await rig.amy.request("POST", f"/holds/{hold}", {"decision": "approve"}))["ok"]
        assert rig.change(rec["id"])["state"] == "asked"
        ok = await rig.bake.request("POST", f"/holds/{hold}", {"decision": "approve"})
        assert ok["ok"] and ok["result"] == {"id": rec["id"], "state": "running"}, ok
        done = await until(ended(rig, rec["id"]), what="the change to end")
        assert done["state"] == "kept" and done["by"] == "person:bake" and rig.state()["x"] == "2"
        assert rig.state()["_by"][0][2] == "person:amy"       # still runs as its author
        # refused: declined, nothing ran
        no = await rig.amy.ok("change.submit", **change(kind="risky", scripts=[S("set", key="y", value="9")]))
        assert (await rig.bake.request("POST", f"/holds/{no['hold']}", {"decision": "refuse"}))["ok"]
        gone = await until(ended(rig, no["id"]), what="declined")
        assert gone["state"] == "declined" and gone["by"] == "person:bake" and "y" not in rig.state()
    on_rig(cfg, body)


def test_apply_runs_only_for_the_plugin(cfg):
    async def body(rig):
        rec = await rig.amy.ok("change.submit", **change(kind="risky"))
        direct = await rig.bake.call("change.apply", id=rec["id"])
        assert direct.get("held"), direct
        out = await rig.bake.request("POST", f"/holds/{direct['hold']}", {"decision": "approve"})
        assert not out["ok"] and "only the change plugin applies a change" in out["error"], out
        assert rig.change(rec["id"])["state"] == "asked" and rig.state() == {}
    on_rig(cfg, body)


def test_revert_a_kept_change(cfg):
    async def body(rig):
        rec = await rig.amy.ok("change.submit", **change(), wait=True)
        assert rec["state"] == "kept"
        out = await rig.pat.call("change.revert", id=rec["id"])
        assert not out["ok"] and "only its author or an owner" in out["error"]
        back = await rig.amy.ok("change.revert", id=rec["id"])
        assert back["state"] == "reverted" and back["by"] == "person:amy" and rig.state()["x"] == "1"
        again = await rig.amy.call("change.revert", id=rec["id"])
        assert not again["ok"] and "only a kept change" in again["error"]
    on_rig(cfg, body)


def test_machines_must_be_on_the_band(cfg):
    async def body(rig):
        out = await rig.amy.call("change.submit", **change(scripts=[{"worker": "nas", "cap": "shell.exec"}]))
        assert not out["ok"] and "nas is not on the band" in out["error"]
    on_rig(cfg, body)


def test_a_held_step_waits_for_the_persons_answer(cfg):
    """A step that is a hard stop (a destructive cap here; a sacred machine
    the same) holds that exact call for the person; the change waits."""
    async def body(rig):
        def waiting(cid):
            return lambda: [e for e in rig.change(cid)["log"] if e.get("hold") and e["ok"] is None]

        rec = await rig.amy.ok("change.submit", **change(scripts=[S("set", key="x", value="2"), S("wipe")]))
        hold = (await until(waiting(rec["id"]), what="the held step"))[0]["hold"]
        card = next(p for e, p in rig.events if e == "hold.created" and p["hold"] == hold)
        assert card["cap"] == "probe.wipe" and card["person"] == "person:amy"
        assert rig.change(rec["id"])["state"] == "running" and "wiped" not in rig.state()
        assert (await rig.amy.request("POST", f"/holds/{hold}", {"decision": "approve"}))["ok"]
        done = await until(ended(rig, rec["id"]), what="the change to end")
        assert done["state"] == "kept" and rig.state()["wiped"] is True, done
        assert done["log"][1]["by"] == "person:amy" and done["log"][1]["ok"]
        # refused: that step never ran, but the first did: roll back
        no = await rig.amy.ok("change.submit", **change(scripts=[S("set", key="x", value="3"), S("wipe")]))
        hold = (await until(waiting(no["id"]), what="the second held step"))[0]["hold"]
        # (a hard stop on amy's call: only amy answers it, not even an owner)
        assert not (await rig.bake.request("POST", f"/holds/{hold}", {"decision": "refuse"}))["ok"]
        assert (await rig.amy.request("POST", f"/holds/{hold}", {"decision": "refuse"}))["ok"]
        back = await until(ended(rig, no["id"]), what="the rollback")
        assert back["state"] == "rolled_back" and f"hold {hold} refused by person:amy" in back["outcome"], back
        assert rig.state()["x"] == "1"
    on_rig(cfg, body)


def test_after_a_restart(cfg):
    async def body():
        rig = await Rig(cfg).start()
        try:
            asked = await rig.amy.ok("change.submit", **change(kind="risky"))
            cut = await rig.amy.ok("change.submit", **change(scripts=[S("slow", key="x", value="7")]))
            await until(lambda: rig.state().get("x") == "7", what="the slow script to start")
        finally:
            await rig.stop()
        rig = await Rig(cfg).start()
        try:
            # cut off mid-script: it may have run halfway, so the rollback runs
            back = await until(ended(rig, cut["id"]), what="the cut-off change settled")
            assert back["state"] == "rolled_back" and "restart" in back["outcome"] and rig.state()["x"] == "1"
            # its card was lost with the old hub's holds: asked again
            fresh = await until(lambda: (r := rig.change(asked["id"]))["hold"] != asked["hold"] and r,
                                what="a new card")
            assert fresh["state"] == "asked" and fresh["hold"] in rig.hub.dispatcher.holds
        finally:
            await rig.stop()
    run(body)


def test_pushes_need_the_change_plugin(cfg, tmp_path):
    (cfg.library / "changes").unlink()

    async def body(rig):
        out = await rig.hub.push_plugin(Caller(person="person:bake"), {"worker": "x", "plugin": "probe", "why": WHY},
                                        "http")
        assert not out["ok"] and "plugin pushes go through change records" in out["error"]
    on_rig(cfg, body)
