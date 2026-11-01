"""Rook core v2 conformance: the reference (rook.core2) against the vectors in
conformance/core-v2/vectors, read the way another implementation would."""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("nacl")
from nacl.signing import SigningKey  # noqa: E402

from rook.core2 import journal, rights, wire  # noqa: E402

DIR = Path(__file__).resolve().parents[1] / "conformance" / "core-v2"


def vec(name: str) -> dict:
    v = json.loads((DIR / "vectors" / f"{name}.json").read_text(encoding="utf-8"))
    assert v["vector"] == name and v["spec"] == "2.0"
    return v


def test_vectors_are_current():
    r = subprocess.run([sys.executable, str(DIR / "generate.py"), "--check"], capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_canonical():
    v = vec("canonical")
    for c in v["cases"]:
        if c.get("error"):
            with pytest.raises(ValueError):
                wire.canonical(c["value"])
        else:
            assert wire.canonical(c["value"]).decode("utf-8") == c["canonical"], c["name"]
    for c in v["args_hash"]:
        assert wire.args_hash(c["args"]) == c["args_hash"]


def test_keys_and_signatures():
    v = vec("signatures")
    for k in v["keys"]:
        sk = SigningKey(bytes.fromhex(k["seed"]))
        assert wire.pub_text(sk) == k["pub"] and wire.key_id(k["pub"]) == k["kid"]
    assert {t: p.decode() for t, p in wire.PREFIX.items()} == v["prefixes"]
    si = v["signing_input"]
    assert wire.signing_input(si["object"]) == si["bytes_utf8"].encode("utf-8")
    for c in v["hubkey"]:
        assert wire.verify_hubkey(c["grant"], c["root"], c["band"], c["now"]) == c["key"], c["name"]
    for c in v["verify"]:
        assert wire.verify_sig(c["pub"], c["object"]) is c["valid"], c["name"]


def test_signing_is_deterministic():
    v = vec("signatures")
    seed = next(k["seed"] for k in v["keys"] if k["name"] == "home-op")
    ticket = v["signing_input"]["object"]
    again = wire.sign(SigningKey(bytes.fromhex(seed)), {k: x for k, x in ticket.items() if k != "sig"})
    assert again["sig"] == ticket["sig"]
    assert len(base64.b64decode(ticket["sig"])) == 64


def test_tickets():
    v = vec("tickets")
    for c in v["cases"]:
        cache = wire.ReplayCache()
        for n in c["seen"]:
            cache.add(n, c["now"], c["now"])
        ok, reason = wire.verify_ticket(c["ticket"], c["request"], {**v["worker"], "band": c["band"]},
                                        c["now"], cache)
        assert (ok, reason) == (c["reason"] == "ok", c["reason"]), c["name"]


def test_ticket_is_single_use():
    v = vec("tickets")
    c = next(c for c in v["cases"] if c["name"] == "ok")
    worker, cache = {**v["worker"], "band": c["band"]}, wire.ReplayCache()
    assert wire.verify_ticket(c["ticket"], c["request"], worker, c["now"], cache) == (True, "ok")
    assert wire.verify_ticket(c["ticket"], c["request"], worker, c["now"] + 1, cache) == (False, "replay")
    # Still remembered after the ticket can no longer pass the expiry check.
    assert c["ticket"]["nonce"] in cache.until
    assert cache.until[c["ticket"]["nonce"]] >= c["ticket"]["exp"] + wire.SKEW
    assert 600 <= wire.REPLAY_KEEP <= 1200


def test_leases():
    v = vec("leases")
    for c in v["cases"]:
        assert wire.verify_lease(c["lease"], v["ticket"], v["keys"], c["now"])[1] == c["reason"], c["name"]


def test_worker_requests():
    v = vec("requests")
    for c in v["cases"]:
        cache = wire.ReplayCache()
        for n in c["seen"]:
            cache.add(n, c["now"], c["now"])
        got = wire.verify_request(c["request"].get("proof"), c["request"], hub=v["hub"], band=v["band"],
                                  keys=v["keys"], now=c["now"], replay=cache)
        assert got == (c["reason"] == "ok", c["reason"]), c["name"]


def test_worker_request_is_single_use():
    v = vec("requests")
    c = next(c for c in v["cases"] if c["name"] == "ok: core.call")
    cache, req = wire.ReplayCache(), c["request"]
    kw = dict(hub=v["hub"], band=v["band"], keys=v["keys"], replay=cache)
    assert wire.verify_request(req["proof"], req, now=c["now"], **kw) == (True, "ok")
    assert wire.verify_request(req["proof"], req, now=c["now"] + 1, **kw) == (False, "replay")
    assert cache.until[req["proof"]["nonce"]] >= req["proof"]["ts"] + wire.REQUEST_SKEW


def test_memberships():
    v = vec("memberships")
    for c in v["cases"]:
        got = wire.verify_membership(c["document"], v["home_hub"], v["home_keys"], v["worker_id"])
        assert got[1] == c["reason"], c["name"]


def test_version():
    v = vec("version")
    for c in v["cases"]:
        assert wire.negotiate(c["mine"], c["theirs"]) == c["speak"]
        assert wire.negotiate(c["theirs"], c["mine"]) == c["speak"]
    assert wire.negotiate(v["current"], v["current"]) == v["current"]


def test_rights():
    v = vec("rights")
    assert (v["risks"], v["scopes"], v["effects"]) == (list(rights.RISKS), list(rights.SCOPES), list(rights.EFFECTS))
    for c in v["cases"]:
        assert rights.check(c["layers"], c["call"]) == c["expect"], c["name"]


def test_presets():
    v = vec("presets")
    assert v["presets"] == rights.PRESETS
    for c in v["cases"]:
        r = rights.preset_rights(v["presets"][c["preset"]], c["plugin_grants"], c["user_grants"])
        assert r == c["rights"], c["name"]
        why = rights.denies(r, c["call"])
        assert ({"ok": True} if why is None else {"ok": False, "reason": why}) == c["expect"], c["name"]


def test_hard_stops():
    assert rights.hard_stop(["destructive"]) and rights.hard_stop(["spends"]) and rights.hard_stop(["sends_as"])
    assert rights.hard_stop(["teleports"])  # unknown effect counts as destructive
    assert not rights.hard_stop(["physical", "persists"]) and not rights.hard_stop([])


def test_gate():
    v = vec("gate")
    for c in v["cases"]:
        assert rights.gate_decision(c["results"], c.get("risk")) == c["expect"], c["name"]


def test_journal():
    v = vec("journal")
    chain = v["chain"]
    assert chain[0]["prev"] == v["genesis"]
    for e in chain:
        assert journal.entry_hash(e) == e["hash"]
    for c in v["checks"]:
        assert journal.first_break(c["entries"]) == c["first_break"], c["name"]
    # Appending continues from the head.
    nxt = journal.append(chain[-1]["hash"], {"id": "j4", "kind": "call", "chain": chain[-1]["chain"]})
    assert journal.first_break(chain + [nxt]) is None


def test_hostile_args_refuse_instead_of_raising():
    v = vec("tickets")
    c = next(c for c in v["cases"] if c["name"] == "ok")
    deep: list = []
    for _ in range(100_000):
        deep = [deep]
    req = {**c["request"], "args": {"x": deep}}
    got = wire.verify_ticket(c["ticket"], req, {**v["worker"], "band": c["band"]}, c["now"], wire.ReplayCache())
    assert got == (False, "wrong_args")
