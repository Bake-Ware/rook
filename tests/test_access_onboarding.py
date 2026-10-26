"""Access v2 item 6: access set in conversation (rook/hub/access.py requests and
new resources, the users.request / users.settle caps, the house agent's
access_people / access_change tools and "who gets this?" sweep in
rook/hub/plugins/home/access_talk.py, and the owner-only Yes)."""
from __future__ import annotations

import hashlib
import json
import time

import pytest

from rook.hub import access as A
from rook.hub.authz import current_principal
from tests.test_access import Accounts, accounts_db, authorizer, member, owner
from tests.test_front_door import _ask, _script, front  # noqa: F401
from tests.test_huddle_chair import hub  # noqa: F401


# -- the store -------------------------------------------------------------------------

def test_a_request_is_one_per_person_and_access_given_answers_it(tmp_path):
    s = A.AccessStore.for_dir(str(tmp_path))
    with pytest.raises(A.AccessError, match="words"):
        s.request("u_new", "  ")
    r = s.request("u_new", "the living room PC please", label="New")
    again = s.request("u_new", "the living room PC and the recipes", label="")
    assert again["id"] == r["id"] and again["words"].endswith("recipes")
    assert again["label"] == "New" and len(s.requests()) == 1
    s.request_asked(r["id"], "a_1")
    assert s.get_request(r["id"])["state"] == "asked"
    s.grant("u_new", "worker", "w_tv", actor="human:u_bake", words="Bake: yes")
    got = s.get_request(r["id"])
    assert got["state"] == "done" and got["decided_words"] == "Bake: yes"
    r2 = s.request("u_new", "and kaiju")
    assert r2["id"] != r["id"]
    assert s.decline(r2["id"], actor="Bake", words="no, not kaiju")["state"] == "declined"
    assert [h["action"] for h in s.history("u_new")][:2] == ["decline", "request"]


def test_the_first_look_takes_everything_as_it_is_and_later_ones_are_new(tmp_path):
    s = A.AccessStore.for_dir(str(tmp_path))
    assert s.note_resources([{"kind": "worker", "ref": "w_a", "label": "a"},
                             {"kind": "band", "ref": "band1", "label": "home"}]) == []
    assert s.resources() == [] and s.resource("worker", "w_a")["state"] == "settled"
    new = s.note_resources([{"kind": "worker", "ref": "w_a", "label": "a2"},
                            {"kind": "worker", "ref": "w_b", "label": "b"},
                            {"kind": "hub", "ref": "x"}])
    assert [n["ref"] for n in new] == ["w_b"] and s.resource("worker", "w_a")["label"] == "a2"
    assert s.note_resources([{"kind": "worker", "ref": "w_b", "label": "b"}]) == []
    with pytest.raises(A.AccessError, match="words"):
        s.settle("worker", "w_b", actor="o", words="")
    # A grant on it settles it too.
    s.grant("u_kid", "worker", "w_b", actor="o", words="his")
    assert s.resource("worker", "w_b")["state"] == "settled"


def test_reaches_covers_machines_through_band_grants(tmp_path):
    s = A.AccessStore.for_dir(str(tmp_path))
    h = hashlib.sha256(b"psk").hexdigest()
    s.sync_bands([{"id": "band1", "psk_hash": h, "name": "home"}])
    assert s.reaches("u_kid", "worker", "w_tv", {h}) is None
    s.grant("u_kid", "band", "band1", caps=["file.*"], tier="read", actor="o", words="w")
    assert s.reaches("u_kid", "worker", "w_tv", {h})["kind"] == "band"
    assert s.reaches("u_kid", "worker", "w_tv", set()) is None
    assert s.reaches("u_kid", "band", "band1")["kind"] == "band"
    s.grant("u_x", "worker", "kids-pc", actor="o", words="w")
    assert s.reaches("u_x", "worker", "w_pc", (), name="Kids-PC") is not None


# -- the users.* caps --------------------------------------------------------------------

class Client:
    def __init__(self, authz):
        self.authz = authz
        self.workers = {"w_pc": {"name": "kids-pc"}}


