"""Approval holds and the spread limit (docs/design/permissions.md 3.11):
which calls are held, waiting for a decision in the band client, approvals
covering one identical retry, the spread limit and grants, the dashboard API,
both hub layouts sharing one queue, contacting the operator, and approving
by voice on a phone."""

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
from rook.hub.approval_contact import Contact, message, question, voice_eligible, voice_verdict
from rook.hub.authz import Authorizer, current_principal, principal_for_user, system
from rook.hub.policy import DEFAULT_POLICY, Policy, PolicyError, PolicyStore, Principal, Target

pytestmark = pytest.mark.approvals

AGENT = Principal("token:agent-1", "token", "agent", label="claude code")
OPERATOR_TOKEN = Principal("token:ops", "token", "operator", label="ops")
CI = Principal("token:ci", "token", "agent", label="ci")
OWNER = principal_for_user({"id": "u1", "username": "alice"}, True)
MEMBER = principal_for_user({"id": "u2", "username": "bob"}, False)
TELEGRAM = Principal("integration:telegram", "integration", "integration")

FAST = A.Config(wait_secs=0.6, contact=())


def cfg(**kw):
    return A.Config(**{**FAST.__dict__, **kw})


def authorizer(tmp_path, doc=None, config=None, record=None, **kw):
    path = tmp_path / "policy.json"
    if doc is not None:
        path.write_text(json.dumps(doc))
    ap = A.Approvals(str(tmp_path / "approvals.db"), config or FAST, record=record, **kw)
    return Authorizer(PolicyStore(str(path)), None, record=record, approvals=ap)


def entry(name, **kw):
    return {"worker_id": name, "name": name, "caps": ["shell.exec", "worker.update"],
            "last_seen": time.time(), **kw}


def check(authz, cap, target="nas", principal=AGENT, local=False):
    e = {"name": "rook", "roles": ["is_hub"]} if local else entry(target)
    return authz.check(cap, "rook" if local else target, e, principal=principal, local=local)


# -- which calls are held ------------------------------------------------------------

def test_default_holds_agent_admin_and_destructive(tmp_path):
    az = authorizer(tmp_path)
    d = check(az, "worker.update")
    assert d.held and d.hold_kind == "default" and d.hold_reason == "tag destructive"
    assert check(az, "customcap.add").hold_reason == "tier admin"
    assert check(az, "proc.close").held                      # exec, tagged destructive
    assert not check(az, "shell.exec").held                  # exec, not held by default
    assert not check(az, "info.ping").held
    for p in (OPERATOR_TOKEN, TELEGRAM, Principal("unverified", "unverified", verified=False),
              Principal("token:static", "token", "operator")):
        assert check(az, "worker.update", principal=p).held, p.id


def test_people_and_hub_code_are_never_held(tmp_path):
    doc = {**DEFAULT_POLICY, "rules": [*DEFAULT_POLICY["rules"],
                                       {"id": "hold-members", "who": "human:member",
                                        "hold": "*", "on": "*"}]}
    az = authorizer(tmp_path, doc)
    for p in (OWNER, MEMBER, system("ota"), Principal("human:dashboard", "human", "owner",
                                                       ("human:owner",))):
        d = check(az, "worker.update", principal=p)
        assert not d.held and not d.denied, (p.id, d)
    assert any("never held" in m for m in Policy(doc).lint())


def test_an_agent_in_the_chain_is_held_even_behind_a_person(tmp_path):
    az = authorizer(tmp_path)
    job_by_agent = Principal("human:u1", "human", "owner", ("human:owner",),
                             via=("job:j1", "token:agent-1"))
    house_agent = Principal("human:u1", "human", "owner", ("human:owner",),
                            via=("agent:home", "job:j2"))
    assert check(az, "worker.update", principal=job_by_agent).held
    assert check(az, "worker.update", principal=house_agent).held
    assert not check(az, "worker.update", principal=Principal(
        "human:u1", "human", "owner", ("human:owner",), via=("job:j3",))).held


def test_deny_beats_hold(tmp_path):
    doc = {**DEFAULT_POLICY, "mode": "enforce",
           "rules": [{"id": "no-updates", "who": "token:agent-1", "deny": "worker.update",
                      "on": "*"}]}
    az = authorizer(tmp_path, doc)
    d = check(az, "worker.update")
    assert d.denied and not d.held and d.rule == "no-updates"
    ro = Principal("token:ro", "token", "readonly")
    assert check(az, "customcap.add", principal=ro).denied


