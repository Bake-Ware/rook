"""Relayed approvals (rook/hub/relay.py, permissions.md 3.12): one list of
everything waiting on the person, and an agent the person enabled passing on
their own decision, with their words recorded."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from starlette.applications import Starlette
from starlette.testclient import TestClient as ASGIClient

from rook.hub import approvals as A
from rook.hub.authz import current_principal
from rook.hub.policy import Principal
from rook.hub.relay import FLOOR, Relay, matches, refusal, summary
from tests.test_huddle_chair import FakeBand, GUEST, _call, _roles, _settle, _texts  # noqa: F401
from tests.test_huddle_chair import hub as chair_hub  # noqa: F401

pytestmark = pytest.mark.advanced

CLAUDE = Principal("token:agent_claude", "token", "agent", label="Claude web")
OTHER = Principal("token:agent_other", "token", "agent", label="Some bot")
SAID = {"who": "Bake", "quote": "yes, restart nginx on box", "session": "https://claude.ai/chat/1"}
FAST = A.Config(wait_secs=0, advanced=True)


@pytest.fixture
def hub(chair_hub, tmp_path):
    """The chaired-huddle test hub with an approval queue on its band client."""
    ap = A.Approvals(str(tmp_path / "approvals.db"), FAST)
    chair_hub.band.authz = SimpleNamespace(approvals=ap)
    chair_hub.ap = ap
    chair_hub.relay = chair_hub.node.plugin("approvals").relay
    return chair_hub


def hold(ap, cap="shell.exec", tier="exec", args=None, principal="token:agent_claude",
         label="Claude web", via=(), target="box", kind="default",
         reason="hard stop: it erases data for good",
         is_hub=False, context=None):
    args = {"argv": ["systemctl", "restart", "nginx"]} if args is None else args
    h, _ = ap.queue.create(principal=principal, cap=cap, tier=tier, args_hash=A.args_hash(args),
                           label=label, via=via, target_id=target, target_name=target,
                           args=args, reason=reason, kind=kind, is_hub=is_hub, context=context)
    return h


async def as_(principal, coro):
    tok = current_principal.set(principal)
    try:
        return await coro
    finally:
        current_principal.reset(tok)


async def cap(hub, name, args, principal=CLAUDE):
    return await as_(principal, hub.node.dispatch(name, args, principal.label))


async def card_and_ask(hub):
    """A QA card in a chaired huddle and a pinned question."""
    await _roles(hub)
    h = await _call(hub, "huddle.start", {"title": "Ops", "roles": ["qa"]})
    hub.band.script = {"huddle-qa": [{"text": "Proposing.", "tools": [
        ("propose", {"kind": "job.run", "summary": "Rerun the nightly", "worker": "box",
                     "cap": "info.ping", "args": {"n": 1}})]}]}
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "@qa ideas?"})
    await _settle(hub)
    await _call(hub, "huddle.write", {"action": "ask", "room": h["room"],
                                      "text": SHIP, "data": {"options": ["yes", "no"]}},
                GUEST)
    return h


#: A yes/no decision says why and how to take it back (rook/hub/plain.py unready).
SHIP = "Ship on Friday?\nWhy: the release is ready.\nUndo: ship the old build again."


# -- the list ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_list_covers_every_kind_with_names_sentences_and_sources(hub):
    h = await card_and_ask(hub)
    plain = hold(hub.ap)
    job = hold(hub.ap, target="nas", via=("job:j_nightly",), principal="token:agent_jobs",
               label="jobs", kind="trust", reason="nas is sacred")
    role = hold(hub.ap, cap="info.host", tier="exec", args={}, principal="home:home/role:qa",
                label="role:qa", target="nas", kind="default",
                reason="hard stop: it erases data for good")
    items = hub.relay.items()
    by_kind = {}
    for it in items:
        by_kind.setdefault(it["kind"], []).append(it)
    assert set(by_kind) == {"hold", "job", "card", "ask"}
    first = next(i for i in items if i["id"] == plain["id"])
    assert first["name"] == plain["name"] and first["name"].count("-") == 2
    assert first["who"] == "Claude web" and first["risk"] == "exec" and first["relayable"]
    assert first["what"].startswith("run shell.exec on box: `systemctl restart nginx`")
    assert "erases data for good" in first["what"] and first["age"].endswith("s")
    jobbed, = by_kind["job"]
    assert jobbed["id"] == job["id"] and jobbed["from"] == {"job": "j_nightly"}
    assert jobbed["who"] == "job j_nightly"
    assert next(i for i in items if i["id"] == role["id"])["from"] == {"role": "qa"}
    card, = by_kind["card"]
    assert card["name"] and card["from"]["role"] == "qa" and card["from"]["huddle"]
    assert card["what"].startswith("Rerun the nightly: runs info.ping on box")
    ask, = by_kind["ask"]
    assert ask["name"] and ask["who"] == GUEST and ask["options"] == ["yes", "no"]
    assert ask["what"] == f'get an answer: "{SHIP}" (options: yes / no)'
    assert ask["from"]["huddle"] == h["name"]
    # Spoken: a count, then name + one line each.
    line = summary(items)
    assert line.startswith("5 waiting: ") and plain["name"] in line
    assert summary([]) == "Nothing is waiting for you."
    # Public items carry no internal facts.
    pub = Relay.public(first)
    assert set(pub) <= {"kind", "id", "name", "plain", "who", "what", "risk", "from", "age",
                        "relayable", "why_not", "options"}
    assert set(pub["plain"]) <= {"title", "for", "who", "want", "why", "why_full", "undo", "more",
                                 "asked_in", "links", "risk"}


@pytest.mark.asyncio
async def test_listing_is_for_people_and_identities_the_person_enabled(hub):
    hold(hub.ap)
    res = await cap(hub, "approvals.list", {})
    assert not res["ok"] and "Tokens > May relay my approvals" in res["error"]
    hub.ap.queue.set_relayer(CLAUDE.id, True, "human:bake", "Claude web")
    res = await cap(hub, "approvals.list", {"summary_only": True})
    assert res["ok"] and res["result"]["count"] == 1 and "items" not in res["result"]
    assert res["result"]["summary"].startswith("1 waiting: ")
    res = await cap(hub, "approvals.list", {"kind": "card"})
    assert res["ok"] and res["result"]["count"] == 0


# -- relaying -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_relay_without_relayed_by_is_refused(hub):
    h = hold(hub.ap)
    hub.ap.queue.set_relayer(CLAUDE.id, True, "human:bake", "Claude web")
    for said in (None, {}, {"who": "Bake", "quote": "yes"}, {**SAID, "quote": " "},
                 {**SAID, "who": ""}, "Bake said yes"):
        res = await cap(hub, "approvals.relay", {"item": h["name"], "decision": "approve",
                                                 "relayed_by": said})
        assert not res["ok"] and "relayed_by" in res["error"], said
    assert hub.ap.queue.get(h["id"])["state"] == "pending"
    assert not hub.ap.queue.relays()


@pytest.mark.asyncio
async def test_relay_from_an_identity_not_enabled_is_refused(hub):
    h = hold(hub.ap)
    hub.ap.queue.set_relayer(OTHER.id, True, "human:bake", "Some bot")
    res = await cap(hub, "approvals.relay", {"item": h["id"], "decision": "approve",
                                             "relayed_by": SAID})
    assert not res["ok"] and "may not relay approvals" in res["error"]
    # People decide on the dashboard; band callers and unverified callers never relay.
    for who in (Principal("human:u1", "human", "owner", ("human:owner",)),
                Principal("unverified", "unverified", verified=False)):
        res = await cap(hub, "approvals.relay", {"item": h["id"], "decision": "approve",
                                                 "relayed_by": SAID}, who)
        assert not res["ok"]
    hub.ap.queue.set_relayer(OTHER.id, False, "human:bake", "Some bot")
    res = await cap(hub, "approvals.relay", {"item": h["id"], "decision": "approve",
                                             "relayed_by": SAID}, OTHER)
    assert not res["ok"] and "may not relay" in res["error"]
    assert hub.ap.queue.get(h["id"])["state"] == "pending"


@pytest.mark.asyncio
async def test_never_relayable_items_are_refused_even_when_enabled(hub):
    hub.ap.queue.set_relayer(CLAUDE.id, True, "human:bake", "Claude web")
    secret = hold(hub.ap, cap="secret.get", tier="admin", args={"name": "nas-admin"},
                  target="rook", kind="default", reason="tier admin", is_hub=True)
    update = hold(hub.ap, cap="worker.update", tier="admin", args={"build": 9}, kind="trust",
                  reason="box is sacred")
    items = {i["id"]: i for i in hub.relay.items()}
    assert not items[secret["id"]]["relayable"] and "never relayed" in items[secret["id"]]["why_not"]
    assert "tier:admin" in items[update["id"]]["why_not"]
    for h in (secret, update):
        res = await cap(hub, "approvals.relay", {"item": h["id"], "decision": "approve",
                                                 "relayed_by": SAID})
        assert not res["ok"] and "decide it on the dashboard" in res["error"]
        assert hub.ap.queue.get(h["id"])["state"] == "pending"
    # The floor holds whatever the configurable list says.
    for c, action in (("secret.set", None), ("vault.get", None), ("policy.set", None),
                      ("settings.set", None), ("token.admin", None), ("job.write", "lock"),
                      ("job.write", "unlock"), ("approvals.relay", None)):
        fact = {"kind": "card", "cap": c, "action": action, "tier": "write", "tags": []}
        assert refusal(fact, ()), c
    assert not refusal({"kind": "card", "cap": "job.write", "action": "run", "tier": "write"}, ())
    assert matches("job.write:delete", {"cap": "job.write", "action": "delete"})
    assert matches("tier:exec", {"cap": "x", "tier": "admin"})
    assert not matches("tier:admin", {"cap": "x", "tier": "exec"})
    assert matches("hub:admin", {"cap": "x", "tier": "admin", "hub": True})
    assert "job.write:lock" in FLOOR and "secret.*" in FLOOR
    # The shipped default list matches the setting's default.
    from rook.hub.settings_schema import _CORE
    setting, = [s for s, _o, _e in _CORE if s.name == "approvals.relay_dashboard_only"]
    assert tuple(setting.default) == A.Config().relay_dashboard_only


@pytest.mark.asyncio
async def test_a_job_run_never_lists_or_relays_even_as_an_enabled_token(hub):
    h = hold(hub.ap)
    hub.ap.queue.set_relayer(CLAUDE.id, True, "human:bake", "Claude web")
    from dataclasses import replace
    as_job = replace(CLAUDE, via=("job:j_sneaky",))
    res = await cap(hub, "approvals.relay", {"item": h["id"], "decision": "approve",
                                             "relayed_by": SAID}, as_job)
    assert not res["ok"] and "on behalf of job:j_sneaky" in res["error"]
    res = await cap(hub, "approvals.list", {}, as_job)
    assert not res["ok"]
    assert hub.ap.queue.get(h["id"])["state"] == "pending"


@pytest.mark.asyncio
async def test_a_card_is_relayable_by_its_tier_with_no_hub_host_floor(hub):
    await card_and_ask(hub)
    card = next(i for i in hub.relay.items() if i["kind"] == "card")
    assert card["tier"] == "read" and card["relayable"]          # box declares info.ping read
    hub.band.workers["w-box"]["tiers"]["info.ping"] = "exec"
    card = next(i for i in hub.relay.items() if i["kind"] == "card")
    assert card["tier"] == "exec" and card["relayable"] and "hold_kind" not in card


@pytest.mark.asyncio
async def test_a_relayed_hold_decision_is_audited_and_the_requester_sees_why(hub, tmp_path):
    from rook.hub.authz import Authorizer
    from rook.hub.policy import DEFAULT_POLICY, PolicyStore
    rows = []
    (tmp_path / "policy.json").write_text(json.dumps(DEFAULT_POLICY))
    hub.ap._record = lambda **kw: rows.append(kw)
    az = Authorizer(PolicyStore(str(tmp_path / "policy.json")), None, approvals=hub.ap)
    entry = {"worker_id": "nas", "name": "nas", "caps": ["shell.exec"]}
    args = {"argv": ["rm", "-rf", "/srv/cache"]}                 # a hard stop: erases for good
    d = az.check("shell.exec", "nas", entry, principal=CLAUDE, args=args)
    held = await az.resolve(d, args)
    assert held["held"]
    hub.ap.queue.set_relayer(CLAUDE.id, True, "human:bake", "Claude web")
    res = await cap(hub, "approvals.relay", {
        "item": held["hold"], "decision": "deny", "reason": "wrong box, use build-box",
        "relayed_by": {**SAID, "quote": "no, not on nas"}})
    assert res["ok"], res
    out = res["result"]
    assert out["state"] == "denied" and out["kind"] == "hold"
    assert out["recorded"] == 'denied by Bake via Claude web: "no, not on nas"'
    # The audit row: relaying identity, the person, their words, the session.
    rel, = hub.ap.queue.relays()
    assert (rel["kind"], rel["item"], rel["decision"], rel["relayer"], rel["who"],
            rel["quote"], rel["session"], rel["reason"]) == (
        "hold", held["hold"], "deny", CLAUDE.id, "Bake", "no, not on nas", SAID["session"],
        "wrong box, use build-box")
    with sqlite3.connect(hub.ap.queue.path) as db:
        for sql in ("UPDATE relays SET quote='yes'", "DELETE FROM relays"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                db.execute(sql)
    jr = [r for r in rows if r["cap"] == "approval.decide"][-1]
    assert jr["reply"]["relayed_by"]["quote"] == "no, not on nas"
    assert jr["reply"]["reason"] == "wrong box, use build-box"
    row = hub.ap.queue.get(held["hold"])
    assert row["decided_by"] == "Bake via Claude web" and row["note"] == "wrong box, use build-box"
    # The requester repeats the call: the person's denial, with their reason, not a new hold.
    again = await az.resolve(az.check("shell.exec", "nas", entry, principal=CLAUDE, args=args),
                             args)
    denial = again.denial()
    assert "Bake via Claude web denied shell.exec" in denial["error"]
    assert "Their reason: wrong box, use build-box" in denial["error"]
    assert denial["denied"]["reason"] == "wrong box, use build-box"
    assert len(hub.ap.queue.pending()) == 0


@pytest.mark.asyncio
async def test_relayed_card_and_question_decisions_reach_the_role_and_the_asker(hub):
    h = await card_and_ask(hub)
    hub.ap.queue.set_relayer(CLAUDE.id, True, "human:bake", "Claude web")
    items = {i["kind"]: i for i in hub.relay.items()}
    card, ask = items["card"], items["ask"]
    res = await cap(hub, "approvals.relay", {
        "item": card["name"].replace("-", " ").upper(), "decision": "deny",
        "reason": "the nightly is fine now", "relayed_by": {**SAID, "quote": "skip the rerun"}})
    assert res["ok"] and res["result"]["state"] == "dismissed", res
    texts = _texts(hub, h["room"])
    told = [t for s, t in texts if s == "agent:home" and "dismissed" not in t
            and "skip the rerun" in t]
    assert told and "Reason: the nightly is fine now" in told[-1]
    assert 'denied by Bake via Claude web: "skip the rerun"' in told[-1]
    msg = [m for m in hub.svc.chat.tail(h["room"], 50) if "skip the rerun" in m["text"]][-1]
    assert "role:qa" in msg["mentions"]
    # The chair (and so the role's next round) sees why it was dismissed.
    assert "the nightly is fine now" in hub.svc.chair.pending_text(h["room"])
    res = await cap(hub, "approvals.relay", {
        "item": ask["id"], "decision": "approve", "answer": "Yes, Friday",
        "relayed_by": {**SAID, "quote": "tell them Friday is fine"}})
    assert res["ok"] and res["result"]["answer"] == "Yes, Friday"
    q = hub.svc.store.question(int(ask["id"][1:]))
    assert q["state"] == "answered" and q["answered_by"] == "Bake via Claude web"
    last = hub.svc.chat.tail(h["room"], 1)[0]
    assert last["text"].startswith(f"Answer for {GUEST}: Yes, Friday (approved by Bake via "
                                   'Claude web: "tell them Friday is fine")')
    assert GUEST in last["mentions"]
    # The dashboard's huddle view says who decided, in the person's words.
    view = await hub.svc.view("user:operator", room=h["room"])
    vcard = next(c for c in view["cards"] if c["id"] == card["id"])
    assert vcard["relayed_text"] == 'denied by Bake via Claude web: "skip the rerun"'
    assert vcard["decided_by"] == "Bake via Claude web"
    vq = next(x for x in view["questions"] if f"q{x['id']}" == ask["id"])
    assert vq["relayed_text"] == 'approved by Bake via Claude web: "tell them Friday is fine"'
    assert {r["kind"] for r in hub.ap.queue.relays()} == {"card", "ask"}
    assert any(r["cap"] == "approval.relay" for r in hub.node.journal.rows)
    assert not hub.relay.items()


@pytest.mark.asyncio
async def test_an_archived_huddles_cards_and_questions_are_put_away_never_expired(hub):
    """Bake 2026-10-23: stale cards from archived huddles stay off his phone and
    lists; nothing is deleted or expired, and unarchiving brings them back."""
    h = await card_and_ask(hub)
    other = await _call(hub, "huddle.write", {"action": "ask", "text": "Elsewhere?"}, GUEST)
    assert other["room"] != h["room"]
    hub.ap.queue.set_relayer(CLAUDE.id, True, "human:bake", "Claude web")
    before = {i["id"] for i in hub.relay.items()}
    assert len(before) == 3
    # While they wait, an agent cannot put them away; a person can.
    res = await hub.node.dispatch("huddle.archive", {"huddle": h["room"]}, GUEST)
    assert not res["ok"] and "only they archive it" in res["error"]
    assert {i["id"] for i in hub.relay.items()} == before
    await _call(hub, "huddle.archive", {"huddle": h["room"]})
    left = hub.relay.items()
    assert [(i["kind"], i["room"]) for i in left] == [("ask", other["room"])]
    res = await cap(hub, "approvals.list", {})
    assert res["result"]["count"] == 1 and "Elsewhere?" in res["result"]["items"][0]["what"]
    assert [c["state"] for c in hub.svc.store.cards(h["room"])] == ["proposed"]
    assert [q["state"] for q in hub.svc.store.questions(h["room"])] == ["open"]
    hidden = next(i for i in before if i not in {x["id"] for x in left} and i.startswith("c_"))
    res = await cap(hub, "approvals.relay", {"item": hidden, "decision": "approve",
                                             "relayed_by": SAID})
    assert not res["ok"]
    # Nothing new waits unseen in it: asking and proposing there are refused.
    res = await hub.node.dispatch("huddle.write", {"action": "ask", "room": h["room"],
                                                   "text": "Still there?"}, GUEST)
    assert not res["ok"] and "archived" in res["error"]
    res = await hub.node.dispatch("huddle.card", {"huddle": h["room"], "action": "propose", "data": {
        "kind": "job.run", "summary": "Ping it", "worker": "box", "cap": "info.ping",
        "args": {}}}, GUEST)
    assert "archived" in json.dumps(res) and len(hub.svc.store.cards(h["room"])) == 1
    # Retention never touches a huddle that is not archived, and nothing expires.
    assert hub.node.plugin("huddle").sweep() == []
    # Anyone may bring it back; the same items wait again.
    await _call(hub, "huddle.archive", {"huddle": h["room"], "archived": False}, GUEST)
    assert {i["id"] for i in hub.relay.items()} == before


@pytest.mark.asyncio
async def test_a_failed_card_never_reaches_the_person(hub, monkeypatch):
    h = await card_and_ask(hub)
    hub.ap.queue.set_relayer(CLAUDE.id, True, "human:bake", "Claude web")
    card = next(i for i in hub.relay.items() if i["kind"] == "card")
    orig = hub.band.call

    async def call(c, args=None, target=None, *a, **kw):
        if c == "info.ping":
            return {"ok": False, "error": "box is unreachable"}
        return await orig(c, args, target, *a, **kw)
    monkeypatch.setattr(hub.band, "call", call)
    out = await _call(hub, "huddle.card", {"huddle": h["room"], "card": card["id"],
                                           "action": "approve"})
    assert out["state"] == "failed" and hub.svc.store.card(card["id"])["state"] == "failed"
    assert [i["kind"] for i in hub.relay.items()] == ["ask"]
    assert not hub.svc.store.pending_cards()
    res = await cap(hub, "approvals.list", {"kind": "card"})
    assert res["ok"] and res["result"]["count"] == 0


@pytest.mark.asyncio
async def test_an_approved_card_runs_as_the_relaying_agent_and_says_who_relayed_it(hub):
    """Never as a person: the agent's own policy, holds and admin gates apply."""
    h = await card_and_ask(hub)
    hub.ap.queue.set_relayer(CLAUDE.id, True, "human:bake", "Claude web")
    card = next(i for i in hub.relay.items() if i["kind"] == "card")
    res = await cap(hub, "approvals.relay", {"item": card["id"], "decision": "approve",
                                             "relayed_by": SAID})
    assert res["ok"] and res["result"]["state"] == "done", res
    ping = [c for c in hub.band.calls if c.cap == "info.ping"][-1]
    assert ping.principal.id == CLAUDE.id and ping.principal.kind == "token"
    assert ping.principal.label == "Bake via Claude web" and ping.identity == CLAUDE.id
    assert any('approved by Bake via Claude web: "yes, restart nginx on box"' in t
               for _s, t in _texts(hub, h["room"]))