@pytest.fixture
def node(tmp_path, monkeypatch):
    from rook.hub.node import HubNode
    accounts_db(tmp_path / "enrollment.db")
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    az = authorizer(tmp_path)
    az.access.seed(Accounts(str(tmp_path / "enrollment.db")))
    return HubNode(str(tmp_path), entry_points=False, build_version="1.test.node",
                   client=Client(az))


async def as_(principal, coro_fn):
    tok = current_principal.set(principal)
    try:
        return await coro_fn()
    finally:
        current_principal.reset(tok)


@pytest.mark.asyncio
async def test_a_new_person_asks_in_their_words_and_owners_see_what_waits(node):
    az = node.client.authz
    # users.request needs no grant: a person with nothing may still ask.
    d = az.check("users.request", node.worker_id, node.entry(), principal=member("u_new"))
    assert not d.denied
    tiers = node.host.tiers()
    assert tiers["users.request"] == "w" and tiers["users.settle"] == "a"
    out = await as_(member("u_new"), lambda: node.invoke(
        "users.request", {"words": "the kids pc for homework"}, "human:u_new"))
    assert out["state"] == "open" and out["plain"].endswith("nothing.")
    mine = await as_(owner(), lambda: node.invoke("users.request", {"words": "x"},
                                                  "human:u_bake"))
    assert "owner" in mine["plain"] and "request" not in mine
    with pytest.raises(PermissionError):
        await node.invoke("users.request", {"words": "x"}, "agent:x")

    az.access.note_resources([])
    az.access.note_resources([{"kind": "worker", "ref": "w_attic", "label": "attic"}])
    listed = await as_(owner(), lambda: node.invoke("users.list", {}, "human:u_bake"))
    w = listed["waiting"]
    assert [(r["name"], r["words"]) for r in w["requests"]] == [("New", "the kids pc for "
                                                                         "homework")]
    assert w["no_access"] == []          # u_new asked, so they are under requests
    assert [r["key"] for r in w["resources"]] == ["worker:w_attic"]

    with pytest.raises(PermissionError):
        await as_(member(), lambda: node.invoke("users.settle", {
            "what": "worker:w_attic", "words": "mine"}, "human:u_kid"))
    got = await as_(owner(), lambda: node.invoke("users.settle", {
        "what": "worker:w_attic", "words": "Bake: just us on the attic box"}, "human:u_bake"))
    assert got["resource"]["state"] == "settled"
    assert got["resource"]["settled_by"].startswith("human:u_bake")
    req = w["requests"][0]["id"]
    got = await as_(owner(), lambda: node.invoke("users.settle", {
        "what": req, "words": "Bake: not yet"}, "human:u_bake"))
    assert got["request"]["state"] == "declined"
    with pytest.raises(ValueError, match="what"):
        await as_(owner(), lambda: node.invoke("users.settle", {"what": "kaiju", "words": "x"},
                                               "human:u_bake"))


# -- in conversation -----------------------------------------------------------------------

@pytest.fixture
def talk(front, tmp_path, monkeypatch):  # noqa: F811
    accounts_db(tmp_path / "enrollment.db")
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    az = authorizer(tmp_path / "authz")
    az.access.seed(Accounts(str(tmp_path / "enrollment.db")))
    front.band.authz = az
    front.access = az.access
    front.talk = front.front.access
    return front


async def owner_turn(hub, said, *tools, text="", who=None):
    _script(hub, "front-voice", {"tools": list(tools), "text": text})
    tok = current_principal.set(who or owner())
    try:
        return await _ask(hub, said)
    finally:
        current_principal.reset(tok)


def seen(hub):
    return hub.band.turns[-1]["seen"]


@pytest.mark.asyncio
async def test_an_owner_gives_access_by_saying_so(talk):
    said = "Kid gets the pianobar box, read only"
    out = await owner_turn(talk, said, ("access_people", {}), ("access_change", {
        "op": "grant", "user": "Kid", "kind": "worker", "ref": "pianobar", "tier": "read",
        "words": "Kid gets the pianobar box, read only"}), text="He can look but not touch.")
    people = seen(talk)[0]
    assert any(p["name"] == "Kid" and "band band1" in p["reaches"] for p in people["people"])
    assert "pianobar" in people["machines"]
    res = seen(talk)[1]
    assert res["ok"] and res["granted"][0]["what"] == "machine pianobar"
    # The hub says what changed; the model's words follow.
    assert out["answer"] == ("Done: Kid now reaches machine pianobar up to read. He can look but "
                             "not touch.") and out["path"] == "do"
    [g] = [g for g in talk.access.grants("u_kid") if g["kind"] == "worker"]
    assert (g["ref"], g["tier"], g["words"]) == ("w-piano", "read", said)
    assert g["actor"].startswith("human:u_bake") and g["actor"].endswith("via agent:home")
    sys = talk.band.turns[-1]["args"]["system"]
    assert "Who may use what" in sys and "access_change" in json.dumps(
        talk.band.turns[-1]["listed"])


