"""Change records (rook/plugins/changes) on a real 0.5 hub, in process with a
loopback band, driven through the HTTP door: records, not a runner
(castling-changes, castling-caps C2). The ceremony each trust level asks
for, records that gate and run nothing, the kit's update/search/history, a
hub without the workbench, and core writing records only when the plugin is
there (``Hub.note_change``, plugin pushes). Every test body runs under a hard
timeout; HTTP requests have their own socket timeout."""

import asyncio
import json
import sys
import urllib.error
import urllib.request

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from port_helpers import PLUGINS  # noqa: E402
from rook.core2.hub.identity import Caller  # noqa: E402
from rook.core2.hub.runtime import Config, Hub, Loopback  # noqa: E402
from rook.kit.ctx import Caller as KitCaller, load_run  # noqa: E402
from rook.kit.manifest import check  # noqa: E402

BAND = "e17904e6ed4c664592f64f6f1d02c682"
WHY = "Check the change records plugin."
mod = sys.modules[load_run(PLUGINS / "changes", "scripts/changes.py:lacks").__module__]

FULL = {"risk": "The probe value changes for everyone reading it.",
        "testing": "Read x back with probe.get; it says 2.",
        "rollback": "Set x to 1 again with probe.set.",
        "steps": "Call probe.set with x = 2, then probe.get to check."}


def record(**kw):
    out = {"what": "Set the probe's x to 2", "why": "The x we need is 2.", **FULL, **kw}
    return {k: v for k, v in out.items() if v is not None}