def test_operator_allow_exception_beats_the_default_hold(tmp_path):
    doc = {**DEFAULT_POLICY, "rules": [
        {"id": "ci-updates-nas", "who": "token:ci", "allow": "worker.update", "on": "nas"},
        {"id": "agents-anything", "who": "role:agent", "allow": "*", "on": "*"}]}
    az = authorizer(tmp_path, doc)
    assert not check(az, "worker.update", "nas", CI).held           # the exception
    assert check(az, "worker.update", "build-box", CI).held         # other machines
    # A broad allow (cap "*") is less specific than tier:admin / tag:destructive.
    assert check(az, "worker.update", "nas", AGENT).held
    # Same specificity as the default (tier selector, any target): the explicit rule wins.
    doc2 = {**DEFAULT_POLICY, "rules": [{"id": "agents-admin", "who": "role:agent",
                                          "allow": "tier:admin", "on": "*"}]}
    (tmp_path / "b").mkdir()
    az2 = authorizer(tmp_path / "b", doc2)
    assert not check(az2, "customcap.add").held
    assert check(az2, "worker.update").held                         # tag:destructive (2) > tier (1)


def test_explicit_hold_rule(tmp_path):
    doc = {**DEFAULT_POLICY, "rules": [{"id": "hold-shell-nas", "who": "role:agent",
                                        "hold": "shell.*", "on": "nas"}]}
    az = authorizer(tmp_path, doc)
    d = check(az, "shell.exec", "nas")
    assert d.held and d.hold_kind == "rule" and d.rule == "hold-shell-nas"
    assert not check(az, "shell.exec", "build-box").held
    with pytest.raises(PolicyError):
        Policy({**DEFAULT_POLICY, "rules": [{"who": "*", "hold": "*", "allow": "*"}]})
    # Enforce mode: a hold still holds (it denies nothing).
    az2 = authorizer(tmp_path, {**doc, "mode": "enforce"})
    assert check(az2, "shell.exec", "nas").held


def test_approvals_disabled_is_the_old_behaviour(tmp_path):
    doc = {**DEFAULT_POLICY, "rules": [{"id": "hold-shell", "who": "role:agent",
                                        "hold": "shell.*", "on": "*"}]}
    az = authorizer(tmp_path, doc, config=cfg(enabled=False))
    assert check(az, "worker.update").decision == "would_deny"      # role:agent admin: audit
    assert check(az, "shell.exec").decision == "allow"


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
    assert check(az, "customcap.add", principal=OPERATOR_TOKEN).held
    assert check(az, "customcap.add", principal=OWNER).decision == "error_allow"


def test_settings_and_policy_changes_by_agents_are_held(tmp_path):
    az = authorizer(tmp_path)
    for cap in ("settings.set", "settings.reset", "policy.set", "secret.get", "token.admin"):
        d = check(az, cap, principal=OPERATOR_TOKEN, local=True)
        assert d.held, cap
    assert not check(az, "settings.set", principal=OWNER, local=True).held


def test_config_from_settings_store_and_env(tmp_path):
    from rook.hub.settings_store import SettingsStore
    store = SettingsStore(tmp_path / "settings.db")
    store.set("core.approvals.spread_max", "hub", "", value=2, actor="human:u1")
    store.set("core.approvals.contact", "hub", "", value=["ask", "telegram", "bogus"],
              actor="human:u1")
    c = A.load_config(store, {})
    assert c.spread_max == 2 and c.contact == ("ask", "telegram") and c.enabled
    c = A.load_config(store, {"ROOK_APPROVALS": "0", "ROOK_APPROVALS_CONTACT": "off"})
    assert not c.enabled and c.contact == ()


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
    call = asyncio.create_task(as_agent(client.call("worker.update", {"build": 9},
                                                    target="nas", timeout=2)))
    # The dashboard (another process) opens the same file and approves.
    other = A.ApprovalQueue(str(tmp_path / "approvals.db"))
    hold = (await pending_hold(other))[0]
    assert hold["principal"] == "token:agent-1" and hold["cap"] == "worker.update"
    assert hold["target_name"] == "nas" and hold["args"] == {"build": 9}
    assert hold["reason"] == "tag destructive" and hold["principal_label"] == "claude code"
    assert not sent
    other.decide(hold["id"], True, "human:u1")
    reply = await call
    assert reply["ok"] and sent[-1]["cap"] == "worker.update"
    assert client.authz.approvals.queue.get(hold["id"])["state"] == "consumed"
    assert any(r["cap"] == "approval.hold" and r["identity"] == "token:agent-1" for r in rows)


@pytest.mark.asyncio
async def test_denied_is_a_denial_and_nothing_is_sent(tmp_path):
    client, sent = band(tmp_path, config=cfg(wait_secs=5))
    call = asyncio.create_task(as_agent(client.call("worker.update", {}, target="nas", timeout=2)))
    hold = (await pending_hold(client.authz.approvals.queue))[0]
    client.authz.approvals.decide(hold["id"], False, "human:u1")
    reply = await call
    assert not reply["ok"] and reply["denied"]["hold"] == hold["id"]
    assert reply["denied"]["by"] == "human:u1" and "human:u1 denied worker.update" in reply["error"]
    assert not sent


