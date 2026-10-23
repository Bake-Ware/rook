"""The house agent as front door (docs/design/front-door.md,
rook/hub/plugins/home/front.py): every request is routed one of four ways
(answer, do, dispatch, huddle) or becomes a one-line ask; the hub, not the
model, says what was done; disputes are ruled from the trust map or the
playbook with a citation, or asked; the digest lists decisions with a
one-tap overturn that only tightens; the playbook takes corrections.

Model turns run on the fake agent host of test_huddle_chair, which calls the
hub's real per-turn tool server."""
from __future__ import annotations

import json
import time

import pytest

from rook.hub import trust as T
from rook.hub.plugins.attention.service import card_for
from rook.hub.plugins.home import front as F
from rook.hub.plugins.home.playbook import Playbook, PlaybookError, direction, parse
from rook.hub.plugins.huddle.service import PERSON
from tests.test_huddle_chair import _call, _roles, _settle, _texts, hub  # noqa: F401
from tests.test_trust import BAKE_KAIJU, BAKE_PIANO, seeded

ASKER = "agent:voice"


@pytest.fixture
def front(hub):  # noqa: F811
    hub.node.settings.set("home.front_door", True, actor="human:op")
    hub.node.plugin("trust")._store = seeded()
    hub.band.workers.update({
        "w-piano": {"name": "pianobar", "caps": ["pianobar.restart", "pianobar.now_playing",
                                                 "file.delete"],
                    "tiers": {"pianobar.restart": "exec", "pianobar.now_playing": "read"},
                    "last_seen": time.time()},
        "w-kaiju": {"name": "kaiju", "caps": ["shell.exec"], "tiers": {"shell.exec": "exec"},
                    "last_seen": time.time()},
    })
    hub.front = hub.node.plugin("home").front
    return hub


def _script(hub, kind, *steps):
    hub.band.script[kind] = [dict(s) for s in steps]


def _ran(hub, cap):
    return [c for c in hub.band.calls if c.cap == cap]


async def _ask(hub, question, source="voice"):
    return await _call(hub, "home.ask", {"question": question, "source": source}, ASKER)


# -- routing: the four paths ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_question_is_answered_directly(front):
    _script(front, "front-voice", {"text": "It's the pianobar station Jazz."})
    out = await _ask(front, "what's playing?")
    assert out["path"] == "answer" and out["answer"] == "It's the pianobar station Jazz."
    assert out["done"] == [] and out["refs"] == []
    turn = front.band.turns[-1]
    for tool in ("do_action", "dispatch_session", "open_huddle", "ask_person", "digest"):
        assert tool in turn["listed"]
    # The playbook is in the router's prompt, with rule ids to cite.
    assert "Just do it" in turn["args"]["system"] and "[pb-" in turn["args"]["system"]


@pytest.mark.asyncio
async def test_a_quick_action_on_ship_it_runs_and_says_done_with_the_citation(front):
    _script(front, "front-voice", {"tools": [("do_action", {
        "worker": "pianobar", "cap": "pianobar.restart", "args": {}, "summary":
        "restart pianobar"})], "text": "The music should be back in a few seconds."})
    out = await _ask(front, "the music stopped, fix it")
    assert out["path"] == "do"
    assert out["answer"].startswith("Done: restart pianobar.")
    (call,) = _ran(front, "pianobar.restart")
    assert call.identity == "agent:home" and call.principal.id == "home:home"
    d = front.front.digest()["decisions"][0]
    assert d["kind"] == "did" and "pianobar-super" in d["cite"] and BAKE_PIANO in d["cite"]
    assert d["name"] and len(d["name"].split("-")) == 3


