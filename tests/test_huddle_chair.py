"""Chaired huddles (docs/design/huddles.md 5): the house agent chairs a
thread with roles. A person's post starts a round: the chair routes it
(call_on / answer / propose), the called roles answer in parallel in their
homes, the chair closes with a synthesis, and action cards wait for a
person's approval. Model turns run on a fake agent host that calls the hub's
real per-turn tool server, as the agenthost worker plugin would."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from rook.band_mcp.chat_rooms import ChatStore
from rook.hub.plugins.home import turn_mcp
from rook.hub.plugins.huddle.chair import validate_card, CardError
from rook.hub.plugins.huddle.service import PERSON
from rook.hub.plugins.huddle.web import routes
from tests.test_home_agent import FakeVault

GUEST = "agent:claude_laptop"


class FakeBand:
    """An agent host whose 'model' follows a script per turn kind (the turn
    id starts with the hub's ``via``: huddle-chair, huddle-<role>,
    huddle-close) and calls the hub's tools through the real turn server."""

    def __init__(self, hub):
        self.hub = hub
        hb = {"agenthost": {"harnesses": {"claude": {"version": "2.1", "logged_in": True}}}}
        self.workers = {
            "w-hosta": {"name": "hosta", "caps": ["agenthost.turn"], "hb": hb,
                        "last_seen": time.time()},
            "w-box": {"name": "box", "caps": ["info.ping", "task.read", "shell.exec"],
                      "tiers": {"info.ping": "read", "task.read": "read", "shell.exec": "exec"},
                      "last_seen": time.time()}}
        self.script: dict[str, list[dict]] = {}
        self.calls: list[SimpleNamespace] = []
        self.turns: list[dict] = []

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None,
                   principal=None):
        self.calls.append(SimpleNamespace(cap=cap, args=args, target=target, identity=identity,
                                          principal=principal))
        if cap == "agenthost.turn":
            kind = args["turn_id"].rsplit("-", 1)[0]
            turn = self.hub.plugin("home").agents.registry.get(args["mcp_token"])
            steps = self.script.get(kind) or []
            step = dict(steps.pop(0)) if steps else {"text": f"{kind} says hi"}
            if step.get("delay"):
                await asyncio.sleep(step["delay"])
            listed = [t["function"]["name"] for t in turn.tools]
            seen = []
            for name, targs in step.get("tools", []):
                r = await turn_mcp.handle(turn, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                 "params": {"name": name, "arguments": targs}})
                seen.append(json.loads(r["result"]["content"][0]["text"]))
            self.turns.append({"kind": kind, "listed": listed, "seen": seen, "args": args})
            if step.get("fail"):
                return {"ok": True, "result": {"ok": False, "error": step["fail"]}}
            return {"ok": True, "result": {"ok": True, "text": step.get("text", ""),
                                           "model": "claude-test"}}
        return {"ok": True, "result": {"pong": True, "via": target, "as": identity}}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    import rook.hub.plugins.roles as roles_plugin
    import rook.hub.plugins.settings as settings_plugin
    from rook.hub.node import HubNode
    from rook.hub.settings_store import SettingsStore
    monkeypatch.setattr(settings_plugin, "_admin_gate", lambda what: None)
    monkeypatch.setattr(roles_plugin, "_admin_gate", lambda what: None)
    monkeypatch.delenv("ROOK_HUDDLE", raising=False)
    node = HubNode(str(tmp_path), entry_points=False, vault=FakeVault({}),
                   settings_store=SettingsStore(tmp_path / "settings.db"),
                   chat=ChatStore(str(tmp_path / "chat.db")), band_max_risk="write")
    node.journal = SimpleNamespace(rows=[])
    node.journal.record = lambda **kw: node.journal.rows.append(kw)
    band = FakeBand(node)
    monkeypatch.setattr(node, "client", band, raising=False)
    node.plugin("home").agents.public_url = "https://mcp.example.com"
    for k, v in {"enabled": True, "backend": "claude-code", "agent_host": "hosta"}.items():
        node.settings.set(f"home.{k}", v, actor="human:op")
    return SimpleNamespace(node=node, band=band, svc=node.plugin("huddle").service,
                           roles=node.plugin("role"))


async def _call(hub, cap, args, who=PERSON):
    res = await hub.node.dispatch(cap, args, who)
    assert res["ok"], res
    return res["result"]


async def _roles(hub, **qa):
    await _call(hub, "role.set", {"role": {"id": "qa", "name": "QA engineer",
                                           "charter": "Watches quality.", "concerns": ["flaky jobs"],
                                           "tools": {"allow": ["info.*", "task.read"]}, **qa}})
    await _call(hub, "role.set", {"role": {"id": "pm", "name": "Project manager",
                                           "charter": "Keeps the deck honest.",
                                           "tools": {"allow": ["task.read"]}}})


