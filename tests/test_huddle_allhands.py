"""Fixes from the first live 'All hands' huddle (2026-10-19): roles told where
code lives, cards checked when proposed and failing loudly, near-duplicate
cards revised instead of added, the chair retrying and following up before
it closes, per-role tool budgets with a warning near the end, card claims
taken from the actual results, and every cut marked
``[truncated: N of M chars]``. Same fake agent host as test_huddle_chair."""
from __future__ import annotations

import json

import pytest

from rook.core.text import clip
from rook.hub.plugins.huddle.chair import near_same, validate_card, CardError
from rook.hub.plugins.huddle.service import PERSON
from rook.hub.plugins.roles import model
from tests.test_huddle_chair import FakeBand, _call, _roles, _settle, _texts, hub  # noqa: F401


@pytest.fixture(autouse=True)
def _knowledge_on(monkeypatch):
    # Task cards run task.write: the hub needs its tasks (knowledge) plugin.
    monkeypatch.setenv("ROOK_KNOWLEDGE", "1")


def _turns(hub, kind):
    return [t for t in hub.band.turns if t["kind"] == kind]


# -- cuts are visible ------------------------------------------------------------------

def test_clip_marks_the_cut():
    assert clip("abc", 5) == "abc"
    assert clip("abcdefgh", 3) == "abc\n[truncated: 3 of 8 chars]"
    assert clip("abcdefgh", 3, tail=True) == "[truncated: 3 of 8 chars]\nfgh"


def test_the_chat_store_marks_its_own_cut(tmp_path):
    from rook.band_mcp.chat_rooms import ChatStore
    store = ChatStore(str(tmp_path / "chat.db"))
    room = store.start("t", PERSON, [])["room"]
    store.send(room, PERSON, "q" * 9000, [], False)
    text = store.tail(room, 1)[0]["text"]
    assert len(text) <= 8000 and text.endswith("of 9000 chars]") and "[truncated: " in text


def test_agent_host_final_text_cut_is_marked():
    from rook.worker.plugins.agenthost import TEXT_CHARS, Turn
    t = Turn()
    t.final = "x" * (TEXT_CHARS + 10)
    text = t.reply()["text"]
    assert text.endswith(f"[truncated: {TEXT_CHARS} of {TEXT_CHARS + 10} chars]")


@pytest.mark.asyncio
async def test_a_roles_tool_result_and_sources_are_cut_visibly(hub):
    await _roles(hub)
    hub.node.settings.set("role.tool_result_chars", 2000, actor="human:op")
    qa = hub.roles.role("qa")
    big = {"ok": True, "result": {"rows": ["r" * 50] * 200}}

    async def fake_call(role, worker, cap, args):
        return big
    hub.roles.role_call = fake_call
    out = await hub.roles.run_tool(qa, "rook_call", {"worker": "box", "cap": "task.read"})
    total = len(json.dumps(big, default=str))
    assert out.endswith(f"[truncated: 2000 of {total} chars]") and len(out) < 2100
    hub.roles.store.update("qa", "human:op", sources=[{"cap": "task.read", "worker": "box"}])
    seen = await hub.roles.preread(hub.roles.role("qa"))
    assert "[truncated: 1500 of" in seen


@pytest.mark.asyncio
async def test_long_role_replies_are_posted_whole(hub):
    """reply_chars is what a role aims for; its answer is never cut there
    (the old '…' at 1200 lost the end of every long answer)."""
    await _roles(hub)
    h = await _call(hub, "huddle.start", {"title": "Long", "roles": ["qa"]})
    long = "Line of findings. " * 150                     # ~2700 chars
    huge = "abc " * 2250
    hub.band.script = {"huddle-qa": [{"text": long}, {"text": huge}]}
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "@qa report"})
    await _settle(hub)
    assert _texts(hub, h["room"])[-1] == ("role:qa", long.strip())
    # Read back (the MCP tool's lean read and the web's view) whole, too.
    from rook.hub.plugins.huddle import lean_view
    view = await _call(hub, "huddle.read", {"room": h["room"]})
    assert lean_view(view)["messages"][-1]["text"] == long.strip()
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "@qa more"})
    await _settle(hub)
    text = _texts(hub, h["room"])[-1][1]
    assert text.endswith("[truncated: 7900 of 8999 chars]")
    qa = _turns(hub, "huddle-qa")[0]
    assert "Aim for under 1200 characters" in qa["args"]["system"]


