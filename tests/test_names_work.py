"""Speakable names (rook.core.names) on tasks, projects, huddles and huddle
cards: assigned on create, backfilled on open, resolved wherever an id is
taken (exact for writes, a single clear near miss for reads), and shown
beside the id in replies."""
from __future__ import annotations

import json
import sqlite3

import pytest

from rook.band_mcp.chat_rooms import ChatStore
from rook.core import names
from rook.hub.node import HubNode
from rook.hub.plugins.huddle.service import PERSON, HuddleStore
from rook.hub.plugins.knowledge.hygiene import HygieneEngine
from rook.hub.plugins.knowledge.service import KnowledgeService
from rook.hub.plugins.knowledge.store import KnowledgeStore, Unresolved

AGENT = {"id": "codex.codex.gpubox", "kind": "agent"}
PERSON_ID = "human:1"


class Enrollment:
    def __init__(self, *ids):
        self.ids = ids

    def bands(self, active_only=True):
        return [{"id": b, "name": b.title(), "label": b[:8], "is_primary": i == 0}
                for i, b in enumerate(self.ids)]


def spoken(name: str) -> str:
    """``brave-otter-lamp`` as a person might type it: ``Brave otter_LAMP``."""
    a, b, c = name.split("-")
    return f"{a.title()} {b}_{c.upper()}"


def near(name: str) -> str:
    """One letter off in the last word: a near miss for ``name`` only."""
    a, b, c = name.split("-")
    return f"{a}-{b}-{c[:-1]}{'z' if c[-1] != 'z' else 'y'}"


def rename(db_path, rid, name):
    with sqlite3.connect(db_path) as db:
        db.execute(f"UPDATE {names.TABLE} SET name=? WHERE id=?", (name, rid))


@pytest.fixture
def kb(tmp_path):
    svc = KnowledgeService(tmp_path / "knowledge.db", lambda: {"actor": AGENT["id"], "kind": "agent"},
                           enrollment=Enrollment("alpha", "beta"))
    n = iter(range(10_000))

    async def make(kind, title, parent=None, band=None, **attrs):
        data = {"title": title, **({"parent": parent} if parent else {}), **({"attrs": attrs} if attrs else {})}
        return await svc.dispatch("create", band, kind, None, "", data, f"r{next(n)}", AGENT)
    svc.make = make
    svc.rid = lambda: f"w{next(n)}"
    return svc


async def tree(kb, band=None):
    c = await kb.make("concept", "Home lab", band=band)
    p = await kb.make("project", "Backups", c["id"], band=band)
    t = await kb.make("task", "Restore test", p["id"], band=band)
    return c, p, t


# -- tasks and projects --------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_names_tasks_and_projects_only(kb):
    c, p, t = await tree(kb)
    page = await kb.make("knowledge", "A page")
    assert names.is_name(p["name"]) and names.is_name(t["name"]) and p["name"] != t["name"]
    assert "name" not in c and "name" not in page
    got = await kb.dispatch("get", None, None, t["id"], lean=True)
    assert got["name"] == t["name"]


@pytest.mark.asyncio
async def test_get_by_id_and_exact_name_in_any_form(kb):
    _, p, t = await tree(kb)
    for ref in (t["id"], t["slug"], t["name"], spoken(t["name"]), t["name"].replace("-", " "),
                t["name"].upper().replace("-", "_")):
        got = await kb.dispatch("get", None, "task", ref, lean=True)
        assert got["id"] == t["id"] and "resolved" not in got, ref
    # A project's name works where a task tool takes a project (deck id=).
    deck = await kb.dispatch("deck", None, "task", spoken(p["name"]), lean=True)
    assert deck["deck"][0]["project"]["id"] == p["id"] and deck["deck"][0]["project"]["name"] == p["name"]


@pytest.mark.asyncio
async def test_fuzzy_read_resolves_and_says_so(kb):
    _, _, t = await tree(kb)
    got = await kb.dispatch("get", None, "task", near(t["name"]), lean=True)
    assert got["id"] == t["id"] and got["resolved"] == {"from": near(t["name"]), "via": "fuzzy"}


