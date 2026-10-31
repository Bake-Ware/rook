#!/usr/bin/env python3
"""Write the Rook core v2 conformance vectors (conformance/core-v2/vectors).

Every case's expected outcome is written by hand below; this script only
computes the parts a person can't (hashes, keys, signatures), checks every
expectation against the reference (rook.core2), and refuses to write if
they disagree.

    python conformance/core-v2/generate.py          # rewrite vectors/
    python conformance/core-v2/generate.py --check  # exit 1 if stale or wrong
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from nacl.signing import SigningKey  # noqa: E402

from rook.core2 import CORE_VERSION, journal, rights, wire  # noqa: E402

OUT = Path(__file__).resolve().parent / "vectors"


def key(name: str) -> SigningKey:
    return SigningKey(hashlib.sha256(b"rook-core-v2-test:" + name.encode()).digest())


KEYS = {n: key(n) for n in ("home-root", "home-op", "guest-root", "guest-op", "attacker", "worker", "worker-b")}
PUB = {n: wire.pub_text(k) for n, k in KEYS.items()}
KID = {n: wire.key_id(p) for n, p in PUB.items()}
HOME, GUEST = KID["home-root"], KID["guest-root"]
BAND = hashlib.sha256(b"conformance-band-psk").digest()[:16].hex()
GUEST_BAND = hashlib.sha256(b"conformance-guest-psk").digest()[:16].hex()
WORKER = "3f0a9c1d2e3f40516273849506a7b8c9"
CALL = "5d1c0e8a9b7f4c2e8a61f0b3c4d5e6f7"
NONCE = "9b1f2e3d4c5b6a798877665544332211"
T0 = 1793300000


def check(cond: bool, what: str) -> None:
    if not cond:
        raise SystemExit(f"reference disagrees with the hand-written expectation: {what}")


# -- canonical ----------------------------------------------------------------

CANONICAL = [
    ("empty object", {}, "{}"),
    ("sorted keys, nested", {"b": [1, {"d": 1, "c": 2}], "a": None}, '{"a":null,"b":[1,{"c":2,"d":1}]}'),
    ("literals", [True, False, None, ""], '[true,false,null,""]'),
    ("integral float is an integer", {"x": 1.0, "y": -0.0}, '{"x":1,"y":0}'),
    ("ECMAScript numbers", [0.1, 1.5, 1e21, 1e20, 1e-7, 1e-6, 4.5e-7, 333333333.3333333, 1e23, -2.5],
     "[0.1,1.5,1e+21,100000000000000000000,1e-7,0.000001,4.5e-7,333333333.3333333,1e+23,-2.5]"),
    ("largest safe integer", 9007199254740991, "9007199254740991"),
    ("short escapes", "q\"\\\b\f\n\r\t", '"q\\"\\\\\\b\\f\\n\\r\\t"'),
    ("other controls as lowercase \\u00xx", "\x01\x1f", '"\\u0001\\u001f"'),
    ("slash and DEL literal", "/\x7f", '"/\x7f"'),
    ("non-ASCII literal UTF-8", "é€😀", '"é€😀"'),
    ("keys sort by UTF-16 code units", {"￿": 1, "\U0001f600": 2, "a": 3}, '{"a":3,"\U0001f600":2,"￿":1}'),
]
CANONICAL_ERRORS = [
    ("integer above 2^53-1", 9007199254740992),
    ("lone surrogate", "\ud800"),
]
ARGS = [None, {}, [], {"argv": ["uptime"]}, {"volume": 0.5, "name": "kitchen lamp é"}]


def gen_canonical() -> dict:
    cases = []
    for name, value, expect in CANONICAL:
        got = wire.canonical(value).decode("utf-8")
        check(got == expect, f"canonical {name}: {got!r}")
        cases.append({"name": name, "value": value, "canonical": expect})
    for name, value in CANONICAL_ERRORS:
        try:
            wire.canonical(value)
            check(False, f"canonical {name} should fail")
        except ValueError:
            pass
        cases.append({"name": name, "value": value, "error": True})
    hashes = [{"args": a, "args_hash": wire.args_hash(a)} for a in ARGS]
    check(hashes[0]["args_hash"] == hashes[1]["args_hash"] != hashes[2]["args_hash"], "only null hashes as {}")
    return {"cases": cases, "args_hash": hashes}


# -- signed objects -------------------------------------------------------------

def hubkey(root: str, op: str, band: str, iat: int = T0 - 86400) -> dict:
    return wire.sign(KEYS[root], {"typ": "rook-hubkey", "v": 2, "hub": KID[root], "key": PUB[op],
                                  "bands": [band], "iat": iat, "exp": iat + 7 * 86400})


def gen_signatures() -> dict:
    keys = [{"name": n, "seed": bytes(KEYS[n]).hex(), "pub": PUB[n], "kid": KID[n]} for n in KEYS]
    grant = hubkey("home-root", "home-op", BAND)
    verify = []
    for name, g, root, band, now, expect in [
        ("ok", grant, "home-root", BAND, T0, PUB["home-op"]),
        ("other band", grant, "home-root", GUEST_BAND, T0, None),
        ("expired past the hour of grace", grant, "home-root", BAND, grant["exp"] + 3601, None),
        ("inside the hour of grace", grant, "home-root", BAND, grant["exp"] + 3600, PUB["home-op"]),
        ("checked against another hub's root", grant, "guest-root", BAND, T0, None),
        ("bands as a string, not a list", wire.sign(KEYS["home-root"], {**grant, "bands": "xx" + BAND + "xx"}),
         "home-root", BAND, T0, None),
        ("self-signed by the operational key", wire.sign(KEYS["home-op"], {**grant, "hub": KID["home-op"]}),
         "home-root", BAND, T0, None),
    ]:
        got = wire.verify_hubkey(g, PUB[root], band, now)
        check(got == expect, f"hubkey {name}")
        verify.append({"name": name, "grant": g, "root": PUB[root], "band": band, "now": now, "key": expect})
    ticket = ticket_for()
    lease_as_ticket = {**ticket, "typ": "rook-lease"}
    extra = wire.sign(KEYS["home-op"], {**ticket, "x_future": {"added": "in 2.1"}})
    plugin_sig = wire.sign(KEYS["home-op"], {
        "typ": "rook-plugin", "v": 2, "hub": KID["home-root"], "worker": WORKER, "plugin": "echo",
        "version": "1.0.0", "files": {"plugin.yaml": hashlib.sha256(b"manifest: 1\n").hexdigest()},
        "python": ["anyio==4.15.1"], "iat": T0})
    release_sig = wire.sign(KEYS["home-op"], {
        "typ": "rook-release", "v": 2, "hub": KID["home-root"], "version": "0.5.1", "core": "2.0",
        "files": {"rook/__init__.py": hashlib.sha256(b"").hexdigest()}, "requires": ["pyyaml>=6"], "iat": T0})
    objects = []
    for name, obj, pub, expect in [
        ("ticket", ticket, "home-op", True),
        ("hubkey grant", grant, "home-root", True),
        ("plugin.sig", plugin_sig, "home-op", True),
        ("plugin.sig with a file added", {**plugin_sig, "files": {**plugin_sig["files"], "x.py": "0" * 64}},
         "home-op", False),
        ("release.sig", release_sig, "home-op", True),
        ("release.sig read as a plugin.sig", {**release_sig, "typ": "rook-plugin"}, "home-op", False),
        ("unknown field is signed and verifies", extra, "home-op", True),
        ("unknown field removed after signing", {k: v for k, v in extra.items() if k != "x_future"}, "home-op", False),
        ("hubkey rides unsigned", {**ticket, "hubkey": grant}, "home-op", True),
        ("field changed after signing", {**ticket, "why": "something else"}, "home-op", False),
        ("same signature under another typ prefix", lease_as_ticket, "home-op", False),
        ("wrong key", ticket, "attacker", False),
    ]:
        got = wire.verify_sig(PUB[pub], obj)
        check(got is expect, f"signature {name}")
        objects.append({"name": name, "object": obj, "pub": PUB[pub], "valid": expect})
    return {
        "keys": keys,
        "prefixes": {t: p.decode() for t, p in wire.PREFIX.items()},
        "signing_input": {"object": ticket, "bytes_utf8": wire.signing_input(ticket).decode("utf-8")},
        "hubkey": verify,
        "verify": objects,
    }


# -- tickets ------------------------------------------------------------------

CAPS = {
    "shell.exec": {"risk": "exec", "effects": []},
    "light.set": {"risk": "write", "effects": ["physical"]},
    "core.manifests": {"risk": "read", "effects": []},
    "core.trust.set": {"risk": "admin", "effects": ["persists"]},
    "kvm.screen.capture": {"risk": "read", "effects": [], "contracts": ["screen.capture"]},
}
WORKER_STATE = {
    "worker_id": WORKER, "name": "kaiju", "caps": CAPS,
    "trust": {
        HOME: {"band": BAND, "role": "home", "keys": {KID["home-op"]: PUB["home-op"]},
               "ceiling": {"grants": ["*"]}},
        GUEST: {"band": GUEST_BAND, "role": "guest", "keys": {KID["guest-op"]: PUB["guest-op"]},
                "ceiling": {"grants": ["light.*@lamp-*", "core.*", "screen.capture"], "max_risk": "write"}},
    },
}


def request(cap="shell.exec", args=None, **kw) -> dict:
    return {"id": CALL, "cap": cap, "target": WORKER, "args": {"argv": ["uptime"]} if args is None else args, **kw}


def ticket_for(req=None, signer="home-op", hub=HOME, band=BAND, **over) -> dict:
    req = req or request()
    t = {"typ": "rook-ticket", "v": 2, "hub": hub, "band": band, "call": req["id"], "nonce": NONCE,
         "worker": WORKER, "cap": req["cap"], "args_hash": wire.args_hash(req.get("args")),
         "person": "person:bake", "agent": "agent:claude-code", "session": "s_7c1e",
         "why": "Check how long kaiju has been up.", "iat": T0, "exp": T0 + 60}
    if "appliance" in req:
        t["appliance"] = req["appliance"]
    for k, v in over.items():
        if v is None:
            t.pop(k, None)
        else:
            t[k] = v
    return wire.sign(KEYS[signer], t)


def gen_tickets() -> dict:
    r = request()
    lamp = request("light.set", {"on": False}, appliance="lamp-kitchen")
    door = request("light.set", {"on": False}, appliance="garage-door")
    forged = {**ticket_for(signer="attacker"), "kid": KID["home-op"]}
    shot = request("kvm.screen.capture", {})
    cases = [
        ("ok", r, ticket_for(), BAND, T0, [], "ok"),
        ("no ticket", r, None, BAND, T0, [], "no_ticket"),
        ("missing why", r, ticket_for(why=None), BAND, T0, [], "malformed"),
        ("blank why", r, ticket_for(why="   "), BAND, T0, [], "malformed"),
        ("why over 500 characters", r, ticket_for(why="x" * 501), BAND, T0, [], "malformed"),
        ("neither person nor agent", r, ticket_for(person=None, agent=None), BAND, T0, [], "malformed"),
        ("agent alone (unattended)", r, ticket_for(person=None, agent="system:hub"), BAND, T0, [], "ok"),
        ("nonce not 32 hex", r, ticket_for(nonce="abc"), BAND, T0, [], "malformed"),
        ("elevate not a boolean", r, ticket_for(elevate="yes"), BAND, T0, [], "malformed"),
        ("elevate up front", r, ticket_for(elevate=True), BAND, T0, [], "ok"),
        ("wrong v", r, ticket_for(v=1), BAND, T0, [], "malformed"),
        ("kid not a string", r, {**ticket_for(), "kid": [KID["home-op"]]}, BAND, T0, [], "malformed"),
        ("empty args list is not {}", request(args=[]), ticket_for(request(args={})), BAND, T0, [], "wrong_args"),
        ("unknown hub", r, ticket_for(hub="00" * 8), BAND, T0, [], "unknown_hub"),
        ("guest hub's ticket on the home band", r, ticket_for(signer="guest-op", hub=GUEST, band=BAND),
         BAND, T0, [], "unknown_hub"),
        ("key the hub never certified", r, ticket_for(signer="attacker"), BAND, T0, [], "unknown_key"),
        ("forged: claims the hub's kid", r, forged, BAND, T0, [], "bad_signature"),
        ("tampered after signing", r, {**ticket_for(), "why": "Delete everything."}, BAND, T0, [], "bad_signature"),
        ("names another band", r, ticket_for(band=GUEST_BAND), BAND, T0, [], "wrong_band"),
        ("another worker", r, ticket_for(worker="0" * 32), BAND, T0, [], "wrong_worker"),
        ("appliance not in the request", r, ticket_for(appliance="lamp-kitchen"), BAND, T0, [], "wrong_appliance"),
        ("another call id", r, ticket_for(call="1" * 32), BAND, T0, [], "wrong_call"),
        ("another cap", r, ticket_for(cap="light.set"), BAND, T0, [], "wrong_cap"),
        ("arguments changed", request(args={"argv": ["rm", "-rf", "/"]}), ticket_for(), BAND, T0, [], "wrong_args"),
        ("floats and unicode in args", request(args={"volume": 0.5, "name": "kitchen lamp é"}),
         ticket_for(request(args={"volume": 0.5, "name": "kitchen lamp é"})), BAND, T0, [], "ok"),
        ("lifetime 300 s", r, ticket_for(exp=T0 + 300), BAND, T0, [], "bad_lifetime"),
        ("lifetime 30 s", r, ticket_for(exp=T0 + 30), BAND, T0, [], "bad_lifetime"),
        ("lifetime 120 s", r, ticket_for(exp=T0 + 120), BAND, T0, [], "ok"),
        ("31 s before iat", r, ticket_for(), BAND, T0 - 31, [], "not_yet_valid"),
        ("30 s before iat", r, ticket_for(), BAND, T0 - 30, [], "ok"),
        ("31 s after exp", r, ticket_for(), BAND, T0 + 91, [], "expired"),
        ("30 s after exp", r, ticket_for(), BAND, T0 + 90, [], "ok"),
        ("replayed nonce", r, ticket_for(), BAND, T0, [NONCE], "replay"),
        ("cap the worker doesn't serve", request("nope.nothing"), ticket_for(request("nope.nothing")),
         BAND, T0, [], "unknown_cap"),
        ("unknown field ignored", r, ticket_for(x_future=1), BAND, T0, [], "ok"),
        ("hubkey inline", r, {**ticket_for(), "hubkey": hubkey("home-root", "home-op", BAND)}, BAND, T0, [], "ok"),
        ("guest: lamp inside ceiling", lamp, ticket_for(lamp, signer="guest-op", hub=GUEST, band=GUEST_BAND),
         GUEST_BAND, T0, [], "ok"),
        ("guest: garage door outside ceiling", door, ticket_for(door, signer="guest-op", hub=GUEST, band=GUEST_BAND),
         GUEST_BAND, T0, [], "outside_ceiling"),
        ("guest: shell outside ceiling", r, ticket_for(signer="guest-op", hub=GUEST, band=GUEST_BAND),
         GUEST_BAND, T0, [], "outside_ceiling"),
        ("guest: core.manifests always allowed", request("core.manifests", {}),
         ticket_for(request("core.manifests", {}), signer="guest-op", hub=GUEST, band=GUEST_BAND),
         GUEST_BAND, T0, [], "ok"),
        ("guest: core.trust.set is home only", request("core.trust.set", {}),
         ticket_for(request("core.trust.set", {}), signer="guest-op", hub=GUEST, band=GUEST_BAND),
         GUEST_BAND, T0, [], "outside_ceiling"),
        ("home: core.trust.set", request("core.trust.set", {}), ticket_for(request("core.trust.set", {})),
         BAND, T0, [], "ok"),
        ("guest: called by a contract's name inside ceiling", shot,
         ticket_for(shot, signer="guest-op", hub=GUEST, band=GUEST_BAND, contract="screen.capture"),
         GUEST_BAND, T0, [], "ok"),
        ("guest: the provider by its own name outside ceiling", shot,
         ticket_for(shot, signer="guest-op", hub=GUEST, band=GUEST_BAND), GUEST_BAND, T0, [], "outside_ceiling"),
        ("contract the cap doesn't provide", shot, ticket_for(shot, contract="hid.click"), BAND, T0, [], "unknown_cap"),
        ("contract not a string", shot, ticket_for(shot, contract=5), BAND, T0, [], "malformed"),
        ("an owner of the hub (owner claim)", r, ticket_for(owner=True), BAND, T0, [], "ok"),
        ("owner claim without a person", r, ticket_for(owner=True, person=None), BAND, T0, [], "malformed"),
        ("owner claim not a bool", r, ticket_for(owner="yes"), BAND, T0, [], "malformed"),
    ]
    out = []
    for name, req, t, band, now, seen, expect in cases:
        cache = wire.ReplayCache()
        for n in seen:
            cache.add(n, T0 + 60, T0)
        ok, reason = wire.verify_ticket(copy.deepcopy(t), req, {**WORKER_STATE, "band": band}, now, cache)
        check(reason == expect and ok == (expect == "ok"), f"ticket {name}: {reason}")
        out.append({"name": name, "band": band, "now": now, "seen": seen, "request": req, "ticket": t,
                    "reason": expect})
    return {"worker": WORKER_STATE, "skew": wire.SKEW, "cases": out}


# -- leases ---------------------------------------------------------------------

def gen_leases() -> dict:
    t = ticket_for()
    base = {"typ": "rook-lease", "v": 2, "id": "c0ffee00c0ffee00c0ffee00c0ffee00", "hub": HOME, "band": BAND,
            "worker": WORKER, "person": t["person"], "agent": t["agent"], "session": t["session"],
            "iat": T0, "exp": T0 + 900}
    keys = {KID["home-op"]: PUB["home-op"]}
    cases = [
        ("ok", wire.sign(KEYS["home-op"], base), T0 + 10, "ok"),
        ("last second", wire.sign(KEYS["home-op"], base), T0 + 900, "ok"),
        ("expired", wire.sign(KEYS["home-op"], base), T0 + 901, "expired"),
        ("longer than 15 minutes", wire.sign(KEYS["home-op"], {**base, "exp": T0 + 1800}), T0, "bad_lifetime"),
        ("another session", wire.sign(KEYS["home-op"], {**base, "session": "s_other"}), T0, "wrong_session"),
        ("another worker", wire.sign(KEYS["home-op"], {**base, "worker": "0" * 32}), T0, "wrong_worker"),
        ("another agent", wire.sign(KEYS["home-op"], {**base, "agent": "agent:other"}), T0, "wrong_identity"),
        ("unknown key", wire.sign(KEYS["attacker"], base), T0, "unknown_key"),
        ("tampered", {**wire.sign(KEYS["home-op"], base), "exp": T0 + 899}, T0, "bad_signature"),
    ]
    out = []
    for name, lease, now, expect in cases:
        ok, reason = wire.verify_lease(lease, t, keys, now)
        check(reason == expect, f"lease {name}: {reason}")
        out.append({"name": name, "lease": lease, "now": now, "reason": expect})
    return {"ticket": t, "keys": keys, "cases": out}


# -- worker requests (13.1) ----------------------------------------------------

HUB_NODE = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
WORKER_B = "7e6d5c4b3a291807f6e5d4c3b2a19080"
REQ_ID = "aa11bb22cc33dd44ee55ff6600778899"
PROOF_NONCE = "c4f0a1b2c3d4e5f60718293a4b5c6d7e"


def sub_call(ticket=None, **args) -> dict:
    a = {"ticket": ticket or ticket_for(), "cap": "echo.say", "target": "kaiju", "args": {"text": "hi"},
         "why": "Say hi from inside the call.", **args}
    return {"id": REQ_ID, "cap": "core.call", "target": HUB_NODE, "args": a}


def proved(req, signer="worker", worker=WORKER, hub=HOME, band=BAND, nonce=PROOF_NONCE, ts=T0 + 5,
           for_request=None, **over) -> dict:
    p = wire.request_proof(KEYS[signer], for_request or req, worker=worker, hub=hub, band=band, nonce=nonce, ts=ts)
    if over:
        p = {**p, **over}
        p = wire.sign(KEYS[signer], {k: v for k, v in p.items() if v is not None and k != "sig"})
    return {**req, "proof": p}


def gen_requests() -> dict:
    t = ticket_for()
    call = sub_call()
    elevate = {"id": REQ_ID, "cap": "core.elevate", "target": HUB_NODE, "args": {"ticket": t, "reason": "upfront"}}
    no_ticket = {"id": REQ_ID, "cap": "core.call", "target": HUB_NODE,
                 "args": {"cap": "echo.say", "target": "kaiju", "args": {}, "why": "No ticket."}}
    other_t = ticket_for(nonce="0" * 32)
    ok = proved(call)
    tampered = {**ok, "proof": {**ok["proof"], "ts": T0 + 6}}
    forged = proved(call, signer="attacker")
    forged["proof"] = {**forged["proof"], "kid": KID["worker"]}
    keys = {WORKER: PUB["worker"], WORKER_B: PUB["worker-b"]}
    cases = [
        ("ok: core.call", ok, T0 + 5, [], "ok"),
        ("ok: core.elevate", proved(elevate), T0 + 5, [], "ok"),
        ("unknown field is signed and verifies", proved(call, x_future=1), T0 + 5, [], "ok"),
        ("no proof: the H2 replay of an in-flight ticket", call, T0 + 5, [], "no_proof"),
        ("wrong v", proved(call, v=1), T0 + 5, [], "malformed"),
        ("nonce not 32 hex", proved(call, nonce="abc"), T0 + 5, [], "malformed"),
        ("ts a string", proved(call, ts=str(T0)), T0 + 5, [], "malformed"),
        ("parent left out", proved(call, parent=None), T0 + 5, [], "malformed"),
        ("a worker this hub has no key for", proved(call, worker="dd" * 16), T0 + 5, [], "unknown_worker"),
        ("signed by another key", proved(call, signer="attacker"), T0 + 5, [], "unknown_key"),
        ("forged: claims the worker's kid", forged, T0 + 5, [], "bad_signature"),
        ("tampered after signing", tampered, T0 + 5, [], "bad_signature"),
        ("for another hub", proved(call, hub=GUEST), T0 + 5, [], "wrong_hub"),
        ("for another band", proved(call, band=GUEST_BAND), T0 + 5, [], "wrong_band"),
        ("the proof of another request", {**call, "id": "bb" * 16, "proof": ok["proof"]}, T0 + 5, [],
         "wrong_call"),
        ("an elevate's proof on a sub-call", {**call, "proof": proved(elevate)["proof"]}, T0 + 5, [],
         "wrong_cap"),
        ("arguments changed after signing", {**sub_call(cap="shell.exec", args={"argv": ["sh"]}),
                                              "proof": ok["proof"]}, T0 + 5, [], "wrong_args"),
        ("the proof moved onto a sub-call of another ticket", proved(sub_call(other_t), for_request=call),
         T0 + 5, [], "wrong_args"),
        ("parent names another ticket", proved(call, parent="0" * 32), T0 + 5, [], "wrong_parent"),
        ("a request with no ticket", proved(no_ticket, parent=NONCE), T0 + 5, [], "wrong_parent"),
        ("another worker's in-flight ticket", proved(call, signer="worker-b", worker=WORKER_B), T0 + 5, [],
         "wrong_worker"),
        ("61 s old", ok, T0 + 66, [], "stale"),
        ("60 s old", ok, T0 + 65, [], "ok"),
        ("61 s ahead", ok, T0 - 56, [], "stale"),
        ("the worker's own request replayed", ok, T0 + 6, [PROOF_NONCE], "replay"),
    ]
    out = []
    for name, req, now, seen, expect in cases:
        cache = wire.ReplayCache()
        for n in seen:
            cache.add(n, now, now)
        ok_, reason = wire.verify_request(copy.deepcopy(req.get("proof")), req, hub=HOME, band=BAND, keys=keys,
                                          now=now, replay=cache)
        check(reason == expect and ok_ == (expect == "ok"), f"request {name}: {reason}")
        out.append({"name": name, "now": now, "seen": seen, "request": req, "reason": expect})
    return {"hub": HOME, "band": BAND, "keys": keys, "skew": wire.REQUEST_SKEW, "cases": out}


# -- memberships ----------------------------------------------------------------

def gen_memberships() -> dict:
    base = {"typ": "rook-membership", "v": 2, "serial": "a1" * 16, "worker": WORKER, "hub": GUEST,
            "root": PUB["guest-root"], "band": GUEST_BAND, "role": "guest",
            "ceiling": {"grants": ["light.*@lamp-*"], "max_risk": "write"}, "by": "person:bake", "iat": T0}
    home_keys = {KID["home-root"]: PUB["home-root"], KID["home-op"]: PUB["home-op"]}

    def m(signer="home-root", **over):
        d = {**base, **over}
        return wire.sign(KEYS[signer], {k: v for k, v in d.items() if v is not None})

    cases = [
        ("guest, signed by the home root", m(), "ok"),
        ("guest, signed by a home operational key", m("home-op"), "ok"),
        ("signed by the guest hub itself", m("guest-root"), "unknown_key"),
        ("for another worker", m(worker="0" * 32), "wrong_worker"),
        ("root doesn't match hub", m(root=PUB["attacker"]), "wrong_root"),
        ("unknown role", m(role="admin"), "bad_role"),
        ("ceiling without grants", m(ceiling={"max_risk": "read"}), "bad_ceiling"),
        ("ceiling with an unknown risk", m(ceiling={"grants": ["*"], "max_risk": "root"}), "bad_ceiling"),
        ("removed needs no ceiling", m(role="removed", ceiling=None), "ok"),
        ("home narrows its own band", m(hub=HOME, root=PUB["home-root"], band=BAND, role="home",
                                        ceiling={"grants": ["*"], "max_risk": "exec"}), "ok"),
        ("tampered ceiling", {**m(), "ceiling": {"grants": ["*"]}}, "bad_signature"),
        ("a second home", m(role="home"), "bad_role"),
        ("home hub as a guest of itself", m(hub=HOME, root=PUB["home-root"], band=BAND), "bad_role"),
    ]
    out = []
    for name, doc, expect in cases:
        ok, reason = wire.verify_membership(doc, HOME, home_keys, WORKER)
        check(reason == expect, f"membership {name}: {reason}")
        out.append({"name": name, "document": doc, "reason": expect})
    return {"worker_id": WORKER, "home_hub": HOME, "home_keys": home_keys, "cases": out}


# -- versions -------------------------------------------------------------------

VERSIONS = [
    ("2.0", "2.0", "2.0"), ("2.0", "2.3", "2.0"), ("2.3", "2.1", "2.1"), ("2.10", "2.9", "2.9"),
    ("2.1", "3.0", None), ("1.0", "2.0", None), ("2", "2.0", None), ("2.0.1", "2.0", None),
    ("02.0", "2.0", None), ("2.01", "2.1", None), ("", "2.0", None), (None, "2.0", None),
    (2.0, "2.0", None), ("v2.0", "2.0", None), ("2.0 ", "2.0", None),
]


def gen_version() -> dict:
    cases = []
    for a, b, expect in VERSIONS:
        check(wire.negotiate(a, b) == expect and wire.negotiate(b, a) == expect, f"negotiate {a!r} {b!r}")
        cases.append({"mine": a, "theirs": b, "speak": expect})
    return {"current": CORE_VERSION, "cases": cases}


# -- rights -------------------------------------------------------------------

def call(cap, risk="read", effects=(), target="kaiju"):
    return {"cap": cap, "risk": risk, "effects": list(effects), "target": target}


ALL = {"grants": ["*"]}
RIGHTS = [
    ("star grants everything", [["person", ALL]], call("shell.exec", "exec", ["persists"]), {"ok": True}),
    ("glob crosses dots", [["agent", {"grants": ["phone.*"]}]], call("phone.motion.started"), {"ok": True}),
    ("glob must match the whole name", [["agent", {"grants": ["phone"]}]], call("phone.sms.send"),
     {"ok": False, "layer": "agent", "reason": "grants"}),
    ("prefix without star is literal", [["agent", {"grants": ["deluge."]}]], call("deluge.list"),
     {"ok": False, "layer": "agent", "reason": "grants"}),
    ("question mark is literal", [["agent", {"grants": ["shell.ex?c"]}]], call("shell.exec"),
     {"ok": False, "layer": "agent", "reason": "grants"}),
    ("target pattern matches", [["ceiling", {"grants": ["light.*@lamp-*"]}]],
     call("light.set", "write", ["physical"], "lamp-kitchen"), {"ok": True}),
    ("target pattern refuses", [["ceiling", {"grants": ["light.*@lamp-*"]}]],
     call("light.set", "write", ["physical"], "garage-door"), {"ok": False, "layer": "ceiling", "reason": "grants"}),
    ("risk above max_risk", [["person", {"grants": ["*"], "max_risk": "write"}]], call("shell.exec", "exec"),
     {"ok": False, "layer": "person", "reason": "risk"}),
    ("risk equal to max_risk", [["person", {"grants": ["*"], "max_risk": "exec"}]], call("shell.exec", "exec"),
     {"ok": True}),
    ("unknown risk counts as admin", [["person", {"grants": ["*"], "max_risk": "exec"}]], call("x.y", "sudo"),
     {"ok": False, "layer": "person", "reason": "risk"}),
    ("effects absent allows all", [["person", {"grants": ["*"]}]], call("bank.pay", "write", ["spends"]),
     {"ok": True}),
    ("effects [] allows none", [["person", {"grants": ["*"], "effects": []}]], call("tv.power", "write", ["physical"]),
     {"ok": False, "layer": "person", "reason": "effects"}),
    ("every effect must be allowed", [["person", {"grants": ["*"], "effects": ["physical"]}]],
     call("lock.open", "write", ["physical", "persists"]), {"ok": False, "layer": "person", "reason": "effects"}),
    ("unknown effect is outside a listed set", [["person", {"grants": ["*"], "effects": ["physical"]}]],
     call("x.y", "write", ["teleports"]), {"ok": False, "layer": "person", "reason": "effects"}),
    ("malformed rights refuse", [["agent", {"grant": ["*"]}]], call("info.ping"),
     {"ok": False, "layer": "agent", "reason": "grants"}),
    ("on behalf of: both allow", [["person", ALL], ["agent", {"grants": ["knowledge.*"]}], ["session", ALL]],
     call("knowledge.page.get"), {"ok": True}),
    ("on behalf of: agent can't exceed its own grants",
     [["person", ALL], ["agent", {"grants": ["knowledge.*"]}], ["session", ALL]], call("shell.exec", "exec"),
     {"ok": False, "layer": "agent", "reason": "grants"}),
    ("on behalf of: agent can't exceed the person",
     [["person", {"grants": ["*"], "max_risk": "write"}], ["agent", ALL], ["session", ALL]],
     call("shell.exec", "exec"), {"ok": False, "layer": "person", "reason": "risk"}),
    ("session scope narrows", [["person", ALL], ["agent", ALL], ["session", {"grants": ["deluge.*", "info.*"]}]],
     call("shell.exec", "exec"), {"ok": False, "layer": "session", "reason": "grants"}),
    ("plugin uses narrows", [["person", ALL], ["session", ALL], ["uses", {"grants": ["notify.send"]}]],
     call("shell.exec", "exec"), {"ok": False, "layer": "uses", "reason": "grants"}),
    ("first refusing layer is reported", [["person", {"grants": ["*"], "max_risk": "read"}],
                                          ["agent", {"grants": ["info.*"]}]],
     call("shell.exec", "exec"), {"ok": False, "layer": "person", "reason": "risk"}),
    ("no layers refuse nothing", [], call("shell.exec", "exec"), {"ok": True}),
    ("called by a contract's name: the contract's name matches",
     [["person", {"grants": ["screen.*"]}], ["uses", {"grants": ["screen.capture"]}]],
     {**call("kvm.screen.capture"), "contract": "screen.capture"}, {"ok": True}),
    ("called by a contract's name: the provider's name still matches",
     [["person", {"grants": ["kvm.*"]}]], {**call("kvm.screen.capture"), "contract": "screen.capture"}, {"ok": True}),
    ("the provider called by its own name: the contract's grant doesn't cover it",
     [["person", {"grants": ["screen.*"]}]], call("kvm.screen.capture"),
     {"ok": False, "layer": "person", "reason": "grants"}),
    ("called by a contract's name: risk still the provider's",
     [["person", {"grants": ["hid.*"], "max_risk": "write"}]],
     {**call("kvm.hid.type", "exec"), "contract": "hid.type"}, {"ok": False, "layer": "person", "reason": "risk"}),
]


def gen_rights() -> dict:
    cases = []
    for name, layers, c, expect in RIGHTS:
        check(rights.check(layers, c) == expect, f"rights {name}: {rights.check(layers, c)}")
        cases.append({"name": name, "layers": layers, "call": c, "expect": expect})
    return {"risks": list(rights.RISKS), "scopes": list(rights.SCOPES), "effects": list(rights.EFFECTS),
            "hard_stop_effects": list(rights.HARD_STOP_EFFECTS), "cases": cases}


# -- presets --------------------------------------------------------------------

PRESET_CASES = [
    ("owner: anything", "owner", [], [], call("shell.exec", "admin", ["destructive"]), {"ok": True}),
    ("adult: admin allowed", "adult", [], [], call("core.trust.set", "admin", ["persists"]), {"ok": True}),
    ("child: chat", "child", [], [], call("agent.chat.send", "write"), {"ok": True}),
    ("child: page view", "child", [], [], call("page.view"), {"ok": True}),
    ("child: shell not granted", "child", [], [], call("shell.exec", "exec"), {"ok": False, "reason": "grants"}),
    ("child: plugin adds a read cap", "child", {"homework": ["homework.item.list"]}, [], call("homework.item.list"),
     {"ok": True}),
    ("child: plugin can't grant outside its namespace", "child", {"homework": ["shell.*", "*"]}, [],
     call("shell.exec", "write"), {"ok": False, "reason": "grants"}),
    ("child: plugin grant can't pass max_risk", "child", {"homework": ["homework.*"]}, [],
     call("homework.item.purge", "admin", ["destructive"]), {"ok": False, "reason": "risk"}),
    ("child: owner grant can't pass effects", "child", [], ["lights.set"],
     call("lights.set", "write", ["physical"]), {"ok": False, "reason": "effects"}),
    ("child: personal data refused", "child", {"phone": ["phone.*"]}, [], call("phone.sms.list", "read", ["personal"]),
     {"ok": False, "reason": "effects"}),
    ("guest: chat", "guest", [], [], call("agent.chat.send", "write"), {"ok": True}),
    ("guest: pages not in preset", "guest", [], [], call("page.view"), {"ok": False, "reason": "grants"}),
    ("guest: owner grant within ceilings", "guest", [], ["music.play"], call("music.play", "write"), {"ok": True}),
]


def gen_presets() -> dict:
    cases = []
    for name, preset, plugin_grants, user_grants, c, expect in PRESET_CASES:
        r = rights.preset_rights(rights.PRESETS[preset], plugin_grants, user_grants)
        why = rights.denies(r, c)
        check(({"ok": True} if why is None else {"ok": False, "reason": why}) == expect, f"preset {name}: {why}")
        cases.append({"name": name, "preset": preset, "plugin_grants": plugin_grants, "user_grants": user_grants,
                      "rights": r, "call": c, "expect": expect})
    return {"presets": rights.PRESETS, "cases": cases}


# -- gate -----------------------------------------------------------------------

def g(gate, verdict, mode="enforce", **kw):
    return {"gate": gate, "mode": mode, "verdict": verdict, "confidence": 0.9, "reason": "anomaly", **kw}


GATES = [
    ("no gates", [], {"verdict": "allow", "by": []}),
    ("all allow", [g("ml", "allow"), g("canary", "allow")], {"verdict": "allow", "by": []}),
    ("flag", [g("ml", "flag"), g("canary", "allow")], {"verdict": "flag", "by": ["ml"]}),
    ("block wins over flag", [g("ml", "flag"), g("canary", "block")], {"verdict": "block", "by": ["canary"]}),
    ("two blocks", [g("ml", "block"), g("canary", "block")], {"verdict": "block", "by": ["ml", "canary"]}),
    ("shadow never counts", [g("ml", "block", "shadow")], {"verdict": "allow", "by": []}),
    ("error counts as flag", [{"gate": "ml", "mode": "enforce", "error": "timeout"}], {"verdict": "flag", "by": ["ml"]}),
    ("unknown verdict counts as flag", [g("ml", "grant")], {"verdict": "flag", "by": ["ml"]}),
    ("allow can't undo a block", [g("ml", "block"), g("allowlist", "allow", confidence=1.0)],
     {"verdict": "block", "by": ["ml"]}),
]


def gen_gate() -> dict:
    cases = []
    for name, results, expect in GATES:
        check(rights.gate_decision(results) == expect, f"gate {name}: {rights.gate_decision(results)}")
        cases.append({"name": name, "results": results, "expect": expect})
    return {"verdicts": list(rights.VERDICTS), "reasons": list(rights.GATE_REASONS), "cases": cases}


# -- journal --------------------------------------------------------------------

def gen_journal() -> dict:
    head, chain = None, []
    for i, (cap, ok) in enumerate([("shell.exec", True), ("deluge.torrents.list", True), ("light.set", False)]):
        e = journal.append(head, {"id": f"j{i + 1}", "kind": "call", "chain": "project:castling", "ts": T0 + i,
                                  "call": f"{i + 1:032x}", "cap": cap, "worker": WORKER, "person": "person:bake",
                                  "agent": "agent:claude-code", "why": "Vector entry.", "ok": ok})
        chain.append(e)
        head = e["hash"]
    check(chain[0]["prev"] == journal.GENESIS and journal.first_break(chain) is None, "journal chain")
    edited = copy.deepcopy(chain)
    edited[1]["ok"] = False
    dropped = [chain[0], chain[2]]
    rehashed = copy.deepcopy(chain)
    rehashed[1]["why"] = "Covering tracks."
    rehashed[1]["hash"] = journal.entry_hash(rehashed[1])
    breaks = [("intact", chain, None), ("edited entry", edited, 1), ("missing entry", dropped, 1),
              ("rehashed entry breaks the next link", rehashed, 2)]
    out = []
    for name, entries, expect in breaks:
        check(journal.first_break(entries) == expect, f"journal {name}")
        out.append({"name": name, "entries": entries, "first_break": expect})
    return {"genesis": journal.GENESIS, "chain": chain, "checks": out}


VECTORS = {
    "canonical": gen_canonical, "signatures": gen_signatures, "tickets": gen_tickets, "leases": gen_leases,
    "memberships": gen_memberships, "requests": gen_requests, "version": gen_version, "rights": gen_rights, "presets": gen_presets,
    "gate": gen_gate, "journal": gen_journal,
}


def render(name: str) -> str:
    body = {"spec": CORE_VERSION, "vector": name, **VECTORS[name]()}
    return json.dumps(body, indent=1) + "\n"


def main(argv: list[str]) -> int:
    stale = []
    OUT.mkdir(exist_ok=True)
    for name in VECTORS:
        text, path = render(name), OUT / f"{name}.json"
        if "--check" in argv:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(path.name)
        else:
            path.write_text(text, encoding="utf-8")
    if stale:
        print("stale vectors (run conformance/core-v2/generate.py):", ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
