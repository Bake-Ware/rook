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
        self.reminds: list[dict] = []
        self.remind_reply: dict = {"accepted": True}

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
        if cap == "attention.remind":
            assert set(args) == {"id", "text", "sample_ms", "speak_unchecked"}
            assert 1 <= len(args["id"]) <= 64 and 0 < len(args["text"]) <= 300
            self.reminds.append(dict(args))
            return {"ok": True, "id": args["id"], **self.remind_reply}
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
    # This phone's app can't speak reminders (no attention.remind): its cards still pop up.
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



# -- the queue and spoken reminders ------------------------------------------------------------

def speaking(att, **kw):
    """A phone whose app has attention.remind (0.4.22-beta.3), reminders right away."""
    att.band.workers[PHONE]["caps"].append("attention.remind")
    att.configure(**{"remind_wait_s": 0, **kw})


def later(att, secs):
    """The hub's clock moves on; the phone stays online (it heartbeats)."""
    base = att.att.service.clock
    att.att.service.clock = lambda: base() + secs
    att.band.workers[PHONE]["last_seen"] = att.att.service.clock()


def outcome(att, outcome, **kw):
    rid = att.phone.reminds[-1]["id"]
    return att.phone.event("reminded", id=rid, outcome=outcome, **kw)


@pytest.mark.asyncio
async def test_cards_queue_quietly_on_a_phone_that_can_remind(att):
    speaking(att, remind_wait_s=600)
    item = hold(att.ap)
    await att.att.service.tick()
    assert att.phone.cards[item["id"]]["channel"] == "digest"
    # Spoken reminders off: the quiet queue alone, as the person chose.
    att.band.workers[PHONE]["caps"].remove("attention.remind")
    att.configure(remind=False)
    other = hold(att.ap, args={"argv": ["uptime"]})
    await att.att.service.tick()
    assert att.phone.cards[other["id"]]["channel"] == "digest"
    # The log names the card by its safe title, never the command.
    assert any(r["what"] == "Card: Run a command on box" for r in att.att.service.eventlog.recent())


@pytest.mark.asyncio
async def test_queue_off_pops_cards_up_as_before(att):
    att.configure(queue=False)
    item = hold(att.ap)
    await att.att.service.tick()
    assert att.phone.cards[item["id"]]["channel"] == "approvals"
    assert att.phone.reminds == []


@pytest.mark.asyncio
async def test_one_waiting_card_is_announced_by_its_plain_title(att):
    speaking(att)
    item = hold(att.ap)
    await att.att.service.tick()
    r, = att.phone.reminds
    # A held call's spoken title comes from its cap and machine only.
    assert r["text"] == "Need your input: Run a command on box."
    assert r["sample_ms"] == 3000 and r["speak_unchecked"] is False
    # Nothing from the call's arguments is ever spoken.
    other = hold(att.ap, args={"argv": ["cat", "/etc/secret-plan.txt"]})
    att.att.service.state["remind"].clear()
    await att.att.service.tick()
    assert "secret-plan" not in att.phone.reminds[-1]["text"]
    assert att.phone.reminds[-1]["text"] == "Hey boss, you have 2 decisions pending."
    assert item["id"] != other["id"]


@pytest.mark.asyncio
async def test_a_spoken_reminder_is_logged_and_paced(att):
    speaking(att)
    hold(att.ap)
    await att.att.service.tick()
    outcome(att, "spoken", via="server")
    await att.att.service.tick()
    rows = att.att.service.eventlog.recent()
    said = [r for r in rows if r["outcome"] == "reminded"]
    assert said and said[0]["what"] == "Reminded you out loud: Need your input: Run a command on box."
    assert said[0]["why"] == "1 decision waiting and the room was quiet"
    assert "in the house voice" in said[0]["how"]
    assert len(att.phone.reminds) == 1                  # nothing new: no second reminder
    # A new card inside the gap waits for the gap; after it, one line for both.
    hold(att.ap, args={"argv": ["uptime"]})
    later(att, 5 * 60)
    await att.att.service.tick()
    assert len(att.phone.reminds) == 1
    later(att, 11 * 60)
    await att.att.service.tick()
    assert att.phone.reminds[-1]["text"] == "Hey boss, you have 2 decisions pending."
    outcome(att, "spoken", via="device")
    await att.att.service.tick()
    # Nothing new: again after remind_again_min (60), not before, and only once the
    # phone was unlocked since (never to an empty or sleeping room).
    later(att, 30 * 60)
    await att.att.service.tick()
    assert len(att.phone.reminds) == 2
    later(att, 31 * 60)
    await att.att.service.tick()
    assert len(att.phone.reminds) == 2
    att.phone.event("unlocked")
    await att.att.service.tick()          # the unlock is read and stamped by the hub
    later(att, 1)
    await att.att.service.tick()
    assert len(att.phone.reminds) == 3


