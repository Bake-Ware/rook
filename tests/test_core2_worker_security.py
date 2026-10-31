"""Worker-side enforcement (rook/core2/worker/security.py): the conformance
vectors through WorkerGate, the replay store, the trust store, hub keys and
contact, elevation and the launch path."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("nacl")
from nacl.signing import SigningKey  # noqa: E402

from rook.core2 import wire  # noqa: E402
from rook.core2.worker import security as sec  # noqa: E402

VEC = Path(__file__).resolve().parents[1] / "conformance" / "core-v2" / "vectors"
NOW = 1793300000


def vec(name: str) -> dict:
    return json.loads((VEC / f"{name}.json").read_text(encoding="utf-8"))


KEYS = {k["name"]: SigningKey(bytes.fromhex(k["seed"])) for k in vec("signatures")["keys"]}
HOME_ROOT, HOME_OP = KEYS["home-root"], KEYS["home-op"]
GUEST_ROOT, GUEST_OP = KEYS["guest-root"], KEYS["guest-op"]
ATTACKER = KEYS["attacker"]
HOME = wire.key_id(bytes(HOME_ROOT.verify_key))
GUEST = wire.key_id(bytes(GUEST_ROOT.verify_key))
TV = vec("tickets")
WORKER = TV["worker"]["worker_id"]
HOME_BAND = TV["worker"]["trust"][HOME]["band"]
GUEST_BAND = TV["worker"]["trust"][GUEST]["band"]
CAPS = {**TV["worker"]["caps"],
        "pkg.install": {"risk": "admin", "effects": [], "elevation": "upfront"},
        "fs.write": {"risk": "write", "effects": [], "elevation": "may"},
        "core.lease.drop": {"risk": "write", "effects": []}}


def arun(coro, seconds=10):
    """Run a coroutine with a hard timeout so no test can hang CI."""
    return asyncio.run(asyncio.wait_for(coro, seconds))


def grant(root, op, band, iat=NOW - 60, exp=NOW + 7 * 86400):
    return wire.sign(root, {"typ": "rook-hubkey", "v": 2, "hub": wire.key_id(bytes(root.verify_key)),
                            "key": wire.pub_text(op), "bands": [band], "iat": iat, "exp": exp})


def membership(hub_root, band, role="guest", ceiling=None, iat=NOW, signer=HOME_ROOT, worker=WORKER):
    doc = {"typ": "rook-membership", "v": 2, "serial": "ab" * 16, "worker": worker,
           "hub": wire.key_id(bytes(hub_root.verify_key)), "root": wire.pub_text(hub_root),
           "band": band, "role": role, "by": "person:bake", "iat": iat}
    if role != "removed":
        doc["ceiling"] = ceiling if ceiling is not None else TV["worker"]["trust"][GUEST]["ceiling"]
    return wire.sign(signer, doc)


def ticket(cap="shell.exec", args=None, *, op=HOME_OP, root=HOME_ROOT, band=HOME_BAND, iat=NOW,
           nonce=None, call="5d1c0e8a9b7f4c2e8a61f0b3c4d5e6f7", session="s_7c1e", **extra):
    t = {"typ": "rook-ticket", "v": 2, "hub": wire.key_id(bytes(root.verify_key)), "band": band,
         "call": call, "nonce": nonce or os.urandom(16).hex(), "worker": WORKER, "cap": cap,
         "args_hash": wire.args_hash(args), "person": "person:bake", "agent": "agent:claude-code",
         "session": session, "why": "test", "iat": iat, "exp": iat + 60, **extra}
    return wire.sign(op, t)


def request(t, args=None):
    return {"id": t["call"], "cap": t["cap"], "target": WORKER, "args": args, "ticket": t}


def anchor_file(tmp_path, **over):
    p = tmp_path / "anchor.json"
    p.write_text(json.dumps({"hub": HOME, "root": wire.pub_text(HOME_ROOT), "band": HOME_BAND, **over}))
    return p


def make_gate(tmp_path, ask=None, guest=True, now=NOW):
    trust = sec.TrustStore(anchor_file(tmp_path), tmp_path / "memberships.json", WORKER)
    gate = sec.WorkerGate(WORKER, TV["worker"]["name"], trust, sec.ReplayStore(tmp_path / "nonces", now),
                          CAPS, ask)
    assert gate.add_hubkey(HOME_BAND, grant(HOME_ROOT, HOME_OP, HOME_BAND), now)
    if guest:
        assert trust.set_membership(membership(GUEST_ROOT, GUEST_BAND), {}) == (True, "ok")
        assert gate.add_hubkey(GUEST_BAND, grant(GUEST_ROOT, GUEST_OP, GUEST_BAND), now)
    return gate


# -- conformance vectors ------------------------------------------------------

@pytest.mark.parametrize("case", TV["cases"], ids=[c["name"] for c in TV["cases"]])
def test_ticket_vectors_through_the_gate(tmp_path, case):
    gate = make_gate(tmp_path, now=case["now"])
    for n in case["seen"]:
        gate.replay.add(n, case["now"], case["now"])
    req = dict(case["request"])
    if case["ticket"] is not None:
        req["ticket"] = case["ticket"]
    out = gate.check(req, case["band"], case["now"])
    if case["reason"] == "ok":
        assert isinstance(out, sec.Call), out
    elif case["reason"] == "unknown_cap":
        assert out["error"] == f"unknown capability: {req['cap']}"
    else:
        assert out == {"id": req["id"], "from": WORKER, "ok": False,
                       "error": f"denied by worker: {case['reason']}"}


@pytest.mark.parametrize("case", vec("memberships")["cases"], ids=lambda c: c["name"])
def test_membership_vectors_through_the_trust_store(tmp_path, case):
    store = sec.TrustStore(anchor_file(tmp_path), tmp_path / "m.json", WORKER)
    op = {wire.key_id(bytes(HOME_OP.verify_key)): wire.pub_text(HOME_OP)}
    ok, reason = store.set_membership(case["document"], op, grant(HOME_ROOT, HOME_OP, HOME_BAND))
    assert reason == case["reason"] and ok is (case["reason"] == "ok")
    if ok:   # and it survives a restart, re-verified from disk
        again = sec.TrustStore(anchor_file(tmp_path), tmp_path / "m.json", WORKER)
        assert again.docs == store.docs and not again.skipped


@pytest.mark.parametrize("case", vec("leases")["cases"], ids=lambda c: c["name"])
def test_lease_vectors_through_elevation(tmp_path, case):
    lv = vec("leases")

    async def ask(band, args):
        return {"ok": True, "result": {"lease": case["lease"]}}

    gate = make_gate(tmp_path, ask)
    gate.contact[(HOME, HOME_BAND)] = case["now"]
    call = sec.Call(request(lv["ticket"]), lv["ticket"], HOME_BAND, gate.trust.home, CAPS["shell.exec"])
    if case["reason"] == "ok":
        assert arun(gate.elevate(call, "upfront", case["now"])) == case["lease"]
    else:
        with pytest.raises(sec.ElevationError, match=case["reason"]):
            arun(gate.elevate(call, "upfront", case["now"]))
        assert not gate.leases


# -- tickets ------------------------------------------------------------------

def test_silent_when_not_addressed(tmp_path):
    gate = make_gate(tmp_path)
    t = ticket()
    assert gate.check({**request(t), "target": "0" * 32}, HOME_BAND, NOW) is None
    assert gate.check("junk", HOME_BAND, NOW) is None


def test_ok_then_replay_and_context(tmp_path):
    gate = make_gate(tmp_path)
    t = ticket(args={"argv": ["uptime"]})
    call = gate.check(request(t, {"argv": ["uptime"]}), HOME_BAND, NOW)
    assert isinstance(call, sec.Call) and call.member.role == "home"
    ctx = call.context()
    assert ctx["person"] == "person:bake" and ctx["risk"] == "exec" and ctx["hub"] == HOME
    assert call.context("l1")["risk"] == "admin" and ctx["owner"] is False
    again = gate.check(request(t, {"argv": ["uptime"]}), HOME_BAND, NOW + 5)
    assert again["error"] == "denied by worker: replay"
    assert not gate.lost(HOME, HOME_BAND, NOW + 90) and gate.lost(HOME, HOME_BAND, NOW + 91)


def test_the_owner_claim_counts_only_from_a_hub_that_manages_the_worker(tmp_path):
    gate = make_gate(tmp_path)
    home = gate.check(request(ticket("shell.exec", owner=True)), HOME_BAND, NOW)
    assert home.context()["owner"] is True
    guest = gate.check(request(ticket("core.manifests", op=GUEST_OP, root=GUEST_ROOT, band=GUEST_BAND,
                                      owner=True)), GUEST_BAND, NOW)
    assert isinstance(guest, sec.Call) and guest.context()["owner"] is False   # a guest hub's owners aren't ours
    nobody = gate.check(request(ticket("shell.exec", owner=True, person=None)), HOME_BAND, NOW)
    assert nobody["error"] == "denied by worker: malformed"


def test_unknown_hub_key_and_forgery(tmp_path):
    gate = make_gate(tmp_path)
    # an attacker on the band signs as itself, and inline-attaches its own grant
    t = ticket(op=ATTACKER, root=ATTACKER, hubkey=grant(ATTACKER, ATTACKER, HOME_BAND))
    assert gate.check(request(t), HOME_BAND, NOW)["error"] == "denied by worker: unknown_hub"
    # the attacker claims the home hub but its key was never certified by the home root
    t = ticket(op=ATTACKER, hubkey=grant(ATTACKER, ATTACKER, HOME_BAND))
    assert gate.check(request(t), HOME_BAND, NOW)["error"] == "denied by worker: unknown_key"


def test_inline_hubkey_teaches_a_new_operational_key(tmp_path):
    gate = make_gate(tmp_path)
    new_op = SigningKey(b"\x07" * 32)
    t = ticket(op=new_op)
    assert gate.check(request(t), HOME_BAND, NOW)["error"] == "denied by worker: unknown_key"
    t = ticket(op=new_op, hubkey=grant(HOME_ROOT, new_op, HOME_BAND))
    assert isinstance(gate.check(request(t), HOME_BAND, NOW), sec.Call)


def test_rotated_out_key_stops_working(tmp_path):
    gate = make_gate(tmp_path)
    old = SigningKey(b"\x08" * 32)
    assert gate.add_hubkey(HOME_BAND, grant(HOME_ROOT, old, HOME_BAND, exp=NOW + 100), NOW)
    later = NOW + 100 + wire.GRANT_GRACE + 1
    t = ticket(op=old, iat=later)
    assert gate.check(request(t), HOME_BAND, later)["error"] == "denied by worker: unknown_key"


def test_grant_for_another_band_is_ignored(tmp_path):
    gate = make_gate(tmp_path)
    new_op = SigningKey(b"\x09" * 32)
    assert not gate.add_hubkey(HOME_BAND, grant(HOME_ROOT, new_op, GUEST_BAND), NOW)
    assert not gate.add_hubkey(GUEST_BAND, grant(HOME_ROOT, new_op, GUEST_BAND), NOW)   # home isn't on guest band


def test_ceilings_per_hub(tmp_path):
    gate = make_gate(tmp_path)
    t = ticket("shell.exec", op=GUEST_OP, root=GUEST_ROOT, band=GUEST_BAND)
    assert gate.check(request(t), GUEST_BAND, NOW)["error"] == "denied by worker: outside_ceiling"
    t = ticket("shell.exec")
    assert isinstance(gate.check(request(t), HOME_BAND, NOW), sec.Call)
    # the guest hub's ticket names the home band: wrong hub for that band
    t = ticket("shell.exec", op=GUEST_OP, root=GUEST_ROOT, band=HOME_BAND)
    assert gate.check(request(t), HOME_BAND, NOW)["error"] == "denied by worker: unknown_hub"


def test_home_ceiling_narrowed_then_guest_removed(tmp_path):
    gate = make_gate(tmp_path)
    narrow = membership(HOME_ROOT, HOME_BAND, role="home", ceiling={"grants": ["light.*"]}, iat=NOW + 1)
    assert gate.trust_set(narrow, NOW) == (True, "ok")
    t = ticket("shell.exec")
    assert gate.check(request(t), HOME_BAND, NOW)["error"] == "denied by worker: outside_ceiling"
    # core.manifests stays within every ceiling
    assert isinstance(gate.check(request(ticket("core.manifests")), HOME_BAND, NOW), sec.Call)
    gone = membership(GUEST_ROOT, GUEST_BAND, role="removed", iat=NOW + 1)
    assert gate.trust_set(gone, NOW) == (True, "ok")
    t = ticket("light.set", op=GUEST_OP, root=GUEST_ROOT, band=GUEST_BAND)
    assert gate.check(request(t), GUEST_BAND, NOW)["error"] == "denied by worker: unknown_hub"


def test_incompatible_hub_serves_nothing(tmp_path):
    gate = make_gate(tmp_path)
    ann = wire.sign(HOME_OP, {"kind": "announce", "typ": "rook-announce", "v": 2, "worker_id": "h" * 32,
                              "name": "rook", "hub": HOME, "band": HOME_BAND, "core": "3.0", "ts": NOW,
                              "seq": 1})
    assert gate.hub_announce(HOME_BAND, ann, NOW)
    assert gate.check(request(ticket()), HOME_BAND, NOW)["error"] == "denied by worker: incompatible"
    # the guest band negotiates on its own
    t = ticket("core.manifests", op=GUEST_OP, root=GUEST_ROOT, band=GUEST_BAND)
    assert isinstance(gate.check(request(t), GUEST_BAND, NOW), sec.Call)


# -- replay store -------------------------------------------------------------

def test_replay_survives_restart(tmp_path):
    p = tmp_path / "nonces"
    a = sec.ReplayStore(p, NOW)
    assert not a.seen("a" * 32, NOW)
    a.add("a" * 32, NOW + 60, NOW)
    b = sec.ReplayStore(p, NOW + 10)
    assert b.seen("a" * 32, NOW + 10) and not b.seen("b" * 32, NOW + 10)
    assert not b.seen("a" * 32, NOW + wire.REPLAY_KEEP + sec.BUCKET)   # aged out (kept to its 10 s set end)


def test_replay_cold_start_when_unknown(tmp_path):
    mem = sec.ReplayStore(None, NOW)
    assert mem.seen("c" * 32, NOW + sec.COLD_START - 1) and not mem.seen("c" * 32, NOW + sec.COLD_START)
    p = tmp_path / "nonces"
    p.write_text("a" * 32 + f" {NOW + 900}\n" + "torn-li")
    torn = sec.ReplayStore(p, NOW)
    assert torn.seen("b" * 32, NOW) and torn.seen("a" * 32, NOW + sec.COLD_START)
    assert sec.ReplayStore(tmp_path / "fresh", NOW).seen("b" * 32, NOW) is False


def test_replay_store_is_bounded_and_compacts(tmp_path):
    p = tmp_path / "nonces"
    s = sec.ReplayStore(p, NOW, max_entries=3)
    for i in range(3):
        s.add(f"{i:032x}", NOW, NOW)
    with pytest.raises(OverflowError):
        s.add("f" * 32, NOW, NOW)
    later = NOW + wire.REPLAY_KEEP + 1
    s.add("e" * 32, later, later)                 # old entries pruned, room again
    assert s.size == 1 and s.seen("e" * 32, later)
    for i in range(1100):
        s.max = 10**6
        s.add(f"{i + 100:032x}", later, later + 2000 + i * 1000)   # each one ages out the last
    assert len(p.read_text().splitlines()) < 1100


def test_replay_store_never_fills_under_sustained_load(tmp_path):
    """40 calls/s for 20 minutes against a 30,000 bound: the old store (every
    nonce 15 minutes, refused when full) refused everything from minute 12.
    Now nonces older than 10 minutes make room, and every nonce younger than
    that is still caught."""
    s = sec.ReplayStore(None, NOW - sec.COLD_START, max_entries=30_000)    # memory only: no fsyncs
    refused, n = 0, 0
    for second in range(20 * 60):
        now = NOW + second
        for _ in range(40):
            nonce = f"{n:032x}"
            n += 1
            assert not s.seen(nonce, now)
            try:
                s.add(nonce, now + 60, now)
            except OverflowError:
                refused += 1
        if second >= sec.REPLAY_FLOOR:              # accepted 10 minutes ago: still held
            assert s.seen(f"{n - 40 * sec.REPLAY_FLOOR:032x}", now)
    assert refused == 0 and s.size <= 30_000


def test_replay_store_refuses_only_nonces_younger_than_its_floor(tmp_path):
    s = sec.ReplayStore(None, NOW - sec.COLD_START, max_entries=3)
    for i in range(3):
        s.add(f"{i:032x}", NOW + 60, NOW)
    with pytest.raises(OverflowError):            # all three younger than 10 minutes
        s.add("f" * 32, NOW + 60, NOW + sec.REPLAY_FLOOR - 1)
    later = NOW + sec.REPLAY_FLOOR + sec.BUCKET
    s.add("f" * 32, NOW + 60, later)              # their bucket is old enough: it goes
    assert s.size == 1 and s.seen("f" * 32, later) and not s.seen("0" * 32, later)


def test_full_replay_store_refuses_the_call(tmp_path):
    gate = make_gate(tmp_path)
    gate.replay.max = 0
    assert gate.check(request(ticket()), HOME_BAND, NOW)["error"] == "denied by worker: replay"


# -- trust store --------------------------------------------------------------

def test_anchor_must_match_its_root(tmp_path):
    with pytest.raises(ValueError):
        sec.TrustStore(anchor_file(tmp_path, root=None), None, WORKER)
    with pytest.raises(ValueError):
        sec.TrustStore(anchor_file(tmp_path, hub=GUEST), None, WORKER)
    with pytest.raises(ValueError):
        sec.TrustStore(anchor_file(tmp_path, band="xyz"), None, WORKER)


def test_anchor_protection_is_reported(tmp_path):
    p = anchor_file(tmp_path)
    assert sec.TrustStore(p, None, WORKER).anchor_protected is (not os.access(p, os.W_OK))
    os.chmod(p, 0o444)
    try:
        if os.geteuid() != 0:
            assert sec.TrustStore(p, None, WORKER).anchor_protected
    finally:
        os.chmod(p, 0o644)


def test_no_rollback_and_tampered_file(tmp_path):
    store = sec.TrustStore(anchor_file(tmp_path), tmp_path / "m.json", WORKER)
    assert store.set_membership(membership(GUEST_ROOT, GUEST_BAND, iat=NOW + 5), {})[0]
    assert store.set_membership(membership(GUEST_ROOT, GUEST_BAND, iat=NOW + 5), {}) == (False, "not_newer")
    assert store.set_membership(membership(GUEST_ROOT, GUEST_BAND, iat=NOW), {}) == (False, "not_newer")
    # widening the ceiling on disk breaks the signature: the entry is dropped on load
    data = json.loads((tmp_path / "m.json").read_text())
    data["memberships"][0]["doc"]["ceiling"] = {"grants": ["*"]}
    (tmp_path / "m.json").write_text(json.dumps(data))
    again = sec.TrustStore(anchor_file(tmp_path), tmp_path / "m.json", WORKER)
    assert again.member(GUEST, GUEST_BAND) is None and again.skipped == ["bad_signature"]
    (tmp_path / "m.json").write_text(json.dumps({"memberships": ["junk"]}))
    assert sec.TrustStore(anchor_file(tmp_path), tmp_path / "m.json", WORKER).skipped == ["malformed"]


def test_op_signed_membership_needs_its_grant(tmp_path):
    store = sec.TrustStore(anchor_file(tmp_path), None, WORKER)
    op = {wire.key_id(bytes(HOME_OP.verify_key)): wire.pub_text(HOME_OP)}
    doc = membership(GUEST_ROOT, GUEST_BAND, signer=HOME_OP)
    assert store.set_membership(doc, op) == (False, "no_grant")
    # a grant from after the document was issued doesn't vouch for it
    late = grant(HOME_ROOT, HOME_OP, HOME_BAND, iat=NOW + 2 * wire.GRANT_GRACE)
    assert store.set_membership(doc, op, late) == (False, "no_grant")
    assert store.set_membership(doc, op, grant(HOME_ROOT, HOME_OP, HOME_BAND)) == (True, "ok")


def test_gate_trust_set_uses_the_known_grant(tmp_path):
    gate = make_gate(tmp_path, guest=False)
    doc = membership(GUEST_ROOT, GUEST_BAND, signer=HOME_OP)
    assert gate.trust_set(doc, NOW) == (True, "ok")
    assert gate.trust.describe()["memberships"][0]["role"] == "guest"
    assert "psk" not in json.dumps(gate.trust.describe())


def test_trust_set_only_from_home(tmp_path):
    gate = make_gate(tmp_path)
    t = ticket("core.trust.set", op=GUEST_OP, root=GUEST_ROOT, band=GUEST_BAND)
    assert gate.check(request(t), GUEST_BAND, NOW)["error"] == "denied by worker: outside_ceiling"


# -- hub announces and contact ------------------------------------------------

def announce(seq, ts=NOW, op=HOME_OP, band=HOME_BAND, **extra):
    return wire.sign(op, {"kind": "announce", "typ": "rook-announce", "v": 2, "worker_id": "a" * 32,
                          "name": "rook", "hub": HOME, "band": band, "core": "2.0", "ts": ts,
                          "seq": seq, **extra})


def test_hub_announce_rules(tmp_path):
    gate = make_gate(tmp_path)
    assert gate.hub_announce(HOME_BAND, announce(5), NOW)
    assert not gate.hub_announce(HOME_BAND, announce(5), NOW)            # seq must grow
    assert not gate.hub_announce(HOME_BAND, announce(6, ts=NOW - 91), NOW)
    assert not gate.hub_announce(GUEST_BAND, announce(7), NOW)           # arrived on another band
    forged = {**announce(8), "core": "2.1"}
    assert not gate.hub_announce(HOME_BAND, forged, NOW)
    assert not gate.hub_announce(HOME_BAND, announce(9, op=ATTACKER), NOW)
    assert gate.versions[HOME_BAND] == "2.0" and gate.contact[(HOME, HOME_BAND)] == NOW


# -- elevation ----------------------------------------------------------------

def lease_for(t, now=NOW, lid=None, exp=None, **over):
    body = {"typ": "rook-lease", "v": 2, "id": lid or os.urandom(16).hex(), "hub": t["hub"],
            "band": t["band"], "worker": t["worker"], "person": t.get("person"), "agent": t.get("agent"),
            "session": t["session"], "iat": now, "exp": exp or now + wire.LEASE_SECS, **over}
    return wire.sign(HOME_OP, body)


class Hub:
    """A fake hub for ``ask``: counts calls, answers with a lease, a refusal,
    an exception or never."""

    def __init__(self, mode="lease"):
        self.mode, self.calls = mode, []

    async def __call__(self, band, args):
        self.calls.append((band, args))
        if self.mode == "hang":
            await asyncio.sleep(3600)
        if self.mode == "down":
            raise ConnectionError("no route")
        if self.mode == "deny":
            return {"ok": False, "error": "elevation: denied"}
        if self.mode == "ceiling":
            return {"ok": False, "error": "elevation: outside ceiling"}
        if self.mode == "bad":
            return {"ok": True, "result": {"lease": lease_for({**args["ticket"], "session": "other"})}}
        return {"ok": True, "result": {"lease": lease_for(args["ticket"])}}


def checked(gate, cap, args=None, **kw):
    t = ticket(cap, args, **kw)
    call = gate.check(request(t, args), HOME_BAND, NOW)
    assert isinstance(call, sec.Call), call
    return call


def test_upfront_elevation_and_reuse(tmp_path):
    hub = Hub()
    gate = make_gate(tmp_path, hub)
    seen = []

    async def action(lease):
        seen.append(lease and lease["id"])
        return "done"

    out = arun(gate.execute(checked(gate, "pkg.install"), action, NOW))
    assert out["ok"] and out["result"] == "done" and out["elevated"] == seen[0]
    assert hub.calls[0][1]["reason"] == "upfront" and hub.calls[0][0] == HOME_BAND
    # a second call in the same session reuses the lease: no hub round trip
    out = arun(gate.execute(checked(gate, "pkg.install"), action, NOW + 10))
    assert out["elevated"] == seen[0] and len(hub.calls) == 1
    # another session doesn't get it
    arun(gate.execute(checked(gate, "pkg.install", session="s_other"), action, NOW + 10))
    assert len(hub.calls) == 2


def test_may_reruns_once_on_access_denied(tmp_path):
    hub = Hub()
    gate = make_gate(tmp_path, hub)
    runs = []

    async def action(lease):
        runs.append(lease)
        if lease is None:
            raise PermissionError("EACCES")
        return "written"

    out = arun(gate.execute(checked(gate, "fs.write"), action, NOW))
    assert out["ok"] and out["elevated"] and len(runs) == 2 and hub.calls[0][1]["reason"] == "access_denied"


def test_never_cap_does_not_elevate(tmp_path):
    hub = Hub()
    gate = make_gate(tmp_path, hub)

    async def action(lease):
        raise sec.NeedsElevation()

    with pytest.raises(sec.NeedsElevation):
        arun(gate.execute(checked(gate, "shell.exec"), action, NOW))
    # elevate: true on a never cap runs normally
    async def ok(lease):
        return lease
    out = arun(gate.execute(checked(gate, "shell.exec", elevate=True), ok, NOW))
    assert out["result"] is None and not hub.calls


def test_ticket_elevate_flag_on_may_cap(tmp_path):
    hub = Hub()
    gate = make_gate(tmp_path, hub)

    async def action(lease):
        return bool(lease)

    out = arun(gate.execute(checked(gate, "fs.write", elevate=True), action, NOW))
    assert out["result"] is True and hub.calls[0][1]["reason"] == "upfront"


@pytest.mark.parametrize("mode,reason", [("down", "hub unreachable"), ("deny", "denied"),
                                         ("ceiling", "outside ceiling"), ("hang", "hub unreachable"),
                                         ("bad", "invalid lease (wrong_session)")])
def test_elevation_fails_closed(tmp_path, monkeypatch, mode, reason):
    monkeypatch.setattr(sec, "ELEVATE_TIMEOUT", 0.2)
    gate = make_gate(tmp_path, Hub(mode))
    ran = []

    async def action(lease):
        ran.append(lease)

    out = arun(gate.execute(checked(gate, "pkg.install"), action, NOW))
    assert out == {"id": out["id"], "from": WORKER, "ok": False, "error": f"elevation: {reason}"}
    assert not ran and not gate.leases


def test_no_hub_connection_fails_closed(tmp_path):
    gate = make_gate(tmp_path, ask=None)

    async def action(lease):
        return "never"

    out = arun(gate.execute(checked(gate, "pkg.install"), action, NOW))
    assert out["error"] == "elevation: hub unreachable"


def test_ceiling_without_core_elevate_never_asks(tmp_path):
    hub = Hub()
    gate = make_gate(tmp_path, hub)
    narrow = membership(HOME_ROOT, HOME_BAND, role="home", ceiling={"grants": ["pkg.*"]}, iat=NOW + 1)
    assert gate.trust_set(narrow, NOW)[0]

    async def action(lease):
        return "never"

    out = arun(gate.execute(checked(gate, "pkg.install"), action, NOW))
    assert out["error"] == "elevation: outside ceiling" and not hub.calls


def test_lease_dropped_on_hub_loss(tmp_path):
    hub = Hub()
    gate = make_gate(tmp_path, hub)

    async def action(lease):
        return lease["id"]

    first = arun(gate.execute(checked(gate, "pkg.install"), action, NOW))["result"]
    assert first in gate.leases
    gate.tick(NOW + sec.HUB_LOST + 1)
    assert not gate.leases
    # contact comes back (a fresh ticket): the old lease stays gone, a new one is asked for
    t = ticket("pkg.install", iat=NOW + 200)
    call = gate.check(request(t), HOME_BAND, NOW + 200)
    second = arun(gate.execute(call, action, NOW + 200))["result"]
    assert second != first and len(hub.calls) == 2


def test_lease_drop_from_hub(tmp_path):
    gate = make_gate(tmp_path, Hub())

    async def action(lease):
        return lease["id"]

    a = arun(gate.execute(checked(gate, "pkg.install"), action, NOW))["result"]
    arun(gate.execute(checked(gate, "pkg.install", session="s2"), action, NOW))
    assert gate.drop_leases(GUEST, HOME_BAND, {}) == {"dropped": 0}       # another hub's leases are not its own
    assert gate.drop_leases(HOME, HOME_BAND, {"lease": a}) == {"dropped": 1}
    assert gate.drop_leases(HOME, HOME_BAND, {"session": "s2"}) == {"dropped": 1}
    assert not gate.leases


def test_trust_change_drops_that_hubs_leases(tmp_path):
    gate = make_gate(tmp_path, Hub())

    async def action(lease):
        return lease["id"]

    arun(gate.execute(checked(gate, "pkg.install"), action, NOW))
    narrow = membership(HOME_ROOT, HOME_BAND, role="home", ceiling={"grants": ["*"]}, iat=NOW + 1)
    assert gate.trust_set(narrow, NOW)[0] and not gate.leases


def test_lease_expires(tmp_path):
    gate = make_gate(tmp_path, Hub())

    async def action(lease):
        return lease["id"]

    arun(gate.execute(checked(gate, "pkg.install"), action, NOW))
    gate.contact[(HOME, HOME_BAND)] = NOW + wire.LEASE_SECS + 1
    gate.tick(NOW + wire.LEASE_SECS + 1)
    assert not gate.leases


# -- launch path --------------------------------------------------------------

def test_launch_plan_linux():
    lease = {"id": "l"}
    assert sec.launch_plan(["id"], None, platform="linux", admin=False) == (["id"], {})
    assert sec.launch_plan(["id"], lease, platform="linux", admin=False) == (["sudo", "-n", "--", "id"], {})
    assert sec.launch_plan(["id"], lease, platform="linux", admin=True) == (["id"], {})
    with pytest.raises(sec.LaunchRefused):
        sec.launch_plan(["id"], None, platform="linux", admin=True)
    import pwd
    me = pwd.getpwuid(os.getuid())
    argv, kw = sec.launch_plan(["id"], None, platform="linux", admin=True, run_as=me.pw_name)
    assert argv == ["id"] and kw["user"] == me.pw_uid and kw["group"] == me.pw_gid
    with pytest.raises(ValueError):
        sec.launch_plan([], None, platform="linux", admin=False)


def test_root_without_run_as_never_elevates(tmp_path, monkeypatch):
    hub = Hub()
    gate = make_gate(tmp_path, hub)
    monkeypatch.setattr(sec, "_is_admin", lambda platform: True)

    async def action(lease):
        await sec.launch(["true"], lease)

    with pytest.raises(sec.LaunchRefused):
        arun(gate.execute(checked(gate, "fs.write"), action, NOW))
    assert not hub.calls


def test_launch_plan_other_platforms_are_stubbed():
    assert sec.launch_plan(["cmd"], None, platform="win32", admin=False) == (["cmd"], {})
    with pytest.raises(sec.LaunchRefused):
        sec.launch_plan(["cmd"], None, platform="win32", admin=True)
    with pytest.raises(sec.LaunchUnsupported):
        sec.launch_plan(["cmd"], {"id": "l"}, platform="win32", admin=False)
    with pytest.raises(sec.LaunchUnsupported):
        sec.launch_plan(["ls"], {"id": "l"}, platform="darwin", admin=False)


@pytest.mark.skipif(not sys.platform.startswith("linux") or os.geteuid() == 0, reason="needs a normal Linux user")
def test_launch_runs_as_the_normal_user():
    async def go():
        proc = await sec.launch([sys.executable, "-c", "import os; print(os.geteuid())"],
                                stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        return out
    assert int(arun(go())) == os.geteuid()


def test_looks_denied():
    assert sec.looks_denied(126)
    assert sec.looks_denied(1, b"mkdir: cannot create directory '/etc/x': Permission denied")
    assert sec.looks_denied(1, "This command must be run as root")
    assert not sec.looks_denied(0, "permission denied")
    assert not sec.looks_denied(1, "no such file")
