"""The knowledge and tasks hub plugins (rook/hub/plugins/knowledge, tasks.py).

Covers: lossless upgrade of a knowledge.db written by the pre-plugin code,
the enable flag and settings (env aliases, stored file, semantic on/off,
http and cap:// embedders), cap placement on worker "rook" with read/write
tiers and the band risk ceiling, DEPENDS in the core host, the MCP tools'
unchanged reply shape, and plugin guidance slots.
"""
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from rook.band_mcp.guidance import Guidance
from rook.core.host import PluginHost
from rook.core.plugin import Candidate, Plugin, capability
from rook.hub.node import HubNode
from rook.hub.plugins.knowledge.store import KnowledgeStore

FIXTURE = Path(__file__).parent / "fixtures" / "knowledge_v2.db"
TABLES = ("records", "links", "claims", "events", "receipts", "actors", "embeddings", "cursors")
ENV = ("ROOK_KNOWLEDGE", "ROOK_KNOWLEDGE_DB", "ROOK_EMBED_URL", "ROOK_EMBED_MODEL",
       "ROOK_KNOWLEDGE_SEMANTIC", "ROOK_HUB_BAND_MAX_RISK")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)


def dump(path):
    db = sqlite3.connect(path)
    try:
        return {t: sorted(tuple(r) for r in db.execute(f"SELECT * FROM {t}")) for t in TABLES}
    finally:
        db.close()


def node(tmp_path, **kw):
    return HubNode(str(tmp_path), entry_points=False, build_version="1.test.node", **kw)


# -- lossless upgrade of an existing knowledge.db -----------------------------------------

@pytest.mark.asyncio
async def test_pre_plugin_database_upgrades_losslessly_in_place(tmp_path, monkeypatch):
    """A knowledge.db written by the pre-plugin store (tests/fixtures/
    make_knowledge_v2_db.py) sits beside the hub stores, where the old code
    kept it. Enabling the plugin must use it in place and keep every record,
    link, claim, event, receipt, actor, embedding and cursor byte for byte."""
    db = tmp_path / "knowledge.db"
    shutil.copy(FIXTURE, db)
    before = dump(db)
    assert all(before[t] for t in TABLES), "fixture should populate every table"
    monkeypatch.setenv("ROOK_KNOWLEDGE", "1")
    n = node(tmp_path)
    kb = n.plugin("knowledge")
    assert kb is not None and Path(kb.service.store.path) == db
    assert dump(db) == before
    raw = sqlite3.connect(db)
    assert raw.execute("SELECT namespace, version FROM _rook_migrations ORDER BY version").fetchall() \
        == [("knowledge", 1), ("knowledge", 2), ("knowledge", 3)]  # 002 hygiene, 003 blocks: new tables only
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 2  # an older release can still open it
    raw.close()
    # Reads behave as before: slugs, backlinks, links with retraction, claims, FTS, deck.
    svc = kb.service
    page = svc.store.get("band0001", "service-port-moved")
    assert page["attrs"]["supersedes"] and page["attrs"].get("verification") == "verified"
    kinds = [l["kind"] for l in page["links"]]
    assert {"journal", "human"} <= set(kinds) and "url" not in kinds  # the url link was retracted
    assert [r["id"] for r in svc.store.lexical("band0001", "runbook", 10)]
    deck = svc.store.deck(["band0001", "band0002"])
    assert deck and deck[0]["in_progress"][0]["claimants"]
    assert svc.store.cursor("hygiene") == "seq-42"
    # Opening it again changes nothing.
    KnowledgeStore(db)
    assert dump(db) == before
    # And new writes still work.
    created = await n.invoke("knowledge.write", {"action": "create", "data": {"title": "After upgrade"},
                                                 "request_id": "after-1"}, "agent:test")
    assert created["slug"] == "after-upgrade"


def test_new_database_gets_the_same_layout_as_the_old_code(tmp_path):
    fresh = tmp_path / "fresh.db"
    KnowledgeStore(fresh)

    def layout(path):
        db = sqlite3.connect(path)
        try:
            return {t: [r[1:3] for r in db.execute(f"PRAGMA table_info({t})")] for t in TABLES}
        finally:
            db.close()
    assert layout(fresh) == layout(FIXTURE)


# -- enable flag, settings ---------------------------------------------------------

def test_off_by_default_and_tasks_follow_knowledge(tmp_path):
    n = node(tmp_path)
    assert n.host.status["knowledge"]["state"] == "unavailable"
    assert n.host.status["tasks"] == {"state": "unavailable", "reason": "depends on knowledge (not loaded)"}
    assert not any(c.startswith(("knowledge.", "task.")) for c in n.caps())
    assert not (tmp_path / "knowledge.db").exists()