@pytest.mark.asyncio
async def test_a_code_change_is_dispatched_and_tracked_by_name(front, monkeypatch):
    rp = front.node.plugin("role")
    await _call(front, "role.set", {"role": {"id": "lead", "name": "Lead developer",
                                             "charter": "Writes code.", "autonomy": "routine"}})
    started = []

    async def fake_start(role, **kw):
        started.append((role["id"], kw))
        return {"ok": True, "id": "s1", "name": "brave-otter-lamp", "branch": kw["branch"]}
    monkeypatch.setattr(rp, "start_session", fake_start)
    monkeypatch.setattr(rp, "session_trust", lambda location, branch: "")
    _script(front, "front-voice", {"tools": [("dispatch_session", {
        "instructions": "Fix the typo in README", "branch": "fix/readme-typo",
        "title": "README typo"})], "text": "It will open a pull request."})
    out = await _ask(front, "fix the typo in the readme")
    assert out["path"] == "dispatch" and out["refs"] == ["brave-otter-lamp"]
    assert out["answer"].startswith("I've got the team on it: brave-otter-lamp.")
    assert started[0][0] == "lead" and started[0][1]["branch"] == "fix/readme-typo"
    assert front.front.digest()["decisions"][0]["kind"] == "dispatched"


@pytest.mark.asyncio
async def test_fuzzy_work_opens_a_huddle_episode_the_roles_pick_up(front):
    await _roles(front)
    _script(front, "front-voice", {"tools": [("open_huddle", {
        "title": "Should voice replies be shorter?", "roles": ["qa", "pm"],
        "brief": "Weigh shorter voice replies against clarity."})], "text": ""})
    front.band.script["huddle-chair"] = [{"tools": [("answer", {"text": "Framing it."})]}]
    out = await _ask(front, "think about whether voice replies should be shorter")
    await _settle(front)
    assert out["path"] == "huddle"
    name = out["refs"][0]
    assert out["answer"] == f"I've got the team on it: {name}."
    h = next(x for x in front.svc.huddles() if x.get("name") == name)
    texts = _texts(front, h["room"])
    assert texts[0][0] == "agent:home" and "Weigh shorter voice replies" in texts[0][1]
    assert any(t["kind"] == "huddle-chair" for t in front.band.turns)   # a round started


# -- asks: sacred, irreversible, the playbook ------------------------------------------------

@pytest.mark.asyncio
async def test_a_sacred_action_becomes_a_one_line_ask_in_the_approvals_list(front):
    _script(front, "front-voice", {"tools": [("do_action", {
        "worker": "kaiju", "cap": "shell.exec", "args": {"argv": ["systemctl", "restart",
                                                                  "strata"]},
        "summary": "restart strata on kaiju"})], "text": ""})
    out = await _ask(front, "restart strata on kaiju")
    assert out["path"] == "ask" and not _ran(front, "shell.exec")
    assert out["answer"].startswith("I need you for one thing: Restart strata on kaiju?")
    assert "Done" not in out["answer"]
    relay = front.node.plugin("approvals").relay
    (item,) = [i for i in relay.items() if i["kind"] == "house"]
    assert item["name"] == out["asks"][0]
    # One line, Yes / No / Explain; the explanation is a few plain sentences that cite Bake.
    assert "\n" not in item["line"] and len(item["line"]) <= F.LINE_MAX
    assert item["options"] == ["Yes", "No", "Explain"]
    assert BAKE_KAIJU in item["explain"] and len(item["explain"]) <= F.EXPLAIN_MAX
    card = card_for(item)
    assert card["line"] == item["line"] and card["actions"] == ["yes", "no", "explain"]
    assert BAKE_KAIJU in card["explain"]
    more = front.front.explain(item["name"], more=True)
    assert "systemctl" in more["more"] and "kaiju" in more["cite"]


@pytest.mark.asyncio
async def test_an_irreversible_action_asks_even_on_ship_it(front):
    _script(front, "front-voice", {"tools": [("do_action", {
        "worker": "pianobar", "cap": "file.delete", "args": {"path": "/tmp/x"},
        "summary": "delete the old station list"})], "text": ""})
    out = await _ask(front, "delete the old station list")
    assert out["path"] == "ask" and not _ran(front, "file.delete")