@pytest.mark.asyncio
async def test_nothing_changes_on_words_they_did_not_say(talk):
    await owner_turn(talk, "hmm, what about Kid?", ("access_change", {
        "op": "grant", "user": "Kid", "kind": "any", "words": "give Kid everything"}))
    assert "exact words" in seen(talk)[0]["error"]
    assert [g["kind"] for g in talk.access.grants("u_kid")] == ["band"]


@pytest.mark.asyncio
async def test_anyone_else_asking_becomes_one_question_only_an_owner_answers(talk):
    kid = member("u_kid")
    said = "can I use kaiju for my game server?"
    _script(talk, "front-voice", {"tools": [("access_change", {
        "op": "grant", "user": "Kid", "kind": "worker", "ref": "kaiju",
        "words": "can I use kaiju for my game server"})], "text": "Asked."})
    tok = current_principal.set(kid)
    try:
        out = await _ask(talk, said)
    finally:
        current_principal.reset(tok)
    assert out["answer"].startswith("I've asked an owner: Give Kid machine kaiju?")
    [a] = talk.front.store.asks("open")
    assert a["kind"] == "access" and a["action"]["user"] == "u_kid"
    [r] = talk.access.requests()
    assert r["state"] == "asked" and r["ask"] == a["id"]
    assert [g["kind"] for g in talk.access.grants("u_kid")] == ["band"]

    # A relayed answer, or one from someone who isn't an owner, never gives access.
    with pytest.raises(Exception, match="only an owner"):
        await talk.front.answer(a["id"], "yes", by="an agent", quote="yes")
    with pytest.raises(Exception, match="only an owner"):
        await talk.front.answer(a["id"], "yes", by="Kid", principal=kid)
    assert talk.front.store.ask(a["id"])["state"] == "open"
    res = await talk.front.answer(a["id"], "yes", by="Bake", principal=owner(),
                                  quote="Yes on the dashboard's Decisions page")
    assert res["ok"] and res["note"].startswith("Done: 1 grant")
    g = [g for g in talk.access.grants("u_kid") if g["kind"] == "worker"][0]
    assert g["ref"] == "w-kaiju" and "Yes to" in g["words"] and "game server" in g["words"]
    assert talk.access.get_request(r["id"])["state"] == "done"


@pytest.mark.asyncio
async def test_a_newer_question_replaces_the_older_and_no_declines(talk):
    for ref in ("kaiju", "pianobar"):
        await owner_turn(talk, f"I'd like {ref} please", ("access_change", {
            "op": "grant", "user": "u_kid", "kind": "worker", "ref": ref,
            "words": f"I'd like {ref} please"}), who=member("u_kid"))
    asks = talk.front.store.asks("open")
    assert len(asks) == 1 and "pianobar" in asks[0]["line"]
    res = await talk.front.answer(asks[0]["id"], "no", by="Bake")
    assert res["note"] == "Nothing was given."
    assert talk.access.requests(("declined",))[0]["user_id"] == "u_kid"


@pytest.mark.asyncio
async def test_only_owners_change_roles_or_settle(talk):
    await owner_turn(talk, "make me an owner", ("access_change", {
        "op": "role", "user": "Kid", "role": "owner", "words": "make me an owner"}),
        who=member("u_kid"))
    assert "only an owner" in seen(talk)[0]["error"] and talk.access.role("u_kid") == "member"
    await owner_turn(talk, "Kid is grown up, make him an owner", ("access_change", {
        "op": "role", "user": "Kid", "role": "owner", "words": "make him an owner"}))
    assert talk.access.role("u_kid") == "owner"


