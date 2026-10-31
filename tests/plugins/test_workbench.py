"""The workbench package (rook/plugins/{tasks,projects,knowledge,concepts,
whiteboard}): hub plugins over kit records, run through the hub's plugin
host with an in-memory record store, as two agents and a person would."""

import asyncio
import shutil

import pytest

pytest.importorskip("jsonschema")

from port_helpers import CONTRACTS, PLUGINS, go  # noqa: E402
from rook.core2.hub.plugins import HubPlugins  # noqa: E402
from rook.kit.contracts import Contracts  # noqa: E402
from rook.kit.ctx import Caller, KitError  # noqa: E402
from rook.kit.manifest import check, load  # noqa: E402
from rook.kit.records import RecordStore  # noqa: E402

FOLDERS = {"concepts": "concept", "knowledge": "knowledge", "projects": "project",
           "tasks": "task", "whiteboard": "whiteboard"}
ANN = Caller(person="person:bake", agent="agent:ann", session="s1", band="b1", why="workbench test")
BOB = Caller(agent="agent:bob", session="s2", band="b1", why="workbench test")
BAKE = Caller(person="person:bake", band="b1", why="workbench test", owner=True)


@pytest.mark.parametrize("folder", sorted(FOLDERS))
def test_manifest_is_clean_and_hub_only(folder):
    data, issues = check(PLUGINS / folder, CONTRACTS)
    assert [str(i) for i in issues] == []
    m = load(PLUGINS / folder, CONTRACTS)
    assert (m.plugin, data["package"], data["runs_on"]) == (FOLDERS[folder], "workbench", "hub")
    assert not data.get("uses"), "the workbench needs no other package (agents, core caps)"
    for cap, spec in m.caps.items():
        assert spec["on"] == "hub" and spec["doc"] and spec["risk"], cap
    assert (PLUGINS / folder / "docs" / "manual.md").read_text().strip()


class Bench:
    """The five plugins loaded in one HubPlugins; ``run`` raises KitError."""

    def __init__(self):
        self.events = []
        emit = lambda e, p, s: self.events.append((e, p))  # noqa: E731
        self.plugins = HubPlugins(records=RecordStore(emit=emit), contracts=Contracts(), call=None, emit=emit)

    async def load(self):
        for folder in sorted(FOLDERS):
            await self.plugins.load(PLUGINS / folder)
        return self

    async def __call__(self, cap, who=ANN, **args):
        return await self.plugins.run(cap, args, who)


def bench(body, timeout=20):
    async def main():
        b = await Bench().load()
        try:
            await body(b)
        finally:
            await b.plugins.close()
    go(main, timeout)


def test_caps_are_the_decided_names():
    async def body(b):
        assert set(b.plugins.caps()) == {
            *(f"task.{v}" for v in ("list", "get", "search", "create", "update", "archive", "history", "claim",
                                    "release", "link", "comment", "handoff", "request_signoff", "signoff",
                                    "comment.list", "signoff.list")),
            *(f"project.{v}" for v in ("list", "get", "search", "create", "update", "archive", "history")),
            *(f"project.trust.{v}" for v in ("list", "get", "create", "update")),
            *(f"concept.{v}" for v in ("list", "get", "search", "create", "update", "archive", "history", "pool")),
            *(f"knowledge.{v}" for v in ("list", "get", "search", "create", "update", "archive", "history", "link")),
            "whiteboard.read", "whiteboard.write", "whiteboard.clear"}
        caps = b.plugins.caps()
        # an agent that may edit a project can't loosen its trust level
        assert caps["project.trust.update"]["risk"] == caps["project.trust.create"]["risk"] == "admin"
        assert caps["project.update"]["risk"] == "write"
    bench(body)