@pytest.mark.asyncio
async def test_yes_acts_then_offers_to_remember_and_yes_again_updates_the_playbook(front):
    _script(front, "front-voice", {"tools": [("do_action", {
        "worker": "pianobar", "cap": "pianobar.restart", "summary": "restart pianobar",
        "ask_first": True})], "text": ""})
    out = await _ask(front, "restart the music")
    assert out["path"] == "ask" and not _ran(front, "pianobar.restart")
    res = await front.front.answer(out["asks"][0], "yes", by="Bake", quote="yes")
    assert res["ok"] and res["note"] == "Done: restart pianobar."
    assert len(_ran(front, "pianobar.restart")) == 1
    offer = front.front.store.find_ask(res["remember"])
    assert offer["kind"] == "remember" and offer["line"].startswith("Next time just restart")
    again = await front.front.answer(offer["name"], "yes", by="Bake", quote="yes")
    assert again["ok"]
    rules = [r for r in front.front.playbook.rules() if r.section == "do"]
    assert any("pianobar.restart on pianobar" in r.text and "Bake" in r.by for r in rules)
    with pytest.raises(F.FrontError, match="already"):
        await front.front.answer(out["asks"][0], "no", by="Bake")


@pytest.mark.asyncio
async def test_no_does_nothing(front):
    _script(front, "front-voice", {"tools": [("do_action", {
        "worker": "kaiju", "cap": "shell.exec", "args": {"argv": ["true"]},
        "summary": "run true on kaiju"})], "text": ""})
    out = await _ask(front, "run true on kaiju")
    res = await front.front.answer(out["asks"][0], "no", by="Bake", reason="not now")
    assert res["note"] == "Nothing was done." and not _ran(front, "shell.exec")
    assert front.front.store.asks("open") == []


@pytest.mark.asyncio
async def test_the_phone_card_answers_through_the_relay(front, monkeypatch):
    _script(front, "front-voice", {"tools": [("do_action", {
        "worker": "pianobar", "cap": "pianobar.restart", "summary": "restart pianobar",
        "ask_first": True})], "text": ""})
    out = await _ask(front, "restart the music")
    relay = front.node.plugin("approvals").relay
    monkeypatch.setattr(relay, "may_relay", lambda principal: principal == "device:w-phone")
    with pytest.raises(PermissionError):
        await relay.decide(out["asks"][0], "approve", {"who": "Bake", "quote": "Tapped Yes",
                                                       "session": "s"}, relayer="token:other")
    got = await relay.decide(out["asks"][0], "approve",
                             {"who": "Bake", "quote": "Tapped Yes on the decision card",
                              "session": "card"}, relayer="device:w-phone",
                             relayer_label="phone")
    assert got["kind"] == "house" and got["state"] == "yes"
    assert len(_ran(front, "pianobar.restart")) == 1
    a = front.front.store.find_ask(out["asks"][0])
    assert a["decided_by"] == "Bake via phone" and a["quote"].startswith("Tapped Yes")


# -- only the back claims actions are done -----------------------------------------------------

@pytest.mark.asyncio
async def test_a_model_claiming_it_acted_without_an_action_is_corrected(front):
    _script(front, "front-voice", {"text": "Done! I restarted pianobar for you."})
    out = await _ask(front, "restart pianobar")
    assert out["path"] == "answer" and out["done"] == []
    assert not out["answer"].lower().startswith("done")
    assert out["answer"].startswith("Nothing has been done yet.")


@pytest.mark.asyncio
async def test_a_failed_action_is_never_reported_done(front, monkeypatch):
    real = front.band.call

    async def failing(cap, args=None, **kw):
        if cap == "pianobar.restart":
            return {"ok": False, "error": "pianobar is not running"}
        return await real(cap, args, **kw)
    monkeypatch.setattr(front.band, "call", failing)
    _script(front, "front-voice", {"tools": [("do_action", {
        "worker": "pianobar", "cap": "pianobar.restart", "summary": "restart pianobar"})],
        "text": "Done, all fixed."})
    out = await _ask(front, "restart pianobar")
    assert out["path"] == "answer" and "Done" not in out["answer"]
    assert out["answer"].startswith("Not done: restart pianobar (pianobar is not running).")
    assert front.front.digest()["decisions"] == []


