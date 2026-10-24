"""The attention hub plugin (rook/hub/plugins/attention, docs/design/attention.md)
against a fake phone that speaks the attention.post/update/poll contract."""
from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient as ASGIClient

from rook.hub.plugins.attention.service import AttentionService
from tests.test_huddle_chair import hub as chair_hub  # noqa: F401
from tests.test_huddle_chair import GUEST, _call
from tests.test_relay import FAST, card_and_ask, hold, hub  # noqa: F401

PHONE = "w-phone"


class FakePhone:
    """The app's side of the contract: cards by id, updates, an event log."""

    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.posts: list[dict] = []
        self.updates: list[tuple[str, str, str]] = []
        self.events: list[dict] = []
        self.polls: list[int] = []
        self.rules: list = []
        self.seq = 0
        self.body = True            # the app shows a card body (0.4.22 and later)

    def event(self, type_: str, **kw) -> dict:
        self.seq += 1
        ev = {"seq": self.seq, "ts": time.time(), "type": type_, **kw}
        self.events.append(ev)
        return ev

    def tap(self, ident: str, decision: str, **kw) -> dict:
        return self.event("decision", id=ident, decision=decision, **kw)

    def handle(self, cap: str, args: dict) -> dict:
        if cap == "attention.post":
            assert set(args) - {"body"} == {"id", "kind", "line", "explain", "urgency", "channel",
                                            "actions", "source", "expires_at"}
            assert len(args.get("body") or "") <= 600 and (args.get("body") or "").count("\n") <= 5
            assert len(args["id"]) <= 64 and len(args["line"]) <= 140
            assert len(args["explain"]) <= 600 and "\n" not in args["line"]
            assert args["urgency"] in ("urgent", "normal")
            assert args["channel"] in ("urgent", "family", "approvals", "digest", "jobs")
            assert set(args["actions"]) <= {"yes", "no", "explain", "ack"}
            self.cards[args["id"]] = dict(args)
            self.posts.append(dict(args))
            return {"ok": True, "id": args["id"]}
        if cap == "attention.update":
            assert args["state"] in ("done", "failed", "gone")
            self.updates.append((args["id"], args["state"], args["message"]))
            return {"ok": True}
        if cap == "attention.poll":
            self.polls.append(args["since"])
            if args["since"] > self.seq:   # the app's contract: behind the caller
                return {"events": list(self.events), "last": self.seq, "reset": True}
            evs = [e for e in self.events if e["seq"] > args["since"]]
            return {"events": evs, "last": self.seq}
        if cap == "attention.rules_set":
            self.rules.append(args["rules"])
            return {"ok": True}
        raise AssertionError(cap)


@pytest.fixture
def att(hub, monkeypatch):  # noqa: F811
    phone = FakePhone()
    hub.band.workers[PHONE] = {"name": "pixel", "last_seen": time.time(),
                               "caps": ["attention.post", "attention.update", "attention.poll"]}
    orig = hub.band.call
    phone_calls = []

    async def call(cap, args=None, target=None, timeout=15.0, identity=None, principal=None):
        if target == PHONE:
            phone_calls.append(SimpleNamespace(cap=cap, args=args, identity=identity,
                                               principal=principal))
            if time.time() - hub.band.workers[PHONE]["last_seen"] > 90:
                return {"ok": False, "error": "worker offline"}
            if cap == "attention.post" and "body" in (args or {}) and not phone.body:
                # An app older than the card body: the worker refuses the unknown arg.
                return {"ok": False, "error": "bad args: _post() got an unexpected keyword "
                                              "argument 'body'"}
            return {"ok": True, "result": phone.handle(cap, args or {})}
        return await orig(cap, args, target, timeout, identity, principal)

    monkeypatch.setattr(hub.band, "call", call)
    plugin = hub.node.plugin("attention")
    hub.phone, hub.phone_calls, hub.att = phone, phone_calls, plugin

    def configure(**kw):
        for k, v in {"devices": ["pixel"], "person": "Bake", **kw}.items():
            hub.node.settings.set(f"attention.{k}", v, actor="human:op")
    hub.configure = configure
    return hub