# -- where code lives -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_roles_are_told_where_code_lives(hub):
    await _roles(hub)
    h = await _call(hub, "huddle.start", {"title": "Code", "roles": ["qa"]})
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "@qa where is it?"})
    await _settle(hub)
    system = _turns(hub, "huddle-qa")[-1]["args"]["system"]
    assert "no code locations are set" in system and "never guess" in system
    hub.node.settings.set("role.code_locations",
                          ["app: worker devbox, /srv/src/app (main checkout; worktrees in /srv/src)"],
                          actor="human:op")
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "@qa and now?"})
    await _settle(hub)
    system = _turns(hub, "huddle-qa")[-1]["args"]["system"]
    assert "- app: worker devbox, /srv/src/app (main checkout; worktrees in /srv/src)" in system


def test_presets_name_no_paths_and_point_at_the_code_locations():
    for p in model.presets():
        text = json.dumps(p)
        assert "/home/" not in text and "~/" not in text, p["id"]
    lead = model.preset("lead")
    assert any("code locations" in c for c in lead["checklist"])
    assert any("task id or slug" in c for c in lead["checklist"])


@pytest.mark.asyncio
async def test_a_session_card_starts_in_a_code_location_with_a_task_id(hub):
    await _roles(hub)
    hub.band.workers["w-box"]["caps"].append("work.stream.open")
    h = await _call(hub, "huddle.start", {"title": "S", "roles": ["qa"]})
    room = h["room"]
    card = {"kind": "session.start", "summary": "Start a session", "worker": "box",
            "cap": "work.stream.open", "args": {"harness": "claude", "cwd": "/elsewhere/app"}}
    ok = await hub.svc.chair._propose(None, room, "role:qa", card)
    assert ok["ok"]                                          # no locations set: not checked
    hub.node.settings.set("role.code_locations", ["app: devbox /srv/src/app, worktrees /srv/wt"],
                          actor="human:op")
    bad = await hub.svc.chair._propose(None, room, "role:qa",
                                       {**card, "summary": "Start another", "args": {
                                           "harness": "claude", "cwd": "/home/x/old-copy"}})
    assert not bad["ok"] and "/srv/src/app" in bad["error"]
    good = await hub.svc.chair._propose(None, room, "role:qa", {
        **card, "summary": "Start in a worktree",
        "args": {"harness": "claude", "cwd": "/srv/wt/fix-deck", "task": "fix-deck-paging"}})
    assert good["ok"]
    with pytest.raises(CardError, match="task id or slug"):
        validate_card({**card, "args": {"harness": "claude", "cwd": "/srv/src/app",
                                        "task": "Step 0: find the cut. Step 1: ..."}})


# -- cards are checked when proposed, and failures say why -------------------------------

def test_task_cards_run_task_write():
    c = validate_card({"kind": "task.create", "summary": "Track it", "worker": "rook",
                       "cap": "task.create", "args": {"title": "Track it", "body": "why",
                                                      "parent": "p_1"}})
    with pytest.raises(CardError, match="parent"):
        validate_card({"kind": "task.create", "summary": "x", "worker": "rook",
                       "cap": "task.write", "args": {"action": "create", "data": {"title": "x"}}})
    assert c["call"] == {"worker": "rook", "cap": "task.write", "args": {
        "action": "create", "kind": "task",
        "data": {"title": "Track it", "body": "why", "parent": "p_1"}}}
    c = validate_card({"kind": "task.update", "summary": "Close it", "worker": "rook",
                       "cap": "task.update", "args": {"id": "t_1", "revision": 3,
                                                      "patch": {"state": "done"}}})
    assert c["call"]["args"] == {"action": "update", "kind": "task", "id": "t_1",
                                 "data": {"revision": 3, "patch": {"state": "done"}}}
    for bad in ({"title": "", "parent": "p"}, {"title": "x", "parent": "p", "colour": "red"}):
        with pytest.raises(CardError):
            validate_card({"kind": "task.create", "summary": "x", "worker": "rook",
                           "cap": "task.create", "args": bad})


