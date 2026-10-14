"""Huddles (rook/hub/plugins/huddle): group rooms on top of the hub's chat
rooms where agents post status and ask the person questions, which stay
pinned until answered; an answer goes back to the asker and wakes it."""
import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from starlette.applications import Starlette

from rook.band_mcp.chat_rooms import ChatStore
from rook.band_mcp.chat_wake import clean_hermes_stdout, find_worker
from rook.hub.node import HubNode
from rook.hub.plugins.huddle.service import PERSON
from rook.hub.plugins.huddle.web import routes

AGENT = "agent:claude_laptop"
OTHER = "agent:codex_gpubox"


class FakeBand:
    def __init__(self):
        self.workers = {"w1": {"name": "laptop", "caps": ["agent.wake"]},
                        "w2": {"name": "nas", "caps": ["hermes.chat"]},
                        "w3": {"name": "gpubox", "caps": []}}
        self.calls = []

    async def call(self, cap, args, target, timeout, identity=None):
        self.calls.append((cap, target, args))
        if cap == "hermes.chat":
            return {"ok": True, "result": {"stdout": "\x1b[1m⚕ Hermes\nOn it, thanks.\nsession: 123"}}
        return {"ok": True, "result": {"ok": True}}


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_KNOWLEDGE", "1")
    monkeypatch.delenv("ROOK_HUDDLE", raising=False)
    n = HubNode(str(tmp_path), entry_points=False, build_version="1.test.node", band_max_risk="write",
                chat=ChatStore(str(tmp_path / "chat.db")))
    n.client = FakeBand()
    return n


def huddle(node):
    return node.plugin("huddle").service


async def call(node, cap, args, who=AGENT):
    body = await node.dispatch(cap, args, who)
    assert body["ok"], body
    return body["result"]


async def project(node):
    kb = node.plugin("knowledge").service
    actor = {"id": "codex.codex.gpubox", "kind": "agent"}
    c = await kb.dispatch("create", None, "concept", None, "", {"title": "Home lab"}, "c1", actor)
    return await kb.dispatch("create", None, "project", None, "", {"title": "Backups", "parent": c["id"]}, "p1", actor)


def test_caps_and_tool_are_on_the_hub(node):
    assert {"huddle.read", "huddle.write"} <= set(node.caps())
    tiers = node.announce_msg()["tiers"]
    assert tiers["huddle.read"] == "r" and tiers["huddle.write"] == "w"
    tools = [t.__name__ for t in node.plugin("huddle").mcp_tools(lambda c, a: None)]
    assert tools == ["rook_huddle"]


@pytest.mark.asyncio
async def test_status_and_questions_in_a_project_huddle(node):
    p = await project(node)
    st = await call(node, "huddle.write", {"action": "status", "project": "backups",
                                           "data": {"working_on": "Installing restic", "task": "install-restic"}})
    assert st["state"] == "working"
    room = st["room"]
    # The huddle is an ordinary chat room: readable text for every chat client.
    chat = node.chat.read(room, None, mark=False)
    assert chat["title"] == "Huddle: Backups" and set(chat["participants"]) == {AGENT, PERSON}
    assert chat["messages"][0]["text"] == "Working on: Installing restic (task install-restic)"
    # ... and it is recorded on the project, so the project page lists it.
    page = await node.plugin("knowledge").service.dispatch("board", None, None, p["id"], "", {}, None)
    assert [r["room"] for r in page["rooms"]] == [room]

    await call(node, "huddle.write", {"action": "status", "project": p["id"],
                                      "data": {"working_on": "Nightly schedule", "blocked_on": "No disk yet"}}, OTHER)
    q = await call(node, "huddle.write", {"action": "ask", "room": room, "text": "Keep 30 or 90 days of backups?",
                                          "data": {"options": ["30 days", "90 days"]}})
    view = await call(node, "huddle.read", {"room": room})
    assert {s["identity"]: s["state"] for s in view["statuses"]} == {AGENT: "working", OTHER: "blocked"}
    assert [(x["id"], x["state"], x["options"]) for x in view["questions"]] == [
        (q["question"], "open", ["30 days", "90 days"])]
    kinds = [m.get("kind") for m in view["messages"]]
    assert kinds == ["status", "status", "question"]
    assert view["messages"][-1]["text"] == "Question: Keep 30 or 90 days of backups? (options: 30 days / 90 days)"
    assert view["messages"][-1]["mentions"] == [PERSON] and view["messages"][-1]["expects_reply"]
    # A needs_decision status pins a question too.
    st = await call(node, "huddle.write", {"action": "status", "room": room,
                                           "data": {"working_on": "Retention", "needs_decision": "Delete old snapshots?"}})
    assert st["state"] == "needs_decision" and st["question"]
    assert len((await call(node, "huddle.read", {"room": room}))["questions"]) == 2


@pytest.mark.asyncio
async def test_band_huddle_by_default_and_errors(node):
    st = await call(node, "huddle.write", {"action": "status", "data": {"working_on": "x"}})
    assert node.plugin("huddle").service.store.get(st["room"])["scope"] == "band"
    bad = await node.dispatch("huddle.write", {"action": "status", "data": {}}, AGENT)
    assert not bad["ok"] and "working_on" in bad["error"]
    bad = await node.dispatch("huddle.write", {"action": "ask", "text": "x", "room": "nope"}, AGENT)
    assert not bad["ok"] and "not a huddle" in bad["error"]
    bad = await node.dispatch("huddle.write", {"action": "ask", "text": "x", "data": {"options": ["a"] * 9}}, AGENT)
    assert not bad["ok"] and "options" in bad["error"]
    q = await call(node, "huddle.write", {"action": "ask", "text": "Which disk?"})
    denied = await node.dispatch("huddle.write", {"action": "withdraw", "data": {"question": q["question"]}}, OTHER)
    assert not denied["ok"]
    assert (await call(node, "huddle.write", {"action": "withdraw", "data": {"question": q["question"]}}))["state"] == "withdrawn"
    assert (await call(node, "huddle.read", {"action": "mine"}))["questions"][0]["state"] == "withdrawn"