@pytest.mark.asyncio
async def test_fuzzy_write_is_rejected_with_candidates(kb):
    _, _, t = await tree(kb)
    with pytest.raises(Unresolved) as e:
        await kb.dispatch("update", None, "task", near(t["name"]), "",
                          {"revision": t["revision"], "patch": {"title": "x"}}, kb.rid(), AGENT)
    assert e.value.payload()["candidates"] == [{"id": t["id"], "name": t["name"]}]
    assert t["name"] in str(e.value)
    # The exact name writes.
    out = await kb.dispatch("update", None, "task", spoken(t["name"]), "",
                            {"revision": t["revision"], "patch": {"title": "Restore drill"}}, kb.rid(), AGENT)
    assert out["id"] == t["id"] and out["title"] == "Restore drill"
    # Claims, notes and batch ops are writes too.
    with pytest.raises(Unresolved):
        await kb.dispatch("claim", None, "task", near(t["name"]), "", {}, kb.rid(), AGENT)
    batch = await kb.dispatch("batch", None, "task", None, "", {"ops": [
        {"action": "claim", "id": near(t["name"])}, {"action": "claim", "id": t["name"]}]}, kb.rid(), AGENT)
    assert batch["failed"] == 1 and batch["results"][0]["candidates"][0]["id"] == t["id"]
    assert batch["results"][1]["result"]["task"] == t["id"]


@pytest.mark.asyncio
async def test_unknown_and_several_near_names(kb):
    _, p, t = await tree(kb)
    with pytest.raises(Unresolved, match="no task or project is named"):
        await kb.dispatch("get", None, "task", "zzzz qqqq xxxx", lean=True)
    a, b, _ = t["name"].split("-")
    rename(kb.store.path, p["id"], f"{a}-{b}-{'q' * 3}")
    rename(kb.store.path, t["id"], f"{a}-{b}-{'q' * 4}")
    with pytest.raises(Unresolved) as e:
        await kb.dispatch("get", None, "task", f"{a} {b} qqqqq", lean=True)
    assert e.value.ambiguous and {c["id"] for c in e.value.payload()["candidates"]} == {p["id"], t["id"]}


@pytest.mark.asyncio
async def test_same_name_in_two_bands_is_ambiguous(kb):
    _, _, t1 = await tree(kb, "alpha")
    _, _, t2 = await tree(kb, "beta")
    rename(kb.store.path, t2["id"], t1["name"])
    with pytest.raises(Unresolved) as e:
        await kb.dispatch("get", None, "task", t1["name"], lean=True)
    assert e.value.ambiguous and {c["id"] for c in e.value.payload()["candidates"]} == {t1["id"], t2["id"]}
    assert (await kb.dispatch("get", "beta", "task", t1["name"], lean=True))["id"] == t2["id"]
    assert kb.store.locate(t1["id"], ["alpha", "beta"]) == "alpha"
    with pytest.raises(Unresolved):
        kb.store.locate(t1["name"], ["alpha", "beta"])


@pytest.mark.asyncio
async def test_names_in_replies_and_search(kb):
    c, p, t = await tree(kb)
    listed = await kb.dispatch("list", None, "task", None, lean=True)
    assert listed["records"][0]["name"] == t["name"]
    deck = await kb.dispatch("deck", None, "task", lean=True)
    assert deck["deck"][0]["project"]["name"] == p["name"] and deck["deck"][0]["todo"][0]["name"] == t["name"]
    board = await kb.dispatch("board", None, "task", spoken(p["name"]))
    assert board["projects"][0]["name"] == p["name"] and board["tasks"][0]["name"] == t["name"]
    got = await kb.dispatch("get", None, "project", p["name"], lean=False)
    assert got["children"][0]["name"] == t["name"]
    # Search matches the name, whole or in part, and shows it.
    for q in (spoken(t["name"]), " ".join(t["name"].split("-")[1:])):
        hits = await kb.dispatch("search", None, "task", None, q, lean=True)
        assert hits["results"][0]["id"] == t["id"] and hits["results"][0]["name"] == t["name"], q