# -- the dashboard -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_approvals_page_shows_who_relayed_a_decision(hub, tmp_path):
    from aiohttp import web
    from rook.remote.approvals_web import ApprovalsWeb
    owner = Principal("human:u1", "human", "owner", ("human:owner",), label="bake")
    h = hold(hub.ap)
    hub.ap.queue.set_relayer(CLAUDE.id, True, "human:u1", "Claude web")
    assert (await cap(hub, "approvals.relay", {"item": h["id"], "decision": "approve",
                                               "relayed_by": SAID}))["ok"]

    async def creds(*_):
        return False
    server = SimpleNamespace(_band=SimpleNamespace(authz=SimpleNamespace(approvals=hub.ap)),
                             _accounts=SimpleNamespace(store=SimpleNamespace(
                                 session_weak=lambda tok: False)),
                             credential_is_vaulted=creds)

    @web.middleware
    async def who(request, handler):
        tok = current_principal.set(owner)
        try:
            return await handler(request)
        finally:
            current_principal.reset(tok)
    app = web.Application(middlewares=[who])
    ApprovalsWeb(server).install(app)
    async with TestClient(TestServer(app)) as http:
        data = await (await http.get("/api/approvals")).json()
    rec, = data["recent"]
    assert rec["state"] == "approved" and rec["name"] == h["name"]
    assert rec["relayed_text"] == 'approved by Bake via Claude web: "yes, restart nginx on box"'
    assert data["relays"][0]["text"] == rec["relayed_text"]
    assert data["relayers"][0]["principal"] == CLAUDE.id
    assert "tier:admin" in data["settings"]["relay_dashboard_only"]