def test_claim_release_and_handoff():
    async def body(b):
        p = await b("project.create", title="Castling", slug="castling")
        t = await b("task.create", title="Workbench merged", project=p["id"], criteria=["tests pass"])
        out = await b("task.claim", id=t["id"])
        assert out["task"]["state"] == "in_progress" and out["also_claimed_by"] == []
        assert [(c["actor"], c["person"], c["session"]) for c in out["task"]["claims"]] == \
            [("agent:ann", "person:bake", "s1")]
        out = await b("task.claim", BOB, id=t["id"])
        assert out["also_claimed_by"] == ["agent:ann"]
        since = out["task"]["claims"][0]["since"]
        again = await b("task.claim", id=t["id"])          # a second claim keeps its start
        assert [c["since"] for c in again["task"]["claims"] if c["actor"] == "agent:ann"] == [since]
        out = await b("task.release", BOB, id=t["id"])
        assert [c["actor"] for c in out["claims"]] == ["agent:ann"] and out["state"] == "in_progress"
        with pytest.raises(KitError, match="holds no claim"):
            await b("task.release", BOB, id=t["id"])
        b.events.clear()
        out = await b("task.handoff", id=t["id"], to="role:qa", note="Ready for QA; see the whiteboard.")
        assert (out["assignee"], out["claims"], out["state"]) == ("role:qa", [], "todo")
        assert b.events[-1] == ("task.handed_off", {"id": t["id"], "title": "Workbench merged", "to": "role:qa",
                                                    "by": "agent:ann", "note": "Ready for QA; see the whiteboard."})
        said = await b("task.comment.list", where={"task": t["id"]})
        assert [(c["text"], c["creator"]) for c in said] == \
            [("Handed to role:qa: Ready for QA; see the whiteboard.", "agent:ann")]
        # a stale claim can be taken off by someone else, naming its actor
        await b("task.claim", BOB, id=t["id"])
        out = await b("task.release", id=t["id"], actor="agent:bob", state="blocked")
        assert (out["claims"], out["state"]) == ([], "blocked")
        await b("task.update", id=t["id"], revision=(await b("task.get", id=t["id"]))["revision"], state="done",
                outcome="Merged.")
        with pytest.raises(KitError, match="is done; set its state to todo"):
            await b("task.claim", id=t["id"])
        with pytest.raises(KitError, match="nothing to hand off"):
            await b("task.handoff", id=t["id"], to="agent:bob", note="x")
    bench(body)


def test_links_comments_and_signoff():
    async def body(b):
        t = await b("task.create", title="Grocery list sorted")
        out = await b("task.link", id=t["id"], kind="pr", ref="bake-ware/rook#300", relation="produced")
        assert [(x["kind"], x["ref"], x["relation"], x["by"]) for x in out["links"]] == \
            [("pr", "bake-ware/rook#300", "produced", "agent:ann")]
        out = await b("task.link", id=t["id"], kind="pr", ref="bake-ware/rook#300", note="again")
        assert len(out["links"]) == 1 and out["links"][0]["note"] == "again"     # same kind+ref replaces
        out = await b("task.link", id=t["id"], kind="pr", ref="bake-ware/rook#300", remove=True)
        assert out["links"] == []
        with pytest.raises(KitError, match="no pr link"):
            await b("task.link", id=t["id"], kind="pr", ref="nope", remove=True)
        assert (await b("task.comment", id=t["id"], text="Milk is out."))["by"] == "agent:ann"
        with pytest.raises(KitError, match="text is empty"):
            await b("task.comment", id=t["id"], text="  ")
        b.events.clear()
        out = await b("task.request_signoff", id=t["id"], reviewers=["role:qa", "person:bake"], note="Check it")
        assert [(s["reviewer"], s["state"]) for s in out["signoff"]] == \
            [("role:qa", "requested"), ("person:bake", "requested")]
        assert b.events[-1][0] == "task.signoff_requested"
        assert b.events[-1][1]["reviewers"] == ["role:qa", "person:bake"]
        out = await b("task.signoff", BOB, id=t["id"], approve=True, reviewer="role:qa", note="Looks right")
        # nobody signs for a person: not another agent, not an agent acting for them
        with pytest.raises(KitError, match="only person:bake signs off as person:bake"):
            await b("task.signoff", BOB, id=t["id"], approve=True, reviewer="person:bake")
        with pytest.raises(KitError, match="from their own login"):
            await b("task.signoff", ANN, id=t["id"], approve=True, reviewer="person:bake")
        with pytest.raises(KitError, match="only agent:bob"):
            await b("task.signoff", ANN, id=t["id"], approve=True, reviewer="agent:bob")
        out = await b("task.signoff", BAKE, id=t["id"], approve=False, note="Add eggs")
        assert [(s["reviewer"], s["state"], s["by"]) for s in out["signoff"]] == \
            [("role:qa", "approved", "agent:bob"), ("person:bake", "declined", "person:bake")]
        out = await b("task.signoff", BOB, id=t["id"], approve=True)     # unasked: added beside
        assert out["signoff"][-1]["reviewer"] == "agent:bob"
        # sign-offs are written only by these caps: no generated create or update
        assert not {"task.signoff.create", "task.signoff.update", "task.comment.create"} & set(b.plugins.caps())
        # asking again resets that reviewer only
        out = await b("task.request_signoff", id=t["id"], reviewers=["person:bake"])
        assert [(s["reviewer"], s["state"]) for s in out["signoff"]] == \
            [("role:qa", "approved"), ("person:bake", "requested"), ("agent:bob", "approved")]
    bench(body)


