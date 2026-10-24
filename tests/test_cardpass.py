"""Clear my cards by voice (rook/hub/plugins/home/cardpass.py): only the sure owner,
only with the voice's relay tick, decided from the heard transcript; loosening the
trust map is read back word for word and needs a fresh yes with a check word."""
from __future__ import annotations

import asyncio
import time

import pytest

from rook.hub import trust as T
from rook.hub.plugins.home import cardpass as CP
from tests.test_persistent_sessions import (OWNER_SURE, UNKNOWN, connect, disconnect,  # noqa: F401
                                            penv, until)

PROBABLY = {**OWNER_SURE, "speaker": {"label": "Bake", "score": 0.55, "sure": False}}
SHORT = {"owner": True, "name": "Bake"}                       # too short for a voiceprint
KID = {"owner": False, "name": "kid"}


class FakeRelay:
    """The approvals relay as cardpass uses it."""

    def __init__(self, items):
        self._items = list(items)
        self.enabled: set[str] = set()
        self.decided: list[dict] = []

    def items(self):
        return [dict(i) for i in self._items]

    def may_relay(self, principal):
        return principal in self.enabled

    async def decide(self, ref, decision, said, *, relayer, relayer_label="", reason="",
                     answer="", principal=None):
        it = next((i for i in self._items if i["id"] == ref), None)
        if it is None:
            from rook.hub.relay import RelayError
            raise RelayError(f"no waiting item {ref!r} (it may be decided or expired already)")
        self._items.remove(it)
        self.decided.append({"id": ref, "decision": decision, "said": said,
                             "relayer": relayer, "label": relayer_label, "answer": answer})
        word = "approved" if decision == "approve" else "denied"
        return {"recorded": f"{word} by {said['who']} via {relayer_label}: {said['quote']}"}


def hold_item(ident="h1", title="Restart nginx", created=1.0, **kw):
    return {"kind": "hold", "id": ident, "created": created, "cap": "shell.exec",
            "worker": "box", "relayable": True,
            "plain": {"title": title, "for": "Cert renewal", "who": "Claude web",
                      "want": "Restart the service nginx on box", "risk": "Medium"}, **kw}


class Env:
    def __init__(self, penv, host, relay):
        self.penv, self.host, self.relay = penv, host, relay
        self.sess = penv.sess
        self.cards = penv.sess.cards
        self.n = 0

    async def hear(self, ident, text, mode=""):
        self.n += 1
        turn = f"v{self.n}"
        ev = {"role": "house", "lane": "voice", "kind": "voice_turn", "turn": turn,
              "identity": ident, "text": text}
        if mode:
            ev["mode"] = mode
        self.host.emit(**ev)
        await until(lambda: self.sess.voice.get("house", {}).get("turn") == turn)
        return turn

    async def route(self, op, turn, **args):
        """The op as the voice routes it, and the pass's own full result."""
        try:
            if op == "cards":
                return await self.cards.start("house", turn)
            return await self.cards.answer("house", dict(args), turn)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def says(self):
        return [m for m in self.host.sent("voice_say")]

    def last_say(self):
        return self.says()[-1]

    async def spoken(self, text=None):
        """The voice says the current card's message (or ``text`` instead)."""
        cur = (self.cards.current("house") or {}).get("cur") or {}
        msg = next((m for m in reversed(self.says()) if m.get("turn") == cur.get("marker")),
                   self.last_say())
        self.n += 1
        self.host.emit(role="house", lane="voice", kind="voice_reply", turn=f"push-{self.n}",
                       push=True, for_turn=msg.get("turn"), ok=True,
                       text=text if text is not None else msg["text"].split(") ", 1)[-1])
        cur_before = len(self.says())
        await until(lambda: (self.cards.current("house") or {}).get("cur") is None
                    or (self.cards.current("house")["cur"].get("spoken") is not None)
                    or len(self.says()) > cur_before)

    async def ask_owner(self, text="clear my cards"):
        turn = await self.hear(OWNER_SURE, text)
        return await self.route("cards", turn)


