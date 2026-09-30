"""Permissions (docs/design/permissions.md): tiers, signed objects, grants,
tickets, the policy engine, hub-side enforcement, worker-side checks and the
three current-code gaps (6.3)."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nacl.signing import SigningKey

from rook.core import authz
from rook.core.facts import NodeFacts, roles_from_announce
from rook.hub.policy import DEFAULT_POLICY, Policy, PolicyError, PolicyStore, Principal, Target

BAND = "ab" * 16


@pytest.fixture
def root(monkeypatch):
    sk = SigningKey.generate()
    monkeypatch.setenv("ROOK_UPDATE_PUBKEY", authz.pub_b64(sk))
    return sk


@pytest.fixture
def op():
    return SigningKey.generate()


def _grant(root, op, **kw):
    kw.setdefault("name", "rook")
    return authz.make_grant(root, authz.pub_b64(op), "is_hub", [BAND], **kw)


# -- tiers -------------------------------------------------------------------

def test_tier_table_is_a_floor_and_unknown_is_exec():
    assert authz.effective_tier("info.ping") == "read"
    assert authz.effective_tier("shell.exec", declared="read") == "exec"   # never lower
    assert authz.effective_tier("info.ping", declared="a") == "admin"      # raise freely
    assert authz.effective_tier("totally.new") == "exec"
    assert authz.effective_tier("totally.new", declared="w") == "write"
    assert authz.effective_tier("cmd.backup", declared="read") == "exec"
    assert authz.effective_tier("deluge.remove", override="exec") == "exec"
    assert authz.effective_tier("shell.exec", override="read") == "exec"
    assert authz.effective_tier("shell.exec", override="read", lower=True) == "read"
    assert authz.builtin_tags("camera.snap") == ("sensitive", "physical")
    assert authz.effective_tier("secret.get") == "admin"


# -- signed objects: domain separation (6.3 deauth gap) ----------------------

def test_manifest_signature_never_passes_as_deauth(root):
    from rook.remote.update_keys import _canonical_payload
    import base64
    manifest = {"schema": 1, "build": 9, "sha256": "00"}
    signed = {**manifest, "sig": base64.b64encode(root.sign(_canonical_payload(manifest)).signature).decode()}
    ok, why = authz.verify_deauth(signed, [authz.pub_b64(root)], "w1")
    assert not ok and "deauth v2" in why
    # Even a legacy v1 deauth body with worker_id/issued_at is refused now.
    legacy = {"worker_id": "w1", "issued_at": int(time.time())}
    legacy["sig"] = base64.b64encode(root.sign(_canonical_payload(legacy)).signature).decode()
    assert not authz.verify_deauth(legacy, [authz.pub_b64(root)], "w1")[0]


def test_deauth_v2_requires_target_and_age(root):
    anchors = [authz.pub_b64(root)]
    order = authz.make_deauth(root, "w1", "box", "gone")
    assert authz.verify_deauth(order, anchors, "w1") == (True, "ok")
    assert not authz.verify_deauth(order, anchors, "w2")[0]
    assert not authz.verify_deauth(order, anchors, None)[0]
    old = authz.make_deauth(root, "w1", now=time.time() - 2 * 86400)
    assert authz.verify_deauth(old, anchors, "w1") == (False, "signed order too old")
    # Stripping issued_at breaks the signature; a re-signed body without it is refused.
    body = {k: v for k, v in order.items() if k not in ("issued_at", "sig")}
    resigned = authz.sign_obj(root, authz.PREFIX_DEAUTH, body)
    assert authz.verify_deauth(resigned, anchors, "w1") == (False, "deauth order has no issued_at")
    other = SigningKey.generate()
    assert not authz.verify_deauth(authz.make_deauth(other, "w1"), anchors, "w1")[0]


def test_hub_deauth_payload_works_for_old_and_new_workers(root):
    from rook.hub.keys import deauth_payload
    from rook.worker._update_verify import verify_manifest
    payload = deauth_payload(root, "w1", "box", "bye")
    assert verify_manifest(payload, authz.pub_b64(root))            # build-167 path
    assert authz.verify_deauth(payload, [authz.pub_b64(root)], "w1")[0]  # v2 path


@pytest.fixture
def selfupdate(tmp_path, monkeypatch):
    from rook.worker.plugins import selfupdate as su
    monkeypatch.setattr(su, "_WORKER_DIR", tmp_path)
    monkeypatch.setattr(su, "_BANNED", tmp_path / "banned")
    monkeypatch.setattr(su, "_WORKER_ID_FILE", tmp_path / "worker_id")
    monkeypatch.setattr(su, "_PYZ", tmp_path / "band-worker.pyz")
    (tmp_path / "worker_id").write_text("w1\n")
    p = su.SelfUpdatePlugin()
    p._schedule_restart = lambda *a, **k: None
    p._persist = lambda argv: False
    return p


@pytest.mark.asyncio
async def test_deauth_cap_accepts_only_v2_for_this_worker(root, selfupdate, tmp_path):
    from rook.hub.keys import deauth_payload
    manifest = authz.sign_obj(root, b"", {"schema": 1, "build": 3})
    assert not (await selfupdate._deauth(manifest))["ok"]
    assert not (await selfupdate._deauth(deauth_payload(root, "w2", "x", "")))["ok"]
    assert not (tmp_path / "banned").exists()
    res = await selfupdate._deauth(deauth_payload(root, "w1", "x", "bye"))
    assert res["ok"] and (tmp_path / "banned").exists()


# -- worker.update(url=) needs a signed manifest (6.3) -------------------------

@pytest.mark.asyncio
async def test_update_url_without_signed_manifest_is_refused(root, selfupdate, tmp_path):
    fetched = []

    async def http_get(url, timeout):
        fetched.append(url)
        return b"evil"
    selfupdate._http_get = http_get
    res = await selfupdate._update(url="http://example.invalid/b.pyz")
    assert not res["ok"] and "signed OTA manifest" in res["error"] and not fetched
    forged = {"build": 999, "sha256": "00", "sig": "AAAA"}
    res = await selfupdate._update(url="http://example.invalid/b.pyz", manifest=forged)
    assert not res["ok"] and not fetched
    # A validly signed manifest whose hash doesn't match the download is refused.
    from rook.remote.update_keys import _canonical_payload
    import base64
    m = {"schema": 1, "build": 999, "sha256": "11" * 32}
    m["sig"] = base64.b64encode(root.sign(_canonical_payload(m)).signature).decode()
    res = await selfupdate._update(url="http://example.invalid/b.pyz", manifest=m)
    assert not res["ok"] and "sha256" in res["error"]
    assert not (tmp_path / "band-worker.pyz").exists()


# -- hub/psk changes need a signed order (6.3) -------------------------------

@pytest.mark.asyncio
async def test_reconfigure_hub_psk_needs_a_verified_ticket(selfupdate, monkeypatch):
    from rook.core.context import call_ticket
    res = await selfupdate._reconfigure(psk="new-psk", restart=False)
    assert not res["ok"] and "hub-signed order" in res["error"]
    res = await selfupdate._reconfigure(name="renamed", restart=False)   # rename is fine
    assert res["ok"]
    tok = call_ticket.set({"verified": True, "reason": "ok"})
    try:
        assert (await selfupdate._reconfigure(hub="hub.example:7474", restart=False))["ok"]
    finally:
        call_ticket.reset(tok)
    tok = call_ticket.set({"verified": False, "reason": "ticket args mismatch"})
    try:
        res = await selfupdate._update(psk="x")
        assert not res["ok"] and "ticket args mismatch" in res["error"]
    finally:
        call_ticket.reset(tok)
    monkeypatch.setenv("ROOK_AUTHZ_ALLOW_UNSIGNED_REPOINT", "1")   # local-root hatch
    assert (await selfupdate._reconfigure(psk="new-psk", restart=False))["ok"]


@pytest.mark.asyncio
async def test_config_apply_hub_psk_needs_a_verified_ticket(tmp_path, monkeypatch):
    from rook.worker import wconfig
    from rook.worker.plugins.config import ConfigPlugin
    monkeypatch.setattr(wconfig, "_DIR", tmp_path)
    monkeypatch.setattr(wconfig, "_ACTIVE", tmp_path / "config.json")
    monkeypatch.setattr(wconfig, "_PREV", tmp_path / "config.json.prev")
    monkeypatch.setattr(wconfig, "_PENDING", tmp_path / "config.json.pending")
    p = ConfigPlugin()
    res = await p._apply({"psk": "x"}, epoch=1, restart=False)
    assert not res["ok"] and not (tmp_path / "config.json").exists()
    assert (await p._apply({"log_level": "debug"}, epoch=1, restart=False))["ok"]


# -- grants ------------------------------------------------------------------

def test_grant_verification(root, op):
    anchors = [authz.pub_b64(root)]
    g = _grant(root, op)
    assert authz.verify_grant(g, anchors, band=BAND) == (True, "ok")
    assert authz.verify_grant(g, anchors, band="cd" * 16)[1] == "band not in grant scope"
    assert authz.verify_grant(g, [authz.pub_b64(op)])[1] == "issuer is not a trusted root"
    assert authz.verify_grant(g, anchors, revoked=[g["serial"]])[1] == "grant revoked"
    expired = _grant(root, op, now=time.time() - 30 * 86400)
    assert authz.verify_grant(expired, anchors)[1] == "grant expired or not yet valid"
    tampered = {**g, "role": "is_hub", "scope": {"bands": [BAND, "ff" * 16]}}
    assert authz.verify_grant(tampered, anchors)[1] == "bad grant signature"
    bad_name = _grant(root, op, name="evil")
    assert not authz.verify_grant(bad_name, anchors)[0]
    # A grant signature is not valid as any other object type.
    assert not authz.verify_obj(authz.pub_b64(root), authz.PREFIX_TICKET, g)


def test_roles_need_grant_plus_proof_of_possession(root, op):
    g = _grant(root, op)
    announce = {"kind": "announce", "worker_id": "hub1", "name": "rook", "caps": ["hub.info"]}
    signed = authz.sign_announce(op, {**announce, "grants": [g]}, seq=1)
    assert roles_from_announce(signed, BAND) == frozenset({"is_hub"})
    assert NodeFacts.from_announce(signed, BAND).is_hub
    assert roles_from_announce(signed, "cd" * 16) == frozenset()       # other band
    # Copying the grant into another peer's announce proves nothing.
    thief = SigningKey.generate()
    copied = authz.sign_announce(thief, {**announce, "worker_id": "x", "grants": [g]}, seq=1)
    assert roles_from_announce(copied, BAND) == frozenset()
    assert roles_from_announce({**announce, "grants": [g]}, BAND) == frozenset()
    # Changing the announced caps after signing invalidates it.
    assert roles_from_announce({**signed, "caps": ["shell.exec"]}, BAND) == frozenset()
    stale = authz.sign_announce(op, {**announce, "grants": [g]}, seq=2, now=time.time() - 600)
    assert roles_from_announce(stale, BAND) == frozenset()


# -- tickets -----------------------------------------------------------------

def test_ticket_binding_replay_and_constraints(root, op):
    g = _grant(root, op)
    kid = g["sub"]["kid"]
    keys = {kid: g}
    args = {"cmd": "uptime"}
    t = authz.make_ticket(op, kid, principal="token:a", cap="shell.exec", target="w1",
                          msg_id="m1", args=args, tier="exec", rev=3)
    replay = authz.ReplayCache()
    common = dict(cap="shell.exec", target="w1", msg_id="m1", keys=keys)
    assert authz.verify_ticket(t, args={"cmd": "rm -rf /"}, **common)[1] == "ticket args mismatch"
    assert authz.verify_ticket(t, args=args, **{**common, "target": "w2"})[1] == "ticket for another worker"
    assert authz.verify_ticket(t, args=args, **{**common, "cap": "file.write"})[1] == "ticket for another cap"
    assert authz.verify_ticket(t, args=args, replay=replay, **common) == (True, "ok")
    assert authz.verify_ticket(t, args=args, replay=replay, **common)[1] == "ticket replayed"
    assert authz.verify_ticket(t, args=args, now=time.time() + 3600, **common)[1] == "ticket expired"
    assert authz.verify_ticket(t, args=args, **{**common, "keys": {}})[1] == "unknown ticket key"
    reader = _grant(root, op, constraints={"max_tier": "read"})
    assert authz.verify_ticket(t, args=args, **{**common, "keys": {kid: reader}})[1] \
        == "ticket tier above grant constraint"
    forged = authz.make_ticket(SigningKey.generate(), kid, principal="token:a", cap="shell.exec",
                               target="w1", msg_id="m1", args=args, tier="exec", rev=3)
    assert authz.verify_ticket(forged, args=args, **common)[1] == "bad ticket signature"


def test_replay_cache_is_bounded():
    c = authz.ReplayCache(max_entries=3)
    for i in range(10):
        assert not c.seen(f"m{i}")
    assert len(c._seen) <= 3


# -- worker guard ------------------------------------------------------------

def test_guard_learns_hub_key_and_enforces_by_mode(root, op):
    from rook.worker.authz_guard import Guard
    g = _grant(root, op)
    announce = authz.sign_announce(op, {"kind": "announce", "worker_id": "hub1", "name": "rook",
                                        "caps": [], "grants": [g]}, seq=5)
    audit_guard = Guard("w1", BAND, [authz.pub_b64(root)], mode="audit")
    assert audit_guard.on_announce(announce)
    assert not audit_guard.on_announce(announce)          # seq must increase
    assert audit_guard.readiness()["kids"] == [g["sub"]["kid"]]
    msg = {"id": "m1", "cap": "shell.exec", "target": "w1", "args": {}}
    info = audit_guard.check(msg, "shell.exec", "exec", {})
    assert not info["verified"] and "refuse" not in info       # audit never refuses

    strict = Guard("w1", BAND, [authz.pub_b64(root)], mode="enforce-exec")
    assert "refuse" in strict.check(msg, "shell.exec", "exec", {})
    assert "refuse" not in strict.check({**msg, "cap": "info.ping"}, "info.ping", "read", {})
    # A ticket with the grant inlined works without a prior announce.
    t = authz.make_ticket(op, g["sub"]["kid"], principal="token:a", cap="shell.exec",
                          target="w1", msg_id="m2", args={}, tier="exec", rev=1, grant=g)
    ok = strict.check({**msg, "id": "m2", "ticket": t}, "shell.exec", "exec", {})
    assert ok["verified"] and "refuse" not in ok
    again = strict.check({**msg, "id": "m2", "ticket": t}, "shell.exec", "exec", {})
    assert again["reason"] == "ticket replayed" and "refuse" in again
    # The ticket's tier can't be below the worker's own table.
    low = authz.make_ticket(op, g["sub"]["kid"], principal="token:a", cap="shell.exec",
                            target="w1", msg_id="m3", args={}, tier="read", rev=1)
    assert "refuse" in strict.check({**msg, "id": "m3", "ticket": low}, "shell.exec", "exec", {})


class _Transport:
    band_id = bytes.fromhex(BAND)

    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))


@pytest.fixture
def worker(tmp_path, monkeypatch):
    from rook.worker import admin, audit, core
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "worker_id")
    monkeypatch.setattr(admin, "_PLUGIN_STATE", tmp_path / "plugins.json")
    monkeypatch.setattr(admin, "_CUSTOM_STATE", tmp_path / "custom_caps.json")
    monkeypatch.setattr(audit, "_AUDIT_DIR", tmp_path)
    monkeypatch.setattr(audit, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    return core.Worker(_Transport(), enabled=["info"], name="w")


@pytest.mark.asyncio
async def test_worker_audit_mode_records_tickets_and_announces_readiness(worker, tmp_path):
    from rook.worker import audit
    await worker._on_message(json.dumps({"id": "m1", "cap": "info.ping",
                                         "target": worker.worker_id, "args": {}}).encode(), ())
    assert worker.transport.sent[-1]["ok"]
    rows = audit.tail(5)
    assert rows[-1]["decision"] == "allow" and rows[-1]["ticket"]["verified"] is False
    await worker.announce()
    assert worker.transport.sent[-1]["authz"]["mode"] == "audit"


@pytest.mark.asyncio
async def test_worker_enforce_mode_refuses_unticketed_admin(worker, monkeypatch):
    worker.guard.mode = "enforce-admin"
    await worker._on_message(json.dumps({"id": "m1", "cap": "worker.plugin.disable",
                                         "target": worker.worker_id,
                                         "args": {"name": "info"}}).encode(), ())
    reply = worker.transport.sent[-1]
    assert not reply["ok"] and reply["error"].startswith("denied by worker")
    assert worker.registry.has("info.ping")


# -- policy engine -----------------------------------------------------------

AGENT = Principal("token:agent_1", "token", "agent")
OWNER = Principal("human:u1", "human", "owner", ("human:owner",))
TELEGRAM = Principal("integration:telegram", "integration", "integration")
UNVERIFIED = Principal("unverified", "unverified", verified=False)
W_A = Target(id="id-a", name="worker-a")
W_C = Target(id="id-c", name="worker-c", facts={"camera": True})
ROOK = Target(id="hub", name="rook", is_rook=True)


def test_compatibility_policy_allows_everything_existing():
    p = Policy(DEFAULT_POLICY)
    assert p.mode == "audit"
    for who in (AGENT, OWNER, UNVERIFIED, Principal("token:static", "token", "operator"),
                Principal("human:dashboard", "human", "owner", ("human:owner",)),
                Principal("system:rook-mcp", "system", "system")):
        for cap in ("info.ping", "shell.exec", "worker.config_apply", "secret.get", "brand.new"):
            d = p.evaluate([who], cap, W_A if not cap.startswith("secret") else ROOK)
            assert not d.denied, (who, cap, d)
    # Agents' admin calls are journaled as would_deny, not denied.
    assert p.evaluate([AGENT], "worker.restart", W_A).decision == "would_deny"
    assert p.evaluate([AGENT], "shell.exec", W_A).decision == "allow"
    # New principal kinds are restricted (shadow only in audit mode).
    assert p.evaluate([TELEGRAM], "shell.exec", W_A).decision == "would_deny"
    enforced = Policy({**DEFAULT_POLICY, "mode": "enforce"})
    assert enforced.evaluate([TELEGRAM], "shell.exec", W_A).decision == "deny"
    assert enforced.evaluate([AGENT], "shell.exec", W_A).decision == "allow"
    assert enforced.evaluate([AGENT], "worker.restart", W_A).decision == "would_deny"
    assert enforced.evaluate([UNVERIFIED], "shell.exec", W_A).decision == "allow"
    assert enforced.evaluate([AGENT], "policy.set", ROOK).decision == "deny"
    assert enforced.evaluate([OWNER], "policy.set", ROOK).decision == "allow"


def test_unverified_exec_denial_is_one_edit():
    doc = json.loads(json.dumps(DEFAULT_POLICY))
    doc["mode"] = "enforce"
    doc["principals"]["unverified"] = {"read": "allow", "write": "allow",
                                       "exec": "deny", "admin": "deny"}
    p = Policy(doc)
    assert p.evaluate([UNVERIFIED], "shell.exec", W_A).decision == "deny"
    assert p.evaluate([UNVERIFIED], "chat.send", W_A).decision == "allow"


def _worked_example(order):
    rules = {
        "telegram-exec-lab": {"id": "telegram-exec-lab", "who": "integration:telegram",
                              "allow": "tier:exec", "on": ["worker-a", "worker-b"]},
        "telegram-no-exec": {"id": "telegram-no-exec", "who": "integration:telegram",
                             "deny": "tier:exec", "on": "*"},
        "no-sensitive": {"id": "no-sensitive", "who": "integration:telegram",
                         "deny": "tag:sensitive", "on": "*"},
        "no-camera": {"id": "no-camera", "who": "role:agent", "deny": "camera.*", "on": "*"},
    }
    return Policy({**DEFAULT_POLICY, "mode": "enforce", "rules": [rules[r] for r in order]})


@pytest.mark.parametrize("order", [
    ["telegram-exec-lab", "telegram-no-exec", "no-sensitive", "no-camera"],
    ["no-camera", "no-sensitive", "telegram-no-exec", "telegram-exec-lab"],
])
def test_most_specific_rule_wins_regardless_of_order(order):
    p = _worked_example(order)
    d = p.evaluate([TELEGRAM], "shell.exec", W_A)
    assert d.decision == "allow" and d.rule == "telegram-exec-lab"
    assert d.runner_up == "telegram-no-exec"
    d = p.evaluate([TELEGRAM], "shell.exec", W_C)
    assert d.decision == "deny" and d.rule == "telegram-no-exec"
    denial = d.denial()
    assert denial["denied"]["rule"] == "telegram-no-exec" and "worker-c" in denial["error"]
    assert p.evaluate([TELEGRAM], "file.read", W_A).rule == "no-sensitive"
    assert p.evaluate([AGENT], "camera.snap", W_C).decision == "deny"
    # On-behalf-of chain: allowed only if every principal is.
    assert p.evaluate([OWNER, TELEGRAM], "shell.exec", W_C).decision == "deny"
    assert p.evaluate([OWNER], "shell.exec", W_C).decision == "allow"


def test_target_selectors_groups_facts_and_negation():
    doc = {**DEFAULT_POLICY, "mode": "enforce",
           "groups": {"lab": ["worker-a"], "cams": {"match": "has(camera)"}},
           "principal_groups": {"ci": ["token:agent_1"]},
           "rules": [
               {"id": "ci-lab-only", "who": "group:ci", "deny": "tier:exec", "on": "!group:lab"},
               {"id": "no-cam-writes", "who": "role:agent", "deny": "tier:write", "on": "group:cams"},
               {"id": "hub-only", "who": "role:agent", "deny": "hub.*", "on": "!rook"},
               {"id": "signed", "who": "role:agent", "allow": "hub.info", "on": "is_hub"},
           ]}
    p = Policy(doc)
    assert p.evaluate([AGENT], "shell.exec", W_A).decision == "allow"
    assert p.evaluate([AGENT], "shell.exec", W_C).rule == "ci-lab-only"
    assert p.evaluate([AGENT], "chat.send", W_C).rule == "no-cam-writes"
    assert p.evaluate([AGENT], "hub.plugins", W_A).rule == "hub-only"
    hub = Target(id="hub", name="rook", is_rook=True, roles=frozenset({"is_hub"}))
    assert p.evaluate([AGENT], "hub.info", hub).rule == "signed"


def test_hard_invariants_and_modes():
    p = Policy({**DEFAULT_POLICY, "mode": "enforce"})
    assert p.evaluate([OWNER], "shell.exec", Target(id="x", name="x", banned=True)).rule \
        == "invariant:banned"
    impostor = Target(id="x", name="rook~x", claims_rook=True)
    assert p.evaluate([OWNER], "info.ping", impostor).decision == "deny"
    assert p.evaluate([OWNER], "shell.exec", Target()).rule == "invariant:broadcast"
    assert p.evaluate([OWNER], "info.ping", Target()).decision == "allow"
    audit = Policy(DEFAULT_POLICY)
    assert audit.evaluate([OWNER], "shell.exec", Target()).decision == "would_deny"
    off = Policy({**DEFAULT_POLICY, "mode": "off"})
    assert off.evaluate([TELEGRAM], "shell.exec", W_A).decision == "off"
    per = json.loads(json.dumps(DEFAULT_POLICY))
    per["mode"] = "enforce"
    per["principals"]["integration:*"]["mode"] = "audit"
    assert Policy(per).evaluate([TELEGRAM], "shell.exec", W_A).decision == "would_deny"


def test_policy_validation_and_lint():
    with pytest.raises(PolicyError):
        Policy({**DEFAULT_POLICY, "mode": "loud"})
    with pytest.raises(PolicyError):
        Policy({**DEFAULT_POLICY, "rules": [{"id": "x", "who": "*", "allow": "*", "deny": "*"}]})
    with pytest.raises(PolicyError):
        Policy({**DEFAULT_POLICY, "rules": [{"id": "x", "who": "group:nope", "allow": "*"}]})
    with pytest.raises(PolicyError):
        Policy({**DEFAULT_POLICY, "defaults": {"exec": "maybe"}})
    p = Policy({**DEFAULT_POLICY, "rules": [
        {"id": "a", "who": "role:agent", "allow": "shell.exec", "on": "worker-a"},
        {"id": "b", "who": "role:agent", "deny": "shell.exec", "on": "worker-b"},
        {"id": "cam", "who": "role:agent", "allow": "tier:exec", "on": "has(camera)"}]})
    lint = p.lint()
    assert any("tie" in m for m in lint) and any("self-reported" in m for m in lint)


def test_policy_store_reload_last_good_and_save_guard(tmp_path):
    path = tmp_path / "policy.json"
    store = PolicyStore(str(path), check_every=0)
    assert store.current().rev == 0 and store.source.startswith("built-in")
    new = store.save({**DEFAULT_POLICY, "mode": "enforce"})
    assert new.rev == 1 and json.loads(path.read_text())["mode"] == "enforce"
    path.write_text("{not json")
    import os
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert store.current().rev == 1 and store.error          # last good kept
    lockout = json.loads(json.dumps(DEFAULT_POLICY))
    for k in ("role:operator", "human:owner", "human:dashboard", "token:static"):
        lockout["principals"][k] = {"admin": "deny"}
    with pytest.raises(PolicyError, match="no principal would hold admin"):
        store.save(lockout)


def test_policy_evaluation_budget():
    p = _worked_example(["telegram-exec-lab", "telegram-no-exec", "no-sensitive", "no-camera"])
    start = time.perf_counter()
    for i in range(10000):
        p.evaluate([TELEGRAM], "shell.exec", W_A if i % 2 else W_C)
    per_call = (time.perf_counter() - start) / 10000
    assert per_call < 200e-6, per_call   # spec budget is 50us p99; generous for CI noise


def test_ticket_sign_and_verify_budget(root, op):
    g = _grant(root, op)
    kid = g["sub"]["kid"]
    args = {"cmd": "uptime", "timeout": 30}
    start = time.perf_counter()
    for i in range(1000):
        t = authz.make_ticket(op, kid, principal="token:a", cap="shell.exec", target="w1",
                              msg_id=f"m{i}", args=args, tier="exec", rev=1)
        assert authz.verify_ticket(t, cap="shell.exec", target="w1", msg_id=f"m{i}",
                                   args=args, keys={kid: g})[0]
    per_round = (time.perf_counter() - start) / 1000
    assert per_round < 2e-3, per_round   # spec: sign <=150us, verify <=1ms on an SBC


# -- hub-side enforcement in the band client ---------------------------------

def _client(tmp_path, doc=None, signer=None):
    from rook.band_mcp.client import BandClient
    from rook.hub.authz import Authorizer
    path = tmp_path / "policy.json"
    if doc is not None:
        path.write_text(json.dumps(doc))
    client = BandClient("test-band")
    client.transport.send = AsyncMock()
    rows = []
    client.authz = Authorizer(PolicyStore(str(path)), signer,
                              record=lambda **kw: rows.append(kw))
    client.workers["w1"] = {"worker_id": "w1", "name": "worker-a", "caps": ["shell.exec"],
                            "last_seen": time.time()}
    return client, rows


@pytest.mark.asyncio
async def test_band_client_enforce_denies_before_sending(tmp_path):
    from rook.hub.authz import current_principal
    client, rows = _client(tmp_path, {**DEFAULT_POLICY, "mode": "enforce"})
    tok = current_principal.set(TELEGRAM)
    try:
        reply = await client.call("shell.exec", {"cmd": "id"}, target="w1", timeout=1)
    finally:
        current_principal.reset(tok)
    assert not reply["ok"] and reply["denied"]["principal"] == "integration:telegram"
    client.transport.send.assert_not_called()
    assert rows and rows[0]["authz"]["decision"] == "deny"


@pytest.mark.asyncio
async def test_band_client_audit_mode_sends_with_signed_ticket(tmp_path, root, op):
    from rook.hub.authz import current_principal, last_decision
    from rook.hub.keys import HubSigner
    signer = HubSigner(root, op_dir=tmp_path / "keys")
    client, rows = _client(tmp_path, signer=signer)
    tok = current_principal.set(TELEGRAM)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await client.call("shell.exec", {"cmd": "id"}, target="w1", timeout=0.05)
        assert last_decision.get().decision == "would_deny"
    finally:
        current_principal.reset(tok)
    sent = json.loads(client.transport.send.call_args[0][0])
    t = sent["ticket"]
    assert t["p"] == "integration:telegram" and t["t"] == "w1" and t["grant"]
    assert rows[0]["authz"]["decision"] == "would_deny"
    # The ticket verifies the way a worker would check it.
    from rook.worker.authz_guard import Guard
    g = Guard("w1", client.band_hex, [authz.pub_b64(root)], mode="enforce-exec")
    info = g.check(sent, "shell.exec", "exec", sent["args"])
    assert info["verified"], info


@pytest.mark.asyncio
async def test_hub_announce_is_granted_and_impostors_quarantined(tmp_path, root):
    from rook.band_mcp.client import BandClient
    from rook.hub.keys import HubSigner
    signer = HubSigner(root, op_dir=tmp_path / "keys")
    band = BandClient("test-band").band_hex
    hub_msg = signer.decorate_announce({"kind": "announce", "worker_id": "hub1",
                                        "name": "rook", "caps": ["hub.info"]}, band)
    viewer, rows = _client(tmp_path)
    viewer._handle_announce(hub_msg)
    assert viewer.workers["hub1"]["name"] == "rook"
    assert viewer.workers["hub1"]["roles"] == ["is_hub"]
    fake = {"kind": "announce", "worker_id": "evil1234x", "name": "Rook", "caps": ["hub.info"],
            "grants": hub_msg["grants"]}
    viewer._handle_announce(fake)
    assert viewer.workers["evil1234x"]["name"] == "Rook~evil1234"
    assert viewer.workers["evil1234x"]["quarantined"]
    assert any(r["cap"] == "audit.impostor" for r in rows)
    # Workers learn the op key from the same announce.
    from rook.worker.authz_guard import Guard
    g = Guard("w1", band, [authz.pub_b64(root)])
    assert g.on_announce(hub_msg) and signer.kid in g.keys


def test_hub_signer_rotates_op_key_with_overlap(tmp_path, root):
    import os
    from rook.hub.keys import HubSigner, OP_KEY_NAME, OP_KEY_ROTATE_SECS
    s1 = HubSigner(root, op_dir=tmp_path)
    old = tmp_path / OP_KEY_NAME
    past = time.time() - OP_KEY_ROTATE_SECS - 10
    os.utime(old, (past, past))
    s2 = HubSigner(root, op_dir=tmp_path)
    assert s2.kid != s1.kid and s2.prev is not None
    grants = s2.grants(BAND)
    assert len(grants) == 2 and {g["sub"]["kid"] for g in grants} == {s1.kid, s2.kid}
    assert (os.stat(old).st_mode & 0o777) == 0o600


def test_no_root_key_means_no_tickets(tmp_path):
    from rook.hub.keys import HubSigner
    s = HubSigner(op_dir=tmp_path)          # conftest points ROOK_UPDATE_KEY at an empty dir
    assert not s.enabled and s.ticket(band=BAND, principal="p", via=[], cap="c", target="t",
                                      msg_id="m", args={}, tier="read", rev=0) is None
    # The dashboard may create the root key after this process started.
    from rook.remote.update_keys import ensure_key
    ensure_key()
    assert not s.ready()                               # retried at most once a minute
    assert s.ready(now=time.time() + s.RETRY_SECS + 1) and s.kid


def test_token_roles_at_mint(tmp_path):
    from rook.band_mcp.tokens import TokenStore
    store = TokenStore(persist_path=str(tmp_path / "t.json"), static_token="s" * 20)
    ro = store.mint_api_token("reader", role="readonly")
    assert store.principal_for(ro["token"])["role"] == "readonly"
    legacy = store.mint_api_token("old")
    assert store.principal_for(legacy["token"])["role"] == "agent"
    assert store.principal_for("s" * 20)["role"] == "operator"
    with pytest.raises(ValueError):
        store.mint_api_token("x", role="root")
    rotated = store.rotate_api_token(ro["id"])
    assert store.principal_for(rotated["token"])["role"] == "readonly"
    # Work-session tokens (scoped, 1-day) are agent tokens.
    work = store.mint_api_token("work:claude:0123abcd", ttl_seconds=86400,
                                scopes=["rook", "work-session:" + "a" * 32])
    assert store.principal_for(work["token"])["role"] == "agent"
    assert work["scopes"][1].startswith("work-session:") and work["expires_at"]
    assert authz.effective_tier("work.stream.open") == "exec"
    assert authz.effective_tier("work.export") == "read" and "sensitive" in authz.builtin_tags("work.export")


def test_hub_tool_mapping():
    from rook.hub.authz import hub_cap_for_tool
    assert hub_cap_for_tool("rook_secret", {"action": "get"}) == "secret.get"
    assert hub_cap_for_tool("rook_secret", {}) == "secret.list"
    assert hub_cap_for_tool("rook_knowledge", {"action": "create"}) == "knowledge.write"
    assert hub_cap_for_tool("rook_task", {}) == "task.read"
    assert hub_cap_for_tool("rook_call", {"cap": "shell.exec"}) is None
    assert authz.effective_tier(hub_cap_for_tool("rook_secret", {"action": "get"})) == "admin"


def test_policy_set_is_owner_or_operator_only(tmp_path):
    from rook.hub.authz import Authorizer, current_principal
    from rook.hub.plugins.policy import PolicyPlugin
    store = PolicyStore(str(tmp_path / "policy.json"))
    plugin = PolicyPlugin()
    plugin.bind_host(SimpleNamespace(client=SimpleNamespace(
        authz=Authorizer(store, None), workers={}), worker_id="hub", entry=lambda: {}))
    doc = {**DEFAULT_POLICY, "mode": "enforce"}
    for who, ok in ((AGENT, False), (None, False), (OWNER, True),
                    (Principal("token:static", "token", "operator"), True)):
        tok = current_principal.set(who)
        try:
            assert plugin.set(doc)["ok"] is ok, who
        finally:
            current_principal.reset(tok)
    assert store.current().mode == "enforce"
    explained = plugin.explain("role:agent", "worker.restart", "worker-x")
    assert explained["decision"] == "would_deny" and explained["tier"] == "admin"
    assert plugin.get()["mode"] == "enforce"