def test_compose_only_says_done_for_actions_that_returned_ok():
    st = {"did": [{"summary": "x", "ok": True}], "started": [], "asked": []}
    assert F.compose(st, "Done. It worked.")[0] == "Done: x. It worked."
    st = {"did": [], "started": [{"path": "huddle", "name": "a-b-c"}], "asked": []}
    assert F.compose(st, "")[0] == "I've got the team on it: a-b-c."
    st = {"did": [], "started": [], "asked": []}
    assert F.compose(st, "I've deployed it.")[0].startswith("Nothing has been done yet.")
    assert F.compose(st, "The hub runs build 860.") == ("The hub runs build 860.", "answer")


# -- disputes ------------------------------------------------------------------------------------

def test_a_dispute_the_trust_map_settles_is_ruled_cited_and_logged(front):
    out = front.front.settle("Deploy the pianobar fix now or after review?",
                             [{"who": "qa", "says": "wait for review"},
                              {"who": "lead", "says": "ship it now"}],
                             cap="pianobar.restart", worker="pianobar", ruling="ship it now")
    assert out["ruled"] and "pianobar-super" in out["text"] and BAKE_PIANO in out["text"]
    assert front.front.store.asks("open") == []
    d = front.front.digest()["decisions"][0]
    assert d["kind"] == "ruled" and d["name"] == out["decision"]


def test_a_sacred_or_uncovered_dispute_is_asked_in_one_line(front):
    out = front.front.settle("Restart strata to free GPU memory?",
                             [{"who": "sre", "says": "restart"}, {"who": "ml", "says": "no"}],
                             cap="shell.exec", worker="kaiju", ruling="restart")
    assert not out["ruled"] and out["why"] == "sacred"
    a = front.front.store.find_ask(out["asked"])
    assert a["kind"] == "dispute" and "\n" not in a["line"] and len(a["line"]) <= F.LINE_MAX
    gap = front.front.settle("OAuth2 or SAML for the new app?",
                             [{"who": "security", "says": "OAuth2: SAML was compromised"},
                              {"who": "lead", "says": "SAML is already wired"}])
    assert gap["why"] == "gap"
    assert "nothing in the trust map or my playbook settles it" in \
        front.front.store.find_ask(gap["asked"])["explain"]


def test_a_playbook_rule_settles_a_gap_and_is_cited(front):
    row = front.front.playbook.change("add", "rule", "When security and lead disagree on a "
                                      "library, pick the one already in use.", who="Bake",
                                      kind="person", words="pick what we already use")
    out = front.front.settle("OAuth2 or SAML for the new app?",
                             [{"who": "security", "says": "OAuth2"}, {"who": "lead",
                                                                       "says": "SAML"}],
                             ruling="SAML (already in use)", rule=row["id"])
    assert out["ruled"] and row["id"] in out["text"]


@pytest.mark.asyncio
async def test_the_chair_settles_through_the_front_door_in_its_close(front):
    await _roles(front)
    h = await _call(front, "huddle.start", {"title": "Auth", "roles": ["qa", "pm"]})
    front.band.script = {
        "huddle-chair": [{"tools": [("call_on", {"calls": [{"role": "qa", "ask": "?"},
                                                           {"role": "pm", "ask": "?"}]})]}],
        "huddle-qa": [{"text": "Restart pianobar now."}],
        "huddle-pm": [{"text": "Wait until tonight."}],
        "huddle-close": [{"tools": [("settle", {
            "question": "Restart pianobar now or tonight?",
            "positions": [{"who": "qa", "says": "now"}, {"who": "pm", "says": "tonight"}],
            "worker": "pianobar", "cap": "pianobar.restart", "ruling": "now"})], "text": ""}],
    }
    await _call(front, "huddle.post", {"huddle": h["room"], "text": "music is broken"})
    await _settle(front)
    close = next(t for t in front.band.turns if t["kind"] == "huddle-close")
    assert "settle" in close["listed"] and close["seen"][0]["ruled"] is True
    assert any(s == "agent:home" and t.startswith("Ruling (") for s, t in _texts(front, h["room"]))