@pytest.mark.asyncio
async def test_timeout_returns_held_with_the_id_and_nothing_is_sent(tmp_path):
    rows = []
    client, sent = band(tmp_path, record=lambda **kw: rows.append(kw))
    t0 = time.monotonic()
    reply = await as_agent(client.call("worker.update", {}, target="nas", timeout=0.1))
    assert time.monotonic() - t0 >= 0.5             # the wait comes before the call's timeout
    assert reply["ok"] is False and reply["held"] is True and reply["hold"].startswith("h")
    assert reply["hold"] in reply["error"] and "Approvals page" in reply["error"]
    assert "repeat the same call" in reply["error"] and "10 minutes" in reply["error"]
    assert not sent
    assert [r["cap"] for r in rows].count("approval.hold") == 1
    # A retry before anyone decides joins the same hold instead of adding another.
    again = await as_agent(client.call("worker.update", {}, target="nas", timeout=0.1))
    assert again["hold"] == reply["hold"] and len(client.authz.approvals.queue.pending()) == 1


@pytest.mark.asyncio
async def test_an_approval_covers_one_identical_retry(tmp_path):
    client, sent = band(tmp_path)
    first = await as_agent(client.call("worker.update", {"build": 9}, target="nas", timeout=1))
    client.authz.approvals.decide(first["hold"], True, "human:u1")
    # Different args, another worker or another agent are not covered.
    other_args = await as_agent(client.call("worker.update", {"build": 10}, target="nas", timeout=1))
    assert other_args["held"] and other_args["hold"] != first["hold"]
    other_box = await as_agent(client.call("worker.update", {"build": 9}, target="build-box",
                                           timeout=1))
    assert other_box["held"]
    other_agent = await as_agent(client.call("worker.update", {"build": 9}, target="nas",
                                             timeout=1), CI)
    assert other_agent["held"]
    assert not sent
    ok = await as_agent(client.call("worker.update", {"build": 9}, target="nas", timeout=1))
    assert ok["ok"] and len(sent) == 1
    again = await as_agent(client.call("worker.update", {"build": 9}, target="nas", timeout=1))
    assert again["held"] and len(sent) == 1


@pytest.mark.asyncio
async def test_approvals_and_pending_holds_expire(tmp_path):
    now = [1000.0]
    client, sent = band(tmp_path)
    client.authz.approvals = A.Approvals(str(tmp_path / "approvals.db"), FAST,
                                         clock=lambda: now[0])
    first = await as_agent(client.call("worker.update", {}, target="nas", timeout=1))
    client.authz.approvals.decide(first["hold"], True, "human:u1")
    now[0] += 601
    late = await as_agent(client.call("worker.update", {}, target="nas", timeout=1))
    assert late["held"] and not sent
    assert client.authz.approvals.queue.get(first["hold"])["state"] in ("approved", "expired")
    now[0] += 601
    client.authz.approvals.queue.sweep(force=True)
    assert client.authz.approvals.queue.get(late["hold"])["state"] == "expired"
    assert client.authz.approvals.queue.get(first["hold"])["state"] == "expired"
    with pytest.raises(A.HoldError):
        client.authz.approvals.decide(late["hold"], True, "human:u1")


@pytest.mark.asyncio
async def test_holds_show_placeholders_never_secret_values(tmp_path):
    client, _sent = band(tmp_path)
    given = {"cmd": "login {{secret:nas-admin}}", "password": "hunter2",
             "note": "x" * 5000}
    tok = A.shown_args.set(given)
    try:
        sent_args = {**given, "cmd": "login s3cr3t-value"}
        reply = await as_agent(client.call("worker.update", sent_args, target="nas", timeout=1))
    finally:
        A.shown_args.reset(tok)
    hold = client.authz.approvals.queue.get(reply["hold"])
    shown = json.dumps(hold["args"])
    assert "{{secret:nas-admin}}" in shown and "s3cr3t-value" not in shown
    assert "hunter2" not in shown and len(shown) < 1000


# -- the spread limit --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spread_five_machines_then_the_sixth_is_held(tmp_path):
    client, sent = band(tmp_path)
    for name in ("nas", "build-box", "w3", "w4", "w5"):
        r = await as_agent(client.call("shell.exec", {"cmd": "id"}, target=name, timeout=1))
        assert r["ok"], name
    # Machines already reached do not count again; read-tier calls never count.
    assert (await as_agent(client.call("shell.exec", {}, target="nas", timeout=1)))["ok"]
    assert (await as_agent(client.call("info.ping", {}, target="w6", timeout=1)))["ok"]
    held = await as_agent(client.call("shell.exec", {"cmd": "id"}, target="w6", timeout=1))
    assert held["held"] and "spread: 6th machine in 10 minutes" in held["error"]
    hold = client.authz.approvals.queue.get(held["hold"])
    assert hold["kind"] == "spread"
    # Another agent has its own count; people are never counted.
    assert (await as_agent(client.call("shell.exec", {}, target="w6", timeout=1), CI))["ok"]
    assert (await as_agent(client.call("shell.exec", {}, target="w7", timeout=1), OWNER))["ok"]