def test_only_a_person_on_the_tokens_page_turns_relaying_on(tmp_path, monkeypatch):
    from rook.band_mcp.account_tokens import build_account_token_routes
    from rook.band_mcp.tokens import TokenStore
    from rook.remote.account_web import AccountWeb
    from rook.remote.enrollment import EnrollmentStore
    monkeypatch.setenv('ROOK_SETUP_PATH', str(tmp_path / 'setup.json'))
    enrollment = EnrollmentStore(tmp_path / 'enrollment.db')
    enrollment.register('Home', 'original-key', 'hub.example.com:443', primary=True)
    account = AccountWeb(SimpleNamespace(_enrollment=enrollment, web_user='operator',
                                         web_pass='operator-password', domain='rook.example.com',
                                         hub_public='hub.example.com:443', _band=None))
    store = account.store
    token = store.new_session(account.bootstrap_id)
    csrf = store.session(token)['csrf']
    ap = A.Approvals(str(tmp_path / "approvals.db"), FAST)
    provider = TokenStore(persist_path=str(tmp_path / 'tokens.json'))
    made = provider.mint_api_token("Claude web")
    app = Starlette(routes=build_account_token_routes(
        provider, accounts=store, approvals=lambda: ap,
        house=lambda: ("agent:home", "House agent (home)")))
    endpoint, headers = '/tokens/account-api', {'Cookie': 'rook_account=' + token}
    with ASGIClient(app) as client:
        listed = client.get(endpoint, headers=headers).json()
        assert listed['tokens'][0]['relay'] is False                  # off for everyone
        assert listed['relay']['house'] == {'identity': 'agent:home',
                                            'label': 'House agent (home)', 'relay': False}
        r = client.post(endpoint, headers=headers, json={'op': 'relay', 'id': made['id'],
                                                         'enabled': True})
        assert r.status_code == 403                                   # CSRF
        r = client.post(endpoint, headers=headers, json={'op': 'relay', 'id': made['id'],
                                                         'enabled': True, 'csrf': csrf})
        assert r.status_code == 200 and r.json()['changed']
        assert ap.queue.relayer(f"token:{made['agent_id']}")['enabled_by'].startswith('human:')
        listed = client.get(endpoint, headers=headers).json()
        assert listed['tokens'][0]['relay'] is True
        client.post(endpoint, headers=headers, json={'op': 'relay', 'id': 'house',
                                                     'enabled': True, 'csrf': csrf})
        assert ap.queue.relayer("agent:home")
        # A sign-in with a password agents could read cannot change it.
        store.mark_weak_session(token)
        r = client.post(endpoint, headers=headers, json={'op': 'relay', 'id': 'house',
                                                         'enabled': False, 'csrf': csrf})
        assert r.status_code == 403 and r.json()['weak_credential']
        assert ap.queue.relayer("agent:home")
        # Revoking the client's only key stops it relaying.
        weak_free = store.new_session(account.bootstrap_id)
        h2 = {'Cookie': 'rook_account=' + weak_free}
        c2 = store.session(weak_free)['csrf']
        assert client.post(endpoint, headers=h2, json={'op': 'revoke', 'id': made['id'],
                                                       'confirm': True,
                                                       'csrf': c2}).status_code == 200
        assert ap.queue.relayer(f"token:{made['agent_id']}") is None
    with sqlite3.connect(ap.queue.path) as db:
        log = db.execute("SELECT principal, enabled FROM relayer_log ORDER BY at").fetchall()
    assert log == [(f"token:{made['agent_id']}", 1), ("agent:home", 1),
                   (f"token:{made['agent_id']}", 0)]