# -- the digest and overturning -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_overturning_a_ship_it_decision_raises_the_trust_entry(front):
    _script(front, "front-voice", {"tools": [("do_action", {
        "worker": "pianobar", "cap": "pianobar.restart", "summary": "restart pianobar"})],
        "text": ""})
    await _ask(front, "restart pianobar")
    dig = await _call(front, "home.digest", {})
    assert dig["spoken"].startswith("1 decision I made for you: restart pianobar")
    d = dig["decisions"][0]
    out = front.front.overturn(d["name"], by="Bake", words="no, ask me about the music")
    assert "trust entry pianobar-super: ship-it -> guarded" in out["effects"]
    st = front.node.plugin("trust").store
    assert next(e for e in st.current()["entries"] if e["id"] == "pianobar-super")["level"] \
        == "guarded"
    with pytest.raises(F.FrontError, match="already overturned"):
        front.front.overturn(d["name"], by="Bake", words="again")
    assert front.front.digest()["decisions"][0]["overturned"]["by"] == "Bake"


def test_overturning_a_playbook_decision_removes_the_rule_and_adds_an_ask_rule(front):
    row = front.front.playbook.change("add", "rule", "Prefer the library already in use.",
                                      who="Bake", kind="person", words="use what we have")
    out = front.front.settle("OAuth2 or SAML?", [{"who": "a", "says": "x"}],
                             ruling="SAML", rule=row["id"])
    res = front.front.overturn(out["decision"], by="Bake", words="no, ask me about auth")
    assert f"removed playbook rule {row['id']}" in res["effects"]
    rules = front.front.playbook.rules()
    assert not any(r.id == row["id"] for r in rules)
    assert any(r.section == "ask" and r.text.startswith("Ask me before: OAuth2 or SAML?")
               for r in rules)


@pytest.mark.asyncio
async def test_only_the_person_overturns_through_the_cap(front):
    out = front.front.settle("restart?", [{"who": "a", "says": "b"}], cap="pianobar.restart",
                             worker="pianobar", ruling="yes")
    res = await front.node.dispatch("home.overturn", {"decision": out["decision"],
                                                      "words": "no"}, ASKER)
    assert not res["ok"]
    assert front.front.digest()["decisions"][0]["overturned"] is None


# -- the playbook ---------------------------------------------------------------------------------

def test_playbook_directions():
    assert direction("add", "ask") == "tighten" and direction("remove", "ask") == "loosen"
    assert direction("add", "do") == "loosen" and direction("remove", "do") == "tighten"
    assert direction("add", "huddle") == "tighten" and direction("add", "rule") == "loosen"


def test_the_playbook_is_a_readable_file_with_ids_and_history(tmp_path):
    pb = Playbook(str(tmp_path / "playbook.md"))
    text = pb.text()
    assert text.startswith("# House agent playbook") and "## Ask me first" in text
    assert all(r.id.startswith("pb-") for r in pb.rules())
    edited = text.replace("## Just answer", "## Just answer\n- Weather questions.")
    out = pb.save_text(edited, who="Bake", words="weather is just an answer")
    assert [c["op"] for c in out["changes"]] == ["add"]
    rule = next(r for r in pb.rules() if r.text == "Weather questions.")
    assert rule.section == "answer" and f"[{rule.id}]" in pb.text()
    assert pb.history(5)[0]["words"] == "weather is just an answer"
    with pytest.raises(PlaybookError, match="loosens"):
        pb.change("add", "do", "Deploy the hub whenever.", who="house", kind="house",
                  words="x y")