def test_calls_to_the_hub_do_not_count(tmp_path):
    az = authorizer(tmp_path, config=cfg(spread_max=1))
    assert not check(az, "shell.exec", "nas").held
    for _ in range(3):
        assert not check(az, "jobs.run", local=True).held
    assert check(az, "shell.exec", "build-box").held


@pytest.mark.asyncio
async def test_spread_grant_lets_the_agent_continue(tmp_path):
    client, sent = band(tmp_path, config=cfg(spread_max=1))
    assert (await as_agent(client.call("shell.exec", {}, target="nas", timeout=1)))["ok"]
    held = await as_agent(client.call("shell.exec", {}, target="build-box", timeout=1))
    ap = client.authz.approvals
    with pytest.raises(A.HoldError):
        ap.decide(held["hold"], False, "human:u1", scope="spread")   # spread scope = approve only
    ap.decide(held["hold"], True, "human:u1", scope="spread")
    assert ap.queue.active_grant("token:agent-1")
    assert (await as_agent(client.call("shell.exec", {}, target="build-box", timeout=1)))["ok"]
    assert (await as_agent(client.call("shell.exec", {}, target="w3", timeout=1)))["ok"]
    # Normal holds still apply during a spread grant.
    assert (await as_agent(client.call("worker.update", {}, target="w4", timeout=1)))["held"]


@pytest.mark.asyncio
async def test_a_call_scope_spread_approval_grants_nothing_more(tmp_path):
    client, sent = band(tmp_path, config=cfg(spread_max=1))
    await as_agent(client.call("shell.exec", {}, target="nas", timeout=1))
    held = await as_agent(client.call("shell.exec", {}, target="build-box", timeout=1))
    client.authz.approvals.decide(held["hold"], True, "human:u1")
    assert (await as_agent(client.call("shell.exec", {}, target="build-box", timeout=1)))["ok"]
    assert (await as_agent(client.call("shell.exec", {}, target="w3", timeout=1)))["held"]


# -- both layouts see the same holds -----------------------------------------------------

@pytest.mark.asyncio
async def test_two_processes_share_one_queue(tmp_path):
    mcp_side = A.Approvals(str(tmp_path / "approvals.db"), cfg(wait_secs=5))
    dashboard_side = A.Approvals(str(tmp_path / "approvals.db"), cfg(wait_secs=5))
    d = check(Authorizer(PolicyStore(None), None, approvals=mcp_side), "worker.update")
    waiting = asyncio.create_task(mcp_side.resolve(d, {"build": 1}, label="claude code"))
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

def test_no_agent_surface_can_approve():
    from rook.core import authz as core_authz
    from rook.hub.authz import _TOOL_CAPS, hub_cap_for_tool
    caps = set(core_authz.builtin_table())
    assert not [c for c in caps if "approv" in c or c.startswith("hold")]
    assert not [t for t in _TOOL_CAPS if "approv" in t]
    assert hub_cap_for_tool("rook_approvals", {}) is None
    import rook.band_mcp.server as srv
    import inspect
    src = inspect.getsource(srv)
    assert "def rook_approv" not in src and ".decide(" not in src


@pytest.mark.asyncio
async def test_mcp_tools_list_has_no_approval_tool(tmp_path):
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
        assert names and not [n for n in names if "approv" in n or "hold" in n]