def offline(hub, yes=True):
    hub.band.workers[PHONE]["last_seen"] = time.time() - (600 if yes else 0)


# -- inert --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inert_while_no_phone_is_configured(att):
    hold(att.ap)
    assert await att.att.service.tick() == {}
    assert att.phone_calls == []
    res = await att.node.dispatch("attention.send", {"text": "hello"}, "agent")
    assert not res["ok"] and "attention.devices" in res["error"]
    assert att.att.relay_rows() == []


# -- cards ------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_items_are_posted_once_and_gone_items_are_withdrawn(att):
    att.configure()
    plain = hold(att.ap)
    secret = hold(att.ap, cap="secret.get", tier="admin", args={"name": "db"})
    out = await att.att.service.tick()
    assert out["pixel"]["posted"] == 2
    card = att.phone.cards[plain["id"]]
    assert card["kind"] == "hold" and card["channel"] == "approvals"
    assert card["urgency"] == "normal" and card["actions"] == ["yes", "no", "explain"]
    assert card["expires_at"] == 0                        # never expires on the phone
    # The plain ask: a title as the line, For/Who/I want to/Risk as the body, the
    # technical detail (cap, worker, hold id) only behind Explain.
    assert card["line"] == "Restart nginx"
    assert card["body"].split("\n") == [
        "For: no task", "Who: Claude web", "I want to: Restart the service nginx on box",
        "Risk: Medium: it runs a command that can change things; this agent is reaching many "
        "machines quickly."]
    assert "shell.exec on box" in card["explain"] and plain["id"] in card["explain"]
    assert "shell.exec" not in card["line"] + card["body"]
    # Dashboard-only: no Yes/No, and the explanation says where to decide.
    dash = att.phone.cards[secret["id"]]
    assert dash["actions"] == ["explain"] and "dashboard's Approvals page" in dash["explain"]
    assert dash["line"] == "Read a secret"
    # Every call to the phone is the hub's own.
    assert {c.identity for c in att.phone_calls} == {"system:rook-hub-attention"}
    assert all(c.principal.kind == "system" for c in att.phone_calls)
    await att.att.service.tick()
    assert len(att.phone.posts) == 2                      # never re-rung
    att.ap.decide(plain["id"], True, "human:bake", scope="call")
    await att.att.service.tick()
    assert (plain["id"], "gone", "No longer waiting on you.") in att.phone.updates
    assert plain["id"] not in att.att.service.state["devices"][PHONE]["posted"]
    # A restart does not re-ring what the phone already has.
    fresh = AttentionService(att.att.service.path, att.att.service._settings,
                             att.att.service._client, att.att.service._relay)
    att.att._service = fresh
    await fresh.tick()
    assert len(att.phone.posts) == 2


@pytest.mark.asyncio
async def test_an_older_app_gets_the_body_folded_into_line_and_explain(att):
    """A phone whose app predates the card body refuses ``body``: the hub posts
    the card again without it (what it is for in the line, the body ahead of
    the detail under Explain) and remembers that for the app's version."""
    att.configure()
    att.phone.body = False
    first = hold(att.ap)
    await att.att.service.tick()
    card = att.phone.cards[first["id"]]
    assert "body" not in card and card["line"] == "Restart nginx"
    assert card["explain"].startswith("For: no task Who: Claude web I want to: Restart the "
                                      "service nginx on box Risk: Medium")
    assert "Details: shell.exec on box" in card["explain"]
    tries = [c for c in att.phone_calls if c.cap == "attention.post"]
    assert len(tries) == 2                       # one refused, one folded
    second = hold(att.ap, args={"argv": ["df", "-h"]})
    await att.att.service.tick()
    tries = [c for c in att.phone_calls if c.cap == "attention.post"]
    assert len(tries) == 3 and "body" not in tries[-1].args   # remembered: no refused try
    assert att.phone.cards[second["id"]]["line"] == "Look at files"
    # The app updated (a new version): bodies again.
    att.phone.body = True
    att.band.workers[PHONE]["version"] = "0.4.22-beta.1"
    third = hold(att.ap, args={"argv": ["uptime"]})
    await att.att.service.tick()
    assert att.phone.cards[third["id"]]["body"].startswith("For: no task\nWho: Claude web")


