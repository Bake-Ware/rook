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
    from rook.core.pluginkit import CapError
    p = ConfigPlugin()
    with pytest.raises(CapError):
        await p._apply({"psk": "x"}, epoch=1, restart=False)
    assert not (tmp_path / "config.json").exists()
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


@pytest.mark.asyncio
async def test_worker_reports_cap_errors_without_a_type_prefix(worker):
    """pluginkit.fail()/CapError reach the caller as the bare message; an
    unexpected exception keeps the type name so it reads as a bug."""
    from rook.core import pluginkit as kit

    def boom():
        kit.fail("file not found: /nope")

    def oops():
        raise RuntimeError("bad")

    worker.registry.register("t.boom", boom)
    worker.registry.register("t.oops", oops)
    worker.registry.register("t.arg", lambda: kit.require_int("x", "n"))
    for mid, cap in (("b", "t.boom"), ("o", "t.oops"), ("a", "t.arg")):
        await worker._on_message(json.dumps({"id": mid, "cap": cap, "args": {},
                                             "target": worker.worker_id}).encode(), ())
    by_id = {m["id"]: m for m in worker.transport.sent if "id" in m}
    assert by_id["b"]["error"] == "file not found: /nope"
    assert by_id["o"]["error"] == "RuntimeError: bad"
    assert by_id["a"]["error"] == "bad args: n must be an integer"