@pytest.fixture
def env(penv):  # noqa: F811
    relay = FakeRelay([hold_item("h1", created=1.0),
                       hold_item("h2", "Look at files", created=2.0)])
    penv.sess.cards.relay = lambda: relay
    return penv, relay


async def started(env_):
    penv_, relay = env_
    host, task = await connect(penv_)
    e = Env(penv_, host, relay)
    return e, task


def log_rows(e):
    att = e.penv.node.plugin("attention")
    return att.service.eventlog.recent()


@pytest.mark.asyncio
async def test_only_the_sure_owner_with_the_voice_tick_starts_a_pass(env):
    e, task = await started(env)
    for ident in (UNKNOWN, PROBABLY, SHORT, KID):
        turn = await e.hear(ident, "clear my cards")
        res = await e.route("cards", turn)
        assert res["ok"] is False and e.cards.current("house") is None, ident
        assert "couldn't confirm it's them" in e.last_say()["text"]
    # The sure owner, but the voice may not relay yet (Tokens page).
    res = await e.ask_owner()
    assert res["ok"] is False and "Tokens page" in e.last_say()["text"]
    e.relay.enabled.add("voice:house")
    res = await e.ask_owner()
    assert res["ok"] and res["cards"] == 2
    say = e.last_say()
    assert say["text"].startswith("(cards) There are 2 waiting. Card 1 of 2: Run a command")
    assert "Cert renewal" in say["text"] and say["turn"].startswith("cards-")
    await disconnect(task, e.host)


@pytest.mark.asyncio
async def test_a_spoken_yes_decides_the_card_like_any_relay_and_is_logged(env):
    e, task = await started(env)
    e.relay.enabled.add("voice:house")
    await e.ask_owner()
    await e.spoken()
    turn = await e.hear(OWNER_SURE, "yes, approve it")
    res = await e.route("card", turn, answer="yes")
    assert res["ok"] and res["decided"] == "h1"
    d, = e.relay.decided
    assert d["decision"] == "approve" and d["relayer"] == "voice:house" and d["label"] == "voice"
    assert d["said"]["quote"] == "yes, approve it"
    assert "voiceprint 0.74 (sure)" in d["said"]["session"]
    assert "Done: approved. Card 2 of 2" in e.last_say()["text"]
    row = next(r for r in log_rows(e) if r["outcome"] == "decided")
    assert row["what"].startswith("Approved by voice: ") and "(0.74)" in row["why"]
    assert row["source"] == "voice" and "said yes to the voice" in row["how"]
    # The second card: a no.
    await e.spoken()
    turn = await e.hear(OWNER_SURE, "no, not now")
    assert (await e.route("card", turn, answer="no"))["ok"]
    assert e.relay.decided[-1]["decision"] == "deny"
    assert "That's all of them: 2 answered" in e.last_say()["text"]
    assert e.cards.current("house") is None
    await disconnect(task, e.host)