@pytest.mark.asyncio
async def test_a_tap_is_relayed_with_exact_attribution(att):
    att.configure()
    item = hold(att.ap)
    await att.att.service.tick()
    att.ap.queue.set_relayer(f"device:{PHONE}", True, "human:bake", "pixel (phone)")
    ev = att.phone.tap(item["id"], "deny", reason="not tonight")
    await att.att.service.tick()
    rel, = att.ap.queue.relays()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ev["ts"]))
    assert (rel["kind"], rel["item"], rel["decision"], rel["relayer"], rel["relayer_label"],
            rel["who"], rel["quote"], rel["session"], rel["reason"]) == (
        "hold", item["id"], "deny", f"device:{PHONE}", "pixel (phone)", "Bake",
        "Tapped No on the decision card on pixel", f"device:{PHONE} card {item['id']} {stamp}",
        "not tonight")
    row = att.ap.queue.get(item["id"])
    assert row["state"] == "denied" and row["decided_by"] == "Bake via pixel (phone)"
    done = [u for u in att.phone.updates if u[0] == item["id"]]
    assert done == [(item["id"], "done",
                     'denied by Bake via pixel (phone): "Tapped No on the decision card on pixel"')]
    await att.att.service.tick()                          # decided: no "gone" after "done"
    assert [u for u in att.phone.updates if u[0] == item["id"]] == done
    # The same tap is never replayed.
    assert att.phone.polls[-1] == ev["seq"]


@pytest.mark.asyncio
async def test_a_tap_from_a_phone_not_enabled_is_refused_and_offered_again(att):
    att.configure()
    item = hold(att.ap)
    await att.att.service.tick()
    att.phone.tap(item["id"], "approve")
    await att.att.service.tick()
    (ident, state, msg), = att.phone.updates
    assert (ident, state) == (item["id"], "failed")
    assert "May relay my approvals" in msg and "pixel (phone)" in msg and "Tokens" in msg
    assert att.ap.queue.get(item["id"])["state"] == "pending" and not att.ap.queue.relays()
    await att.att.service.tick()
    assert len(att.phone.posts) == 1
    # Once the person turns relaying on, the card comes back so the tap counts.
    att.ap.queue.set_relayer(f"device:{PHONE}", True, "human:bake", "pixel (phone)")
    await att.att.service.tick()
    assert len(att.phone.posts) == 2
    att.phone.tap(item["id"], "approve")
    await att.att.service.tick()
    assert att.ap.queue.get(item["id"])["state"] == "approved"
    assert att.phone.updates[-1][:2] == (item["id"], "done")


@pytest.mark.asyncio
async def test_a_dashboard_only_item_is_never_decided_from_the_phone(att):
    att.configure()
    item = hold(att.ap, cap="secret.get", tier="admin", args={"name": "db"})
    att.ap.queue.set_relayer(f"device:{PHONE}", True, "human:bake", "pixel (phone)")
    await att.att.service.tick()
    att.phone.tap(item["id"], "approve")
    att.phone.tap("h-not-posted", "approve")
    await att.att.service.tick()
    assert att.ap.queue.get(item["id"])["state"] == "pending" and not att.ap.queue.relays()
    states = {u[0]: u[1] for u in att.phone.updates}
    assert states == {item["id"]: "failed", "h-not-posted": "gone"}