@pytest.mark.asyncio
async def test_a_correction_tightens_now_and_a_loosening_is_asked(front):
    _script(front, "front-voice", {"tools": [("playbook_edit", {
        "op": "add", "section": "ask", "text": "Anything on the hub after 10pm.",
        "words": "ask me about hub stuff after 10pm"})], "text": "Got it."})
    out = await _ask(front, "ask me about hub stuff after 10pm")
    assert any(r.text == "Anything on the hub after 10pm." and r.section == "ask"
               for r in front.front.playbook.rules())
    assert out["path"] == "answer" and front.front.digest()["decisions"][0]["kind"] == "tightened"
    _script(front, "front-voice", {"tools": [("playbook_edit", {
        "op": "add", "section": "do", "text": "Restart pianobar when it stops.",
        "words": "next time just do that"})], "text": ""})
    out = await _ask(front, "next time just do that")
    assert out["path"] == "ask"
    assert not any(r.text == "Restart pianobar when it stops." for r in front.front.playbook.rules())
    a = front.front.store.find_ask(out["asks"][0])
    assert a["kind"] == "playbook" and a["line"].startswith('Add to my playbook: "Restart')
    await front.front.answer(a["id"], "yes", by="Bake", quote="yes")
    assert any(r.text == "Restart pianobar when it stops." and r.section == "do"
               for r in front.front.playbook.rules())


# -- every client reaches the same front door ------------------------------------------------------

@pytest.mark.asyncio
async def test_the_web_one_to_one_huddle_goes_through_the_front_door(front):
    h = await _call(front, "huddle.start", {"title": "Home", "roles": []})
    _script(front, "front-web", {"tools": [("do_action", {
        "worker": "pianobar", "cap": "pianobar.restart", "summary": "restart pianobar"})],
        "text": ""})
    await _call(front, "huddle.post", {"huddle": h["room"], "text": "restart the music"})
    await _settle(front)
    assert any(t["kind"] == "front-web" for t in front.band.turns)
    assert ("agent:home", "Done: restart pianobar.") in _texts(front, h["room"])


@pytest.mark.asyncio
async def test_the_front_door_is_off_by_default_and_ask_answers_as_before(front):
    front.node.settings.set("home.front_door", False, actor="human:op")
    out = await _ask(front, "hello?")
    assert "path" not in out and front.band.turns[-1]["kind"] == "ask"


def test_parse_keeps_unknown_headings_as_notes():
    rules, problems = parse("## Just do it\n- [pb-abcd] Tasks. -- Bake\n## Whatever\n- not a rule")
    assert [(r.id, r.section, r.text, r.by) for r in rules] == [("pb-abcd", "do", "Tasks.", "Bake")]
    assert problems and "unknown heading" in problems[0]


def test_the_relay_summary_speaks_house_asks(front):
    front.front.make_ask("gap", "Use the blue theme for the dashboard?", "Two roles disagree.")
    relay = front.node.plugin("approvals").relay
    from rook.hub.relay import summary
    line = summary(relay.items())
    assert "Use the blue theme for the dashboard?" in line
    assert json.dumps(relay.public(relay.items()[0]))


def test_irreversible_actions_are_recognised():
    assert F.irreversible("file.delete", "change", (), {})
    assert F.irreversible("shell.exec", "change", (), {"argv": ["sudo", "rm", "-rf", "/x"]})
    assert F.irreversible("shell.exec", "change", (), {"argv": ["/usr/bin/reboot"]})
    assert F.irreversible("worker.update", "admin", ("destructive",), {})
    assert not F.irreversible("pianobar.restart", "change", (), {})
    assert not F.irreversible("shell.exec", "change", (), {"argv": ["systemctl", "restart", "x"]})
    assert not F.irreversible("task.write", "prepare", (), {"action": "create"})