@pytest.mark.asyncio
async def test_answers_that_do_not_count_leave_the_card(env):
    e, task = await started(env)
    e.relay.enabled.add("voice:house")
    await e.ask_owner()
    # Before the voice has asked it: refused.
    turn = await e.hear(OWNER_SURE, "yes, approve it")
    assert (await e.route("card", turn, answer="yes"))["ok"] is False
    await e.spoken()
    for ident, text, routed, mode in ((UNKNOWN, "yes, approve it", "yes", ""),
                                      (PROBABLY, "yes, approve it", "yes", ""),
                                      (SHORT, "yes", "yes", ""),
                                      (KID, "yes, approve it", "yes", ""),
                                      (OWNER_SURE, "yes no maybe", "yes", ""),
                                      (OWNER_SURE, "no way", "yes", ""),       # the voice lied
                                      (OWNER_SURE, "is that safe?", "yes", ""),
                                      (OWNER_SURE, "yes, approve it", "yes", "listen")):
        turn = await e.hear(ident, text, mode=mode)
        res = await e.route("card", turn, answer=routed)
        assert res["ok"] is False, (ident, text, mode)
    assert e.relay.decided == []
    assert any(r["what"].startswith("A spoken answer didn't count") for r in log_rows(e))
    # Too late after the question: refused.
    e.cards.current("house")["cur"]["spoken"] = time.time() - 300
    turn = await e.hear(OWNER_SURE, "yes, approve it")
    assert (await e.route("card", turn, answer="yes"))["ok"] is False
    # Asked again, answered properly: it counts.
    turn = await e.hear(OWNER_SURE, "say it again")
    await e.route("card", turn, answer="repeat")
    await e.spoken()
    turn = await e.hear(OWNER_SURE, "yes, approve it")
    assert (await e.route("card", turn, answer="yes"))["ok"]
    await disconnect(task, e.host)


@pytest.mark.asyncio
async def test_a_reply_to_something_else_never_marks_the_card_asked(env):
    e, task = await started(env)
    e.relay.enabled.add("voice:house")
    await e.ask_owner()
    # A background result's push, not the card's own message.
    e.host.emit(role="house", lane="voice", kind="voice_reply", turn="push-x", push=True,
                for_turn="t-other", ok=True, text="The weather is fine.")
    await asyncio.sleep(0.05)
    assert e.cards.current("house")["cur"]["spoken"] is None
    # The card's own message, talked over: still not asked.
    msg = e.last_say()
    e.host.emit(role="house", lane="voice", kind="voice_reply", turn="push-y", push=True,
                for_turn=msg["turn"], ok=False, interrupted=True, text="Card one")
    await asyncio.sleep(0.05)
    assert e.cards.current("house")["cur"]["spoken"] is None
    await disconnect(task, e.host)


@pytest.mark.asyncio
async def test_cards_decided_elsewhere_and_dashboard_only_cards(env):
    e, task = await started(env)
    penv_, relay = env
    relay._items.append({**hold_item("s1", "Read a secret", created=0.5), "cap": "secret.get",
                         "relayable": False, "why_not": "never relayed (secrets, tokens, ...)"})
    relay.enabled.add("voice:house")
    await e.ask_owner()
    text = e.last_say()["text"]
    assert "Card 1 of 3: Read a secret on box. That one stays on the dashboard, because it's " \
           "about secrets, tokens" in text and "Card 2 of 3: Run a command" in text
    await e.spoken()
    relay._items = [i for i in relay._items if i["id"] != "h1"]       # tapped on the phone
    turn = await e.hear(OWNER_SURE, "yes, approve it")
    res = await e.route("card", turn, answer="yes")
    assert res["gone"] == "h1"
    assert "already decided somewhere else. Card 3 of 3" in e.last_say()["text"]
    turn = await e.hear(OWNER_SURE, "skip that one")
    await e.route("card", turn, answer="skip")
    assert "1 left" in e.last_say()["text"] or "2 left" in e.last_say()["text"]
    await disconnect(task, e.host)


def _trust_with_sacred_kaiju():
    st = T.TrustStore(None)
    st.change("set_entry", {"id": "kaiju", "title": "kaiju", "kind": "host", "level": "sacred",
                            "targets": ["worker:kaiju"]},
              T.Actor("person", "Bake", words="kaiju is sacred"))
    return st