@pytest.mark.asyncio
async def test_a_busy_room_is_tried_again_later_and_logged_once(att):
    speaking(att)
    hold(att.ap)
    await att.att.service.tick()
    outcome(att, "busy", level={"active_frac": 0.4, "peak_dbfs": -20})
    await att.att.service.tick()
    assert len(att.phone.reminds) == 1                  # not again at once
    later(att, 6 * 60)
    await att.att.service.tick()
    assert len(att.phone.reminds) == 2
    outcome(att, "busy")
    await att.att.service.tick()
    skipped = [r for r in att.att.service.eventlog.recent() if r["outcome"] == "skipped"]
    assert len(skipped) == 1 and skipped[0]["why"].startswith("someone was talking")
    later(att, 12 * 60)
    await att.att.service.tick()
    outcome(att, "call")
    await att.att.service.tick()
    skipped = [r for r in att.att.service.eventlog.recent() if r["outcome"] == "skipped"]
    assert len(skipped) == 2 and skipped[0]["why"].startswith("you were on a call")
    st = (await att.node.dispatch("attention.status", {}, "agent"))["result"]["reminders"]
    assert st["last_outcome"] == "call" and st["speaker"] == "pixel"


@pytest.mark.asyncio
async def test_a_reminder_the_phone_never_answers_counts_as_lost(att):
    speaking(att)
    hold(att.ap)
    await att.att.service.tick()
    later(att, 200)
    await att.att.service.tick()
    assert any(r["why"].startswith("the phone didn't answer")
               for r in att.att.service.eventlog.recent())
    # A late answer for the lost reminder changes nothing.
    rid = att.phone.reminds[0]["id"]
    att.phone.event("reminded", id=rid, outcome="spoken")
    await att.att.service.tick()
    assert not att.att.service.state["remind"].get("spoken_at")


@pytest.mark.asyncio
async def test_new_cards_wait_before_they_are_announced(att):
    speaking(att, remind_wait_s=120)
    item = hold(att.ap)
    await att.att.service.tick()
    assert att.phone.reminds == []
    att.ap.decide(item["id"], True, "human:bake", scope="call")   # decided quickly elsewhere
    later(att, 200)
    await att.att.service.tick()
    assert att.phone.reminds == []
    gone = [r for r in att.att.service.eventlog.recent() if r["outcome"] == "decided"]
    assert gone and gone[0]["why"] == "it was answered elsewhere or withdrawn"


@pytest.mark.asyncio
async def test_no_reminder_without_the_apps_remind_cap_or_with_reminders_off(att):
    att.configure(remind_wait_s=0)
    hold(att.ap)
    await att.att.service.tick()
    assert att.phone.reminds == []
    st = (await att.node.dispatch("attention.status", {}, "agent"))["result"]["reminders"]
    assert "0.4.22-beta.3" in st["note"]
    att.band.workers[PHONE]["caps"].append("attention.remind")
    att.configure(remind=False, remind_wait_s=0)
    await att.att.service.tick()
    assert att.phone.reminds == []


@pytest.mark.asyncio
async def test_quiet_hours_hold_reminders(att):
    from rook.hub.plugins.attention.service import parse_hours
    assert parse_hours("22:00-07:00") == (1320, 420)
    assert parse_hours("") is None and parse_hours("soon") is None
    assert parse_hours("25:00-07:00") is None and parse_hours("07:00-07:00") is None
    speaking(att, quiet_hours="00:00-24:00")
    hold(att.ap)
    await att.att.service.tick()
    assert att.phone.reminds == []
    svc = att.att.service
    import datetime as dt
    noon = dt.datetime(2026, 10, 24, 12, 0).astimezone().timestamp()
    att.configure(quiet_hours="22:00-07:00")
    assert not svc.quiet_hours(noon)
    assert svc.quiet_hours(noon + 11 * 3600) and svc.quiet_hours(noon - 6 * 3600)


@pytest.mark.asyncio
async def test_a_phone_that_refuses_the_reminder_is_tried_later(att):
    speaking(att)
    att.phone.remind_reply = {"accepted": False, "reason": "off"}
    hold(att.ap)
    await att.att.service.tick()
    assert len(att.phone.reminds) == 1
    await att.att.service.tick()
    assert len(att.phone.reminds) == 1
    assert any("spoken reminders are off in the app" in r["why"]
               for r in att.att.service.eventlog.recent())