def test_no_cap_or_tool_changes_who_may_relay():
    from rook.core import authz as core_authz
    caps = set(core_authz.builtin_table())
    assert not [c for c in caps if "relayer" in c or c.startswith("relay.")]
    import inspect
    import rook.hub.plugins.approvals as plugin
    assert "set_relayer" not in inspect.getsource(plugin)


# -- the chair --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_chair_answers_whats_waiting_and_relays_the_persons_words(hub):
    await _roles(hub)
    h = await _call(hub, "huddle.start", {"title": "Just us", "roles": []})
    held = hold(hub.ap)
    hub.band.script = {"huddle-chair": [{"tools": [("waiting", {}), ("answer", {
        "text": "1 waiting."})]}]}
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "what's waiting?"})
    await _settle(hub)
    turn = hub.band.turns[-1]
    assert turn["listed"] == ["answer", "propose", "waiting"]       # not enabled: no relay
    assert turn["seen"][0]["count"] == 1 and held["name"] in turn["seen"][0]["summary"]
    assert "what is waiting" in turn["args"]["system"]
    hub.ap.queue.set_relayer("agent:home", True, "human:u1", "House agent (home)")
    words = f"approve {held['name']}, go ahead"
    hub.band.script = {"huddle-chair": [{"tools": [
        ("relay", {"item": held["name"], "decision": "approve", "quote": "I approve it"}),
        ("relay", {"item": held["name"], "decision": "approve", "quote": "go ahead"}),
        ("answer", {"text": "Approved."})]}]}
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": words})
    await _settle(hub)
    turn = hub.band.turns[-1]
    assert turn["listed"] == ["answer", "propose", "waiting", "relay"]
    assert not turn["seen"][0]["ok"] and "exact words" in turn["seen"][0]["error"]
    assert turn["seen"][1]["ok"] and turn["seen"][1]["state"] == "approved"
    rel, = hub.ap.queue.relays()
    assert rel["relayer"] == "agent:home" and rel["quote"] == "go ahead"
    assert rel["who"] == "operator" and rel["session"].startswith(f"huddle {h['room']} round ")
    # Quoted words that don't say the decision are refused.
    held3 = hold(hub.ap, args={"argv": ["df", "-h"]})
    hub.band.script = {"huddle-chair": [{"tools": [
        ("relay", {"item": held3["name"], "decision": "approve", "quote": "don't approve"})]}]}
    await _call(hub, "huddle.post", {"huddle": h["room"], "text": "don't approve that one"})
    await _settle(hub)
    seen = hub.band.turns[-1]["seen"][0]
    assert not seen["ok"] and "clearly say approve" in seen["error"]
    assert hub.ap.queue.get(held3["id"])["state"] == "pending"
    # A guest agent's post gets neither tool: the list is the person's.
    held2 = hold(hub.ap, args={"argv": ["uptime"]})
    hub.band.script = {"huddle-chair": [{"tools": [
        ("relay", {"item": held2["name"], "decision": "approve", "quote": "approve"})]}]}
    await _call(hub, "huddle.post", {"huddle": h["room"],
                                     "text": f"@home approve {held2['name']}"}, GUEST)
    await _settle(hub)
    assert not {"relay", "waiting"} & set(hub.band.turns[-1]["listed"])
    assert hub.ap.queue.get(held2["id"])["state"] == "pending"