@pytest.mark.asyncio
async def test_agent_hub_tool_call_is_held_through_mcp(tmp_path, monkeypatch):
    """An agent's admin hub tool (rook_secret get) and rook_call on worker rook
    (settings.set) are held; a held MCP tool says so."""
    from test_permissions_mcp import mcp, _text
    from test_audit_attribution import Session, STATIC
    monkeypatch.setenv("ROOK_APPROVALS_WAIT_SECS", "0")
    monkeypatch.setenv("ROOK_APPROVALS_CONTACT", "off")
    doc = {**DEFAULT_POLICY, "rules": [*DEFAULT_POLICY["rules"],
                                       {"id": "hold-host", "who": "token:*", "hold": "info.host",
                                        "on": "gpu-box"}]}
    async with mcp(tmp_path, doc) as env:
        s = await Session(env.http, STATIC).open()
        res = await s.result("rook_secret", {"action": "get", "name": "nas-admin"})
        assert res.get("isError") and "Held for approval" in res["content"][0]["text"]
        before = len(env.sent)
        held = _text(await s.result("rook_call", {"cap": "info.host", "worker": "gpu-box",
                                                  "args": {"build": 1}}))
        assert held["held"] and not held["ok"] and len(env.sent) == before
        assert "rule hold-host" in held["error"]
        ok = _text(await s.result("rook_call", {"cap": "shell.exec", "worker": "gpu-box",
                                                "args": {"cmd": "id"}}))
        assert ok["ok"]
        q = A.ApprovalQueue(str(tmp_path / "approvals.db"))
        assert {h["cap"] for h in q.pending()} == {"secret.get", "info.host"}


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
        r1 = await az.resolve(check(az, "worker.update"), {"build": 1})
        r2 = await az.resolve(check(az, "customcap.add", "build-box"), {})
        r = await http.get("/api/approvals?count=1")
        assert (await r.json()) == {"pending": 2}
        r = await http.get("/api/approvals")
        data = await r.json()
        assert [h["id"] for h in data["pending"]] == [r1["hold"], r2["hold"]]
        assert data["pending"][0]["worker"] == "nas" and data["settings"]["enabled"]
        r = await http.post(f"/api/approvals/{r1['hold']}", json={"decision": "approve"})
        assert r.status == 200 and (await r.json())["hold"]["state"] == "approved"
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
        held = await az.resolve(check(az, "worker.update"), {})
        for p in (AGENT, OPERATOR_TOKEN, MEMBER, system("x"), None):
            principal[0] = p
            assert (await http.get("/api/approvals")).status == 403, p
            r = await http.post(f"/api/approvals/{held['hold']}", json={"decision": "approve"})
            assert r.status == 403, p
        assert az.approvals.queue.get(held["hold"])["state"] == "pending"
        principal[0] = OWNER
        r = await http.post(f"/api/approvals/{held['hold']}", json={"decision": "approve"})
        assert r.status == 200


# -- contacting the operator ---------------------------------------------------------

class FakeClient:
    """A band client with a hub node and an optional phone worker."""

    def __init__(self, phone=True, answers=(), fail=False):
        self._local = SimpleNamespace(worker_id="hub1")
        self.workers = {"hub1": {"name": "rook", "roles": ["is_hub"]}}
        if phone:
            self.workers["p1"] = {"name": "phone", "caps": ["voice.speak", "notify.post"]}
        self.calls = []
        self.answers = list(answers)
        self.fail = fail

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None,
                   principal=None):
        self.calls.append({"cap": cap, "args": args, "target": target,
                           "principal": getattr(principal, "id", None)})
        if self.fail:
            raise ConnectionError("relay down")
        if cap == "voice.speak" and args.get("reply"):
            text = self.answers.pop(0) if self.answers else ""
            res = {"ok": True, "spoken": True, "reply_state": "answered" if text else "none"}
            if text:
                res["reply"] = {"text": text, "via": "voice"}
            return {"id": "x", "from": target, "ok": True, "result": res}
        return {"id": "x", "from": target, "ok": True, "result": {"ok": True}}


def make_hold(ap, cap="worker.update", target="nas", principal=AGENT, tier=None, is_hub=False):
    d = check(Authorizer(PolicyStore(None), None, approvals=ap), cap,
              "rook" if is_hub else target, principal, local=is_hub)
    hold, _ = ap.queue.create(principal=d.principal, cap=d.cap, tier=tier or d.tier,
                              args_hash=A.args_hash({"cap": cap, "t": target}),
                              label=principal.label, target_id=d.target,
                              target_name="rook" if is_hub else target, is_hub=is_hub,
                              reason=d.hold_reason, kind=d.hold_kind)
    return hold


async def settle(contact):
    for _ in range(100):
        live = [t for t in contact._tasks if not t.done()]
        if not live:
            return
        await asyncio.gather(*live, return_exceptions=True)


def test_message_text_is_short_and_has_no_args(tmp_path):
    ap = A.Approvals(str(tmp_path / "a.db"), FAST)
    h = make_hold(ap)
    text = message([h], "https://hub.example.org/#approvals")
    assert text == ("Rook: claude code wants to run worker.update on nas. Approve on the "
                    "dashboard: https://hub.example.org/#approvals")
    assert question(h) == "Rook: claude code wants to run worker update on nas. Approve?"
    many = message([h] * 7)
    assert many.startswith("Rook: 7 calls are waiting") and "and 2 more" in many


@pytest.mark.asyncio
async def test_notifications_are_coalesced(tmp_path):
    ap = A.Approvals(str(tmp_path / "a.db"), cfg(contact=("telegram",), notify_every_secs=0.3))
    client = FakeClient()
    ap.attach_client(client)
    for t in ("nas", "build-box", "w3"):
        ap.contact.hold_created(make_hold(ap, target=t))
    await asyncio.sleep(0.05)
    sends = [c for c in client.calls if c["cap"] == "notify.send"]
    assert len(sends) == 1 and "worker.update on nas" in sends[0]["args"]["text"]
    assert sends[0]["target"] == "hub1" and sends[0]["principal"] == "system:approvals"
    await asyncio.sleep(0.4)
    await settle(ap.contact)
    sends = [c for c in client.calls if c["cap"] == "notify.send"]
    assert len(sends) == 2 and sends[1]["args"]["text"].startswith("Rook: 2 calls are waiting")