@pytest.mark.asyncio
async def test_loosening_the_trust_map_by_voice_is_read_back_and_needs_the_word(env):
    e, task = await started(env)
    penv_, relay = env
    relay._items = []
    relay.enabled.add("voice:house")
    st = _trust_with_sacred_kaiju()
    e.cards.trust = lambda: st
    prop = st.propose({"op": "add_exception", "data": {
        "id": "deploy-kaiju", "title": "deploy to kaiju", "applies_to": ["*"],
        "targets": ["worker:kaiju"], "kinds": ["change"], "tier": "go",
        "expires": "2026-10-26T00:00:00Z"}}, by="agent:Claude web", why="deploys every night")
    await e.ask_owner()
    assert "This loosens the rules, on something sacred" in e.last_say()["text"]
    await e.spoken()
    turn = await e.hear(OWNER_SURE, "yes, approve it")
    res = await e.route("card", turn, answer="yes")
    assert res["confirm"] == prop["id"]
    cur = e.cards.current("house")["cur"]
    word = cur["word"]
    assert word in CP.WORDS and "(read exactly)" in e.last_say()["text"]
    sentence = cur["readback"]
    assert sentence == ("Let any agent make changes without asking on the machine kaiju until "
                        "2026-10-26T00:00:00Z.")
    # The voice paraphrased: asked once more; paraphrased again: the card is left.
    await e.spoken("Sure, so you want agents to deploy to kaiju, right?")
    assert cur["spoken"] is None and "(read exactly)" in e.last_say()["text"]
    await e.spoken("Okay, agents on kaiju until Sunday. Say yes.")
    assert e.cards.current("house") is None or e.cards.current("house")["cur"] is None
    assert "couldn't read that change back word for word" in e.last_say()["text"]
    assert st.current()["exceptions"] == []
    # Again, read back properly this time.
    await e.ask_owner()
    await e.spoken()
    turn = await e.hear(OWNER_SURE, "yes, approve it")
    await e.route("card", turn, answer="yes")
    cur = e.cards.current("house")["cur"]
    word = cur["word"]
    await e.spoken()                                   # read exactly as written
    assert cur["spoken"] is not None
    # A yes without the word (an old recording of a yes): refused.
    turn = await e.hear(OWNER_SURE, "yes, approve it")
    assert (await e.route("card", turn, answer="yes"))["ok"] is False
    assert st.current()["exceptions"] == []
    # Read back again; someone else speaks first, then the owner: not the first turn.
    turn = await e.hear(OWNER_SURE, "read it again")
    await e.route("card", turn, answer="repeat")
    await e.spoken()
    turn = await e.hear(OWNER_SURE, "yes, approve it")
    await e.route("card", turn, answer="yes")
    cur = e.cards.current("house")["cur"]
    word = cur["word"]
    await e.spoken()
    await e.hear(UNKNOWN, "what's for dinner")
    turn = await e.hear(OWNER_SURE, f"yes, {word}")
    assert (await e.route("card", turn, answer="yes"))["ok"] is False
    assert st.current()["exceptions"] == []
    # The echo of the read-back itself: refused.
    turn = await e.hear(OWNER_SURE, "read it again")
    await e.route("card", turn, answer="repeat")
    await e.spoken()
    turn = await e.hear(OWNER_SURE, "yes, approve it")
    await e.route("card", turn, answer="yes")
    cur = e.cards.current("house")["cur"]
    await e.spoken()
    turn = await e.hear(OWNER_SURE, f"To confirm: {cur['readback']} If that's right, say yes, "
                                    f"{cur['word']}.")
    assert (await e.route("card", turn, answer="yes"))["ok"] is False
    # Finally: the very next turn, the sure owner, yes and the word.
    turn = await e.hear(OWNER_SURE, "read it again")
    await e.route("card", turn, answer="repeat")
    await e.spoken()
    turn = await e.hear(OWNER_SURE, "yes, approve it")
    await e.route("card", turn, answer="yes")
    cur = e.cards.current("house")["cur"]
    await e.spoken()
    turn = await e.hear(OWNER_SURE, f"yes, {cur['word']}")
    res = await e.route("card", turn, answer="yes")
    assert res["ok"] and res["decided"] == prop["id"]
    x, = st.current()["exceptions"]
    assert x["id"] == "deploy-kaiju"
    row = st.history(5)[0] if st.history(5)[0]["op"] == "decide_proposal" else st.history(5)[-1]
    actor = row["actor"]
    assert actor["kind"] == "person" and actor["via"] == "voice" and actor["who"]
    ev = actor["evidence"]
    assert ev["voiceprint"] == 0.74 and ev["readback"] == sentence and ev["challenge"] == cur["word"]
    assert row["words"] == f"yes, {cur['word']}"
    assert any("read back and confirmed" in r["why"] for r in log_rows(e))
    await disconnect(task, e.host)