@pytest.mark.asyncio
async def test_parent_dependency_link_and_auto_link_by_name(kb):
    _, p, t = await tree(kb)
    sub = await kb.make("task", "Second", spoken(p["name"]), dependencies=[t["name"]])
    assert sub["parent"] == p["id"]
    got = await kb.dispatch("get", None, "task", sub["id"], lean=True)
    assert got["dependencies"] == [{"id": t["id"], "slug": t["slug"], "name": t["name"], "state": "todo"}]
    board = await kb.dispatch("board", None, "task", p["id"])
    row = next(x for x in board["tasks"] if x["id"] == sub["id"])
    assert row["dependencies"][0]["id"] == t["id"] and row["unblocked"] is False
    link = await kb.dispatch("link", None, "task", sub["name"], "",
                             {"kind": "record", "ref": spoken(t["name"]), "relation": "mentions"}, kb.rid(), AGENT)
    assert link["record"] == sub["id"] and link["ref"] == t["id"]
    # handoff task= and console task_id= go through auto_link: exact names only.
    assert kb.auto_link(AGENT, "handoff", "thread-1", task=spoken(t["name"])) == t["id"]
    with pytest.raises(Unresolved):
        kb.store.auto_link(AGENT, "handoff", "thread-2", task=near(t["name"]))


@pytest.mark.asyncio
async def test_commit_message_names_the_task(kb):
    _, _, t = await tree(kb)
    engine = HygieneEngine(kb.store, conf=lambda: {"hygiene_enabled": True})
    await kb.dispatch("claim", None, "task", t["name"], "", {}, kb.rid(), AGENT)
    reply = {"ok": True, "result": {"stdout": "[main 1a2b3c4] Fix restore\n 1 file changed"}}
    msg = f"Fix restore\n\nrook: {spoken(t['name'])}"
    linked = engine.on_call(AGENT, "shell.exec", {"argv": ["git", "commit", "-m", msg]}, reply)
    assert linked == [t["id"]]
    got = await kb.dispatch("get", None, "task", t["id"])
    assert any(l["kind"] == "commit" and l["relation"] == "evidence" for l in got["links"])


@pytest.mark.asyncio
async def test_backfill_on_open(kb):
    c, p, t = await tree(kb)
    page = await kb.make("knowledge", "Page")
    with sqlite3.connect(kb.store.path) as db:
        names.drop(db)
    KnowledgeStore(kb.store.path)
    with sqlite3.connect(kb.store.path) as db:
        first = dict(db.execute(f"SELECT id, name FROM {names.TABLE}").fetchall())
        band = dict(db.execute(f"SELECT id, band FROM {names.TABLE}").fetchall())
    assert set(first) == {p["id"], t["id"]} and all(names.is_name(n) for n in first.values())
    assert band[t["id"]] == "alpha" and c["id"] not in first and page["id"] not in first
    KnowledgeStore(kb.store.path)
    with sqlite3.connect(kb.store.path) as db:
        assert dict(db.execute(f"SELECT id, name FROM {names.TABLE}").fetchall()) == first
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2


# -- over MCP ------------------------------------------------------------------------

@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_KNOWLEDGE", "1")
    monkeypatch.delenv("ROOK_HUDDLE", raising=False)
    return HubNode(str(tmp_path), entry_points=False, build_version="1.test.node", band_max_risk="write",
                   chat=ChatStore(str(tmp_path / "chat.db")))