# -- the dashboard's Decisions page --------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_decisions_page_answers_overturns_and_edits_the_playbook(front):
    import httpx
    from starlette.applications import Starlette
    from rook.hub.plugins.huddle.web import routes

    class Accounts:
        weak = False

        def session(self, token):
            return {"id": "1", "username": "bake", "name": "Bake", "admin": True,
                    "csrf": "tok"} if token == "s" else None

        def session_weak(self, token):
            return self.weak
    accounts = Accounts()
    a = front.front.make_ask("action", "Restart pianobar? (pianobar.restart on pianobar)",
                             "The playbook says ask first.", more="details",
                             action={"worker": "pianobar", "wid": "w-piano",
                                     "cap": "pianobar.restart", "args": {},
                                     "summary": "restart pianobar"})
    front.front.settle("restart?", [{"who": "a", "says": "b"}], cap="pianobar.restart",
                       worker="pianobar", ruling="yes")
    app = Starlette(routes=routes(lambda: front.node, accounts))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 cookies={"rook_account": "s"}) as c:
        async def api(**body):
            r = await c.post("/huddle/account-api", json={"csrf": "tok", **body})
            return r.status_code, r.json()
        code, body = await api(action="front_count")
        assert code == 200 and body["result"] == {"asks": 1}
        code, body = await api(action="front_view")
        view = body["result"]
        assert view["front_door"] and view["asks"][0]["line"].startswith("Restart pianobar?")
        assert view["decisions"][0]["kind"] == "ruled" and "Bake" not in view["asks"][0]["line"]
        assert "## Just do it" in view["playbook"]["text"]
        code, body = await api(action="front_explain", ask=a["id"], more=True)
        assert body["result"]["more"] == "details"
        accounts.weak = True
        code, body = await api(action="front_answer", ask=a["id"], decision="yes")
        assert code == 403 and "band secret" in body["error"] and not _ran(front, "pianobar.restart")
        accounts.weak = False
        code, body = await api(action="front_answer", ask=a["id"], decision="yes")
        assert code == 200 and body["result"]["note"] == "Done: restart pianobar."
        assert front.front.store.find_ask(a["id"])["decided_by"] == "Bake"
        code, body = await api(action="front_overturn", decision=view["decisions"][0]["id"],
                               words="ask me next time")
        assert code == 200 and body["result"]["effects"]
        text = view["playbook"]["text"].replace("## Just answer", "## Just answer\n- Jokes.")
        code, body = await api(action="front_playbook_save", text=text, words="jokes are fine")
        assert code == 200 and body["result"]["changes"][0]["text"] == "Jokes."


@pytest.mark.asyncio
async def test_an_answer_in_conversation_is_relayed_only_when_the_house_agent_may_relay(front,
                                                                                    monkeypatch):
    a = front.front.make_ask("action", "Restart pianobar?", "Asked first.",
                             action={"worker": "pianobar", "wid": "w-piano",
                                     "cap": "pianobar.restart", "args": {},
                                     "summary": "restart pianobar"})
    h = await _call(front, "huddle.start", {"title": "Home", "roles": []})
    step = {"tools": [("answer_ask", {"ask": a["name"], "decision": "yes",
                                      "words": "yes, restart it"})], "text": ""}
    _script(front, "front-web", step)
    await _call(front, "huddle.post", {"huddle": h["room"], "text": "yes, restart it"})
    await _settle(front)
    seen = next(t for t in front.band.turns if t["kind"] == "front-web")["seen"][0]
    assert seen["ok"] is False and "relay" in seen["error"] and not _ran(front, "pianobar.restart")
    relay = front.node.plugin("approvals").relay
    monkeypatch.setattr(relay, "may_relay", lambda p: p == "agent:home")
    _script(front, "front-web", step)
    await _call(front, "huddle.post", {"huddle": h["room"], "text": "yes, restart it"})
    await _settle(front)
    assert len(_ran(front, "pianobar.restart")) == 1
    assert front.front.store.find_ask(a["id"])["decided_by"].endswith("via the house agent (home)")


@pytest.mark.asyncio
async def test_the_decisions_badge_counts_open_asks_without_the_mcp_service(tmp_path,
                                                                           monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from rook.hub.plugins.home.front import FrontStore
    from rook.hub.trust import TrustStore
    from tests.test_trust import AGENT, OWNER, dashboard
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path))
    principal = [OWNER]
    async with TestClient(TestServer(dashboard(tmp_path, principal, TrustStore(None)))) as http:
        assert (await (await http.get("/api/decisions?count=1")).json()) == {"waiting": 0}
        st = FrontStore(str(tmp_path / "plugins" / "home" / "front.db"))
        st.add_ask({"kind": "gap", "line": "Blue theme?", "explain": "x"})
        st.add_ask({"kind": "gap", "line": "Old one?", "explain": "x", "ttl": -1})
        assert (await (await http.get("/api/decisions?count=1")).json()) == {"waiting": 1}
        principal[0] = AGENT
        assert (await http.get("/api/decisions?count=1")).status == 403
        st.close()