@pytest.mark.asyncio
async def test_an_approved_task_card_creates_the_task_once(hub):
    await _roles(hub)
    h = await _call(hub, "huddle.start", {"title": "T", "roles": ["qa"]})
    await _call(hub, "task.write", {"action": "create", "kind": "concept", "request_id": "c1",
                                    "data": {"title": "Rook", "slug": "rook"}})
    await _call(hub, "task.write", {"action": "create", "kind": "project", "request_id": "p1",
                                    "data": {"title": "Beta", "slug": "beta", "parent": "rook"}})
    out = await hub.svc.chair._propose(None, h["room"], "role:qa", {
        "kind": "task.create", "summary": "Make the deck readable", "worker": "rook",
        "cap": "task.create", "args": {"title": "Make the deck readable", "body": "paging",
                                       "parent": "beta"}})
    assert out["ok"], out
    done = await _call(hub, "huddle.card", {"huddle": h["room"], "card": out["card"],
                                            "action": "approve"})
    assert done["state"] == "done", done
    ran, = [c for c in hub.band.calls if c.cap == "task.write"]
    assert ran.identity == PERSON and ran.target == hub.node.worker_id
    assert ran.args == {"action": "create", "kind": "task", "request_id": f"huddle-card:{out['card']}",
                        "data": {"title": "Make the deck readable", "body": "paging",
                                 "parent": "beta"}}
    # The same args through the hub's own task.write make exactly one task.
    for _ in range(2):
        await _call(hub, "task.write", ran.args)
    found = await _call(hub, "task.read", {"action": "deck", "id": "beta"})
    assert [t["title"] for t in found["deck"][0]["todo"]] == ["Make the deck readable"]


@pytest.mark.asyncio
async def test_a_card_for_a_cap_the_worker_lacks_is_refused_when_proposed(hub):
    await _roles(hub)
    h = await _call(hub, "huddle.start", {"title": "T", "roles": ["qa"]})
    out = await hub.svc.chair._propose(None, h["room"], "role:qa", {
        "kind": "job.run", "summary": "Do a thing", "worker": "box", "cap": "task.frobnicate"})
    assert not out["ok"] and "box has no cap task.frobnicate" in out["error"] \
        and "task.read" in out["error"]
    out = await hub.svc.chair._propose(None, h["room"], "role:qa", {
        "kind": "job.run", "summary": "Do a thing", "worker": "nowhere", "cap": "info.ping"})
    assert not out["ok"] and "is on the band now" in out["error"]
    assert hub.svc.store.cards(h["room"]) == []


@pytest.mark.asyncio
async def test_a_failed_card_says_why_on_the_card_and_in_the_room(hub):
    await _roles(hub)
    box = hub.band.workers["w-box"]
    box["caps"].append("boom.now")
    real = hub.band.call

    async def call(cap, args=None, **kw):
        if cap == "boom.now":
            return {"ok": False, "error": "the boom went off"}
        return await real(cap, args, **kw)
    hub.band.call = call
    h = await _call(hub, "huddle.start", {"title": "F", "roles": ["qa"]})
    out = await hub.svc.chair._propose(None, h["room"], "role:qa", {
        "kind": "job.run", "summary": "Set it off", "worker": "box", "cap": "boom.now"})
    res = await _call(hub, "huddle.card", {"huddle": h["room"], "card": out["card"],
                                           "action": "approve"})
    assert res["state"] == "failed" and res["error"] == "the boom went off"
    sender, text = _texts(hub, h["room"])[-1]
    assert sender == "agent:home" and f"Card {out['card']}" in text \
        and "failed: the boom went off" in text
    view = await _call(hub, "huddle.read", {"room": h["room"]})
    assert view["cards"][0]["error"] == "the boom went off"
    from rook.hub.plugins.huddle import lean_view
    assert lean_view(view)["cards"][0]["error"] == "the boom went off"