@pytest.mark.asyncio
async def test_contact_failure_never_reaches_the_call(tmp_path):
    client, sent = band(tmp_path, config=cfg(contact=("telegram", "notify"),
                                             contact_worker="phone"))
    client.authz.approvals.attach_client(FakeClient(fail=True))
    reply = await as_agent(client.call("worker.update", {}, target="nas", timeout=1))
    assert reply["held"]
    await settle(client.authz.approvals.contact)


@pytest.mark.asyncio
async def test_notify_and_voice_channels_use_the_phone(tmp_path):
    ap = A.Approvals(str(tmp_path / "a.db"), cfg(contact=("notify",), contact_worker="Phone"))
    client = FakeClient()
    ap.attach_client(client)
    ap.contact.hold_created(make_hold(ap))
    await settle(ap.contact)
    assert [(c["cap"], c["target"]) for c in client.calls] == [("notify.post", "p1")]
    assert client.calls[0]["args"]["title"] == "Rook approval"
    ap2 = A.Approvals(str(tmp_path / "b.db"), cfg(contact=("voice",), contact_worker="phone"))
    client2 = FakeClient()
    ap2.attach_client(client2)
    ap2.contact.hold_created(make_hold(ap2))
    await settle(ap2.contact)
    assert client2.calls[0]["cap"] == "voice.speak" and client2.calls[0]["args"]["wait"] is False


# -- approving by voice -------------------------------------------------------------------

def test_voice_verdicts():
    for t in ("yes", "Yes.", "approve", "approved!", "allow", "do it", "go ahead", "OK", "okay"):
        assert voice_verdict(t) == "approve", t
    for t in ("no", "No!", "deny", "don't", "do not", "stop", "cancel", "nope"):
        assert voice_verdict(t) == "deny", t
    for t in ("yes but no", "ok wait", "not now", "maybe", "", "know what, yes no", "wait",
              "yes, don't"):
        assert voice_verdict(t) == "unclear", t


def voice_setup(tmp_path, answers=(), phone=True, **kw):
    ap = A.Approvals(str(tmp_path / "a.db"),
                     cfg(contact=("ask", "telegram"), contact_worker="phone", **kw))
    client = FakeClient(phone=phone, answers=answers)
    ap.attach_client(client)
    return ap, client


@pytest.mark.asyncio
async def test_voice_yes_approves(tmp_path):
    rows = []
    ap, client = voice_setup(tmp_path, ["Yes, go ahead."])
    ap._record = lambda **kw: rows.append(kw)
    h = make_hold(ap)
    ap.contact.hold_created(h)
    await settle(ap.contact)
    got = ap.queue.get(h["id"])
    assert got["state"] == "approved" and got["decided_by"] == "voice:phone"
    ask = client.calls[0]
    assert ask["cap"] == "voice.speak" and ask["args"]["reply"] and ask["target"] == "p1"
    assert ask["principal"] == "system:approvals"
    assert ask["args"]["text"] == "Rook: claude code wants to run worker update on nas. Approve?"
    assert not [c for c in client.calls if c["cap"] == "notify.send"]
    row = next(r for r in rows if r["cap"] == "approval.decide")
    assert row["identity"] == "voice:phone" and row["reply"]["heard"] == "Yes, go ahead."


@pytest.mark.asyncio
async def test_voice_no_denies(tmp_path):
    ap, client = voice_setup(tmp_path, ["no"])
    h = make_hold(ap)
    ap.contact.hold_created(h)
    await settle(ap.contact)
    assert ap.queue.get(h["id"])["state"] == "denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("answers", [["maybe later"], [], ["yes but no"]])
async def test_unclear_or_no_answer_stays_pending_and_falls_back(tmp_path, answers):
    ap, client = voice_setup(tmp_path, answers)
    h = make_hold(ap)
    ap.contact.hold_created(h)
    await settle(ap.contact)
    assert ap.queue.get(h["id"])["state"] == "pending"
    assert [c["cap"] for c in client.calls] == ["voice.speak", "notify.send"]


@pytest.mark.asyncio
async def test_offline_phone_falls_back(tmp_path):
    ap, client = voice_setup(tmp_path, ["yes"], phone=False)
    h = make_hold(ap)
    ap.contact.hold_created(h)
    await settle(ap.contact)
    assert ap.queue.get(h["id"])["state"] == "pending"
    assert [c["cap"] for c in client.calls] == ["notify.send"]