# -- who gets this? ----------------------------------------------------------------------

def _new_machine(hub, wid="w-attic", name="attic"):
    hub.band.workers[wid] = {"name": name, "caps": ["shell.exec"], "last_seen": time.time()}


@pytest.mark.asyncio
async def test_a_new_machine_is_asked_about_once_and_yes_shares_it(talk):
    assert await talk.talk.sweep() == []            # the first look asks nothing
    _new_machine(talk)
    [name] = await talk.talk.sweep()
    [a] = talk.front.store.asks("open")
    assert a["name"] == name and a["line"] == "Let Kid use the new machine attic?"
    assert "up to exec" in a["explain"] and talk.access.resource("worker", "w-attic")[
        "state"] == "asked"
    assert await talk.talk.sweep() == []            # never asked twice
    # The phone card (the person's own tap) counts; the relay passes owner=tap.
    res = await talk.front.answer(a["id"], "yes", by="Bake", owner=True)
    assert res["ok"]
    g = [g for g in talk.access.grants("u_kid") if g["kind"] == "worker"][0]
    assert (g["ref"], g["tier"]) == ("w-attic", "exec")
    assert talk.access.resource("worker", "w-attic")["state"] == "settled"


@pytest.mark.asyncio
async def test_no_keeps_it_with_the_owners_and_nobody_to_ask_means_no_question(talk):
    await talk.talk.sweep()
    _new_machine(talk)
    await talk.talk.sweep()
    [a] = talk.front.store.asks("open")
    res = await talk.front.answer(a["id"], "no", by="Bake")
    assert "stays with the owners" in res["note"]
    assert talk.access.resource("worker", "w-attic")["state"] == "settled"
    assert [g["kind"] for g in talk.access.grants("u_kid")] == ["band"]
    # Every member already reaches the next one (a band grant): settled, no question.
    talk.band.authz.bands_of = lambda wid: {"h1"}
    talk.access.sync_bands([{"id": "band1", "psk_hash": "h1", "name": "home"}])
    _new_machine(talk, "w-den", "den")
    assert await talk.talk.sweep() == []
    assert talk.access.resource("worker", "w-den")["words"] == "every member already reaches it"


@pytest.mark.asyncio
async def test_an_owner_saying_who_in_conversation_closes_the_question(talk):
    await talk.talk.sweep()
    _new_machine(talk)
    await talk.talk.sweep()
    sys_seen = []
    await owner_turn(talk, "the attic box is just for us", ("access_change", {
        "op": "settle", "what": "worker:w-attic", "words": "just for us"}))
    sys_seen.append(talk.band.turns[-1]["args"]["system"])
    assert "new machine attic (worker:w-attic)" in sys_seen[0]
    assert seen(talk)[0]["ok"] and talk.front.store.asks("open") == []
    assert talk.access.resource("worker", "w-attic")["words"] == "just for us"
    assert talk.front.store.requests(1)[0]["reply"].startswith(
        "Done: kept machine attic with the owners")


@pytest.mark.asyncio
async def test_a_new_persons_request_is_handed_to_the_house_agent_once(talk):
    talk.access.request("u_new", "the pianobar box so I can change the music", label="New")
    _script(talk, "front-access", {"tools": [("access_change", {
        "op": "grant", "user": "u_new", "kind": "worker", "ref": "pianobar",
        "words": "the pianobar box so I can change the music"})]})
    made = await talk.talk.sweep()
    [a] = talk.front.store.asks("open")
    assert made == [a["name"]] and a["line"] == "Give New machine pianobar?"
    assert "New asked" in a["explain"]
    turn = talk.band.turns[-1]
    assert turn["kind"] == "front-access" and "the pianobar box" in turn["args"]["prompt"]
    # Their words are untrusted: the turn gets the access tools and nothing else.
    assert sorted(turn["listed"]) == ["access_change", "access_people"]
    assert talk.access.requests()[0]["ask"] == a["id"]
    assert await talk.talk.sweep() == []            # handed over once
    # Waiting items are shown to owners, never to anyone else.
    assert "asked for access" in talk.talk.prompt_text(owner())
    assert "asked for access" not in talk.talk.prompt_text(member("u_kid"))