def test_stored_setting_enables_it(tmp_path):
    (tmp_path / "hub_plugin_settings.json").write_text(json.dumps({"knowledge": {"enabled": True}}))
    n = node(tmp_path)
    assert {"knowledge.read", "knowledge.write", "task.read", "task.write"} <= set(n.caps())
    assert n.host.tiers()["knowledge.read"] == "r" and n.host.tiers()["task.write"] == "w"
    assert (tmp_path / "knowledge.db").exists()


def test_broken_store_leaves_the_plugins_unloaded(tmp_path, monkeypatch):
    bad = tmp_path / "is-a-directory"
    bad.mkdir()
    monkeypatch.setenv("ROOK_KNOWLEDGE", "1")
    monkeypatch.setenv("ROOK_KNOWLEDGE_DB", str(bad))
    n = node(tmp_path)
    assert n.plugin("knowledge") is None and n.plugin("task") is None


def test_embedder_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_KNOWLEDGE", "1")
    s = node(tmp_path).plugin("knowledge").service.search
    assert not s.configured and s.url == ""                     # nothing set: keyword only
    monkeypatch.setenv("ROOK_EMBED_URL", "http://127.0.0.1:8768/embed")  # legacy env alias
    s = node(tmp_path).plugin("knowledge").service.search
    assert s.configured and s.url == "http://127.0.0.1:8768/embed" and s.resource is None
    monkeypatch.setenv("ROOK_KNOWLEDGE_SEMANTIC", "0")
    assert not node(tmp_path).plugin("knowledge").service.search.configured
    monkeypatch.delenv("ROOK_KNOWLEDGE_SEMANTIC")
    monkeypatch.setenv("ROOK_EMBED_URL", "")                   # explicitly empty: keyword only
    assert not node(tmp_path).plugin("knowledge").service.search.configured


class FakeBand:
    def __init__(self):
        self.calls = []
        self.workers = {"w1": {"worker_id": "w1", "name": "gpu-box", "caps": ["embed.text"]}}

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        self.calls.append((cap, target, identity))
        vectors = [[1.0] + [0.0] * 7 if "car" in t or "Automobile" in t else [0.0] * 7 + [1.0]
                   for t in args["texts"]]
        return {"ok": True, "result": {"model": "tiny", "vectors": vectors}}


@pytest.mark.asyncio
async def test_cap_embedder_indexes_and_searches_over_the_band(tmp_path):
    (tmp_path / "hub_plugin_settings.json").write_text(json.dumps({"knowledge": {
        "enabled": True, "embedder": "cap://any/embed.text", "embed_model": "tiny"}}))
    band = FakeBand()
    n = node(tmp_path, client=band)
    svc = n.plugin("knowledge").service
    assert svc.search.configured and svc.search.endpoint == "cap://any/embed.text"
    await n.invoke("knowledge.write", {"action": "create", "data": {"title": "Automobile repair"},
                                       "request_id": "r1"}, "agent:test")
    await svc.search.index_batch()
    assert band.calls[0] == ("embed.text", "w1", "system:rook-hub")
    found = await n.invoke("knowledge.read", {"action": "search", "query": "fix a car"})
    assert found["semantic"] is True and found["results"][0]["title"] == "Automobile repair"
    status = await n.invoke("knowledge.read", {"action": "status"})
    assert status["semantic_configured"] is True


# -- caps on worker "rook" ----------------------------------------------------------