async def _settle(hub):
    while hub.svc.chair.tasks:
        await asyncio.gather(*list(hub.svc.chair.tasks))


def _texts(hub, room):
    return [(m["sender"], m["text"]) for m in hub.svc.chat.tail(room, 50)]


@pytest.mark.asyncio
async def test_a_round_the_chair_calls_on_roles_they_answer_and_it_closes(hub):
    await _roles(hub)
    h = await _call(hub, "huddle.start", {"title": "Standup"})
    assert h["roles"] == ["pm", "qa"]                         # default roles that exist (no lead)
    hub.node.settings.set("huddle.default_roles", ["qa", "pm", "lead"], actor="human:op")
    h = await _call(hub, "huddle.start", {"title": "Standup"})
    assert h["roles"] == ["qa", "pm"]                         # only the ones that exist
    hub.band.script = {
        "huddle-chair": [{"tools": [("call_on", {"calls": [
            {"role": "qa", "ask": "Anything failing?"}, {"role": "pm", "ask": "What is next?"},
            {"role": "ops", "ask": "?"}]})]}],
        "huddle-qa": [{"text": "Two flaky jobs.", "tools": [("propose", {
            "kind": "job.run", "summary": "Rerun the nightly", "worker": "box", "cap": "info.ping"})]}],
        "huddle-pm": [{"text": "The deck is clear.", "delay": 0.05}],
        "huddle-close": [{"text": "QA has two flaky jobs; the deck is clear. Rerun the nightly."}]}
    posted = await _call(hub, "huddle.post", {"huddle": h["room"], "text": "What's on deck?"})
    assert posted["round"]
    await _settle(hub)
    chair, = [t for t in hub.band.turns if t["kind"] == "huddle-chair"]
    assert chair["listed"] == ["call_on", "answer", "propose"]
    assert chair["seen"][0] == {"ok": True, "called": ["qa", "pm"], "not_in_huddle": ["ops"],
                                "roles": ["pm", "qa"]}
    assert "@qa (QA engineer): Watches quality." in chair["args"]["system"]
    assert "What's on deck?" in chair["args"]["prompt"]
    qa, = [t for t in hub.band.turns if t["kind"] == "huddle-qa"]
    assert qa["args"]["role"] == "qa" and "propose" in qa["args"]["tools"]
    assert "under 1200 characters" in qa["args"]["system"]
    assert "[agent:home] Anything failing?" in qa["args"]["prompt"]
    texts = _texts(hub, h["room"])
    assert texts[0] == (PERSON, "What's on deck?")
    assert texts[1] == ("agent:home", "@qa: Anything failing?\n@pm: What is next?")
    # Roles answer in parallel: QA (fast) lands before PM (delayed).
    assert texts[2:4] == [("role:qa", "Two flaky jobs."), ("role:pm", "The deck is clear.")]
    assert texts[4] == ("agent:home", "QA has two flaky jobs; the deck is clear. Rerun the nightly.")
    view = await _call(hub, "huddle.read", {"room": h["room"]})
    r, = view["rounds"]
    assert r["state"] == "done" and r["close_seq"] and [x["state"] for x in r["replies"]] == ["done", "done"]
    card, = view["cards"]
    assert card["kind"] == "job.run" and card["by"] == "role:qa" and card["state"] == "proposed"
    assert card["seq"] == view["messages"][2]["seq"]          # attached to QA's reply


@pytest.mark.asyncio
async def test_a_mention_calls_the_role_directly_and_a_one_to_one_is_the_chair_alone(hub):
    await _roles(hub)
    h = await _call(hub, "huddle.start", {"title": "Ask QA", "roles": ["qa", "pm"]})
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "@qa is CI green?"})
    await _settle(hub)
    assert [t["kind"] for t in hub.band.turns] == ["huddle-qa"]       # no chair, no close
    assert _texts(hub, h["room"])[-1] == ("role:qa", "huddle-qa says hi")

    one = await _call(hub, "huddle.start", {"title": "Just us", "roles": []})
    hub.band.script = {"huddle-chair": [{"tools": [("answer", {"text": "Hello, Bake."})]}]}
    await _call(hub, "huddle.post", {"huddle": one["room"], "text": "hi"})
    await _settle(hub)
    chair = hub.band.turns[-1]
    assert chair["listed"] == ["answer", "propose"]                    # nobody to call on
    assert _texts(hub, one["room"])[-1] == ("agent:home", "Hello, Bake.")
    # Plain text with no tool call is the answer too.
    await _call(hub, "huddle.post", {"huddle": one["room"], "text": "and?"})
    await _settle(hub)
    assert _texts(hub, one["room"])[-1] == ("agent:home", "huddle-chair says hi")