@pytest.mark.asyncio
async def test_holds_above_voice_max_tier_are_not_voice_approvable(tmp_path):
    ap, client = voice_setup(tmp_path, ["yes"], voice_max_tier="exec")
    h = make_hold(ap)                                      # worker.update: admin
    assert not voice_eligible(h, ap.config())
    ap.contact.hold_created(h)
    await settle(ap.contact)
    assert ap.queue.get(h["id"])["state"] == "pending"
    assert [c["cap"] for c in client.calls] == ["notify.send"]
    # Admin calls to the hub itself always need the dashboard.
    hub = make_hold(ap, cap="settings.set", principal=OPERATOR_TOKEN, is_hub=True)
    assert hub["is_hub"] and not voice_eligible(hub, A.Config(voice_max_tier="admin"))
    assert not voice_eligible(make_hold(ap, target="w3"), A.Config(voice_max_tier="off"))


@pytest.mark.asyncio
async def test_a_spoken_yes_only_approves_the_hold_it_named(tmp_path):
    ap, client = voice_setup(tmp_path, ["yes", "maybe"])
    a = make_hold(ap, target="nas")
    b = make_hold(ap, target="build-box")
    ap.contact.hold_created(a)
    ap.contact.hold_created(b)
    await settle(ap.contact)
    assert ap.queue.get(a["id"])["state"] == "approved"
    assert ap.queue.get(b["id"])["state"] == "pending"
    asks = [c["args"]["text"] for c in client.calls if c["cap"] == "voice.speak"]
    assert asks == [question(a), question(b)]               # one at a time, each named


@pytest.mark.asyncio
async def test_a_hold_decided_on_the_dashboard_is_not_asked(tmp_path):
    ap, client = voice_setup(tmp_path, ["yes"])
    h = make_hold(ap)
    ap.decide(h["id"], False, "human:u1")
    ap.contact.hold_created(h)
    await settle(ap.contact)
    assert not client.calls and ap.queue.get(h["id"])["state"] == "denied"


@pytest.mark.asyncio
async def test_the_agents_call_proceeds_once_the_voice_approval_lands(tmp_path):
    def speak(msg):
        if msg["args"].get("reply"):
            return {"ok": True, "spoken": True, "reply_state": "answered",
                    "reply": {"text": "yes", "via": "voice"}}
        return {"ok": True}
    client, sent = band(tmp_path, config=cfg(wait_secs=5, contact=("ask",),
                                             contact_worker="phone"),
                        replies={"voice.speak": speak})
    client.workers["phone"] = entry("phone", caps=["voice.speak"])
    client.authz.attach_client(client)
    reply = await as_agent(client.call("worker.update", {"build": 3}, target="nas", timeout=2))
    assert reply["ok"]
    assert [m["cap"] for m in sent] == ["voice.speak", "worker.update"]
    assert sent[0]["target"] == "phone" and sent[0]["identity"] == "system:approvals"
    hold = client.authz.approvals.queue.recent()[0]
    assert hold["state"] == "consumed" and hold["decided_by"] == "voice:phone"


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
    assert check(authorizer(tmp_path), "worker.update", principal=ident.principal).held
    mine = resolve_identity({"id": "j2", "identity": {"mode": "creator"}}, human,
                            directory=directory)
    assert mine.principal.via == ("job:j2",)
    assert not check(authorizer(tmp_path), "worker.update", principal=mine.principal).held


@pytest.mark.asyncio
async def test_hub_plugin_tools_cannot_carry_an_agent_past_a_hold(tmp_path):
    from rook.hub.mcp_tools import _hold_gate
    az = authorizer(tmp_path, config=cfg(wait_secs=0))
    node = SimpleNamespace(worker_id="rook", entry=lambda: {"name": "rook", "roles": ["is_hub"]},
                           client=SimpleNamespace(authz=az))
    with pytest.raises(PermissionError, match="Held for approval"):
        await as_agent(_hold_gate(node, "memory.forget", {"id": "m1"}))
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
             allowed_caps=["worker.*", "customcap.*"])
    p.bind_host(node)
    m = Inbound(chat_id=CHAT, user_id="42", username="a", text="")
    out = await p.run_command('call nas worker.restart {}', m)
    assert "Held for approval" in out and not sent             # admin + destructive: held
    out = await p.run_command('call nas customcap.add {"name": "x"}', m)
    assert "Held for approval" not in out and sent[-1]["cap"] == "customcap.add"  # the rule



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
        held = await az.resolve(check(az, "worker.update"), {})
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
        held = await az.resolve(check(az, "worker.update"), {})
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
        held = await az.resolve(check(az, "worker.update"), {})
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


# -- shell on the hub's own machine (permissions 3.11, bypass 2) ---------------------------