# -- alerts, signals, notices ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_alerts_queue_while_offline_and_are_delivered_on_reconnect(att):
    att.configure()
    offline(att)
    res = await att.node.dispatch("attention.send", {"text": "Garage door open",
                                                     "urgency": "urgent"}, "house")
    assert res["ok"], res
    sent = res["result"]
    assert not sent["delivered_now"] and sent["queued_for"] == ["pixel"]
    await att.att.service.tick()
    assert att.phone.posts == []
    status = (await att.node.dispatch("attention.status", {}, "house"))["result"]
    dev, = status["devices"]
    assert not dev["online"] and dev["pending_alerts"] == 1
    offline(att, False)
    await att.att.service.tick()
    card, = att.phone.posts
    assert card["id"] == sent["id"] and card["kind"] == "alert" and card["actions"] == ["ack"]
    assert card["urgency"] == "urgent" and card["channel"] == "urgent"
    await att.att.service.tick()
    assert len(att.phone.posts) == 1
    status = (await att.node.dispatch("attention.status", {}, "house"))["result"]
    assert status["devices"][0]["pending_alerts"] == 0
    assert status["recent_alerts"][0]["delivered"]["pixel"]["at"].endswith("Z")
    # Online: delivered at once.
    res = await att.node.dispatch("attention.send", {"text": "Dinner", "channel": "family"}, "h")
    assert res["result"]["delivered_now"] and att.phone.posts[-1]["channel"] == "family"


@pytest.mark.asyncio
async def test_poll_seq_is_persisted_and_signals_and_notices_are_kept(att):
    att.configure()
    att.phone.event("unlocked")
    att.phone.event("seen", id="x1")
    att.phone.event("notice", app="Messages", category="msg", priority="family",
                    summary="Mom: call me when you can " + "x" * 300)
    await att.att.service.tick()
    assert att.att.service.state["devices"][PHONE]["last_seq"] == 3
    fresh = AttentionService(att.att.service.path, att.att.service._settings,
                             att.att.service._client, att.att.service._relay)
    att.att._service = fresh
    await fresh.tick()
    assert att.phone.polls == [0, 3]
    status = (await att.node.dispatch("attention.status", {}, "house"))["result"]
    dev, = status["devices"]
    assert set(dev["last_seen"]) == {"unlocked", "seen"}
    assert dev["last_event"]["type"] == "notice" and dev["last_poll"]
    assert [s["type"] for s in dev["recent_signals"]] == ["seen", "unlocked"]
    notes = (await att.node.dispatch("attention.notices", {"limit": 5}, "house"))["result"]
    n, = notes["notices"]
    assert n["app"] == "Messages" and n["priority"] == "family" and len(n["summary"]) == 140
    assert n["device"] == "pixel"


@pytest.mark.asyncio
async def test_rules_are_pushed_only_when_enabled(att):
    att.configure()
    att.band.workers[PHONE]["caps"].append("attention.rules_set")
    await att.att.service.tick()
    assert att.phone.rules == []
    att.configure(push_rules=True)
    await att.att.service.tick()
    await att.att.service.tick()
    assert att.phone.rules == [[]]


# -- consent on the Tokens page -----------------------------------------------------------

def test_the_tokens_page_lists_configured_phones_off_by_default(tmp_path, monkeypatch):
    from rook.band_mcp.account_tokens import build_account_token_routes
    from rook.band_mcp.tokens import TokenStore
    from rook.hub import approvals as A
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
    app = Starlette(routes=build_account_token_routes(
        TokenStore(persist_path=str(tmp_path / 'tokens.json')), accounts=store,
        approvals=lambda: ap, devices=lambda: [(f"device:{PHONE}", "pixel (phone)")]))
    endpoint, headers = '/tokens/account-api', {'Cookie': 'rook_account=' + token}
    with ASGIClient(app) as client:
        listed = client.get(endpoint, headers=headers).json()
        assert listed['relay']['devices'] == [{'identity': f'device:{PHONE}',
                                               'label': 'pixel (phone)', 'relay': False}]
        r = client.post(endpoint, headers=headers, json={'op': 'relay', 'id': 'device:other',
                                                         'enabled': True, 'csrf': csrf})
        assert r.status_code == 404 and ap.queue.relayer('device:other') is None
        r = client.post(endpoint, headers=headers, json={'op': 'relay', 'id': f'device:{PHONE}',
                                                         'enabled': True, 'csrf': csrf})
        assert r.status_code == 200 and r.json()['changed']
        assert ap.queue.relayer(f'device:{PHONE}')['label'] == 'pixel (phone)'
        listed = client.get(endpoint, headers=headers).json()
        assert listed['relay']['devices'][0]['relay'] is True
    with sqlite3.connect(ap.queue.path) as db:
        assert db.execute("SELECT principal, enabled FROM relayer_log").fetchall() == [
            (f"device:{PHONE}", 1)]