@pytest.fixture
def cfg(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ("changes", "projects"):
        (lib / name).symlink_to(PLUGINS / name, target_is_directory=True)
    return Config(data=tmp_path / "data", band=BAND, library=lib,
                  users={"bake": {"preset": "owner"}, "amy": {"preset": "adult"}},
                  agents={"helper": {"grants": ["change.*"]}}, doors={"http": {"bind": "127.0.0.1:0"}})


class Door:
    def __init__(self, hub: Hub, token: str):
        self.hub, self.token = hub, token

    async def call(self, cap: str, **args):
        def go():
            req = urllib.request.Request(f"http://127.0.0.1:{self.hub.doors[0].port}/call", method="POST",
                                         data=json.dumps({"worker": "rook", "cap": cap, "args": args,
                                                          "why": WHY}).encode())
            req.add_header("Authorization", f"Bearer {self.token}")
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                return json.loads(e.read())
        return await asyncio.to_thread(go)

    async def ok(self, cap: str, **args):
        out = await self.call(cap, **args)
        assert out["ok"], out
        return out["result"]


def on_hub(cfg, body, timeout=60):
    async def main():
        hub = Hub(cfg, transport=Loopback())
        await hub.start()
        try:
            bake = Door(hub, (hub.data / "owner.token").read_text().strip())
            amy = Door(hub, hub.add_token(Caller(person="person:amy"), label="amy"))
            helper = Door(hub, hub.add_token(Caller(agent="agent:helper"), label="helper"))
            await body(hub, bake, amy, helper)
        finally:
            await hub.stop()
    asyncio.run(asyncio.wait_for(main(), timeout))


# -- the record, without a hub ---------------------------------------------------------------

def test_manifest_is_clean():
    raw, issues = check(PLUGINS / "changes")
    assert raw is not None and not issues, [str(i) for i in issues]
    # records, not a runner: none of the binned caps (castling-caps C2), no runner settings or hooks
    assert set(raw["caps"]) == {"create"} and not raw.get("settings") and not raw.get("hooks")
    assert "kind" not in raw["records"]["change"]["fields"]


def test_what_each_level_asks_for():
    assert mod.lacks(record(), "normal") == mod.lacks(record(), "sacred") == []
    bare = {"what": "Set x", "why": "Because."}
    assert mod.lacks(bare, "normal") == ["risk", "testing", "rollback", "steps"]
    assert mod.lacks(bare, "ship-it") == []
    assert mod.lacks(record(risk="n/a"), "normal") == ["risk"]


# -- on the hub ------------------------------------------------------------------------------

def test_a_record_is_written_and_gates_nothing(cfg):
    async def body(hub, bake, amy, helper):
        rec = await amy.ok("change.create", **record(workers=["rook"], caps=["probe.set"]))
        assert rec["level"] == "normal" and rec["creator"] == rec["person"] == "person:amy"
        assert rec["workers"] == ["rook"] and "state" not in rec and not hub.dispatcher.holds
        # the words afterwards; history keeps every revision
        done = await amy.ok("change.update", id=rec["id"], revision=rec["revision"], outcome="x is 2 now")
        assert done["outcome"] == "x is 2 now"
        assert [h["revision"] for h in await bake.ok("change.history", id=rec["id"])] == [1, 2]
        found = await helper.ok("change.search", query="probe")
        assert [r["id"] for r in found] == [rec["id"]]
        for cap in ("change.submit", "change.apply", "change.revert"):
            out = await bake.call(cap, id=rec["id"])
            assert not out["ok"] and "unknown capability" in out["error"], (cap, out)
        out = await amy.call("change.create", what="Set x", why="Because.")
        assert not out["ok"] and "the record needs risk, testing, rollback, steps (the project is normal)" \
            in out["error"]
        out = await amy.call("change.create", what="  ", why="Because.", **FULL)
        assert not out["ok"] and "what and why" in out["error"]
    on_hub(cfg, body)


def test_ceremony_follows_the_projects_trust_level(cfg):
    async def body(hub, bake, amy, helper):
        quick = await bake.ok("project.create", title="Quick", slug="quick")
        careful = await bake.ok("project.create", title="Careful", slug="careful")
        await bake.ok("project.trust.create", project=quick["id"], level="ship-it")
        await bake.ok("project.trust.create", project=careful["id"], level="sacred")
        note = await amy.ok("change.create", what="Rebooted the router", why="It hung.", project="quick")
        assert note["level"] == "ship-it" and note["project"] == "quick"
        out = await amy.call("change.create", what="Rebooted the router", why="It hung.", project=careful["id"])
        assert not out["ok"] and "(the project is sacred)" in out["error"]
        sac = await amy.ok("change.create", **record(), project=careful["id"])
        assert sac["level"] == "sacred" and not hub.dispatcher.holds
        out = await amy.call("change.create", **record(), project="nowhere")
        assert not out["ok"] and "no project nowhere" in out["error"]
        assert [r["id"] for r in await bake.ok("change.list", where={"project": "quick"})] == [note["id"]]
    on_hub(cfg, body)


def test_without_the_workbench_every_change_is_normal(cfg):
    (cfg.library / "projects").unlink()

    async def body(hub, bake, amy, helper):
        rec = await amy.ok("change.create", **record(), project="anything")
        assert rec["level"] == "normal"
    on_hub(cfg, body)


def test_core_writes_a_record_only_when_the_plugin_is_there(cfg):
    async def body(hub, bake, amy, helper):
        who = KitCaller(person="person:bake", why="Record a test change")
        cid = await hub.note_change(who, record(category="plugin"))
        rec = hub.records.type("change.change").get(KitCaller(band=BAND, owner=True), cid)
        assert rec["category"] == "plugin" and rec["person"] == "person:bake"
        # a record that can't be written never stops core
        assert await hub.note_change(who, {"what": "x", "why": "y"}) is None
    on_hub(cfg, body)

    (cfg.library / "changes").unlink()

    async def bare(hub, bake, amy, helper):
        assert await hub.note_change(KitCaller(person="person:bake", why="w"), record()) is None
        # plugin pushes don't need the plugin (they fail here only for the unknown worker)
        out = await hub.push_plugin(Caller(person="person:bake"), {"worker": "x", "plugin": "probe", "why": WHY},
                                    "http")
        assert out == {"ok": False, "error": "unknown worker: x"}
    on_hub(cfg, bare)