@pytest.mark.asyncio
async def test_rook_task_tool_names(node):
    async def invoke(cap, args):
        return await node.invoke(cap, args, "agent:x")
    tools = {t.__name__: t for t in node.plugin("task").mcp_tools(invoke)}
    task = lambda **kw: tools["rook_task"](**kw)
    c = json.loads(await tools["rook_concept"](action="create", data={"title": "C"}, request_id="1"))["result"]
    p = json.loads(await tools["rook_project"](action="create", data={"title": "P", "parent": c["id"]},
                                               request_id="2"))["result"]
    t = json.loads(await task(action="create", data={"title": "T", "parent": p["name"]}, request_id="3"))["result"]
    got = json.loads(await task(action="get", id=spoken(t["name"])))["result"]
    assert got["id"] == t["id"] and got["name"] == t["name"]
    bad = json.loads(await task(action="claim", id=near(t["name"]), request_id="4"))
    assert bad["ok"] is False and bad["candidates"] == [{"id": t["id"], "name": t["name"]}]
    ok = json.loads(await task(action="claim", id=t["name"], request_id="5"))
    assert ok["ok"] and ok["result"]["task"] == t["id"]


# -- huddles and cards -----------------------------------------------------------------

def huddle(node):
    return node.plugin("huddle").service


async def chaired(node):
    svc = huddle(node)
    h = await svc.start(PERSON, "Standup", [])
    card = await svc.chair._propose(None, h["room"], "role:qa", {"kind": "ask_bake", "summary": "Ship it?"})
    return svc, h, card


@pytest.mark.asyncio
async def test_huddles_and_cards_get_names(node):
    svc, h, card = await chaired(node)
    assert names.is_name(h["name"]) and names.is_name(card["name"])
    assert svc.store.name("room", h["room"]) == h["name"]
    row = next(x for x in svc.huddles() if x["room"] == h["room"])
    assert row["name"] == h["name"]
    view = await svc.view(PERSON, room=spoken(h["name"]))
    assert view["room"] == h["room"] and view["name"] == h["name"] and "resolved" not in view
    assert view["cards"][0]["name"] == card["name"]
    # A project's or band's status huddle is named too.
    st = await node.dispatch("huddle.write", {"action": "status", "data": {"working_on": "x"}}, "agent:a")
    assert names.is_name(st["result"]["name"])


@pytest.mark.asyncio
async def test_huddle_room_refs(node):
    svc, h, _ = await chaired(node)
    read = await node.dispatch("huddle.read", {"action": "read", "room": near(h["name"])}, "agent:a")
    assert read["ok"] and read["result"]["room"] == h["room"]
    assert read["result"]["resolved"] == {"from": near(h["name"]), "via": "fuzzy"}
    posted = await svc.post(PERSON, h["name"].replace("-", " "), "hello")
    assert posted["room"] == h["room"] and posted["name"] == h["name"]
    with pytest.raises(ValueError) as e:
        await svc.post(PERSON, near(h["name"]), "hello")
    assert e.value.payload()["candidates"] == [{"id": h["room"], "name": h["name"]}]
    with pytest.raises(ValueError):
        svc.archive(near(h["name"]))
    assert svc.archive(spoken(h["name"]), by=PERSON)["room"] == h["room"]
    # Writes over the band: a near miss is refused, naming the candidate.
    bad = await node.dispatch("huddle.write", {"action": "say", "room": near(h["name"]), "text": "x"}, "agent:a")
    assert not bad["ok"] and h["name"] in bad["error"]


@pytest.mark.asyncio
async def test_card_refs(node):
    svc, h, card = await chaired(node)
    with pytest.raises(ValueError) as e:
        await svc.card(h["name"], near(card["name"]), "dismiss", PERSON_ID)
    assert e.value.payload()["candidates"][0]["id"] == card["card"]
    out = await svc.card(spoken(h["name"]), spoken(card["name"]), "dismiss", PERSON_ID)
    assert out["card"] == card["card"] and out["state"] == "dismissed" and out["name"] == card["name"]

    async def invoke(cap, args):
        return await node.invoke(cap, args, PERSON)
    tool = node.plugin("huddle").mcp_tools(invoke)[0]
    bad = json.loads(await tool(action="card", data={"room": h["name"], "card": near(card["name"])}))
    assert bad["ok"] is False and bad["candidates"][0]["name"] == card["name"]
    read = json.loads(await tool(action="read", data={"room": h["name"]}))["result"]
    assert read["name"] == h["name"] and read["cards"][0]["name"] == card["name"]