# -- duplicates ------------------------------------------------------------------------

def test_near_same_cards():
    a = {"kind": "task.create", "summary": "Make sure roles can read the full task deck: add "
         "project_fields and project paging to task.read, and make every cut visible",
         "call": {"worker": "rook", "cap": "task.write", "args": {"data": {
             "title": "Make sure roles can read the full task deck"}}}}
    b = {"kind": "task.create", "summary": "Make sure roles can read the full task deck: trim "
         "project headers, page over projects, and make every cut-off visible",
         "call": {"worker": "rook", "cap": "task.write", "args": {"data": {
             "title": "Make sure roles can read the full task deck"}}}}
    assert near_same(a, b)
    assert not near_same(a, {**b, "kind": "task.update"})
    c = {"kind": "note", "summary": "Create a task for the PM deck fix"}
    d = {"kind": "note", "summary": "Create a task for the QA flaky test fix"}
    assert not near_same(c, d)


@pytest.mark.asyncio
async def test_the_proposer_revises_its_waiting_card_instead_of_adding_one(hub):
    await _roles(hub)
    h = await _call(hub, "huddle.start", {"title": "D", "roles": ["qa", "pm"]})
    room, chair = h["room"], hub.svc.chair

    def task(summary, body):
        return {"kind": "task.create", "summary": summary, "worker": "rook", "cap": "task.write",
                "args": {"action": "create", "data": {
                    "title": "Make sure roles can read the full task deck", "body": body,
                    "parent": "beta"}}}
    first = await chair._propose("r_1", room, "agent:home", task("Deck: paging", "v1"))
    again = await chair._propose("r_2", room, "agent:home", task("Deck: paging, marked cuts", "v2"))
    assert again == {"ok": True, "card": first["card"], "revised": True, "state": "proposed"}
    cards = hub.svc.store.cards(room)
    assert len(cards) == 1 and cards[0]["summary"] == "Deck: paging, marked cuts" \
        and cards[0]["call"]["args"]["data"]["body"] == "v2"
    other = await chair._propose("r_3", room, "role:pm", task("Deck read fix", "v3"))
    assert other["duplicate"] and other["card"] == first["card"]
    assert len(hub.svc.store.cards(room)) == 1
    # Explicit revise: only your own waiting card.
    note = await chair._propose("r_3", room, "role:pm", {"kind": "note", "summary": "PM note"})
    bad = await chair._propose("r_4", room, "agent:home",
                               {"kind": "note", "summary": "x", "revise": note["card"]})
    assert not bad["ok"] and "only its proposer" in bad["error"]
    ok = await chair._propose("r_4", room, "role:pm",
                              {"kind": "note", "summary": "PM note, revised", "revise": note["card"]})
    assert ok["revised"] and hub.svc.store.card(note["card"])["summary"] == "PM note, revised"
    # The chair sees what is waiting, so it revises rather than re-proposes.
    text = chair.pending_text(room)
    assert f"{first['card']} [task.create, yours]" in text and "revise=<id>" in text
    # A decided card is not revised: a new one is made.
    await _call(hub, "huddle.card", {"huddle": room, "card": first["card"], "action": "dismiss"})
    fresh = await chair._propose("r_5", room, "agent:home", task("Deck: paging", "v4"))
    assert fresh["ok"] and fresh["card"] != first["card"] and not fresh.get("revised")