def test_concurrent_writes_all_land():
    async def body(b):
        t = await b("task.create", title="Busy task")
        await asyncio.gather(*(b("task.comment", Caller(agent=f"agent:{i}", band="b1"), id=t["id"], text=f"c{i}")
                               for i in range(6)))
        await asyncio.gather(*(b("task.link", Caller(agent=f"agent:{i}", band="b1"), id=t["id"], kind="file",
                                 ref=f"f{i}") for i in range(6)))
        assert sorted(x["ref"] for x in (await b("task.get", id=t["id"]))["links"]) == [f"f{i}" for i in range(6)]
        said = await b("task.comment.list", where={"task": t["id"]})
        assert sorted(c["text"] for c in said) == [f"c{i}" for i in range(6)]
        await asyncio.gather(*(b("task.request_signoff", id=t["id"], reviewers=["role:qa"]) for _ in range(4)))
        assert len(await b("task.signoff.list", where={"task": t["id"]})) == 1
        await asyncio.gather(*(b("whiteboard.write", task=t["id"], append=f"- line {i}") for i in range(6)))
        text = (await b("whiteboard.read", task=t["id"]))["text"]
        assert sorted(text.splitlines()) == [f"- line {i}" for i in range(6)]
    bench(body)


def test_whiteboards():
    async def body(b):
        p = await b("project.create", title="Castling")
        t = await b("task.create", title="A task", project=p["id"])
        assert await b("whiteboard.read", task=t["id"]) == {"task": t["id"], "text": "", "by": None, "updated": None}
        out = await b("whiteboard.write", task=t["id"], text="Problem: X\nPlan: Y")
        assert (out["text"], out["by"]) == ("Problem: X\nPlan: Y", "agent:ann")
        out = await b("whiteboard.write", BOB, task=t["id"], append="Now: step 2")
        assert (out["text"], out["by"]) == ("Problem: X\nPlan: Y\nNow: step 2", "agent:bob")
        await b("whiteboard.write", project=p["id"], text="Release when B lands")
        assert (await b("whiteboard.read", project=p["id"]))["text"] == "Release when B lands"
        assert (await b("whiteboard.clear", task=t["id"]))["text"] == ""
        assert (await b("whiteboard.read", project=p["id"]))["text"] == "Release when B lands"
        with pytest.raises(KitError, match="give task or project"):
            await b("whiteboard.read", task=t["id"], project=p["id"])
        with pytest.raises(KitError, match="give text"):
            await b("whiteboard.write", task=t["id"])
        with pytest.raises(KitError, match="no task.task"):
            await b("whiteboard.write", task="r_nope", text="x")
        with pytest.raises(KitError, match="holds essentials"):
            await b("whiteboard.write", task=t["id"], text="x" * 20000)
        # the boards are records: one per task, none duplicated by the writes above
        boards = b.plugins.records.type("whiteboard.board").list(ANN)
        assert {(x["task"], x["project"]) for x in boards} == {(t["id"], None), (None, p["id"])} and len(boards) == 2
    bench(body)


def test_knowledge_pages_links_and_provenance():
    async def body(b):
        folder = await b("knowledge.create", title="Castling", kind="summary")
        assert folder["slug"] == "castling"
        page = await b("knowledge.create", title="Castling design", parent="castling",
                       body="Concepts are tags.[^bake] See [[workbench]] and [[nowhere]].",
                       sources=[{"key": "bake", "kind": "user", "ref": "person:bake",
                                 "quote": "concepts are tags, not parents"}])
        assert page["sources"][0]["by"] == "agent:ann" and page["sources"][0]["when"]
        assert page["parent"] == folder["id"] and page["kind"] == "observation"
        await b("knowledge.create", BOB, title="Workbench", kind="decision",
                body="Part of [[castling-design]].[^me]\n\n[^me]: My own conclusion from the voice notes.")
        with pytest.raises(KitError, match=r"cites \[\^x\] with no source"):
            await b("knowledge.create", title="Unsourced", body="Claim.[^x]")
        dup = await b("knowledge.create", title="Castling")
        assert dup["slug"] == "castling-2"
        await b("knowledge.archive", id=dup["id"], revision=dup["revision"])
        assert (await b("knowledge.create", title="Castling"))["slug"] == "castling-3"   # archived slugs stay taken
        assert (await b("knowledge.get", id="castling-2"))["archived"]
        got = await b("knowledge.get", id="castling-design")
        assert [x["slug"] for x in got["links_out"]] == ["workbench"] and got["missing"] == ["nowhere"]
        assert [x["slug"] for x in got["backlinks"]] == ["workbench"]
        assert [x["slug"] for x in (await b("knowledge.get", id=folder["id"]))["children"]] == ["castling-design"]
        hits = await b("knowledge.search", query="concepts tags")
        assert [h["slug"] for h in hits] == ["castling-design"]
        # an edit needs the current revision; sources merge by key
        with pytest.raises(KitError, match="Record changed"):
            await b("knowledge.update", id="castling-design", revision=9, title="x")
        out = await b("knowledge.update", BOB, id="castling-design", revision=page["revision"],
                      body="Concepts are tags.[^bake] Projects carry several.[^j]",
                      sources=[{"key": "j", "kind": "journal", "ref": "call-17"},
                               {"key": "bake", "kind": "user", "ref": "person:bake", "quote": "a project carries several"}])
        assert [(s["key"], s["by"]) for s in out["sources"]] == [("j", "agent:bob"), ("bake", "agent:bob")]
        assert [h["revision"] for h in await b("knowledge.history", id=out["id"])] == [1, 2]
        with pytest.raises(KitError, match="sit under itself"):
            await b("knowledge.update", id="castling", revision=folder["revision"], parent="castling-design")
        # link adds a source or related link; a cited one can't be removed
        out = await b("knowledge.link", id="castling-design", kind="commit", ref="abc123", note="the code")
        assert out["sources"][-1]["kind"] == "commit"
        out = await b("knowledge.link", id="castling-design", kind="commit", ref="abc123", remove=True)
        assert "commit" not in [s["kind"] for s in out["sources"]]
        with pytest.raises(KitError, match=r"still cites \[\^j\]"):
            await b("knowledge.link", id="castling-design", kind="journal", key="j", remove=True)
        skill = await b("knowledge.create", title="Wiki sweep", kind="skill", body="When to use: weekly.")
        assert [p["slug"] for p in await b("knowledge.list", where={"kind": "skill"})] == [skill["slug"]]
    bench(body)