@pytest.mark.asyncio
async def test_answer_unpins_mentions_and_wakes_the_asker(node):
    svc = huddle(node)
    q = await call(node, "huddle.write", {"action": "ask", "text": "Reboot the NAS tonight?"})
    q2 = await call(node, "huddle.write", {"action": "ask", "text": "Pick a theme"}, "agent:hermes_nas")
    q3 = await call(node, "huddle.write", {"action": "ask", "text": "Which GPU?"}, OTHER)
    done = await svc.answer(q["question"], "Yes, after 22:00", "human:1", "Operator")
    assert done["wake"] == {"state": "waking", "worker": "laptop"}
    await asyncio.gather(*svc.wakes)
    cap, target, args = node.client.calls[0]
    assert (cap, target) == ("agent.wake", "w1") and "Yes, after 22:00" in args["note"]
    got = svc.store.question(q["question"])
    assert got["state"] == "answered" and got["answer"] == "Yes, after 22:00" and got["answered_by"] == "Operator"
    assert got["wake"]["state"] == "woken"
    msg = node.chat.read(q["room"], None, since_seq=got["answer_seq"] - 1, mark=False)["messages"][0]
    assert msg["sender"] == PERSON and msg["mentions"] == [AGENT]
    # The asker gets an unread notice for the answer on its next call.
    assert node.chat.unread_summary(AGENT)[0]["from"] == PERSON
    with pytest.raises(ValueError, match="already answered"):
        await svc.answer(q["question"], "again", "human:1", "Operator")
    # hermes: its reply is posted in the room.
    await svc.answer(q2["question"], "Dark", "human:1", "Operator")
    await asyncio.gather(*svc.wakes)
    assert node.chat.tail(q2["room"], 1)[0] == {**node.chat.tail(q2["room"], 1)[0],
                                                 "sender": "agent:hermes_nas", "text": "On it, thanks."}
    # A worker that can't be woken: the notice is all it gets.
    out = await svc.answer(q3["question"], "The 4090", "human:1", "Operator")
    assert out["wake"]["state"] == "notice" and "cannot be woken" in out["wake"]["reason"]
    open_ = [x for x in (await call(node, "huddle.read", {}))["questions"] if x["state"] == "open"]
    assert open_ == []


@pytest.mark.asyncio
async def test_mcp_tool_shape(node):
    async def invoke(cap, args):
        return await node.invoke(cap, args, AGENT)
    tool = node.plugin("huddle").mcp_tools(invoke)[0]
    st = json.loads(await tool(action="status", data={"working_on": "Docs"}))
    assert st["ok"] and st["result"]["state"] == "working"
    await tool(action="say", text="Anyone touching the README?", data={"mention": OTHER})
    read = json.loads(await tool())["result"]
    assert set(read) == {"room", "title", "statuses", "questions", "messages", "last_seq"}
    assert read["messages"][-1] == {"seq": read["last_seq"], "sender": AGENT, "text": "Anyone touching the README?"}
    assert read["statuses"][0]["working_on"] == "Docs"
    bad = json.loads(await tool(action="bogus"))
    assert bad == {"ok": False, "error": "action must be one of status, ask, say, withdraw", "code": "ValueError"}


def test_find_worker_and_hermes_cleanup():
    roster = {"w1": {"name": "laptop"}, "w2": {"name": "nas"}, "w9": {"name": "big_box"}}
    assert find_worker(roster, "agent:claude_laptop") == "w1"
    assert find_worker(roster, "agent:hermes_nas") == "w2"
    assert find_worker(roster, "agent:x_big_box") == "w9"
    assert find_worker(roster, "agent:claude") is None
    assert find_worker(roster, "agent:claude", "nas") == "w2"
    assert clean_hermes_stdout("Query: hi\n┌─⚕ Hermes─┐\nHello there\nDuration: 3s") == "Hello there"


@pytest.mark.asyncio
async def test_account_api_answers_with_csrf(node, monkeypatch):
    class Accounts:
        def session(self, token):
            return {"id": "1", "name": "Operator", "admin": True, "csrf": "tok"} if token == "s" else None
    app = Starlette(routes=routes(lambda: node, Accounts()))
    q = await call(node, "huddle.write", {"action": "ask", "text": "Ship it?"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 cookies={"rook_account": "s"}) as c:
        assert (await c.get("/huddle/account-api")).json()["csrf"] == "tok"
        r = await c.post("/huddle/account-api", json={"action": "answer", "question": q["question"], "text": "Yes"})
        assert r.status_code == 403
        r = await c.post("/huddle/account-api", json={"csrf": "tok", "action": "answer",
                                                     "question": q["question"], "text": "Yes"})
        assert r.status_code == 200 and r.json()["result"]["question"] == q["question"]
        listed = (await c.post("/huddle/account-api", json={"csrf": "tok", "action": "list"})).json()["result"]
        assert listed["huddles"][0]["open_questions"] == 0
        view = (await c.post("/huddle/account-api", json={"csrf": "tok", "action": "view",
                                                         "room": q["room"]})).json()["result"]
        assert view["questions"][0]["state"] == "answered"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/huddle/account-api")).status_code == 401
    await asyncio.gather(*huddle(node).wakes)
