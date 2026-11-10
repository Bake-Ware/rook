"""Change records (rook/plugins/changes) on a real 0.5 hub, in process with a
loopback band, driven through the HTTP door: records only (castling-design:
"Change records: records only"). Records that gate and run nothing, the
kit's create/update/search/history, and core writing records only when the
plugin is there (``Hub.note_change``, plugin pushes). Every test body runs
under a hard timeout; HTTP requests have their own socket timeout."""

import asyncio
import json
import urllib.error
import urllib.request

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from port_helpers import PLUGINS  # noqa: E402
from rook.core2.hub.identity import Caller  # noqa: E402
from rook.core2.hub.runtime import Config, Hub, Loopback  # noqa: E402
from rook.kit.ctx import Caller as KitCaller  # noqa: E402
from rook.kit.manifest import check  # noqa: E402

BAND = "e17904e6ed4c664592f64f6f1d02c682"
WHY = "Check the change records plugin."

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
    (lib / "changes").symlink_to(PLUGINS / "changes", target_is_directory=True)
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
    # records only: a record type with the kit's caps, nothing it calls, no settings or hooks
    assert raw["package"] == "workbench" and not raw.get("caps") and not raw.get("uses")
    assert not raw.get("settings") and not raw.get("hooks")
    assert "create" in raw["records"]["change"]["caps"]
    assert not {"kind", "level"} & set(raw["records"]["change"]["fields"])


# -- on the hub ------------------------------------------------------------------------------

def test_a_record_is_written_and_gates_nothing(cfg):
    async def body(hub, bake, amy, helper):
        rec = await amy.ok("change.create", **record(workers=["rook"], caps=["probe.set"]))
        assert rec["creator"] == rec["person"] == "person:amy"
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
        # saved with what it was given, whatever project it names
        note = await amy.ok("change.create", what="Rebooted the router", why="It hung.", project="anything")
        assert note["project"] == "anything" and not note.get("risk")
        assert [r["id"] for r in await bake.ok("change.list", where={"project": "anything"})] == [note["id"]]
        out = await amy.call("change.create", why="Because.")
        assert not out["ok"], out
    on_hub(cfg, body)


def test_core_writes_a_record_only_when_the_plugin_is_there(cfg):
    async def body(hub, bake, amy, helper):
        who = KitCaller(person="person:bake", why="Record a test change")
        cid = await hub.note_change(who, record(category="plugin"))
        rec = hub.records.type("change.change").get(KitCaller(band=BAND, owner=True), cid)
        assert rec["category"] == "plugin" and rec["person"] == "person:bake"
        # a record that can't be written never stops core
        assert await hub.note_change(who, {"what": "x"}) is None
    on_hub(cfg, body)

    (cfg.library / "changes").unlink()

    async def bare(hub, bake, amy, helper):
        assert await hub.note_change(KitCaller(person="person:bake", why="w"), record()) is None
        # plugin pushes don't need the plugin (they fail here only for the unknown worker)
        out = await hub.push_plugin(Caller(person="person:bake"), {"worker": "x", "plugin": "probe", "why": WHY},
                                    "http")
        assert out == {"ok": False, "error": "unknown worker: x"}
    on_hub(cfg, bare)