@pytest.mark.asyncio
async def test_tightening_and_a_no_apply_at_once(env):
    e, task = await started(env)
    penv_, relay = env
    relay._items = []
    relay.enabled.add("voice:house")
    st = _trust_with_sacred_kaiju()
    e.cards.trust = lambda: st
    st.change("add_exception", {"id": "x1", "title": "x", "targets": ["worker:kaiju"],
                                "expires": "2099-01-01T00:00:00Z"},
              T.Actor("person", "Bake", words="for tonight"))
    end = st.propose({"op": "end_exception", "data": {"id": "x1"}}, by="agent:a",
                     why="the work is done")
    loose = st.propose({"op": "set_unlisted", "data": {"level": "ship-it"}}, by="agent:a",
                       why="it keeps asking")
    await e.ask_owner()
    assert "This only makes the rules stricter" in e.last_say()["text"]
    await e.spoken()
    turn = await e.hear(OWNER_SURE, "yes, end it")
    res = await e.route("card", turn, answer="yes")
    assert res["decided"] == end["id"]                 # no read-back for tightening
    assert st.current()["exceptions"][0].get("ended")
    await e.spoken()
    turn = await e.hear(OWNER_SURE, "no, not that")
    res = await e.route("card", turn, answer="no")
    assert res["decided"] == loose["id"]               # a no is immediate too
    assert st.current().get("unlisted", "balanced") == "balanced"
    assert next(p for p in st.current()["proposals"] if p["id"] == loose["id"])["state"] == \
        "rejected"
    await disconnect(task, e.host)


@pytest.mark.asyncio
async def test_a_sacred_hold_needs_the_read_back_too(env):
    e, task = await started(env)
    penv_, relay = env
    relay._items = [hold_item("t1", "Deploy to kaiju", hold_kind="trust", worker="kaiju")]
    relay.enabled.add("voice:house")
    await e.ask_owner()
    await e.spoken()
    turn = await e.hear(OWNER_SURE, "yes, deploy it")
    res = await e.route("card", turn, answer="yes")
    assert res.get("confirm") == "t1" and relay.decided == []
    cur = e.cards.current("house")["cur"]
    assert cur["readback"] == "Approve, just this once: Restart the service nginx on box."
    await e.spoken()
    turn = await e.hear(OWNER_SURE, f"yes {cur['word']}")
    assert (await e.route("card", turn, answer="yes"))["ok"]
    assert "after the read-back" in relay.decided[0]["said"]["quote"]
    await disconnect(task, e.host)


@pytest.mark.asyncio
async def test_stop_explain_and_no_pass(env):
    e, task = await started(env)
    e.relay.enabled.add("voice:house")
    turn = await e.hear(OWNER_SURE, "yes")
    out = await e.route("card", turn, answer="yes")
    assert out["ok"] is False and "clear my cards" in out["error"]
    await e.ask_owner()
    await e.spoken()
    turn = await e.hear(OWNER_SURE, "tell me more")
    await e.route("card", turn, answer="explain")
    assert "under Explain" in e.last_say()["text"]
    turn = await e.hear(OWNER_SURE, "stop, that's enough")
    assert (await e.route("card", turn, answer="stop"))["stopped"]
    assert e.cards.current("house") is None and e.relay.decided == []
    await disconnect(task, e.host)


