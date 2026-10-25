"""A card is the decision (Bake, 2026-10-25: "Cards aren't associated with like a
decision"): with advanced security off, any agent client records the person's
quoted words on a waiting item and every copy of it clears; a hard stop stays
their own tap; a question that no longer needs them is withdrawn as moot."""
from __future__ import annotations

import dataclasses

import pytest

from rook.hub import approvals as A
from rook.hub.plugins.home.front import FrontError
from rook.hub.policy import Principal
from rook.hub.relay import own_tap
from tests.test_attention import att  # noqa: F401
from tests.test_front_door import front  # noqa: F401
from tests.test_huddle_chair import hub as chair_hub  # noqa: F401
from tests.test_huddle_chair import GUEST, _call, _texts
from tests.test_relay import CLAUDE, FAST, SAID, cap, card_and_ask, hold, hub  # noqa: F401

QUOTE = {**SAID, "quote": "Deploy I'm not getting cards"}


@pytest.fixture(autouse=True)
def core(hub):  # noqa: F811
    """The shipped default: advanced security off."""
    hub.ap.config_source = A.ConfigSource(fixed=dataclasses.replace(FAST, advanced=False))


@pytest.mark.asyncio
async def test_an_agent_records_the_persons_words_and_the_card_goes_everywhere(att):  # noqa: F811
    att.configure()
    h = await card_and_ask(att)
    await att.att.service.tick()
    ask = next(i for i in att.relay.items() if i["kind"] == "ask")
    assert ask["id"] in att.phone.cards
    assert ask["id"] in {i["id"] for i in att.att.service.waiting()}
    # No Tokens tick: their quoted words are the consent.
    listed = await cap(att, "approvals.list", {})
    assert listed["ok"] and listed["result"]["count"] == 2, listed
    res = await cap(att, "approvals.relay", {"item": ask["name"], "decision": "approve",
                                             "relayed_by": QUOTE})
    assert res["ok"] and res["result"]["state"] == "answered", res
    await att.att.service.tick()
    assert (ask["id"], "gone") in {(i, s) for i, s, _ in att.phone.updates}
    assert ask["id"] not in {i["id"] for i in att.att.service.waiting()}   # no reminder
    said = [t for _, t in _texts(att, h["room"]) if t.startswith(f"Answer for {GUEST}: Yes")]
    assert said and "Deploy I'm not getting cards" in said[-1]
    assert [r["quote"] for r in att.ap.queue.relays()] == [QUOTE["quote"]]


@pytest.mark.asyncio
async def test_an_agents_relay_never_clears_a_hard_stop(hub):  # noqa: F811
    for kind, via in (("hold", ()), ("job", ("job:nightly",))):
        h = hold(hub.ap, kind="default", reason="hard stop: erases data for good", via=via)
        for ticked in (False, True):
            hub.ap.queue.set_relayer(CLAUDE.id, ticked, "human:bake", "Claude web")
            res = await cap(hub, "approvals.relay", {"item": h["id"], "decision": "approve",
                                                     "relayed_by": SAID})
            assert not res["ok"] and "hard stop" in res["error"], (kind, ticked, res)
        assert hub.ap.queue.get(h["id"])["state"] == "pending"
    # The house agent and its roles never talk with the person through rook_approvals.
    await card_and_ask(hub)
    ask = next(i for i in hub.relay.items() if i["kind"] == "ask")
    role = Principal("home:Sojourn/role:qa", "token", "agent", label="agent:Sojourn/role:qa")
    res = await cap(hub, "approvals.relay", {"item": ask["id"], "decision": "approve",
                                             "relayed_by": SAID}, role)
    assert not res["ok"] and "may not relay" in res["error"]
    assert not hub.ap.queue.relays()
    # A house ask's Yes approves the hold its call meets, or runs a change with a stop in it.
    assert own_tap({"kind": "house", "cap": "shell.exec"})
    assert own_tap({"kind": "house", "cap": "", "hard_stop": True})
    assert own_tap({"kind": "house", "cap": "", "request": "fr_1"})   # Yes re-runs it, approved
    assert not own_tap({"kind": "house", "cap": "", "request": ""})
    assert not own_tap({"kind": "ask"}) and not own_tap({"kind": "card", "cap": "info.ping"})


@pytest.mark.asyncio
async def test_with_advanced_security_only_ticked_clients_relay(hub):  # noqa: F811
    hub.ap.config_source = A.ConfigSource(fixed=FAST)
    await card_and_ask(hub)
    ask = next(i for i in hub.relay.items() if i["kind"] == "ask")
    res = await cap(hub, "approvals.relay", {"item": ask["id"], "decision": "approve",
                                             "relayed_by": SAID})
    assert not res["ok"] and "may not relay" in res["error"]
    assert not (await cap(hub, "approvals.list", {}))["ok"]


@pytest.mark.asyncio
async def test_a_withdrawn_question_is_moot_everywhere_and_the_room_says_why(att):  # noqa: F811
    att.configure()
    h = await card_and_ask(att)
    await att.att.service.tick()
    ask = next(i for i in att.relay.items() if i["kind"] == "ask")
    res = await _call(att, "huddle.write", {"action": "withdraw", "room": h["room"],
                                            "text": "already deployed",
                                            "data": {"question": ask["question"]}}, GUEST)
    assert res["state"] == "withdrawn"
    await att.att.service.tick()
    assert (ask["id"], "gone") in {(i, s) for i, s, _ in att.phone.updates}
    assert not [i for i in att.relay.items() if i["kind"] == "ask"]
    assert any(t == f"Withdrew question {ask['question']}: it no longer needs you "
               "(already deployed)" for _, t in _texts(att, h["room"]))


@pytest.mark.asyncio
async def test_the_house_agents_chat_still_needs_the_tick(front):  # noqa: F811
    gap = front.front.make_ask("gap", "Blue theme for the dashboard?", "Asked first.")
    claude = Principal("token:agent_claude", "token", "agent", label="Claude web")
    with pytest.raises(FrontError, match="May relay my approvals"):
        await front.front._answer_in_chat({"ask": gap["name"], "decision": "yes", "words": "yes"},
                                          asker="agent:Claude web", principal=claude,
                                          said="Bake said yes")
    assert front.front.store.find_ask(gap["id"])["state"] == "open"