# -- the chair keeps going -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_chair_follows_up_until_settled_within_its_limit(hub):
    await _roles(hub)
    h = await _call(hub, "huddle.start", {"title": "F", "roles": ["qa", "pm"]})
    hub.node.settings.set("huddle.max_followups", 2, actor="human:op")
    follow = {"tools": [("call_on", {"calls": [{"role": "qa", "ask": "Which job exactly?"}]})]}
    hub.band.script = {
        "huddle-chair": [{"tools": [("call_on", {"calls": [{"role": "qa", "ask": "a"},
                                                           {"role": "pm", "ask": "b"}]})]}],
        "huddle-qa": [{"text": "Something is flaky."}, {"text": "The nightly, job 7."},
                      {"text": "Still job 7."}],
        "huddle-close": [follow, follow, {"text": "Job 7 is flaky; rerun it."}]}
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "status?"})
    await _settle(hub)
    closes = _turns(hub, "huddle-close")
    assert len(closes) == 3                       # the first close and two follow-ups
    assert "call_on" in closes[0]["listed"] and "call_on" not in closes[2]["listed"]
    assert "no more this round" in closes[2]["args"]["system"]
    assert len(_turns(hub, "huddle-qa")) == 3
    r = (await _call(hub, "huddle.read", {"room": h["room"]}))["rounds"][-1]
    assert [x["role"] for x in r["replies"]] == ["qa", "pm", "qa", "qa"]
    assert r["state"] == "done"
    texts = [t for _, t in _texts(hub, h["room"])]
    assert texts.count("@qa: Which job exactly?") == 2


@pytest.mark.asyncio
async def test_a_settled_single_answer_posts_no_close(hub):
    await _roles(hub)
    h = await _call(hub, "huddle.start", {"title": "One", "roles": ["qa", "pm"]})
    hub.band.script = {
        "huddle-chair": [{"tools": [("call_on", {"calls": [{"role": "qa", "ask": "green?"}]})]}],
        "huddle-qa": [{"text": "All green."}],
        "huddle-close": [{"text": "SETTLED"}]}
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "CI?"})
    await _settle(hub)
    assert _texts(hub, h["room"])[-1] == ("role:qa", "All green.")
    assert len(_turns(hub, "huddle-close")) == 1


@pytest.mark.asyncio
async def test_a_direct_question_that_fails_is_retried_then_closed(hub):
    await _roles(hub)
    h = await _call(hub, "huddle.start", {"title": "Direct", "roles": ["qa"]})
    hub.band.script = {"huddle-qa": [{"fail": "agent: stopped after 34 tool calls"},
                                     {"fail": "still stuck"}],
                       "huddle-close": [{"text": "QA could not finish; I need you to say "
                                                 "which job to look at."}]}
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "@qa dig in"})
    await _settle(hub)
    qa = _turns(hub, "huddle-qa")
    assert len(qa) == 2 and "stopped before you answered" in qa[1]["args"]["prompt"] \
        and "stopped after 34 tool calls" in qa[1]["args"]["prompt"]
    close = _turns(hub, "huddle-close")[0]
    assert "@qa: hosta: still stuck" in close["args"]["prompt"]
    assert _texts(hub, h["room"])[-1][1].startswith("QA could not finish")


# -- tool budgets ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_budgets_per_role_and_category(hub):
    await _roles(hub)
    assert hub.roles.tool_limit(hub.roles.role("pm")) == 12              # the setting
    lead = model.preset("lead")
    await _call(hub, "role.set", {"role": lead})
    assert hub.roles.tool_limit(hub.roles.role("lead")) == 30            # engineering
    # A lead saved before categories existed still gets the engineering budget.
    legacy = {k: v for k, v in lead.items() if k != "category"}
    hub.roles.store.save({**legacy, "id": "lead"}, "human:op")
    assert hub.roles.tool_limit(hub.roles.role("lead")) == 30
    await _call(hub, "role.set", {"role": {**hub.roles.role("lead"), "max_tool_calls": 40}})
    assert hub.roles.tool_limit(hub.roles.role("lead")) == 40            # its own
    with pytest.raises(model.RoleError):
        model.validate({"id": "x", "max_tool_calls": 500})
    assert "max_tool_calls" not in model.validate({"id": "x", "max_tool_calls": 0})
    sre = model.preset("sre")
    assert [c.get("max_tool_calls") for c in model.categories() if c["id"] == sre["category"]] \
        == [24]