@pytest.mark.asyncio
async def test_pending_trust_proposals_count_as_decisions(att, tmp_path):
    from rook.hub.trust import TrustStore
    store = TrustStore(str(tmp_path / "trust.json"))
    store.propose({"op": "set_unlisted", "data": {"level": "ship-it"}}, by="agent:x",
                  why="it keeps asking for the same thing")
    att.att.service._trust = lambda: store
    speaking(att)
    await att.att.service.tick()
    assert att.phone.reminds[-1]["text"] == "Need your input: a change to the trust map."


@pytest.mark.asyncio
async def test_the_log_records_cards_and_taps_in_plain_lines(att):
    att.configure()
    item = hold(att.ap)
    await att.att.service.tick()
    att.ap.queue.set_relayer(f"device:{PHONE}", True, "human:bake", "pixel (phone)")
    att.phone.tap(item["id"], "approve")
    await att.att.service.tick()
    res = (await att.node.dispatch("attention.log", {"limit": 10}, "agent"))["result"]
    whats = [r["what"] for r in res["rows"]]
    assert "Card: Run a command on box" in whats
    assert any(w.startswith("Approved: ") for w in whats)
    decided = next(r for r in res["rows"] if r["what"].startswith("Approved"))
    assert decided["how"] == "Bake tapped Yes on pixel" and decided["outcome"] == "decided"
    assert all("ref" not in r for r in res["rows"])      # internal pointers stay internal
    assert any("Because: a decision for you" in line for line in res["lines"])
    # Never a command's arguments in the log.
    assert not any("systemctl" in line for line in res["lines"])


def test_the_log_keeps_30_days(tmp_path):
    from rook.hub.eventlog import EventLog
    now = [1_800_000_000.0]
    log_ = EventLog(tmp_path / "e.db", clock=lambda: now[0])
    log_.add("attention", "ask", "Card: old one", "why")
    now[0] += 31 * 86400
    log_.add("attention", "ask", "Card: new one", "why")
    assert [r["what"] for r in log_.recent()] == ["Card: new one"]
    with pytest.raises(ValueError):
        log_.add("attention", "nonsense", "x")
    row = log_.add("voice", "decided", "x" * 500, "y" * 500)
    assert len(row["what"]) == 140 and len(row["why"]) == 200


@pytest.mark.asyncio
async def test_cards_pop_up_again_when_the_phone_cant_remind(att):
    """Review of #194 (skeptic): the app's switch off, no mic or a phone that never
    answers must not leave cards sitting quietly forever."""
    speaking(att)
    item = hold(att.ap)
    await att.att.service.tick()
    assert att.phone.cards[item["id"]]["channel"] == "digest"
    outcome(att, "unchecked")
    await att.att.service.tick()
    assert att.phone.cards[item["id"]]["channel"] == "digest"      # one miss: still quiet
    later(att, 6 * 60)
    await att.att.service.tick()
    outcome(att, "off")
    await att.att.service.tick()
    # Two in a row: the queued card is posted again on the approvals channel (same id).
    assert att.phone.cards[item["id"]]["channel"] == "approvals"
    other = hold(att.ap, args={"argv": ["uptime"]})
    await att.att.service.tick()
    assert att.phone.cards[other["id"]]["channel"] == "approvals"
    whys = [r["why"] for r in att.att.service.eventlog.recent()]
    assert any(w.startswith("the phone can't remind you") for w in whys)
    assert any("turn Spoken reminders on in the app's Attention tab" in w for w in whys)
    st = (await att.node.dispatch("attention.status", {}, "agent"))["result"]["reminders"]
    assert st["popping_up"]
    # A spoken reminder: new cards wait quietly again.
    later(att, 6 * 60)
    await att.att.service.tick()
    outcome(att, "spoken")
    await att.att.service.tick()
    third = hold(att.ap, args={"argv": ["df"]})
    await att.att.service.tick()
    assert att.phone.cards[third["id"]]["channel"] == "digest"


@pytest.mark.asyncio
async def test_a_repeat_waits_for_an_unlock_the_hub_heard(att):
    """Review of #194 (lead): the unlock that allows a repeat is timed by the hub, not
    by the phone's clock."""
    speaking(att)
    hold(att.ap)
    await att.att.service.tick()
    outcome(att, "spoken")
    await att.att.service.tick()
    later(att, 61 * 60)
    ev = att.phone.event("unlocked")
    ev["ts"] = time.time() + 10 * 86400          # a phone clock far ahead
    await att.att.service.tick()
    later(att, 1)
    # The hub heard this unlock after its own last reminder: the repeat may go.
    await att.att.service.tick()
    assert len(att.phone.reminds) == 2
    outcome(att, "spoken")
    await att.att.service.tick()
    later(att, 61 * 60)
    ev = att.phone.event("seen", id="x")         # not an unlock
    await att.att.service.tick()
    assert len(att.phone.reminds) == 2