def test_covering_more_with_a_lenient_entry_loosens():
    """Review of #195 (all four voters): adding targets to an entry less strict than
    the unlisted level loosens them (an unlisted machine, balanced, under a ship-it
    entry would just go), and so does a new such entry or a changed kind."""
    st = _trust_with_sacred_kaiju()
    st.change("set_entry", {"id": "pianobar-super", "kind": "project", "level": "ship-it",
                            "targets": ["worker:pianobar"]},
              T.Actor("person", "Bake", words="toy"))
    st.change("set_entry", {"id": "rook-hub", "kind": "host", "level": "guarded",
                            "targets": ["worker:rook-hub"]},
              T.Actor("person", "Bake", words="ask first"))
    doc = st.current()
    wider = T.describe_change(doc, {"op": "set_entry", "data": {
        "id": "pianobar-super", "kind": "project", "level": "ship-it",
        "targets": ["worker:pianobar", "worker:*"]}})
    assert wider["loosens"] and wider["sentence"] == ("Also cover the machine * under "
                                                      "pianobar-super (ship-it).")
    assert T.describe_change(doc, {"op": "set_entry", "data": {
        "id": "toys", "kind": "project", "level": "ship-it", "targets": ["worker:x"]}})["loosens"]
    # Guarded covers more: stricter than unlisted (balanced), so not a loosening...
    assert not T.describe_change(doc, {"op": "set_entry", "data": {
        "id": "rook-hub", "kind": "host", "level": "guarded",
        "targets": ["worker:rook-hub", "worker:box"]}})["loosens"]
    assert not T.describe_change(doc, {"op": "set_entry", "data": {
        "id": "new-guard", "kind": "host", "level": "guarded", "targets": ["worker:y"]}})["loosens"]
    # ...unless the person made unlisted stricter than guarded.
    st.change("set_unlisted", {"level": "sacred"}, T.Actor("person", "Bake", words="lock it"))
    assert T.describe_change(st.current(), {"op": "set_entry", "data": {
        "id": "new-guard", "kind": "host", "level": "guarded", "targets": ["worker:y"]}})["loosens"]
    # A changed kind (devices entries rule private reads) counts as loosening.
    assert T.describe_change(doc, {"op": "set_entry", "data": {
        "id": "rook-hub", "kind": "devices", "level": "guarded",
        "targets": ["worker:rook-hub"]}})["loosens"]
    # Only a title or description change is not.
    assert not T.describe_change(doc, {"op": "set_entry", "data": {
        "id": "rook-hub", "kind": "host", "level": "guarded", "title": "The hub",
        "targets": ["worker:rook-hub"]}})["loosens"]


def test_describe_change_says_what_loosens():
    st = _trust_with_sacred_kaiju()
    doc = st.current()
    d = T.describe_change(doc, {"op": "set_entry", "data": {
        "id": "kaiju", "level": "guarded", "targets": ["worker:kaiju"], "kind": "host"}})
    assert d == {"sentence": "Change kaiju from sacred to guarded.", "loosens": True,
                 "sacred": True, "op": "set_entry"}
    assert not T.describe_change(doc, {"op": "set_entry", "data": {
        "id": "kaiju", "level": "sacred", "kind": "host",
        "targets": ["worker:kaiju", "worker:kaiju2"]}})["loosens"]
    assert T.describe_change(doc, {"op": "delete_entry", "data": {"id": "kaiju"}})["loosens"]
    assert T.describe_change(doc, {"op": "grant_tool", "data": {"role": "qa", "server": "rea"}}) \
        ["sentence"] == "Give the qa role the rea tool."
    assert not T.describe_change(doc, {"op": "revoke_tool",
                                       "data": {"role": "qa", "server": "rea"}})["loosens"]
    assert not T.describe_change(doc, {"op": "set_unlisted", "data": {"level": "sacred"}})["loosens"]


def test_said_reads_the_transcript_not_the_voice():
    assert CP.said("yes, approve it") == "yes" and CP.said("no, not now") == "no"
    assert CP.said("yes no") is None and CP.said("hmm") is None
    assert CP.said("don't") == "no" and CP.said("okay go ahead") == "yes"
    assert CP.has_word("Yes, Mango.", "mango") and not CP.has_word("mangoes are nice", "mangos")


