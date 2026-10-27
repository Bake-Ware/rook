"""A trust-map proposal is one waiting decision like the rest (rook/hub/relay.py):
listed by rook_approvals, a card on the phone, in the reminders and the voice pass,
and decided anywhere clears everywhere. Only the person decides it: their tap on
the card, Trust > Trust map or their sure voice; an agent's relay never does."""
from __future__ import annotations

import dataclasses

import pytest

from rook.hub import approvals as A
from rook.hub.trust import Actor, TrustStore
from tests.test_attention import PHONE, att  # noqa: F401
from tests.test_huddle_chair import hub as chair_hub  # noqa: F401
from tests.test_relay import CLAUDE, FAST, SAID, cap, hub  # noqa: F401

LOOSEN = {"op": "batch", "data": {"changes": [{"op": "delete_entry", "data": {"id": "rook-hub"}}]}}


@pytest.fixture(autouse=True)
def core(hub):  # noqa: F811
    """The shipped default: advanced security off."""
    hub.ap.config_source = A.ConfigSource(fixed=dataclasses.replace(FAST, advanced=False))


@pytest.fixture
def store(hub, tmp_path):  # noqa: F811
    st = TrustStore(str(tmp_path / "trust.json"))
    st.change("set_entry", {"id": "rook-hub", "title": "Rook hub", "kind": "resource",
                            "level": "guarded", "targets": ["worker:bakenetcanada"]},
              Actor("person", "Bake", words="the hub is guarded"))
    hub.relay.trust = lambda: st
    prop = st.propose(LOOSEN, by="Claude web", principal=CLAUDE.id,
                      why="only the hard stops ask now")
    st.prop = prop["id"]
    return st


def pending(st) -> list[str]:
    return [p["id"] for p in st.current()["proposals"] if p["state"] == "pending"]


@pytest.mark.asyncio
async def test_a_trust_proposal_is_listed_but_never_relayed_by_an_agent(hub, store):  # noqa: F811
    listed = await cap(hub, "approvals.list", {})
    assert listed["ok"] and listed["result"]["count"] == 1, listed
    it = listed["result"]["items"][0]
    assert it["kind"] == "trust" and it["id"] == store.prop
    assert it["relayable"] is False and "own tap or voice" in it["why_not"]
    assert it["plain"]["title"] == "Change the trust map"
    assert "rook-hub" in it["plain"]["want"] and it["plain"]["risk"].startswith("High")
    assert "Change the trust map" in listed["result"]["summary"]
    for ticked in (False, True):
        hub.ap.queue.set_relayer(CLAUDE.id, ticked, "human:bake", "Claude web")
        for decision in ("approve", "deny"):
            res = await cap(hub, "approvals.relay", {"item": store.prop, "decision": decision,
                                                     "relayed_by": SAID})
            assert not res["ok"], res
            assert "trust changes need Bake's own tap or voice" in res["error"], res
            assert "nothing is waiting" not in res["error"]
    assert pending(store) == [store.prop]             # never lost
    assert hub.ap.queue.relays() == []


@pytest.mark.asyncio
async def test_a_ticked_house_agent_or_chair_cannot_accept_one_either(hub, store):  # noqa: F811
    for relayer in ("home:home", "system:rook-hub-chair", "voice:house"):
        hub.ap.queue.set_relayer(relayer, True, "human:bake", relayer)
        with pytest.raises(PermissionError, match="own tap or voice"):
            await hub.relay.decide(store.prop, "approve", SAID, relayer=relayer)
    assert pending(store) == [store.prop]


@pytest.mark.asyncio
async def test_the_phone_card_is_posted_and_bakes_tap_decides_it(att, store):  # noqa: F811
    att.configure()
    await att.att.service.tick()
    card = att.phone.cards[store.prop]
    assert card["line"] == "Change the trust map" and card["actions"] == ["yes", "no", "explain"]
    assert "rook-hub" in card["body"]
    assert store.prop in {i["id"] for i in att.att.service.waiting()}
    assert att.att.service.status()["devices"][0]["open_cards"] == 1
    # A tap from a phone the person never ticked does nothing.
    att.phone.tap(store.prop, "approve")
    await att.att.service.tick()
    assert att.phone.updates[-1][:2] == (store.prop, "failed")
    assert pending(store) == [store.prop]
    # Ticked: Bake's own tap accepts it, with his tap recorded as the words.
    att.ap.queue.set_relayer(f"device:{PHONE}", True, "human:bake", "pixel (phone)")
    await att.att.service.tick()                       # offered again
    att.phone.tap(store.prop, "approve")
    await att.att.service.tick()
    assert (store.prop, "done") in {(i, s) for i, s, _ in att.phone.updates}
    doc = store.current()
    prop = next(p for p in doc["proposals"] if p["id"] == store.prop)
    assert prop["state"] == "accepted"
    assert prop["decided_by"]["kind"] == "person" and prop["decided_by"]["who"] == "Bake"
    assert prop["decided_by"]["principal"] == f"device:{PHONE}"
    assert "decision card on pixel" in prop["decided_by"]["words"]
    assert [e["id"] for e in doc["entries"]] == []
    assert att.att.service.waiting() == []
    assert (await cap(att, "approvals.list", {}))["result"]["count"] == 0


@pytest.mark.asyncio
async def test_decided_on_manage_trust_the_card_goes(att, store):  # noqa: F811
    att.configure()
    await att.att.service.tick()
    assert store.prop in att.phone.cards
    store.change("decide_proposal", {"id": store.prop, "accept": False},
                 Actor("person", "bake", words="no, keep it", session="dashboard"))
    await att.att.service.tick()
    assert (store.prop, "gone") in {(i, s) for i, s, _ in att.phone.updates}
    assert att.att.service.status()["devices"][0]["open_cards"] == 0
    assert att.att.service.waiting() == []


@pytest.mark.asyncio
async def test_the_dashboard_approvals_page_lists_it_in_plain_words(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from tests.test_approvals import OWNER, dashboard
    app, az = dashboard(tmp_path, [OWNER])
    st = TrustStore(str(tmp_path / "trust.json"))
    st.change("set_entry", {"id": "rook-hub", "title": "Rook hub", "kind": "resource",
                            "level": "guarded", "targets": ["worker:bakenetcanada"]},
              Actor("person", "Bake", words="the hub is guarded"))
    prop = st.propose(LOOSEN, by="Claude web", why="only the hard stops ask now")
    az.approvals.trust = st
    async with TestClient(TestServer(app)) as http:
        data = await (await http.get("/api/approvals")).json()
        assert [(t["id"], t["by"], t["loosens"]) for t in data["trust"]] == \
            [(prop["id"], "Claude web", True)]
        assert "rook-hub" in data["trust"][0]["sentence"]
        st.change("decide_proposal", {"id": prop["id"], "accept": False},
                  Actor("person", "bake", words="no", session="dashboard"))
        assert (await (await http.get("/api/approvals")).json())["trust"] == []