@pytest.mark.asyncio
async def test_band_reaches_reads_only_and_writes_are_attributed(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_KNOWLEDGE", "1")
    n = node(tmp_path)
    denied = await n.dispatch("knowledge.write", {"action": "create", "data": {"title": "x"},
                                                  "request_id": "b1"}, "agent:someone", source="band")
    assert not denied["ok"] and "not callable over the band" in denied["error"]
    made = await n.dispatch("task.write", {"action": "create", "kind": "concept", "data": {"title": "Why"},
                                           "request_id": "m1"}, "agent:mcp-user")
    assert made["ok"], made
    read = await n.dispatch("task.read", {"action": "get", "kind": "concept", "id": "why"},
                            "agent:someone", source="band")
    assert read["ok"] and read["result"]["kind"] == "concept"
    deck = await n.dispatch("task.read", {}, "agent:someone", source="band")
    assert deck["ok"] and "deck" in deck["result"]
    # A read cap never writes, even when the band ceiling allows it.
    sneaky = await n.dispatch("knowledge.read", {"action": "create", "data": {"title": "x"}}, "agent:x")
    assert not sneaky["ok"] and "is a write" in sneaky["error"]
    # With the ceiling raised, a band write is recorded under the band's
    # unauthenticated principal, never the self-stamped identity (permissions 1).
    monkeypatch.setenv("ROOK_HUB_BAND_MAX_RISK", "write")
    n2 = node(tmp_path)
    w = await n2.dispatch("knowledge.write", {"action": "create", "data": {"title": "From band"},
                                              "request_id": "b2"}, "agent:someone", source="band")
    assert w["ok"] and w["result"]["creator"] == "band:unauthenticated"
    with sqlite3.connect(tmp_path / "knowledge.db") as db:
        assert db.execute("SELECT kind FROM actors WHERE id='band:unauthenticated'").fetchone() == ("band",)
        assert db.execute("SELECT kind FROM actors WHERE id='agent:someone'").fetchone() is None


@pytest.mark.asyncio
async def test_mcp_tools_keep_their_names_arguments_and_replies(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_KNOWLEDGE", "1")
    n = node(tmp_path)

    async def invoke(cap, args):
        return await n.invoke(cap, args, "agent:test")
    tools = {t.__name__: t for p in n.host.plugins if hasattr(p, "mcp_tools") for t in p.mcp_tools(invoke)}
    assert set(tools) - {"rook_jobs"} == {"rook_knowledge", "rook_concept", "rook_project", "rook_task"}
    no_req = json.loads(await tools["rook_knowledge"](action="create", data={"title": "x"}))
    assert no_req == {"ok": False, "error": "request_id is required for writes", "code": "ValueError"}
    bogus = json.loads(await tools["rook_task"](action="bogus"))
    assert not bogus["ok"] and bogus["error"].startswith("Actions: bands, deck")
    page = json.loads(await tools["rook_knowledge"](action="create", data={"title": "Deploy notes", "body": "deploy"},
                                                    request_id="k1"))
    assert page["ok"] and page["result"]["kind"] == "knowledge" and "comparable" in page["result"]
    found = json.loads(await tools["rook_knowledge"](query="deploy"))["result"]
    assert set(found["results"][0]) <= {"id", "slug", "kind", "title", "state", "score", "excerpt"}
    c = json.loads(await tools["rook_concept"](action="create", data={"title": "Idea"}, request_id="c1"))["result"]
    p = json.loads(await tools["rook_project"](action="create", data={"title": "P", "parent": c["slug"]},
                                               request_id="p1"))["result"]
    assert p["kind"] == "project" and p["parent"] == c["id"]
    assert [r["kind"] for r in json.loads(await tools["rook_project"]())["result"]["records"]] == ["project"]


# -- core: DEPENDS ------------------------------------------------------------------

class _Base(Plugin):
    NAMESPACE = "base"

    @capability("x")
    def x(self):
        return 1


class _Child(Plugin):
    NAMESPACE = "child"
    DEPENDS = ("base",)

    def available(self):
        return self.dependency("base") is not None

    @capability("y")
    def y(self):
        return self.dependency("base").x() + 1


def test_depends_loads_after_its_dependency_regardless_of_order():
    host = PluginHost(check_placement=False)
    loaded = host.load([Candidate("child", "test", lambda: _Child), Candidate("base", "test", lambda: _Base)])
    assert [p.NAMESPACE for p in loaded] == ["base", "child"]
    assert host.plugin("child").manifest()["depends"] == ["base"]
    alone = PluginHost(check_placement=False)
    assert alone.load([Candidate("child", "test", lambda: _Child)]) == []
    assert alone.status["child"]["state"] == "unavailable"


# -- guidance -----------------------------------------------------------------------

def test_plugin_guidance_slots_become_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_KNOWLEDGE", "1")
    slots = node(tmp_path).guidance_defaults()
    assert "tool:rook_knowledge" in slots and "tool:rook_task" in slots
    assert slots["cap:hub.info"].startswith("Call hub.info")  # bare cap name -> cap: slot
    g = Guidance(None)
    g.add_defaults(slots)
    assert g.get("cap:knowledge.").startswith("knowledge.read/knowledge.write")
    assert g.tips("s1", "knowledge.read")["_tips"] == [slots["cap:knowledge."]]
    g.add_defaults({"cap:shell.exec": "override attempt", "bad key": "x"})
    assert g.get("cap:shell.exec") != "override attempt"  # core defaults win


@pytest.mark.asyncio
async def test_start_runs_background_indexing_and_stop_ends_it(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_KNOWLEDGE", "1")
    n = node(tmp_path)
    kb = n.plugin("knowledge")
    await n.start()
    task = kb._maintain
    assert task is not None and not task.done()
    await n.stop()
    assert task.done() and kb._maintain is None