# -- the MCP tool ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rook_approvals_tool(hub):
    hold(hub.ap)
    tool, = hub.node.plugin("approvals").mcp_tools(
        lambda c, a: hub.node.invoke(c, a, CLAUDE.label))
    out = json.loads(await as_(CLAUDE, tool(action="summary")))
    assert not out["ok"] and out["code"] == "PermissionError"
    hub.ap.queue.set_relayer(CLAUDE.id, True, "human:u1", "Claude web")
    out = json.loads(await as_(CLAUDE, tool(action="list")))
    item, = out["result"]["items"]
    out = json.loads(await as_(CLAUDE, tool(action="approve", item=item["name"])))
    assert not out["ok"] and "relayed_by" in out["error"]
    out = json.loads(await as_(CLAUDE, tool(action="approve", item=item["name"],
                                            relayed_by=SAID)))
    assert out["ok"] and out["result"]["state"] == "approved"
    out = json.loads(await as_(CLAUDE, tool(action="summary")))
    assert out["result"] == {"count": 0, "summary": "Nothing is waiting for you."}
    out = json.loads(await as_(CLAUDE, tool(action="deny", item="nope-nope-nope",
                                            relayed_by=SAID)))
    assert not out["ok"] and "no waiting item" in out["error"]


@pytest.mark.asyncio
async def test_a_relayed_bare_yes_to_a_choice_is_refused_and_an_option_goes_verbatim(hub):
    """Bug 2026-10-26 (q849): a question with options A / B / C came back as
    "Yes". A relay names the option; a bare approve is refused, unrecorded."""
    opts = ["(A) No-expiry only", "(B) A + remove plugin features", "(C) Everything"]
    h = await _call(hub, "huddle.start", {"title": "Approvals"})
    await _call(hub, "huddle.write", {"action": "ask", "room": h["room"], "text": "Which scope?",
                                      "data": {"options": opts}}, GUEST)
    hub.ap.queue.set_relayer(CLAUDE.id, True, "human:bake", "Claude web")
    ask, = [i for i in hub.relay.items() if i["kind"] == "ask"]
    res = await cap(hub, "approvals.relay", {"item": ask["id"], "decision": "approve",
                                             "relayed_by": {**SAID, "quote": "yes"}})
    assert not res["ok"] and "is a choice, not a yes or no" in res["error"], res
    res = await cap(hub, "approvals.relay", {"item": ask["id"], "decision": "approve",
                                             "answer": "yes",
                                             "relayed_by": {**SAID, "quote": "yes"}})
    assert not res["ok"] and "doesn't say which option" in res["error"], res
    assert hub.ap.queue.relays() == []
    res = await cap(hub, "approvals.relay", {"item": ask["id"], "decision": "approve",
                                             "answer": "b",
                                             "relayed_by": {**SAID, "quote": "go with B"}})
    assert res["ok"] and res["result"]["answer"] == opts[1], res
    q = hub.svc.store.question(int(ask["id"][1:]))
    assert q["state"] == "answered" and q["answer"] == opts[1]