@pytest.mark.asyncio
async def test_huddle_project_by_name(node):
    kb = node.plugin("knowledge").service
    c = await kb.dispatch("create", None, "concept", None, "", {"title": "Lab"}, "c1", AGENT)
    p = await kb.dispatch("create", None, "project", None, "", {"title": "Backups", "parent": c["id"]}, "p1", AGENT)
    st = await node.dispatch("huddle.write", {"action": "status", "project": spoken(p["name"]),
                                              "data": {"working_on": "x"}}, "agent:a")
    assert st["ok"], st
    bad = await node.dispatch("huddle.write", {"action": "status", "project": near(p["name"]),
                                               "data": {"working_on": "x"}}, "agent:a")
    assert not bad["ok"] and p["name"] in bad["error"]
    read = await node.dispatch("huddle.read", {"action": "read", "project": near(p["name"])}, "agent:a")
    assert read["ok"] and read["result"]["room"] == st["result"]["room"] and read["result"]["resolved"]["via"] == "fuzzy"


@pytest.mark.asyncio
async def test_huddle_backfill_on_open(node, tmp_path):
    svc, h, card = await chaired(node)
    path = svc.store.path
    with sqlite3.connect(path) as db:
        names.drop(db)
    store = HuddleStore(path)
    first = {r: store.name("room", r) for r in [h["room"]]} | {card["card"]: store.name("card", card["card"])}
    assert all(names.is_name(n) for n in first.values())
    again = HuddleStore(path)
    assert {r: again.name("room" if r == h["room"] else "card", r) for r in first} == first
    store.close()
    again.close()


@pytest.mark.asyncio
async def test_huddle_web_takes_names(node):
    import httpx
    from starlette.applications import Starlette
    from rook.hub.plugins.huddle.web import routes
    svc, h, card = await chaired(node)

    class Accounts:
        def session(self, token):
            return {"id": "1", "name": "Op", "admin": True, "csrf": "c"} if token == "s" else None
    app = Starlette(routes=routes(lambda: node, Accounts()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 cookies={"rook_account": "s"}) as client:
        post = lambda body: client.post("/huddle/account-api", json={"csrf": "c", **body})
        r = await post({"action": "view", "room": spoken(h["name"])})
        assert r.status_code == 200 and r.json()["result"]["name"] == h["name"], r.text
        r = await post({"action": "send", "room": h["name"], "text": "hi"})
        assert r.status_code == 200 and r.json()["result"]["room"] == h["room"], r.text
        r = await post({"action": "card", "room": h["name"], "card": near(card["name"]), "decision": "dismiss"})
        assert r.status_code == 400 and r.json()["candidates"][0]["id"] == card["card"]


@pytest.mark.asyncio
async def test_knowledge_web_takes_names(kb):
    import httpx
    from starlette.applications import Starlette
    from rook.hub.plugins.knowledge.web import routes
    _, p, t = await tree(kb)

    class Accounts:
        def session(self, token):
            return {"id": "1", "name": "Op", "admin": True, "csrf": "c"} if token == "s" else None
    app = Starlette(routes=routes(kb, Accounts()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 cookies={"rook_account": "s"}) as client:
        post = lambda body: client.post("/knowledge/account-api", json={"csrf": "c", **body})
        r = await post({"action": "board", "id": spoken(p["name"])})
        assert r.status_code == 200 and r.json()["result"]["projects"][0]["id"] == p["id"], r.text
        r = await post({"action": "claim", "id": near(t["name"]), "request_id": "x"})
        assert r.status_code == 400 and r.json()["candidates"] == [{"id": t["id"], "name": t["name"]}], r.text
