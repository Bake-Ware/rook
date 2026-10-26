"""Approval holds (docs/design/permissions.md 3.11, access v2): only the hard
stops and the trust map hold a call, nothing expires, operator hold rules are
ignored; waiting for a decision in the band client, an approval covering one
identical retry, the dashboard API and both hub layouts sharing one queue.
Relaying a person's words is tests/test_relay.py; clearing cards by voice is
tests/test_cardpass.py."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rook.band_mcp.client import BandClient
from rook.hub import approvals as A
from rook.hub.authz import Authorizer, current_principal, principal_for_user, system
from rook.hub.policy import DEFAULT_POLICY, Policy, PolicyError, PolicyStore, Principal

pytestmark = [pytest.mark.approvals]

AGENT = Principal("token:agent-1", "token", "agent", label="claude code")
OPERATOR_TOKEN = Principal("token:ops", "token", "operator", label="ops")
CI = Principal("token:ci", "token", "agent", label="ci")
OWNER = principal_for_user({"id": "u1", "username": "alice"}, True)
MEMBER = principal_for_user({"id": "u2", "username": "bob"}, False)
TELEGRAM = Principal("integration:telegram", "integration", "integration")

FAST = A.Config(wait_secs=0.6)
#: A hard stop (erases a data directory for good), and another one.
RM = {"argv": ["rm", "-rf", "/srv/data"]}
RM2 = {"argv": ["rm", "-rf", "/srv/old"]}
STOP_REASON = "hard stop: it deletes a database, or a top-level, home or data directory"


def cfg(**kw):
    return A.Config(**{**FAST.__dict__, **kw})


def authorizer(tmp_path, doc=None, config=None, record=None, **kw):
    path = tmp_path / "policy.json"
    if doc is not None:
        path.write_text(json.dumps(doc))
    ap = A.Approvals(str(tmp_path / "approvals.db"), config or FAST, record=record, **kw)
    return Authorizer(PolicyStore(str(path)), None, record=record, approvals=ap)


def entry(name, **kw):
    return {"worker_id": name, "name": name,
            "caps": ["shell.exec", "worker.update", "proc.close", "customcap.add"],
            "last_seen": time.time(), **kw}


def check(authz, cap, target="nas", principal=AGENT, local=False, args=None):
    e = {"name": "rook", "roles": ["is_hub"]} if local else entry(target)
    return authz.check(cap, "rook" if local else target, e, principal=principal, local=local,
                       args=args)


def stop(authz, target="nas", principal=AGENT, args=RM):
    return check(authz, "shell.exec", target, principal, args=args)


# -- which calls are held ------------------------------------------------------------

def test_only_hard_stops_hold_and_no_blanket_admin_or_destructive_hold(tmp_path):
    for advanced in (False, True):
        (tmp_path / str(advanced)).mkdir()
        az = authorizer(tmp_path / str(advanced), config=cfg(advanced=advanced))
        d = stop(az)
        assert d.held and d.hold_kind == "default" and d.hold_reason == STOP_REASON
        assert d.rule == "hardstop"
        for cap in ("worker.update", "customcap.add", "proc.close", "shell.exec", "info.ping"):
            assert not check(az, cap).held, (advanced, cap)
        assert not check(az, "settings.set", local=True,
                         args={"key": "hub.motd", "value": "x"}).held
        for p in (OPERATOR_TOKEN, TELEGRAM, Principal("unverified", "unverified", verified=False),
                  Principal("token:static", "token", "operator")):
            assert stop(az, principal=p).held, p.id


def test_people_and_hub_code_are_never_held(tmp_path):
    doc = {**DEFAULT_POLICY, "rules": [*DEFAULT_POLICY["rules"],
                                       {"id": "hold-members", "who": "human:member",
                                        "hold": "*", "on": "*"}]}
    az = authorizer(tmp_path, doc)
    for p in (OWNER, MEMBER, system("ota"), Principal("human:dashboard", "human", "owner",
                                                       ("human:owner",))):
        d = stop(az, principal=p)
        assert not d.held and not d.denied, (p.id, d)
    assert any("hold rules have no effect" in m for m in Policy(doc).lint())


def test_an_agent_in_the_chain_is_held_even_behind_a_person(tmp_path):
    az = authorizer(tmp_path)
    job_by_agent = Principal("human:u1", "human", "owner", ("human:owner",),
                             via=("job:j1", "token:agent-1"))
    house_agent = Principal("human:u1", "human", "owner", ("human:owner",),
                            via=("agent:home", "job:j2"))
    assert stop(az, principal=job_by_agent).held
    assert stop(az, principal=house_agent).held
    assert not stop(az, principal=Principal(
        "human:u1", "human", "owner", ("human:owner",), via=("job:j3",))).held


def test_deny_beats_hold(tmp_path):
    doc = {**DEFAULT_POLICY, "mode": "enforce",
           "rules": [{"id": "no-shell", "who": "token:agent-1", "deny": "shell.exec",
                      "on": "*"}]}
    az = authorizer(tmp_path, doc)
    d = stop(az)
    assert d.denied and not d.held and d.rule == "no-shell"
    ro = Principal("token:ro", "token", "readonly")
    assert check(az, "customcap.add", principal=ro).denied


def test_an_allow_rule_never_lifts_a_hard_stop(tmp_path):
    doc = {**DEFAULT_POLICY, "rules": [
        {"id": "agents-anything", "who": "role:agent", "allow": "*", "on": "*"},
        {"id": "ci-shell-nas", "who": "token:ci", "allow": "shell.exec", "on": "nas"}]}
    az = authorizer(tmp_path, doc)
    assert stop(az).held and stop(az, principal=CI).held
    assert not check(az, "shell.exec", principal=CI, args={"argv": ["uptime"]}).held


def test_operator_hold_rules_are_ignored(tmp_path):
    doc = {**DEFAULT_POLICY, "rules": [{"id": "hold-shell-nas", "who": "role:agent",
                                        "hold": "shell.*", "on": "nas"}]}
    for mode in ("audit", "enforce"):
        (tmp_path / mode).mkdir()
        az = authorizer(tmp_path / mode, {**doc, "mode": mode})
        d = check(az, "shell.exec", "nas", args={"argv": ["uptime"]})
        assert not d.held and "hold rules are retired" in d.reason, mode
        assert stop(az).hold_kind == "default"                     # the hard stop still holds
    with pytest.raises(PolicyError):
        Policy({**DEFAULT_POLICY, "rules": [{"who": "*", "hold": "*", "allow": "*"}]})


def test_no_queue_means_hold_rules_refuse(tmp_path):
    (tmp_path / "policy.json").write_text(json.dumps(
        {**DEFAULT_POLICY, "rules": [{"id": "h", "who": "role:agent", "hold": "*", "on": "*"}]}))
    az = Authorizer(PolicyStore(str(tmp_path / "policy.json")), None)
    assert check(az, "shell.exec").denied


def test_failing_open_on_a_policy_crash_still_holds(tmp_path, monkeypatch):
    az = authorizer(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("bug")
    monkeypatch.setattr(az.store.current(), "evaluate", boom)
    assert stop(az, principal=OPERATOR_TOKEN).held
    assert stop(az, principal=OWNER).decision == "error_allow"


def test_secrets_are_held_and_guardrails_refused(tmp_path):
    az = authorizer(tmp_path)
    assert check(az, "secret.get", principal=OPERATOR_TOKEN, local=True,
                 args={"name": "nas-admin"}).held
    for cap in ("settings.set", "settings.reset"):               # ordinary settings just go
        assert not check(az, cap, principal=OPERATOR_TOKEN, local=True,
                         args={"key": "hub.motd"}).held, cap
    for cap in ("policy.set", "token.admin"):                   # no ratchet: a person's alone
        d = check(az, cap, principal=OPERATOR_TOKEN, local=True)
        assert d.denied and d.rule == "hardstop:guardrail", cap
    d = check(az, "settings.set", principal=OPERATOR_TOKEN, local=True,
              args={"key": "core.approvals.wait_secs", "value": 0})
    assert d.denied and d.rule == "hardstop:guardrail"


def test_config_from_settings_store_and_env(tmp_path):
    from rook.hub.settings_store import SettingsStore
    store = SettingsStore(tmp_path / "settings.db")
    store.set("core.approvals.wait_secs", "hub", "", value=20, actor="human:u1")
    c = A.load_config(store, {})
    assert c.wait_secs == 20 and not c.advanced
    assert A.load_config(store, {"ROOK_APPROVALS_WAIT_SECS": "5"}).wait_secs == 5
    retired = {"enabled", "hold", "approval_secs", "spread_max", "spread_window_secs",
               "spread_grant_secs", "contact", "contact_worker", "voice_max_tier",
               "notify_every_secs", "hub_host_workers"}
    assert not retired & set(A._settings())
    assert not retired & {f.name for f in A.Config.__dataclass_fields__.values()}


# -- the band client waits for a decision ---------------------------------------------

def band(tmp_path, doc=None, config=None, record=None, replies=None):
    client = BandClient("test-band")
    sent = []

    async def send(data, route=0):
        msg = json.loads(data)
        sent.append(msg)
        if msg.get("cap"):
            result = (replies or {}).get(msg["cap"], "done")
            result = result(msg) if callable(result) else result
            asyncio.get_running_loop().call_soon(client._handle_reply, {
                "id": msg["id"], "from": msg.get("target"), "ok": True, "result": result})
    client.transport.send = send
    for name in ("nas", "build-box", "w3", "w4", "w5", "w6", "w7"):
        client.workers[name] = entry(name)
    client.authz = authorizer(tmp_path, doc, config, record)
    return client, sent


async def as_agent(coro, principal=AGENT):
    tok = current_principal.set(principal)
    try:
        return await coro
    finally:
        current_principal.reset(tok)


async def pending_hold(queue, n=1, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        rows = queue.pending()
        if len(rows) >= n:
            return rows
        await asyncio.sleep(0.02)
    raise AssertionError("no pending hold")


@pytest.mark.asyncio
async def test_approved_within_the_wait_proceeds_and_is_sent(tmp_path):
    rows = []
    client, sent = band(tmp_path, config=cfg(wait_secs=5), record=lambda **kw: rows.append(kw))
    call = asyncio.create_task(as_agent(client.call("shell.exec", RM, target="nas", timeout=2)))
    # The dashboard (another process) opens the same file and approves.
    other = A.ApprovalQueue(str(tmp_path / "approvals.db"))
    hold = (await pending_hold(other))[0]
    assert hold["principal"] == "token:agent-1" and hold["cap"] == "shell.exec"
    assert hold["target_name"] == "nas" and hold["args"] == RM
    assert hold["reason"] == STOP_REASON and hold["principal_label"] == "claude code"
    assert not sent
    other.decide(hold["id"], True, "human:u1")
    reply = await call
    assert reply["ok"] and sent[-1]["cap"] == "shell.exec"
    assert client.authz.approvals.queue.get(hold["id"])["state"] == "consumed"
    assert any(r["cap"] == "approval.hold" and r["identity"] == "token:agent-1" for r in rows)


@pytest.mark.asyncio
async def test_denied_is_a_denial_and_nothing_is_sent(tmp_path):
    client, sent = band(tmp_path, config=cfg(wait_secs=5))
    call = asyncio.create_task(as_agent(client.call("shell.exec", RM, target="nas", timeout=2)))
    hold = (await pending_hold(client.authz.approvals.queue))[0]
    client.authz.approvals.decide(hold["id"], False, "human:u1")
    reply = await call
    assert not reply["ok"] and reply["denied"]["hold"] == hold["id"]
    assert reply["denied"]["by"] == "human:u1" and "human:u1 denied shell.exec" in reply["error"]
    assert not sent


@pytest.mark.asyncio
async def test_timeout_returns_held_with_the_id_and_nothing_is_sent(tmp_path):
    rows = []
    client, sent = band(tmp_path, record=lambda **kw: rows.append(kw))
    t0 = time.monotonic()
    reply = await as_agent(client.call("shell.exec", RM, target="nas", timeout=0.1))
    assert time.monotonic() - t0 >= 0.5             # the wait comes before the call's timeout
    assert reply["ok"] is False and reply["held"] is True and reply["hold"].startswith("h")
    assert reply["hold"] in reply["error"] and "Approvals page" in reply["error"]
    assert "repeat the same call" in reply["error"] and "does not expire" in reply["error"]
    assert "minutes" not in reply["error"]
    assert not sent
    assert [r["cap"] for r in rows].count("approval.hold") == 1
    # A retry before anyone decides joins the same hold instead of adding another.
    again = await as_agent(client.call("shell.exec", RM, target="nas", timeout=0.1))
    assert again["hold"] == reply["hold"] and len(client.authz.approvals.queue.pending()) == 1


@pytest.mark.asyncio
async def test_an_approval_covers_one_identical_retry(tmp_path):
    client, sent = band(tmp_path)
    first = await as_agent(client.call("shell.exec", RM, target="nas", timeout=1))
    client.authz.approvals.decide(first["hold"], True, "human:u1")
    # Different args, another worker or another agent are not covered.
    other_args = await as_agent(client.call("shell.exec", RM2, target="nas", timeout=1))
    assert other_args["held"] and other_args["hold"] != first["hold"]
    other_box = await as_agent(client.call("shell.exec", RM, target="build-box", timeout=1))
    assert other_box["held"]
    other_agent = await as_agent(client.call("shell.exec", RM, target="nas", timeout=1), CI)
    assert other_agent["held"]
    assert not sent
    ok = await as_agent(client.call("shell.exec", RM, target="nas", timeout=1))
    assert ok["ok"] and len(sent) == 1
    again = await as_agent(client.call("shell.exec", RM, target="nas", timeout=1))
    assert again["held"] and len(sent) == 1


@pytest.mark.asyncio
async def test_nothing_expires_approvals_or_pending_holds(tmp_path):
    now = [1000.0]
    client, sent = band(tmp_path)
    client.authz.approvals = A.Approvals(str(tmp_path / "approvals.db"), FAST,
                                         clock=lambda: now[0])
    ap = client.authz.approvals
    first = await as_agent(client.call("shell.exec", RM, target="nas", timeout=1))
    ap.decide(first["hold"], True, "human:u1")
    assert ap.queue.get(first["hold"])["valid_until"] is None
    now[0] += 365 * 86400                                   # a year later
    ap.queue.sweep(force=True)
    assert ap.queue.get(first["hold"])["state"] == "approved"
    late = await as_agent(client.call("shell.exec", RM, target="nas", timeout=1))
    assert late["ok"] and len(sent) == 1                    # the approval still covers it
    # A pending hold nobody answered waits too, and the person can still decide it.
    waiting = await as_agent(client.call("shell.exec", RM2, target="nas", timeout=1))
    now[0] += 365 * 86400
    ap.queue.sweep(force=True)
    assert ap.queue.get(waiting["hold"])["state"] == "pending"
    assert ap.queue.count_pending() == 1
    assert waiting["hold"] in [i["id"] for i in ap.queue.pending()]
    ap.decide(waiting["hold"], True, "human:u1")
    assert (await as_agent(client.call("shell.exec", RM2, target="nas", timeout=1)))["ok"]


def test_rows_from_before_no_expiry_keep_their_old_deadline(tmp_path):
    """Removing expiry never revives an approval or an ask the old code had
    already timed out, even one the sweep had not marked yet; a denial stays a
    denial; an old approval still inside its window works once."""
    import sqlite3
    now = [10_000.0]
    q = A.ApprovalQueue(str(tmp_path / "a.db"), clock=lambda: now[0])
    q.count_pending()                                        # creates the file
    cols = ("id, state, kind, principal, cap, tier, target_id, args_hash, created, expires, "
            "decided_by, decided_at, valid_until")
    rows = [("h_stale", "approved", "a1", 9_000.0, 9_600.0),      # window passed, unswept
            ("h_fresh", "approved", "a2", 9_900.0, 10_500.0),     # still inside its window
            ("h_denied", "denied", "a3", 9_900.0, None),
            ("h_oldask", "pending", "a4", 9_000.0, None)]         # pending, expired at 9600
    with sqlite3.connect(q.path) as db:
        for hid, state, digest, created, until in rows:
            db.execute(f"INSERT INTO holds({cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (hid, state, "default", "token:agent-1", "shell.exec", "exec", "nas",
                        digest, created, created + 600, "human:u1" if state != "pending"
                        else None, created if state != "pending" else None, until))
    assert q.consume_match("token:agent-1", "shell.exec", "nas", "a1") is None
    q.sweep(force=True)
    assert q.get("h_stale")["state"] == "expired"
    assert q.consume_match("token:agent-1", "shell.exec", "nas", "a3") is None
    assert q.get("h_denied")["state"] == "denied"
    assert q.consume_match("token:agent-1", "shell.exec", "nas", "a2")["id"] == "h_fresh"
    assert q.get("h_oldask")["state"] == "expired" and q.count_pending() == 0
    with pytest.raises(A.HoldError):
        q.decide("h_oldask", True, "human:u1")
    again, created = q.create(principal="token:agent-1", cap="shell.exec", tier="exec",
                              args_hash="a4", target_id="nas")
    assert created and again["id"] != "h_oldask" and again["expires"] == 0
    now[0] += 365 * 86400                                    # the new ask never expires
    assert q.get(again["id"])["state"] == "pending" and q.count_pending() == 1


def test_decide_takes_only_the_call_scope(tmp_path):
    ap = A.Approvals(str(tmp_path / "a.db"), FAST)
    az = Authorizer(PolicyStore(None), None, approvals=ap)
    h, _ = ap.queue.create(principal="token:agent-1", cap="shell.exec", tier="exec",
                           args_hash=A.args_hash(RM), target_id="nas", args=RM,
                           reason=STOP_REASON)
    with pytest.raises(A.HoldError):
        ap.decide(h["id"], True, "human:u1", scope="spread")
    assert ap.queue.get(h["id"])["state"] == "pending"
    assert not hasattr(ap.queue, "active_grant") and not hasattr(ap, "spread")
    assert stop(az).held


@pytest.mark.asyncio
async def test_holds_show_placeholders_never_secret_values(tmp_path):
    client, _sent = band(tmp_path)
    given = {"cmd": "rm -rf /srv/data && login {{secret:nas-admin}}", "password": "hunter2",
             "note": "x" * 5000}
    tok = A.shown_args.set(given)
    try:
        sent_args = {**given, "cmd": "rm -rf /srv/data && login s3cr3t-value"}
        reply = await as_agent(client.call("shell.exec", sent_args, target="nas", timeout=1))
    finally:
        A.shown_args.reset(tok)
    hold = client.authz.approvals.queue.get(reply["hold"])
    shown = json.dumps(hold["args"])
    assert "{{secret:nas-admin}}" in shown and "s3cr3t-value" not in shown
    assert "hunter2" not in shown and len(shown) < 1000


# -- both layouts see the same holds -----------------------------------------------------

@pytest.mark.asyncio
async def test_two_processes_share_one_queue(tmp_path):
    mcp_side = A.Approvals(str(tmp_path / "approvals.db"), cfg(wait_secs=5))
    dashboard_side = A.Approvals(str(tmp_path / "approvals.db"), cfg(wait_secs=5))
    d = stop(Authorizer(PolicyStore(None), None, approvals=mcp_side))
    waiting = asyncio.create_task(mcp_side.resolve(d, RM, label="claude code"))
    hold = (await pending_hold(dashboard_side.queue))[0]
    assert dashboard_side.queue.count_pending() == 1
    dashboard_side.decide(hold["id"], True, "human:u1", scope="call")
    out = await waiting
    assert out.decision == "allow" and out.rule == f"approval:{hold['id']}"
    assert [h["id"] for h in dashboard_side.queue.recent()] == [hold["id"]]


def test_the_queue_lives_beside_policy_json(tmp_path):
    from rook.hub.authz import build_authorizer
    az = build_authorizer(str(tmp_path), signer=object())
    assert az.approvals.queue.path == str(tmp_path / "approvals.db")
    assert az.store.path == str(tmp_path / "policy.json")
    assert build_authorizer(None, signer=object()).approvals is None


def test_the_band_tui_explains_a_held_reply():
    from rook.cli.band_tui import BandHTTP
    http = BandHTTP("http://hub.example.org", "", "pw")
    out = http.explain({"ok": False, "held": True, "hold": "h1", "error": "Held for approval"})
    assert "Approvals page" in out["hint"] and "h1" in out["hint"]
    out = http.explain({"ok": False, "error": "denied: human:u1 denied x",
                        "denied": {"hold": "h2", "by": "human:u1"}})
    assert out["hint"] == "human:u1 denied this held call"


# -- agents cannot approve ------------------------------------------------------------------

def test_the_only_agent_approval_surface_is_the_relay():
    """Agents decide nothing on their own: the one surface is relaying a
    person's words (approvals.relay / rook_approvals, rook/hub/relay.py),
    which tests/test_relay.py covers. No other cap or tool approves."""
    from rook.core import authz as core_authz
    from rook.hub.authz import _TOOL_CAPS, hub_cap_for_tool
    caps = set(core_authz.builtin_table())
    assert sorted(c for c in caps if "approv" in c or c.startswith("hold")) == \
        ["approvals.list", "approvals.relay"]
    assert core_authz.builtin_tier("approvals.relay") == "write"
    assert not [t for t in _TOOL_CAPS if "approv" in t]
    assert hub_cap_for_tool("rook_approvals", {}) is None
    import rook.band_mcp.server as srv
    import inspect
    src = inspect.getsource(srv)
    assert "def rook_approv" not in src and ".decide(" not in src


@pytest.mark.asyncio
async def test_mcp_tools_list_has_no_approval_tool_but_the_relay(tmp_path):
    from test_permissions_mcp import mcp
    from test_audit_attribution import Session, STATIC
    async with mcp(tmp_path) as env:
        s = await Session(env.http, STATIC).open()
        s.n += 1
        r = await env.http.post("/mcp", headers=s.headers(), json={
            "jsonrpc": "2.0", "id": s.n, "method": "tools/list"})
        body = r.json() if r.headers["content-type"].startswith("application/json") else \
            json.loads(next(l[5:] for l in r.text.splitlines() if l.startswith("data:")))
        names = [t["name"] for t in body["result"]["tools"]]
        assert names and [n for n in names if "approv" in n or "hold" in n] in \
            ([], ["rook_approvals"])


@pytest.mark.asyncio
async def test_agent_hub_tool_call_is_held_through_mcp(tmp_path, monkeypatch):
    """An agent's hard stop through a hub tool (rook_secret get) and rook_call
    (a command that erases a data directory) is held; a hold rule in the
    policy holds nothing."""
    from test_permissions_mcp import mcp, _text
    from test_audit_attribution import Session, STATIC
    monkeypatch.setenv("ROOK_APPROVALS_WAIT_SECS", "0")
    doc = {**DEFAULT_POLICY, "rules": [*DEFAULT_POLICY["rules"],
                                       {"id": "hold-host", "who": "token:*", "hold": "info.host",
                                        "on": "gpu-box"}]}
    async with mcp(tmp_path, doc) as env:
        s = await Session(env.http, STATIC).open()
        res = await s.result("rook_secret", {"action": "get", "name": "nas-admin"})
        assert res.get("isError") and "Held for approval" in res["content"][0]["text"]
        before = len(env.sent)
        held = _text(await s.result("rook_call", {"why": "test", "cap": "shell.exec", "worker": "gpu-box",
                                                  "args": {"cmd": "rm -rf /srv/data"}}))
        assert held["held"] and not held["ok"] and len(env.sent) == before
        assert "hard stop" in held["error"]
        ok = _text(await s.result("rook_call", {"why": "test", "cap": "info.host", "worker": "gpu-box",
                                                "args": {"build": 1}}))
        assert ok["ok"]                                       # the hold rule holds nothing
        q = A.ApprovalQueue(str(tmp_path / "approvals.db"))
        assert {h["cap"] for h in q.pending()} == {"secret.get", "shell.exec"}


# -- the dashboard API ------------------------------------------------------------------

def dashboard(tmp_path, principal, *, weak_sessions=(), vaulted=lambda *v: False):
    from rook.remote.approvals_web import ApprovalsWeb
    az = authorizer(tmp_path)

    async def credential_is_vaulted(*values):
        return vaulted(*values)

    store = SimpleNamespace(session_weak=lambda tok: tok in set(weak_sessions))
    server = SimpleNamespace(_band=SimpleNamespace(authz=az), _authz=az,
                             _accounts=SimpleNamespace(store=store),
                             credential_is_vaulted=credential_is_vaulted)

    @web.middleware
    async def who(request, handler):
        tok = current_principal.set(principal[0])
        try:
            return await handler(request)
        finally:
            current_principal.reset(tok)

    app = web.Application(middlewares=[who])
    ApprovalsWeb(server).install(app)
    return app, az


@pytest.mark.asyncio
async def test_dashboard_api_lists_approves_and_denies(tmp_path):
    principal = [OWNER]
    app, az = dashboard(tmp_path, principal)
    az.approvals.config_source.fixed = cfg(wait_secs=0)
    async with TestClient(TestServer(app)) as http:
        r1 = await az.resolve(stop(az), RM)
        r2 = await az.resolve(stop(az, "build-box", args=RM2), RM2)
        r = await http.get("/api/approvals?count=1")
        assert (await r.json()) == {"pending": 2}
        r = await http.get("/api/approvals")
        data = await r.json()
        assert [h["id"] for h in data["pending"]] == [r1["hold"], r2["hold"]]
        assert data["pending"][0]["worker"] == "nas"
        assert set(data["settings"]) == {"wait_secs", "relay_dashboard_only"}
        assert "grants" not in data and "hub_host" not in data
        assert "voice_ok" not in data["pending"][0]
        r = await http.post(f"/api/approvals/{r1['hold']}", json={"decision": "approve"})
        assert r.status == 200 and (await r.json())["hold"]["state"] == "approved"
        assert (await r.json())["hold"]["valid_until"] is None        # never runs out
        r = await http.post(f"/api/approvals/{r2['hold']}", json={"decision": "deny"})
        assert (await r.json())["hold"]["decided_by"] == "human:u1"
        r = await http.post(f"/api/approvals/{r2['hold']}", json={"decision": "approve"})
        assert r.status == 409                                   # already decided
        r = await http.post(f"/api/approvals/{r1['hold']}", data="decision=approve",
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status in (409, 415)
        r = await http.post(f"/api/approvals/{r1['hold']}", json={"decision": "approve"},
                            headers={"Origin": "https://evil.example.org"})
        assert r.status == 403
        data = await (await http.get("/api/approvals")).json()
        assert not data["pending"] and {h["state"] for h in data["recent"]} == {"approved",
                                                                               "denied"}


@pytest.mark.asyncio
async def test_dashboard_api_requires_a_person_with_admin(tmp_path):
    principal = [AGENT]
    app, az = dashboard(tmp_path, principal)
    az.approvals.config_source.fixed = cfg(wait_secs=0)
    async with TestClient(TestServer(app)) as http:
        held = await az.resolve(stop(az), RM)
        for p in (AGENT, OPERATOR_TOKEN, MEMBER, system("x"), None):
            principal[0] = p
            assert (await http.get("/api/approvals")).status == 403, p
            r = await http.post(f"/api/approvals/{held['hold']}", json={"decision": "approve"})
            assert r.status == 403, p
        assert az.approvals.queue.get(held["hold"])["state"] == "pending"
        principal[0] = OWNER
        r = await http.post(f"/api/approvals/{held['hold']}", json={"decision": "approve"})
        assert r.status == 200


# -- no bypass through other paths ---------------------------------------------------------

def test_a_job_acting_as_someone_else_keeps_its_agent_owner_in_the_chain(tmp_path):
    from rook.hub.plugins.jobs.identity import resolve_identity
    from rook.hub.plugins.jobs.principals import Found
    owner = {"id": "token:agent-1", "kind": "token", "role": "operator", "groups": [],
             "label": "claude code", "verified": True}
    human = {"id": "human:u1", "kind": "human", "role": "owner", "groups": ["human:owner"],
             "label": "alice", "verified": True}
    directory = SimpleNamespace(lookup=lambda ref: Found(human),
                                status=lambda info: Found(info), tokens=object())
    ident = resolve_identity({"id": "j1", "identity": {"mode": "key", "ref": "human:u1"}},
                             owner, directory=directory)
    assert ident.principal.id == "human:u1"
    assert ident.principal.via == ("job:j1", "token:agent-1")
    assert stop(authorizer(tmp_path), principal=ident.principal).held
    mine = resolve_identity({"id": "j2", "identity": {"mode": "creator"}}, human,
                            directory=directory)
    assert mine.principal.via == ("job:j2",)
    assert not stop(authorizer(tmp_path), principal=mine.principal).held


@pytest.mark.asyncio
async def test_hub_plugin_tools_cannot_carry_an_agent_past_a_hold(tmp_path):
    from rook.hub.mcp_tools import _hold_gate
    az = authorizer(tmp_path, config=cfg(wait_secs=0))
    node = SimpleNamespace(worker_id="rook", entry=lambda: {"name": "rook", "roles": ["is_hub"]},
                           client=SimpleNamespace(authz=az))
    with pytest.raises(PermissionError, match="Held for approval"):
        await as_agent(_hold_gate(node, "memory.forget", {"id": "m1"}))   # erases for good
    await as_agent(_hold_gate(node, "memory.recall", {}))              # not held
    await as_agent(_hold_gate(node, "memory.forget", {"id": "m1"}), OWNER)
    tok = A.gated_caps.set(frozenset({"memory.forget"}))           # the tool's own cap, held already
    try:
        await as_agent(_hold_gate(node, "memory.forget", {"id": "m1"}))
    finally:
        A.gated_caps.reset(tok)


@pytest.mark.asyncio
async def test_integration_commands_are_held(tmp_path):
    from test_integrations import CHAT, TG_TOKEN, make_node, wire
    from rook.hub.integrations import Inbound
    from rook.hub.plugins.telegram import Telegram
    rule = {"id": "tg-admin", "who": "integration:telegram", "allow": "tier:admin", "on": "*"}
    client, sent = band(tmp_path, {**DEFAULT_POLICY, "rules": [rule]})
    node = make_node(tmp_path)
    node.client = client
    p = wire(Telegram(), tmp_path, TG_TOKEN, chat_id=CHAT, commands=["call"],
             allowed_caps=["worker.*", "customcap.*", "shell.*"])
    p.bind_host(node)
    m = Inbound(chat_id=CHAT, user_id="42", username="a", text="")
    out = await p.run_command('call nas shell.exec {"argv": ["rm", "-rf", "/srv/data"]}', m)
    assert "Held for approval" in out and not sent             # a hard stop: held
    out = await p.run_command('call nas worker.restart {}', m)
    assert "Held for approval" not in out and sent[-1]["cap"] == "worker.restart"  # admin goes


# -- an agent-usable sign-in can't approve (permissions 3.11, bypass 1) --------------------

def _vault(tmp_path):
    from rook.band_mcp import secret_mask
    from rook.band_mcp.vault import Vault
    v = Vault(str(tmp_path / "vault.db"), key_path=str(tmp_path / "vault.key"))
    masker = secret_mask.install(v)
    return v, masker


def test_secret_masker_contains_value_and_encoded_forms(tmp_path):
    import base64
    from rook.band_mcp import secret_mask
    v, masker = _vault(tmp_path)
    try:
        v.set("dash-pass", "correct horse battery", "the dashboard password", "human:u1")
        assert masker.contains("correct horse battery")
        assert masker.contains(base64.b64encode(b"correct horse battery").decode())
        assert not masker.contains("correct horse batter")
        assert not masker.contains("")
    finally:
        secret_mask.install(None)


@pytest.mark.asyncio
async def test_is_secret_route_returns_only_a_boolean(tmp_path):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from rook.band_mcp import secret_mask
    from rook.band_mcp.mask_web import is_secret_route
    v, _ = _vault(tmp_path)
    try:
        v.set("dash-pass", "correct horse battery", "", "human:u1")
        app = Starlette(routes=[is_secret_route("tok")])
        c = TestClient(app)
        hdr = {"Authorization": "Bearer tok"}
        assert c.post("/internal/is-secret", json={"value": "nope"}, headers=hdr).json() == {"secret": False}
        r = c.post("/internal/is-secret", json={"value": "correct horse battery"}, headers=hdr)
        assert r.json() == {"secret": True}                 # only a boolean: no names, no values
        assert c.post("/internal/is-secret", json={"value": "x"}).status_code == 401
    finally:
        secret_mask.install(None)


@pytest.mark.asyncio
async def test_basic_auth_with_a_vaulted_password_cannot_approve(tmp_path):
    import base64
    principal = [OWNER]
    app, az = dashboard(tmp_path, principal, vaulted=lambda *v: "s3cret-pass" in v)
    az.approvals.config_source.fixed = cfg(wait_secs=0)
    async with TestClient(TestServer(app)) as http:
        held = await az.resolve(stop(az), RM)
        good = {"Authorization": "Basic " + base64.b64encode(b"op:other-pass").decode()}
        bad = {"Authorization": "Basic " + base64.b64encode(b"op:s3cret-pass").decode()}
        d = await (await http.get("/api/approvals", headers=bad)).json()
        assert d["can_decide"] is False and d["weak_credential"] and d["weak_message"]
        r = await http.post(f"/api/approvals/{held['hold']}", json={"decision": "approve"}, headers=bad)
        assert r.status == 403 and (await r.json())["weak_credential"]
        assert az.approvals.queue.get(held["hold"])["state"] == "pending"
        r = await http.post(f"/api/approvals/{held['hold']}", json={"decision": "approve"}, headers=good)
        assert r.status == 200
        assert (await (await http.get("/api/approvals", headers=good)).json())["can_decide"] is True


@pytest.mark.asyncio
async def test_a_flagged_session_cookie_cannot_approve(tmp_path):
    principal = [OWNER]
    app, az = dashboard(tmp_path, principal, weak_sessions=["weaktok"])
    az.approvals.config_source.fixed = cfg(wait_secs=0)
    async with TestClient(TestServer(app)) as http:
        held = await az.resolve(stop(az), RM)
        http.session.cookie_jar.update_cookies({"rook_account": "weaktok"})
        r = await http.post(f"/api/approvals/{held['hold']}", json={"decision": "approve"})
        assert r.status == 403 and az.approvals.queue.get(held["hold"])["state"] == "pending"


@pytest.mark.asyncio
async def test_approvals_fail_closed_when_the_vault_is_unreachable(tmp_path):
    import base64
    principal = [OWNER]
    app, az = dashboard(tmp_path, principal, vaulted=lambda *v: None)  # None = can't tell
    az.approvals.config_source.fixed = cfg(wait_secs=0)
    async with TestClient(TestServer(app)) as http:
        held = await az.resolve(stop(az), RM)
        hdr = {"Authorization": "Basic " + base64.b64encode(b"op:any").decode()}
        r = await http.post(f"/api/approvals/{held['hold']}", json={"decision": "approve"}, headers=hdr)
        assert r.status == 403


def test_weak_session_flag_round_trips(tmp_path, monkeypatch):
    from rook.remote.accounts import AccountStore
    from rook.remote.enrollment import EnrollmentStore
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enroll.db"))
    store = AccountStore(EnrollmentStore())
    tok = store.dashboard_session()
    store.mark_weak_session(tok)
    assert store.session_weak(tok)
    assert not store.session_weak(store.dashboard_session())
    store.dashboard_logout(tok)
    assert not store.session_weak(tok)


# -- retired: hub-host holds, spread limit, operator contact -------------------------------

@pytest.mark.asyncio
async def test_no_hub_host_or_spread_hold_and_no_operator_contact(tmp_path):
    client, sent = band(tmp_path)
    client._local = SimpleNamespace(worker_id="hub1", facts=SimpleNamespace(hw={"host_id": "h"}))
    client.workers["nas"] = entry("nas", facts={"host_id": "h"})      # on the hub's machine
    client.authz.attach_client(client)
    ok = await as_agent(client.call("shell.exec", {"cmd": "echo x"}, target="nas", timeout=0.5))
    assert ok["ok"]
    for name in ("build-box", "w3", "w4", "w5", "w6", "w7"):          # many machines quickly
        r = await as_agent(client.call("shell.exec", {"cmd": "id"}, target=name, timeout=0.5))
        assert r["ok"], name
    ap = client.authz.approvals
    assert not hasattr(ap, "contact") and not hasattr(ap, "hub_host_workers")
    held = await as_agent(client.call("shell.exec", RM, target="nas", timeout=0.5))
    assert held["held"]
    assert not [m for m in sent if m.get("cap") in ("notify.send", "notify.post", "voice.speak")]
    import importlib.util
    for mod in ("rook.hub.plugins.security.contact", "rook.hub.plugins.security.holds"):
        assert importlib.util.find_spec(mod) is None, mod


def test_the_voice_verdict_used_for_relayed_words_lives_in_the_relay():
    from rook.hub.relay import voice_verdict
    for t in ("yes", "Yes.", "approve", "approved!", "allow", "do it", "go ahead", "OK", "okay"):
        assert voice_verdict(t) == "approve", t
    for t in ("no", "No!", "deny", "don't", "do not", "stop", "cancel", "nope"):
        assert voice_verdict(t) == "deny", t
    for t in ("yes but no", "ok wait", "not now", "maybe", "", "know what, yes no", "wait",
              "yes, don't"):
        assert voice_verdict(t) == "unclear", t


# -- an agent's own console/chat is never held (noise, item 3) ----------------------------

@pytest.mark.asyncio
async def test_tidying_up_your_own_resource_is_never_held(tmp_path):
    client, sent = band(tmp_path)
    assert (await as_agent(client.call("shell.exec", RM, target="nas", timeout=0.5)))["held"]
    # With own_resource set (the server sets it for the caller's own console), it proceeds.
    tok = A.own_resource.set(True)
    try:
        ok = await as_agent(client.call("shell.exec", RM, target="nas", timeout=0.5))
    finally:
        A.own_resource.reset(tok)
    assert ok["ok"]


def test_own_resource_check_in_the_server(tmp_path, monkeypatch):
    # The server marks own_resource for the caller's own console/chat only.
    from types import SimpleNamespace as NS
    import rook.band_mcp.server as srv
    import inspect
    src = inspect.getsource(srv)
    assert "_own_resource(name, arguments)" in src
    assert 'name in ("rook_console_signal", "rook_console_close")' in src
    assert 'name == "rook_chat_delete"' in src


def test_own_chat_room_means_started_by_the_caller_with_no_people(tmp_path, monkeypatch):
    # Deleting a room shared with a person removes their messages: held.
    from rook.band_mcp.chat_rooms import ChatStore
    store = ChatStore(str(tmp_path / "chat.db"))
    mine = store.start("tidy", "agent:alpha", [])["room"]
    shared = store.start("ops", "agent:alpha", ["user:op"])["room"]
    theirs = store.start("other", "agent:beta", ["agent:alpha"])["room"]

    def own(rid, who):
        parts = list((store.room(rid) or {}).get("participants") or [])
        return bool(parts and parts[0] == who and not any(
            str(p).startswith(("user:", "human:")) for p in parts))
    assert own(mine, "agent:alpha")
    assert not own(shared, "agent:alpha")
    assert not own(theirs, "agent:alpha")
    import inspect
    import rook.band_mcp.server as srv
    assert 'parts[0] == who' in inspect.getsource(srv)