@pytest.mark.asyncio
async def test_cards_are_approved_by_people_and_run_as_them(hub):
    await _roles(hub)
    h = await _call(hub, "huddle.start", {"title": "Ops", "roles": ["qa"]})
    hub.band.script = {"huddle-qa": [{"text": "Proposing.", "tools": [
        ("propose", {"kind": "task.create", "summary": "Track the flake", "worker": "box",
                     "cap": "info.ping", "args": {"n": 1}}),
        ("propose", {"kind": "task.create", "summary": "Track the flake again", "worker": "box",
                     "cap": "info.ping", "args": {"n": 1}}),
        ("propose", {"kind": "note", "summary": "Flakes cluster after deploys"}),
        ("propose", {"kind": "job.run", "summary": "no call"})]}]}
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "@qa ideas?"})
    await _settle(hub)
    seen = hub.band.turns[-1]["seen"]
    assert seen[1]["duplicate"] and seen[1]["card"] == seen[0]["card"]
    assert not seen[3]["ok"] and "needs the call" in seen[3]["error"]
    cards = (await _call(hub, "huddle.read", {"room": h["room"]}))["cards"]
    assert [c["kind"] for c in cards] == ["task.create", "note"]
    task_card, note = cards

    # Agents never approve.
    res = await hub.node.dispatch("huddle.card", {"huddle": h["room"], "card": task_card["id"],
                                                 "action": "approve"}, GUEST)
    assert not res["ok"] and "only a person" in res["error"]
    out = await _call(hub, "huddle.card", {"huddle": h["room"], "card": task_card["id"],
                                           "action": "approve"})
    assert out["state"] == "done" and out["result"]["result"] == {"pong": True, "via": "w-box",
                                                                  "as": PERSON}
    ping = [c for c in hub.band.calls if c.cap == "info.ping"][-1]
    assert ping.args == {"n": 1} and ping.identity == PERSON
    res = await hub.node.dispatch("huddle.card", {"huddle": h["room"], "card": task_card["id"],
                                                 "action": "dismiss"}, PERSON)
    assert not res["ok"] and "already done" in res["error"]
    assert (await _call(hub, "huddle.card", {"huddle": h["room"], "card": note["id"],
                                             "action": "dismiss"}))["state"] == "dismissed"
    assert any(r["cap"] == "huddle.card" and r["args"]["card"] == task_card["id"]
               and r["thread_id"] == h["room"] for r in hub.node.journal.rows)


@pytest.mark.asyncio
async def test_a_role_that_acts_runs_cards_in_its_scope_itself(hub):
    await _roles(hub, autonomy="act")
    h = await _call(hub, "huddle.start", {"title": "Ops", "roles": ["qa"]})
    hub.band.script = {"huddle-qa": [{"text": "Done.", "tools": [
        ("propose", {"kind": "job.run", "summary": "Ping box", "worker": "box", "cap": "info.ping"}),
        ("propose", {"kind": "job.run", "summary": "Shell", "worker": "box", "cap": "shell.exec"})]}]}
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "@qa go"})
    await _settle(hub)
    a, b = (await _call(hub, "huddle.read", {"room": h["room"]}))["cards"]
    assert a["state"] == "done" and a["result"]["result"]["as"] == "agent:home/role:qa"
    assert b["state"] == "proposed"                            # outside its tools: a person decides


@pytest.mark.asyncio
async def test_guests_rounds_limits_and_failures(hub):
    await _roles(hub)
    h = await _call(hub, "huddle.start", {"title": "Mixed", "roles": ["qa", "pm"]})
    # A guest agent's post starts nothing unless it addresses a role or the chair.
    out = await _call(hub, "huddle.post", {"huddle": h["room"], "text": "FYI deploy done"}, GUEST)
    assert out["round"] is None
    out = await _call(hub, "huddle.post", {"huddle": h["room"], "text": "@pm thoughts?"}, GUEST)
    assert out["round"]
    await _settle(hub)
    # max_role_turns bounds the round; a failing role does not sink the others.
    hub.node.settings.set("huddle.max_role_turns", 1, actor="human:op")
    hub.band.script = {"huddle-chair": [{"tools": [("call_on", {"calls": [
        {"role": "qa", "ask": "a"}, {"role": "pm", "ask": "b"}]})]}],
        "huddle-qa": [{"fail": "model refused"}]}
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "status?"})
    await _settle(hub)
    r = (await _call(hub, "huddle.read", {"room": h["room"]}))["rounds"][-1]
    assert [x["role"] for x in r["replies"]] == ["qa"] and r["replies"][0]["state"] == "failed"
    assert r["state"] == "done"
    assert "could not answer" in _texts(hub, h["room"])[-1][1]
    # Status huddles have no chair.
    res = await hub.node.dispatch("huddle.write", {"action": "status", "data": {"working_on": "x"}},
                                  GUEST)
    bad = await hub.node.dispatch("huddle.post", {"huddle": res["result"]["room"], "text": "x"},
                                  PERSON)
    assert not bad["ok"] and "no chair" in bad["error"]
    # The chair's own chat loop leaves chaired rooms alone.
    assert await hub.node.plugin("home").tick() == []