def test_concept_pool_and_project_tags():
    async def body(b):
        await b("concept.create", title="Software", slug="software", roles=["lead", "qa"], skills=["git-flow"])
        await b("concept.create", title="Hardware", slug="hardware", roles=["hardware-review", "qa"],
                signoff="user", trust="sacred")
        p = await b("project.create", title="ESP32 firmware", concepts=["software", "hardware"])
        pool = await b("concept.pool", concepts=p["concepts"] + ["crochet"])
        assert (pool["roles"], pool["skills"], pool["signoff"], pool["trust"], pool["missing"]) == \
            (["lead", "qa", "hardware-review"], ["git-flow"], "user", "sacred", ["crochet"])
        assert (await b("concept.pool", concepts=["crochet"]))["signoff"] is None
        trust = await b("project.trust.create", BAKE, project=p["id"], level="ship-it", note="no users yet")
        assert trust["level"] == "ship-it"
        with pytest.raises(KitError, match="already used"):
            await b("project.trust.create", BAKE, project=p["id"], level="normal")
    bench(body)


def test_the_hub_serves_the_workbench_to_an_agent_without_the_agents_package(tmp_path):
    """A real 0.5 hub with only the workbench in its library: an external
    agent (an MCP client's token) works a task through the door."""
    pytest.importorskip("nacl")
    from rook.core2.hub.runtime import Config, Hub, Loopback

    lib = tmp_path / "lib"
    for folder in FOLDERS:
        shutil.copytree(PLUGINS / folder, lib / folder)

    async def body():
        hub = Hub(Config(data=tmp_path / "data", band="e17904e6ed4c664592f64f6f1d02c682", library=lib,
                         users={"bake": {"preset": "owner"}}, agents={"claude": {"grants": ["*"], "max_risk": "exec"}},
                         doors={}), transport=Loopback())
        await hub.start()
        try:
            assert {"concept", "knowledge", "project", "task", "whiteboard"} <= set(hub.plugins.plugins)
            from rook.core2.hub.identity import Caller as DoorCaller
            agent = DoorCaller("person:bake", "agent:claude")

            async def call(cap, **args):
                out = await hub.call(agent, {"worker": "rook", "cap": cap, "args": args,
                                             "why": "Work a task end to end."}, "test")
                assert out["ok"], out
                return out["result"]
            t = await call("task.create", title="Try the workbench")
            claimed = await call("task.claim", id=t["id"])
            assert claimed["task"]["claims"][0]["actor"] == "agent:claude"
            await call("whiteboard.write", task=t["id"], text="Plan: call every verb once.")
            await call("knowledge.create", title="Workbench works", body="It does.[^t]",
                       sources=[{"key": "t", "kind": "task", "ref": t["id"]}])
            assert [e["cap"] for e in hub.journal.query(cap="task.*")] == ["task.claim", "task.create"]
            refused = await hub.call(agent, {"worker": "rook", "cap": "task.claim", "args": {"id": t["id"]}}, "test")
            assert not refused["ok"] and "why" in refused["error"]
        finally:
            await hub.stop()
    go(body, 60)