@pytest.mark.asyncio
async def test_relay_rows_name_the_configured_phones(att):
    att.configure()
    assert att.att.relay_rows() == [(f"device:{PHONE}", "pixel (phone)")]
    att.configure(devices=[PHONE, "missing-phone"])
    assert att.att.relay_rows() == [(f"device:{PHONE}", "pixel (phone)")]
    status = att.att.service.status()
    assert status["devices"][1]["note"] == "no worker on the band has this name or id"


@pytest.mark.asyncio
async def test_a_phone_whose_log_restarted_is_read_from_the_start_and_reposted(att):
    """App data cleared: the phone answers reset with its new, low-seq events;
    a tap among them still counts, and the cards it lost are posted again."""
    att.configure()
    item = hold(att.ap)
    for _ in range(5):
        att.phone.event("unlocked")
    await att.att.service.tick()
    assert att.att.service.state["devices"][PHONE]["last_seq"] == 5
    assert len(att.phone.posts) == 1
    # Reinstall: cards and log gone, the counter starts again.
    att.phone.cards.clear()
    att.phone.events.clear()
    att.phone.seq = 0
    att.phone.event("seen", id=item["id"])
    await att.att.service.tick()
    assert att.att.service.state["devices"][PHONE]["last_seq"] == 1
    assert item["id"] in att.phone.cards and len(att.phone.posts) == 2
    sig = att.att.service.state["signals"][-1]
    assert sig["type"] == "seen" and sig["id"] == item["id"]


@pytest.mark.asyncio
async def test_archiving_a_huddle_withdraws_its_cards_and_unarchiving_posts_them_again(att):
    att.configure()
    h = await card_and_ask(att)
    await att.att.service.tick()
    ids = {i["id"] for i in att.relay.items()}
    assert len(ids) == 2 and ids <= set(att.phone.cards)
    await _call(att, "huddle.archive", {"huddle": h["room"]})
    await att.att.service.tick()
    assert {(i, s) for i, s, _ in att.phone.updates} == {(i, "gone") for i in ids}
    assert not att.att.service.state["devices"][PHONE]["posted"]
    await att.att.service.tick()
    assert len(att.phone.posts) == 2                      # never re-rung while archived
    await _call(att, "huddle.archive", {"huddle": h["room"], "archived": False}, GUEST)
    await att.att.service.tick()
    assert [p["id"] for p in att.phone.posts[2:]] and {p["id"] for p in att.phone.posts[2:]} == ids


@pytest.mark.asyncio
async def test_a_failed_card_is_withdrawn_from_the_phone_and_never_posted_again(att, monkeypatch):
    att.configure()
    h = await card_and_ask(att)
    await att.att.service.tick()
    card = next(i for i in att.relay.items() if i["kind"] == "card")
    orig = att.band.call

    async def call(cap, args=None, target=None, *a, **kw):
        if cap == "info.ping":
            return {"ok": False, "error": "box is unreachable"}
        return await orig(cap, args, target, *a, **kw)
    monkeypatch.setattr(att.band, "call", call)
    out = await _call(att, "huddle.card", {"huddle": h["room"], "card": card["id"],
                                           "action": "approve"})
    assert out["state"] == "failed"
    await att.att.service.tick()
    await att.att.service.tick()
    assert (card["id"], "gone") in {(i, s) for i, s, _ in att.phone.updates}
    assert [p["id"] for p in att.phone.posts].count(card["id"]) == 1