@pytest.mark.asyncio
async def test_archive_hides_and_retention_deletes(hub):
    h = await _call(hub, "huddle.start", {"title": "Old", "roles": []})
    assert [x["room"] for x in (await _call(hub, "huddle.list", {}))["huddles"]] == [h["room"]]
    await _call(hub, "huddle.archive", {"huddle": h["room"]})
    assert (await _call(hub, "huddle.list", {}))["huddles"] == []
    assert (await _call(hub, "huddle.list", {"archived": True}))["huddles"][0]["archived"]
    res = await hub.node.dispatch("huddle.post", {"huddle": h["room"], "text": "x"}, PERSON)
    assert not res["ok"] and "archived" in res["error"]
    plugin = hub.node.plugin("huddle")
    assert plugin.sweep() == []
    assert plugin.sweep(now=time.time() + 91 * 86400) == [h["room"]]
    assert not hub.svc.chat.exists(h["room"])


@pytest.mark.asyncio
async def test_rook_huddle_tool_and_the_dashboard_api(hub):
    await _roles(hub)

    async def invoke(cap, args):
        return await hub.node.invoke(cap, args, GUEST)
    tool = hub.node.plugin("huddle").mcp_tools(invoke)[0]
    started = json.loads(await tool(action="start", data={"title": "From MCP", "roles": ["qa"]}))
    room = started["result"]["room"]
    assert started["ok"] and started["result"]["roles"] == ["qa"]
    posted = json.loads(await tool(action="post", text="@qa hello", data={"room": room}))
    assert posted["ok"] and posted["result"]["round"]
    await _settle(hub)
    read = json.loads(await tool(action="read", data={"room": room}))["result"]
    assert read["roles"] == ["qa"] and read["messages"][-1]["sender"] == "role:qa"

    class Accounts:
        def session(self, token):
            return {"id": "1", "name": "Operator", "admin": True, "csrf": "tok"} if token == "s" else None
    app = Starlette(routes=routes(lambda: hub.node, Accounts()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 cookies={"rook_account": "s"}) as c:
        async def api(**body):
            r = await c.post("/huddle/account-api", json={"csrf": "tok", **body})
            return r.status_code, r.json()
        code, body = await api(action="roles")
        assert code == 200 and [r["id"] for r in body["result"]["roles"]] == ["pm", "qa"]
        assert body["result"]["chair"] == "agent:home" and body["result"]["chair_on"]
        code, body = await api(action="start", title="Web", roles=["pm"])
        web_room = body["result"]["room"]
        hub.band.script = {"huddle-pm": [{"text": "Card.", "tools": [("propose", {
            "kind": "task.create", "summary": "Do it", "worker": "box", "cap": "info.ping"})]}]}
        code, body = await api(action="send", room=web_room, text="@pm plan?")
        assert code == 200 and body["result"]["round"]
        await _settle(hub)
        code, body = await api(action="view", room=web_room)
        card = body["result"]["cards"][0]
        code, body = await api(action="card", room=web_room, card=card["id"], decision="x")
        assert code == 400
        ping = await c.post("/huddle/account-api", json={"csrf": "tok", "action": "card",
                                                         "room": web_room, "card": card["id"],
                                                         "decision": "approve"})
        assert ping.status_code == 200 and ping.json()["result"]["state"] == "done"
        assert [c_.identity for c_ in hub.band.calls if c_.cap == "info.ping"][-1] == "human:1"
        code, body = await api(action="list")
        assert {h_["room"] for h_ in body["result"]["huddles"]} >= {room, web_room}


def test_cards_are_validated():
    assert validate_card({"kind": "note", "summary": " x "}) == {"kind": "note", "summary": "x"}
    c = validate_card({"kind": "session.send", "summary": "nudge", "call": {
        "worker": "w", "cap": "sessions.send", "args": {"text": "hi"}}})
    assert c["call"] == {"worker": "w", "cap": "sessions.send", "args": {"text": "hi"}}
    for bad in ({"kind": "rm -rf", "summary": "x"}, {"kind": "note"}, "x",
                {"kind": "job.run", "summary": "x", "worker": "w"}):
        with pytest.raises(CardError):
            validate_card(bad)