def _hub_host_band(tmp_path, host_id="hub-abc", worker_host=None, hub_host_workers=("auto",)):
    client = BandClient("test-band")
    sent = []

    async def send(data, route=0):
        msg = json.loads(data)
        sent.append(msg)
        if msg.get("cap"):
            asyncio.get_running_loop().call_soon(client._handle_reply, {
                "id": msg["id"], "from": msg.get("target"), "ok": True, "result": "done"})
    client.transport.send = send
    client._local = SimpleNamespace(
        worker_id="hub1",
        facts=SimpleNamespace(hw={"host_id": host_id} if host_id else {}))
    client.workers["hub1"] = {"name": "rook", "roles": ["is_hub"], "facts": {"host_id": host_id}}
    client.workers["nas"] = entry("nas", facts={"host_id": worker_host if worker_host is not None else host_id})
    client.workers["remote"] = entry("remote", facts={"host_id": "elsewhere"})
    client.authz = authorizer(tmp_path, config=cfg(hub_host_workers=hub_host_workers))
    client.authz.attach_client(client)
    return client, sent


def test_hub_host_detection(tmp_path):
    client, _ = _hub_host_band(tmp_path)
    ap = client.authz.approvals
    hh = ap.hub_host_workers()
    assert hh["mode"] == "auto" and [w["worker"] for w in hh["workers"]] == ["nas"]
    assert hh["detected"]
    # No machine id announced: can't detect, says so.
    client2, _ = _hub_host_band(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, host_id="")
    assert client2.authz.approvals.hub_host_workers()["note"]


@pytest.mark.asyncio
async def test_exec_on_a_hub_host_worker_is_held(tmp_path):
    client, sent = _hub_host_band(tmp_path)
    held = await as_agent(client.call("shell.exec", {"cmd": "echo x"}, target="nas", timeout=0.5))
    assert held["held"] and not sent
    row = client.authz.approvals.queue.get(held["hold"])
    assert row["kind"] == "hub-host" and row["reason"] == "runs on the hub's machine"
    # A worker on another machine is not held for this reason.
    ok = await as_agent(client.call("shell.exec", {"cmd": "echo x"}, target="remote", timeout=0.5))
    assert ok["ok"]
    # read tier on the hub-host worker is fine.
    assert (await as_agent(client.call("info.ping", {}, target="nas", timeout=0.5)))["ok"]


@pytest.mark.asyncio
async def test_hub_host_hold_respects_an_operator_allow_and_off(tmp_path):
    doc = {**DEFAULT_POLICY, "rules": [{"id": "nas-shell", "who": "role:agent",
                                        "allow": "shell.exec", "on": "nas"}]}
    client = BandClient("test-band")
    sent = []

    async def send(data, route=0):
        msg = json.loads(data)
        sent.append(msg)
        asyncio.get_running_loop().call_soon(client._handle_reply,
                                             {"id": msg["id"], "from": msg.get("target"), "ok": True, "result": "x"})
    client.transport.send = send
    client._local = SimpleNamespace(worker_id="hub1", facts=SimpleNamespace(hw={"host_id": "h"}))
    client.workers["nas"] = entry("nas", facts={"host_id": "h"})
    client.authz = authorizer(tmp_path, doc, config=cfg())
    client.authz.attach_client(client)
    assert (await as_agent(client.call("shell.exec", {}, target="nas", timeout=0.5)))["ok"]  # allow rule names it
    # off: not held even without a rule.
    client.authz.approvals.config_source.fixed = cfg(hub_host_workers=("off",))
    client2, sent2 = _hub_host_band(tmp_path / "c" if (tmp_path / "c").mkdir() is None else tmp_path,
                                    hub_host_workers=("off",))
    assert (await as_agent(client2.call("shell.exec", {}, target="nas", timeout=0.5)))["ok"]


# -- an agent's own console/chat is not held by the built-in hold (noise, item 3) ----------

@pytest.mark.asyncio
async def test_own_console_signal_is_not_held_but_anothers_is(tmp_path):
    client, sent = band(tmp_path)
    ap = client.authz.approvals
    # proc.close is exec + destructive, held by default for an agent.
    assert (await as_agent(client.call("proc.close", {"handle": "h"}, target="nas", timeout=0.5)))["held"]
    # With own_resource set (the server sets it for the caller's own console), it proceeds.
    tok = A.own_resource.set(True)
    try:
        ok = await as_agent(client.call("proc.close", {"handle": "h"}, target="nas", timeout=0.5))
    finally:
        A.own_resource.reset(tok)
    assert ok["ok"]
    # Operator hold rules still apply even to your own console.
    client2, sent2 = band(tmp_path / "d" if (tmp_path / "d").mkdir() is None else tmp_path,
                          {**DEFAULT_POLICY, "rules": [{"id": "hold-proc", "who": "role:agent",
                                                        "hold": "proc.close", "on": "*"}]})
    tok = A.own_resource.set(True)
    try:
        assert (await as_agent(client2.call("proc.close", {"handle": "h"}, target="nas", timeout=0.5)))["held"]
    finally:
        A.own_resource.reset(tok)


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