@pytest.mark.asyncio
async def test_a_role_is_told_its_budget_and_stopped_at_the_hub(hub):
    await _roles(hub)
    hub.roles.store.update("qa", "human:op", max_tool_calls=4)
    h = await _call(hub, "huddle.start", {"title": "B", "roles": ["qa"]})
    ping = ("rook_call", {"worker": "box", "cap": "info.ping"})
    hub.band.script = {"huddle-qa": [{"text": "Partial answer.", "tools": [ping] * 6}]}
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "@qa check"})
    await _settle(hub)
    turn = _turns(hub, "huddle-qa")[0]
    assert turn["args"]["max_tool_calls"] == 4 + 4                       # hub budget + slack
    assert "at most 4 tool calls" in turn["args"]["system"]
    seen = turn["seen"]
    pings = [c for c in hub.band.calls if c.cap == "info.ping"]
    assert len(pings) == 4                                               # the 5th and 6th refused
    assert seen[4] == seen[5] and "tool budget spent (4 calls)" in seen[4]["error"]
    assert "[budget: 0 of 4 tool calls left; answer now" in seen[3]
    assert _texts(hub, h["room"])[-1] == ("role:qa", "Partial answer.")


@pytest.mark.asyncio
async def test_budget_notes_ride_the_last_tool_results(hub):
    await _roles(hub)
    hub.roles.store.update("qa", "human:op", max_tool_calls=6)
    qa = hub.roles.role("qa")
    seen = []

    async def run(cfg, msgs, *, tools, handler, via, max_tool_calls, role):
        for _ in range(7):
            seen.append(await handler("rook_workers", {}))
        return {"content": "ok", "model": "m", "worker": "hosta", "harness": "claude",
                "tools_used": [], "latency_ms": 1}
    hub.node.plugin("home").agents.run = run
    hub.roles.store.update("qa", "human:op", host="hosta")
    await hub.roles.turn(hub.roles.role("qa"), "go")
    assert "[budget" not in seen[0] and "[budget" not in seen[1]
    assert seen[2].endswith("[budget: 3 of 6 tool calls left; answer with what you have soon]")
    assert seen[5].endswith("[budget: 0 of 6 tool calls left; answer now with what you have]")
    assert "tool budget spent" in seen[6]
    assert qa["id"] == "qa"


# -- the chair only claims what happened ---------------------------------------------------

@pytest.mark.asyncio
async def test_the_chairs_message_lists_the_cards_it_actually_made(hub):
    await _roles(hub)
    h = await _call(hub, "huddle.start", {"title": "C", "roles": ["qa", "pm"]})
    hub.band.script = {
        "huddle-chair": [{"tools": [
            ("propose", {"kind": "note", "summary": "Decision: page the deck"}),
            ("propose", {"kind": "job.run", "summary": "Run a cap box lacks", "worker": "box",
                         "cap": "nope.never"}),
            ("answer", {"text": "Here is where we are."})]}]}
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "where are we?"})
    await _settle(hub)
    card, = hub.svc.store.cards(h["room"])
    sender, text = _texts(hub, h["room"])[-1]
    assert sender == "agent:home" and text.startswith("Here is where we are.")
    assert f"- Card {card['id']} (note, waiting for your approval): Decision: page the deck" in text
    assert "- Not proposed (job.run: Run a cap box lacks): not proposed: box has no cap" in text
    assert card["seq"] == hub.svc.chat.tail(h["room"], 1)[0]["seq"]
    chair = _turns(hub, "huddle-chair")[0]
    assert "only after its tool call returned ok" in chair["args"]["system"]