def test_the_voice_rules_route_cards(penv):  # noqa: F811
    from rook.hub.plugins.home import roledir
    text = roledir.VOICE_RULES
    assert "cards {{}}" in text and "card {{\"answer\", \"choice\"}}" in text
    assert "(read exactly)" in text
    assert penv.home.voice_relay_rows() == [("voice:house", f"{penv.home.display_name}'s voice "
                                                            "(your voiceprint)")]


@pytest.mark.asyncio
async def test_the_routing_lines_reach_the_pass(env):
    e, task = await started(env)
    e.relay.enabled.add("voice:house")
    turn = await e.hear(OWNER_SURE, "clear my cards")
    out = await e.sess.route("house", [{"op": "cards"}], turn=turn)
    assert out == [{"op": "cards", "ok": True}]
    await e.spoken()
    turn = await e.hear(OWNER_SURE, "yes, approve it")
    out = await e.sess.route("house", [{"op": "card", "answer": "yes"}], turn=turn)
    assert out == [{"op": "card", "ok": True}] and e.relay.decided[0]["id"] == "h1"
    # A refusal is said by the pass itself, never twice.
    turn = await e.hear(UNKNOWN, "yes, approve it")
    n = len(e.says())
    out = await e.sess.route("house", [{"op": "card", "answer": "yes"}], turn=turn)
    assert out[0]["ok"] is False and len(e.says()) == n + 1
    # A reply that speaks a result (a push) never decides anything.
    out = await e.sess.route("house", [{"op": "card", "answer": "yes"}], turn=turn, push=True)
    assert len(e.relay.decided) == 1
    await disconnect(task, e.host)


@pytest.mark.asyncio
async def test_two_answers_racing_decide_once_and_skip_nothing(env):
    e, task = await started(env)
    e.relay.enabled.add("voice:house")
    await e.ask_owner()
    await e.spoken()
    turn = await e.hear(OWNER_SURE, "yes, approve it")
    a, b = await asyncio.gather(e.route("card", turn, answer="yes"),
                                e.route("card", turn, answer="yes"))
    assert [d["id"] for d in e.relay.decided] == ["h1"]
    assert e.cards.current("house")["cur"]["id"] == "h2"     # the next card, not past it
    assert sum(1 for m in e.says() if "Card 2 of 2" in m["text"]) == 1
    await disconnect(task, e.host)


@pytest.mark.asyncio
async def test_a_spoken_yes_approves_a_real_hold_through_the_relay(penv, tmp_path):  # noqa: F811
    """No fakes between the voice and the approval queue: the hold is approved as
    "Bake via voice", recorded in the relays table with the words and voiceprint."""
    from types import SimpleNamespace

    from rook.hub import approvals as A
    from tests.test_relay import hold
    ap = A.Approvals(str(tmp_path / "approvals.db"), A.Config(wait_secs=0, contact=()))
    penv.band.authz = SimpleNamespace(approvals=ap)
    h = hold(ap)
    host, task = await connect(penv)
    e = Env(penv, host, None)
    relay = penv.node.plugin("approvals").relay
    ap.queue.set_relayer("voice:house", True, "human:bake", "voice")
    out = await e.ask_owner()
    assert out["ok"] and out["cards"] == 1
    await e.spoken()
    turn = await e.hear(OWNER_SURE, "yes, approve it")
    res = await e.route("card", turn, answer="yes")
    assert res["ok"] and res["decided"] == h["id"]
    row = ap.queue.get(h["id"])
    assert row["state"] == "approved" and row["decided_by"].endswith("via voice")
    rel, = ap.queue.relays()
    assert rel["relayer"] == "voice:house" and rel["quote"] == "yes, approve it"
    assert "voiceprint 0.74 (sure)" in rel["session"]
    assert relay.items() == []
    await disconnect(task, host)