# -- the People page ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_people_page_shows_owners_everything_and_members_their_own(node):
    import httpx
    from types import SimpleNamespace
    from starlette.applications import Starlette
    from rook.hub.plugins.access.web import routes
    names = {"u_op": "Operator", "u_bake": "Bake", "u_kid": "Kid", "u_new": "New", "weak": "Bake"}
    accounts = SimpleNamespace(
        session=lambda c: {"id": "u_bake" if c == "weak" else c, "name": names.get(c, c),
                           "csrf": "k", "admin": c == "u_op"} if c else None,
        session_weak=lambda c: c == "weak")
    app = Starlette(routes=routes(lambda: node, accounts))
    api = "/people/account-api"
    hdr = {u: {"Cookie": f"rook_account={u}"} for u in names}
    node.client.authz.access.note_resources([])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get(api)).status_code == 401
        new = (await c.get(api, headers=hdr["u_new"])).json()
        assert new["owner"] is False and new["request"] is None and "nothing" in new["me"]["plain"]
        r = await c.post(api, headers=hdr["u_new"], json={"csrf": "k", "action": "request",
                                                          "words": "the kids pc please"})
        assert r.status_code == 200 and r.json()["request"]["words"] == "the kids pc please"
        no = await c.post(api, headers=hdr["u_new"], json={"csrf": "k", "action": "grant",
                                                           "user": "u_new", "kind": "any"})
        assert no.status_code == 403 and "owner" in no.json()["error"]
        bad = await c.post(api, headers=hdr["u_new"], json={"csrf": "x", "action": "request",
                                                            "words": "x"})
        assert bad.status_code == 403

        # u_bake is an owner by access.db (a band owner at the seed), not the operator.
        view = (await c.get(api, headers=hdr["u_bake"])).json()
        assert view["owner"] is True and view["can_edit"] is True
        assert [m["name"] for m in view["choices"]["machines"]] == ["kids-pc"]
        assert view["waiting"]["requests"][0]["words"] == "the kids pc please"
        weak = await c.post(api, headers=hdr["weak"], json={"csrf": "k", "action": "grant",
                                                            "user": "u_new", "kind": "worker",
                                                            "ref": "w_pc"})
        assert weak.status_code == 403 and "agents" in weak.json()["error"]
        assert (await c.get(api, headers=hdr["weak"])).json()["can_edit"] is False
        got = (await c.post(api, headers=hdr["u_bake"], json={
            "csrf": "k", "action": "grant", "user": "u_new", "kind": "worker", "ref": "w_pc",
            "tier": "read", "note": "homework only"})).json()
        [g] = node.client.authz.access.grants("u_new")
        assert (g["ref"], g["tier"]) == ("w_pc", "read")
        assert g["words"] == "Bake on the People page: homework only"
        assert got["waiting"]["requests"] == []         # the grant answered the request
        assert got["history"][0]["action"] == "grant"
        out = await c.post(api, headers=hdr["u_bake"], json={"csrf": "k", "action": "revoke",
                                                             "grant": g["id"]})
        assert out.status_code == 200 and node.client.authz.access.grants("u_new") == []
        last = await c.post(api, headers=hdr["u_bake"], json={"csrf": "k", "action": "role",
                                                              "user": "u_kid", "role": "owner"})
        assert last.status_code == 200 and node.client.authz.access.role("u_kid") == "owner"


def test_the_people_page_is_wired_into_the_dashboard():
    import re
    import shutil
    import subprocess
    from pathlib import Path
    from rook.remote.knowledge_web import PeopleWeb
    root = Path(__file__).resolve().parent.parent
    html = (root / "rook" / "web" / "index.html").read_text(encoding="utf-8")
    assert "import('/account/people/assets/people.js')" in html and 'id="view-people"' in html
    assert PeopleWeb.ASSETS == ("people.js",) and PeopleWeb.ADMIN_ONLY is False
    js = (root / "rook" / "web" / "people.js").read_text(encoding="utf-8")
    css = js[js.index("const CSS=`"):js.index("`;", js.index("const CSS=`"))]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css)
    assert "innerHTML" not in js
    if shutil.which("node"):
        assert subprocess.run(["node", "--check", str(root / "rook" / "web" / "people.js")]
                              ).returncode == 0
