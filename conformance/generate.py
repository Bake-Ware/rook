#!/usr/bin/env python3
"""Generate the language-neutral conformance vectors from the Python reference.

    python conformance/generate.py           # rewrite conformance/vectors/*.json
    python conformance/generate.py --check   # exit 1 if any file is stale

Every expected value below is computed by calling the reference code
(``telesthete.protocol``, ``rook.core``, ``rook.worker.core``), never typed in
by hand, so the vectors cannot drift from what the Python hub and workers do.
Output is deterministic: fixed PSKs, sequence numbers, fragment ids, clocks
and ed25519 seeds (test-only keys derived from public labels).

See docs/spec/core-v1.md and conformance/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "vectors"
sys.path.insert(0, str(ROOT))

SPEC_VERSION = "1.0"
NOW = 1790800000  # fixed clock for every signed object


def _hex(b: bytes) -> str:
    return b.hex()


def doc(name: str, description: str, cases: list, **extra) -> dict:
    return {"spec": SPEC_VERSION, "vector": name, "description": description, **extra,
            "cases": cases}


# -- band crypto -------------------------------------------------------------

def band_crypto() -> dict:
    from telesthete.protocol.crypto import BandCrypto, derive_band_id, derive_encryption_key
    cases = []
    for psk, seq, pt in (
        ("correct horse battery staple", 0, b""),
        ("correct horse battery staple", 1, b"\x00"),
        ("k3y-with-ünicode-☃", 0x0123456789ABCDEF, b'{"kind":"announce"}'),
        ("a", (1 << 63) - 1, bytes(range(64))),
    ):
        c = BandCrypto(psk)
        cases.append({
            "psk": psk,
            "band_id": _hex(derive_band_id(psk)),
            "key": _hex(derive_encryption_key(psk)),
            "sequence": str(seq),  # u64: a decimal string (JSON numbers lose precision past 2**53)
            "nonce": _hex(b"\x00\x00\x00\x00" + seq.to_bytes(8, "big")),
            "plaintext": _hex(pt),
            "ciphertext": _hex(c.encrypt(seq, pt)),
        })
    return doc("band_crypto",
               "band_id = SHA256(psk)[:16]; key = HKDF-SHA256(salt 'telesthete-v1', ikm psk, "
               "info 'encryption-chacha20-poly1305'); ChaCha20-Poly1305 (IETF) with nonce "
               "4 zero bytes || u64be(sequence) and EMPTY associated data (Rook profile). "
               "ciphertext includes the 16-byte tag. All byte strings are lowercase hex.", cases)


# -- fragmentation -----------------------------------------------------------

def fragments() -> dict:
    from telesthete.protocol.fragment import MAX_CHUNK_PAYLOAD, fragment, parse_chunk
    fid = bytes.fromhex("00112233445566778899aabbccddeeff")
    split = []
    for payload in (b"", b"\x00", b"x" * MAX_CHUNK_PAYLOAD, b"y" * (MAX_CHUNK_PAYLOAD + 1),
                    bytes(i % 251 for i in range(2500))):
        split.append({"payload": _hex(payload), "fragment_id": _hex(fid),
                      "chunk_size": MAX_CHUNK_PAYLOAD,
                      "chunks": [_hex(c) for c in fragment(payload, MAX_CHUNK_PAYLOAD, fid)]})
    parse = []
    for label, chunk in (
        ("single", bytes([1]) + fid + b"\x00\x00\x00\x01" + b"hi"),
        ("wrong version", bytes([2]) + fid + b"\x00\x00\x00\x01" + b"hi"),
        ("total zero", bytes([1]) + fid + b"\x00\x00\x00\x00"),
        ("seq >= total", bytes([1]) + fid + b"\x00\x02\x00\x02"),
        ("short header", bytes([1]) + fid[:10]),
        ("empty data", bytes([1]) + fid + b"\x00\x01\x00\x03"),
    ):
        got = parse_chunk(chunk)
        parse.append({"label": label, "chunk": _hex(chunk), "valid": got is not None,
                      **({"fragment_id": _hex(got[0]), "seq": got[1], "total": got[2],
                          "data": _hex(got[3])} if got else {})})
    # Reassembly: feed chunks in the listed order; expect the emitted messages.
    from telesthete.protocol.fragment import Reassembler
    a = fragment(b"A" * 2100, MAX_CHUNK_PAYLOAD, fid)            # 3 chunks
    b = fragment(b"hello", MAX_CHUNK_PAYLOAD, bytes(16))           # 1 chunk
    scenarios = []
    for label, feed in (("in order", a), ("reversed", list(reversed(a))),
                        ("duplicate + interleaved", [a[0], a[0], b[0], a[2], a[1]]),
                        ("invalid chunk dropped", [b"\x07junk", b[0]])):
        r = Reassembler()
        out = [x for x in (r.feed(c) for c in feed) if x is not None]
        scenarios.append({"label": label, "feed": [_hex(c) for c in feed],
                          "emits": [_hex(m) for m in out]})
    return doc("fragments",
               "Channel fragmentation envelope: version(0x01) || fragment_id(16) || "
               "u16be seq || u16be total || data; data <= 1003 bytes; empty and 1-chunk "
               "messages still carry the header (seq 0, total 1).",
               [], split=split, parse=parse, reassemble=scenarios)


# -- frames (end to end) -----------------------------------------------------

def frames() -> dict:
    from telesthete.protocol.crypto import BandCrypto
    from telesthete.protocol.fragment import MAX_CHUNK_PAYLOAD, fragment
    from telesthete.protocol.framing import ChannelType, pack_packet
    psk = "conformance-band-psk"
    crypto = BandCrypto(psk)
    cases = []
    msgs = [
        ("keepalive", b"\x00"),
        ("call", json.dumps({"id": "7f0c", "cap": "conformance.echo",
                             "args": {"value": "hi"}, "target": "w-1"}).encode()),
        ("large reply", json.dumps({"id": "7f0d", "from": "w-1", "ok": True,
                                    "result": {"value": "z" * 2400}}).encode()),
    ]
    seq = 0x1000
    for n, (label, payload) in enumerate(msgs):
        fid = hashlib.sha256(f"fid-{n}".encode()).digest()[:16]
        out = []
        for chunk in fragment(payload, MAX_CHUNK_PAYLOAD, fid):
            ct = crypto.encrypt(seq, chunk)
            out.append(_hex(pack_packet(crypto.band_id, ChannelType.CHANNEL, 0, seq, ct)))
            seq += 1
        cases.append({"label": label, "psk": psk, "fragment_id": _hex(fid),
                      "first_sequence": seq - len(out), "message": _hex(payload),
                      "frames": out})
    return doc("frames",
               "Complete UDP datagrams: band_id(16) || channel_type(1)=0x02 CHANNEL || "
               "u16be channel_id=0 || u64be sequence || AEAD(fragment chunk). One sequence "
               "per datagram, incrementing across fragments. A decoder that accepts these "
               "frames and reassembles them MUST produce `message`.", cases)


# -- canonical JSON and args hash -------------------------------------------

def canonical() -> dict:
    from rook.core.authz import args_hash, canonical as canon
    values = [
        {},
        {"b": 1, "a": [True, False, None], "c": {"z": "", "y": 0}},
        {"cmd": "echo 'hi' \"there\"\n\t\\ /"},
        {"text": "café ☃ \U0001F600", "é": 1},
        {"￿": 1, "\U0001F600": 2, "~": 3, "A": 4},
        {"ctl": "\x00\x01\x1f\x7f", "big": 9007199254740991, "neg": -7},
        [1, "two", {"three": [3]}],
    ]
    cases = [{"value": v, "canonical": canon(v).decode("ascii"), "args_hash": args_hash(v)}
             for v in values]
    return doc("canonical",
               "canonical(v) = JSON, keys sorted by Unicode code point, separators ',' and "
               "':', no whitespace, every non-ASCII character escaped as \\uXXXX (surrogate "
               "pairs above U+FFFF, lowercase hex), short escapes for \\\" \\\\ \\b \\f \\n "
               "\\r \\t, other controls as \\u00XX, DEL (0x7f) as \\u007f, '/' unescaped. Integers within +/-(2**53-1), no floats in signed "
               "bodies. args_hash(v) = base64url(sha256(canonical(v))) without padding.", cases)


# -- signatures: grants, announces, tickets ---------------------------------

def _key(label: str):
    from nacl.signing import SigningKey
    return SigningKey(hashlib.sha256(f"rook-conformance/{label}".encode()).digest())


def signatures() -> dict:
    from rook.core import authz
    root, op, other = _key("root"), _key("op-key"), _key("impostor")
    root_pub, op_pub, other_pub = (authz.pub_b64(k) for k in (root, op, other))
    band = hashlib.sha256(b"conformance-band-psk").digest()[:16].hex()
    keys = [{"label": lbl, "seed": _hex(bytes(k._seed)), "public": authz.pub_b64(k),
             "kid": authz.key_id(authz.pub_b64(k))}
            for lbl, k in (("root", root), ("op-key", op), ("impostor", other))]

    def grant(role="is_hub", name="rook", bands=(band,), max_tier="admin", signer=root,
              prefix=authz.PREFIX_GRANT, serial="0" * 31 + "1", worker_id=None, exp=None):
        sub = {"key": f"ed25519:{op_pub}", "kid": authz.key_id(op_pub)}
        if worker_id:
            sub["worker_id"] = worker_id
        body = {"typ": "rook-grant", "v": 1, "serial": serial,
                "iss": authz.key_id(root_pub), "sub": sub, "role": role,
                "scope": {"bands": sorted(bands)}, "constraints": {"max_tier": max_tier},
                "iat": NOW, "nbf": NOW, "exp": exp if exp is not None else NOW + 7 * 86400}
        if name:
            body["name"] = name
        return authz.sign_obj(signer, prefix, body)

    good = grant()
    tampered = {**good, "role": "is_hub", "name": "rook", "exp": good["exp"] + 1}
    grant_cases = []
    for label, g, ctx in (
        ("valid", good, {}),
        ("tampered body", tampered, {}),
        ("untrusted issuer", good, {"anchors": [other_pub]}),
        ("signed under the ticket prefix", grant(prefix=authz.PREFIX_TICKET), {}),
        ("signed by the wrong key", grant(signer=other), {}),
        ("expired beyond grace", good, {"now": good["exp"] + authz.GRANT_GRACE + 1}),
        ("inside expiry grace", good, {"now": good["exp"] + authz.GRANT_GRACE - 1}),
        ("not yet valid", good, {"now": NOW - authz.GRANT_GRACE - 1}),
        ("band not in scope", good, {"band": "00" * 16}),
        ("revoked serial", good, {"revoked": [good["serial"]]}),
        ("unknown role", grant(role="is_admin"), {}),
        ("is_hub with a non-reserved name", grant(name="hub-2"), {}),
        ("bound to another worker", grant(worker_id="w-other"), {"worker_id": "w-1"}),
        ("bound to this worker", grant(worker_id="w-1"), {"worker_id": "w-1"}),
    ):
        c = {"anchors": [root_pub], "band": band, "now": NOW + 60, "revoked": [], **ctx}
        ok, reason = authz.verify_grant(g, c["anchors"], band=c["band"], now=c["now"],
                                        revoked=c["revoked"], worker_id=c.get("worker_id"))
        grant_cases.append({"label": label, "grant": g, "context": c, "ok": ok,
                            "reason": reason})

    base = {"kind": "announce", "worker_id": "hub-node-1", "name": "rook",
            "caps": ["caps.describe", "chat.read", "chat.write", "hub.info"],
            "grants": [good]}
    signed = authz.sign_announce(op, base, seq=7, now=NOW)
    forged = authz.sign_announce(other, base, seq=7, now=NOW)
    changed_caps = {**signed, "caps": signed["caps"] + ["shell.exec"]}
    announce_cases = []
    for label, msg, now in (
        ("signed by the grant key", signed, NOW + 10),
        ("stale ts", signed, NOW + authz.ANNOUNCE_FRESH_SECS + 1),
        ("signed by another key (grant copied)", forged, NOW + 10),
        ("caps changed after signing", changed_caps, NOW + 10),
        ("no asig", {k: v for k, v in signed.items() if k != "asig"}, NOW + 10),
    ):
        held = authz.held_roles(msg, [root_pub], band=band, now=now)
        announce_cases.append({"label": label, "announce": msg,
                               "context": {"anchors": [root_pub], "band": band, "now": now},
                               "held_roles": sorted(held)})

    keys_by_kid = {authz.key_id(op_pub): good}
    ro_grant = grant(max_tier="read", serial="0" * 31 + "2")
    args = {"value": "hi", "n": 1}

    def ticket(**kw):
        p = {"principal": "token:agent_1", "via": [], "cap": "conformance.echo",
             "target": "w-1", "msg_id": "m-1", "args": args, "tier": "read", "rev": 3,
             "now": NOW}
        p.update(kw)
        return authz.make_ticket(op, authz.key_id(op_pub), **p)

    t = ticket()
    ticket_cases = []
    for label, tk, env, kmap, now in (
        ("valid", t, {}, keys_by_kid, NOW + 5),
        ("for another worker", t, {"target": "w-2"}, keys_by_kid, NOW + 5),
        ("for another message", t, {"msg_id": "m-2"}, keys_by_kid, NOW + 5),
        ("for another cap", t, {"cap": "shell.exec"}, keys_by_kid, NOW + 5),
        ("args changed", t, {"args": {"value": "hi", "n": 2}}, keys_by_kid, NOW + 5),
        ("expired beyond skew", t, {}, keys_by_kid,
         t["exp"] + authz.TICKET_SKEW + 1),
        ("unknown key", t, {}, {}, NOW + 5),
        ("tampered", {**t, "p": "token:someone_else"}, {}, keys_by_kid, NOW + 5),
        ("tier above grant constraint", ticket(tier="exec"), {},
         {authz.key_id(op_pub): ro_grant}, NOW + 5),
    ):
        e = {"cap": "conformance.echo", "target": "w-1", "msg_id": "m-1", "args": args, **env}
        ok, reason = authz.verify_ticket(tk, cap=e["cap"], target=e["target"],
                                         msg_id=e["msg_id"], args=e["args"], keys=kmap,
                                         now=now)
        ticket_cases.append({"label": label, "ticket": tk, "envelope": e,
                             "grants_by_kid": kmap, "now": now, "ok": ok, "reason": reason})
    replay = authz.ReplayCache()
    seqd = []
    for _ in range(2):
        ok, reason = authz.verify_ticket(t, cap="conformance.echo", target="w-1", msg_id="m-1",
                                         args=args, keys=keys_by_kid, now=NOW + 5, replay=replay)
        seqd.append({"ok": ok, "reason": reason})
    return doc("signatures",
               "ed25519 over PREFIX || canonical(body minus 'sig'); prefixes 'rook-grant-v1\\n', "
               "'rook-ticket-v1\\n', 'rook-announce-v1\\n'. key_id = first 16 hex of "
               "sha256(raw 32-byte public key). Keys are test-only (seeds published here). "
               "`ok` and `held_roles` are normative; `reason` strings are the reference's and "
               "informative.", [],
               keys=keys, band=band, grants=grant_cases, announces=announce_cases,
               tickets=ticket_cases,
               ticket_replay={"ticket": t, "grants_by_kid": keys_by_kid, "now": NOW + 5,
                              "envelope": {"cap": "conformance.echo", "target": "w-1",
                                           "msg_id": "m-1", "args": args},
                              "results": seqd})


# -- placement ---------------------------------------------------------------

def placement() -> dict:
    from rook.core.facts import NodeFacts, PlacementError, compile_placement, evaluate_placement
    nodes = {
        "hub": {"roles": ["is_hub"], "hw": {"os": "linux", "arch": "x86_64", "cpus": 8,
                                             "mem_gb": 31.2, "pty": True}},
        "gpu-box": {"roles": [], "hw": {"os": "linux", "arch": "x86_64", "cpus": 32,
                                         "pty": True, "display": True,
                                         "gpu": [{"vendor": "nvidia", "name": "A", "vram_gb": 24.0},
                                                 {"vendor": "nvidia", "name": "B", "vram_gb": 6.0}]}},
        "phone": {"roles": [], "hw": {"os": "android", "arch": "aarch64", "cpus": 8,
                                       "embedded": True, "camera": True}},
        "bare": {"roles": [], "hw": {}},
    }
    exprs = [
        "is_hub", "not is_hub", "any", "anywhere", "True", "is_embedded",
        "has('camera')", "has('gpu')", "has('gpu', vram_gb >= 8)", "has('gpu', vram_gb >= 32)",
        "has('gpu', vendor='nvidia')", "has('gpu', vram_gb > 10, vram_gb < 20)",
        "has('gpu', name == 'B', vram_gb <= 6)",
        "os == 'android'", "os != 'linux'", "is_embedded or os == 'android'",
        "has('pty') and arch in ('x86_64', 'aarch64')", "arch not in ['x86_64']",
        "cpus >= 16", "2 < cpus <= 8", "mem_gb > 16.5", "missing == None", "missing > 3",
        "'lin' in os", "'x' in missing", "not (is_hub or is_embedded)",
        "os < 3 or is_hub", "is_hub and os < 3", "cpus == 8.0",
        "is_worker", "role == 'hub'",
    ]
    cases = []
    for e in exprs:
        results = {}
        for n, f in nodes.items():
            facts = NodeFacts(node_id=n, name=n, roles=frozenset(f["roles"]), hw=f["hw"])
            results[n] = evaluate_placement(e, facts)
        cases.append({"expr": e, "valid": True, "results": results})
    for bad in ["", "   ", "__import__('os')", "os.path", "cpus + 1 > 2", "has(gpu)",
                "has()", "lambda: 1", "-1 < cpus", "x if y else z", "f(1)", "[x for x in y]",
                "cpus > 1; is_hub", "{'a': 1}", "os[0] == 'l'"]:
        try:
            compile_placement(bad)
            valid = True
        except PlacementError:
            valid = False
        assert not valid, bad
        cases.append({"expr": bad, "valid": False,
                      "results": {n: False for n in nodes}})
    return doc("placement",
               "Placement predicates over node facts. `valid: false` expressions MUST be "
               "rejected at declaration time and evaluate to false. `roles` come from verified "
               "grants only; `hw` is self-reported. Evaluation errors (e.g. ordering a string "
               "against a number) make the WHOLE expression false; and/or short-circuit.",
               cases, nodes=nodes)


# -- tiers -------------------------------------------------------------------

def tiers() -> dict:
    from rook.core.authz import BUILTIN, BUILTIN_PREFIX, effective_tier
    table = {c: {"tier": t, **({"tags": list(g)} if g else {})} for c, (t, g) in sorted(BUILTIN.items())}
    cases = []
    for cap, declared, override, lower in (
        ("shell.exec", None, None, False), ("shell.exec", "read", None, False),
        ("info.ping", "admin", None, False), ("info.ping", "a", None, False),
        ("info.ping", None, "write", False), ("worker.deauth", None, "read", False),
        ("worker.deauth", None, "read", True), ("custom.thing", None, None, False),
        ("custom.thing", "r", None, False), ("custom.thing", "bogus", None, False),
        ("custom.thing", "w", "x", False), ("custom.thing", "x", "r", True),
        ("cmd.backup", "read", None, False), ("cmd.backup", None, "admin", False),
        ("cmd.backup", None, "read", True), ("chat.write", None, None, False),
        ("conformance.echo", "read", None, False), ("secret.get", "read", None, False),
    ):
        cases.append({"cap": cap, "declared": declared, "override": override, "lower": lower,
                      "tier": effective_tier(cap, declared, override, lower)})
    return doc("tiers",
               "effective_tier = max(builtin[cap], declared) (unknown everywhere -> exec); "
               "'cmd.*' is always exec; an override raises freely and lowers only with "
               "lower=true. Tiers order read < write < exec < admin; letters r/w/x/a accepted.",
               cases, table=table, prefixes=[{"prefix": p, "tier": t} for p, t in BUILTIN_PREFIX])


# -- limit / fields projection ----------------------------------------------

def projection() -> dict:
    from rook.core.plugin import capability
    from rook.core.registry import CapabilityRegistry
    rows = [{"id": i, "name": f"n{i}", "extra": i * i} for i in range(5)]
    env = {"items": rows, "total": 5, "next": None}
    cases = []
    for label, meta, params, args, result in (
        ("no metadata: untouched", None, [], {}, rows),
        ("limit trims a list", {"limit": 2}, [], {}, rows),
        ("caller limit wins", {"limit": 2}, [], {"limit": 4}, rows),
        ("bad caller limit falls back", {"limit": 2}, [], {"limit": 0}, rows),
        ("limit trims an items envelope", {"limit": 3}, [], {}, env),
        ("handler takes limit: default injected", {"limit": 3}, ["limit"], {}, rows),
        ("handler takes limit: caller value passed", {"limit": 3}, ["limit"], {"limit": 1}, rows),
        ("fields default list", {"fields": ["id", "name"]}, [], {}, rows),
        ("fields star by default", {"fields": "*"}, [], {}, rows),
        ("caller fields list", {"fields": "*"}, [], {"fields": ["name"]}, rows),
        ("caller fields csv", {"fields": ["id"]}, [], {"fields": "id, extra"}, rows),
        ("caller fields star", {"fields": ["id"]}, [], {"fields": "*"}, rows),
        ("fields on a dict", {"fields": ["id"]}, [], {}, {"id": 1, "name": "x"}),
        ("fields on an envelope", {"fields": ["name"], "limit": 2}, [], {}, env),
        ("handler takes fields: passed through", {"fields": ["id"]}, ["fields"],
         {"fields": "name"}, rows),
        ("scalars untouched", {"fields": ["id"], "limit": 1}, [], {}, 42),
    ):
        seen: dict = {}
        src = "def h(" + ", ".join(f"{p}=None" for p in params) + "):\n    return 0\n"
        ns: dict = {}
        exec(src, ns)  # noqa: S102 - builds a handler with exactly these parameters

        def handler(__res=result, __seen=seen, **kw):
            __seen.update(kw)
            return json.loads(json.dumps(__res))
        handler.__signature__ = inspect.signature(ns["h"])
        reg = CapabilityRegistry()
        if meta is not None:
            capability("x", **meta)(handler)
        reg.register("t.x", handler)
        out = asyncio.run(reg.call("t.x", **json.loads(json.dumps(args))))
        cases.append({"label": label, "meta": meta, "handler_params": params,
                      "args": args, "handler_result": result,
                      "handler_received": seen, "result": out})
    return doc("projection",
               "Core-enforced output contract of a cap declaring `limit`/`fields` "
               "(see spec 'Capabilities'). `handler_params` lists which of limit/fields the "
               "handler accepts itself; `handler_received` is the kwargs it got.", cases)


# -- core_api ranges and resources -------------------------------------------

def core_api() -> dict:
    from rook.core.plugin import core_api_compatible
    cases = []
    for spec, have in ((">=1.0,<2", "1.1"), (">=1.1,<2", "1.0"), ("==1.0", "1.0"),
                       ("==1.0", "1.1"), ("1", "1.9"), ("1", "2.0"), ("1.1", "1.0"),
                       ("", "1.1"), ("!=1.1", "1.1"), (">1.0", "1.0.1"), ("<=1.1", "1.1"),
                       (">=2", "1.1"), ("~1.0", "1.1"), (">=1.0 , <2", "1.5")):
        cases.append({"spec": spec, "have": have, "compatible": core_api_compatible(spec, have)})
    return doc("core_api", "Plugin CORE_API range matching against a core API version.", cases)


def resources() -> dict:
    from rook.core.plugin import parse_resource
    cases = []
    for url in ("cap://any/embed.text", "cap://gpu-box/llm.complete", "https://api.example.com/v1",
                "sqlite:///var/lib/app/db.sqlite", "file:///tmp/x", "cap://any/",
                "cap:///embed.text", "ftp://example.com/x", "not a url"):
        try:
            r = parse_resource(url)
            cases.append({"url": url, "valid": True, "scheme": r.scheme, "target": r.target,
                          "path": r.path})
        except ValueError:
            cases.append({"url": url, "valid": False})
    return doc("resources", "Resource connection strings (plugin settings of type resource).",
               cases)


# -- worker message handling -------------------------------------------------

def messages() -> dict:
    """Run each inbound message through the reference Worker._on_message and
    record what it sent back (nothing, or one reply)."""
    import tempfile
    from unittest.mock import patch
    from rook.core.plugin import Plugin, capability

    class Conf(Plugin):
        NAMESPACE = "conformance"

        @capability("echo", risk="read")
        def echo(self, value=None):
            return value

        @capability("add", risk="read")
        def add(self, a: int, b: int):
            return a + b

    sent: list = []

    async def send(payload, peer_id=None):
        sent.append(json.loads(payload))

    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
        from rook.worker import audit, core
        audit_path = Path(tmp) / "audit.jsonl"
        with patch.object(core, "_WORKER_ID_FILE", Path(tmp) / "worker_id"), \
                patch.object(audit, "_AUDIT_PATH", audit_path), \
                patch.object(audit, "_AUDIT_DIR", Path(tmp)):
            (Path(tmp) / "worker_id").write_text("w-1\n")
            w = core.Worker(SimpleNamespace(send=send, band_id=None), enabled=[], name="worker-a")
            w.registry.register("conformance.echo", Conf().echo)
            w.registry.register("conformance.add", Conf().add)
            cases = []
            inputs = [
                ("targeted call", {"id": "m1", "cap": "conformance.echo", "args": {"value": 1},
                                   "target": "w-1"}),
                ("open call for an owned cap", {"id": "m2", "cap": "conformance.add",
                                                "args": {"a": 2, "b": 3}}),
                ("call for another worker", {"id": "m3", "cap": "conformance.echo",
                                             "target": "w-2"}),
                ("open call for a cap not owned", {"id": "m4", "cap": "nope.nothing"}),
                ("targeted call for a cap not owned", {"id": "m5", "cap": "nope.nothing",
                                                       "target": "w-1"}),
                ("args not an object", {"id": "m6", "cap": "conformance.echo", "args": [1],
                                        "target": "w-1"}),
                ("args null means {}", {"id": "m7", "cap": "conformance.echo", "args": None,
                                        "target": "w-1"}),
                ("args [] is falsy, means {}", {"id": "m10", "cap": "conformance.echo",
                                                 "args": [], "target": "w-1"}),
                ("args false is falsy, means {}", {"id": "m11", "cap": "conformance.echo",
                                                   "args": False, "target": "w-1"}),
                ("empty cap is not a request", {"id": "m12", "cap": "", "target": "w-1"}),
                ("empty target is an open call", {"id": "m13", "cap": "conformance.echo",
                                                  "args": {"value": 3}, "target": ""}),
                ("unexpected arg", {"id": "m14", "cap": "conformance.add",
                                    "args": {"a": 1, "b": 2, "c": 3}, "target": "w-1"}),
                ("bad args", {"id": "m8", "cap": "conformance.add", "args": {"a": 1},
                              "target": "w-1"}),
                ("unknown envelope keys ignored", {"id": "m9", "cap": "conformance.echo",
                                                   "args": {"value": "x"}, "target": "w-1",
                                                   "identity": "agent:x", "ticket": {"v": 1},
                                                   "future_field": {"a": 1}}),
                ("no id: reply without id", {"cap": "conformance.echo", "args": {"value": 2},
                                             "target": "w-1"}),
                ("announce is not a request", {"kind": "announce", "worker_id": "w-9",
                                               "name": "x", "caps": []}),
                ("reply is not a request", {"id": "m1", "from": "w-9", "ok": True, "result": 1}),
                ("not an object", [1, 2]),
            ]
            for label, msg in inputs:
                sent.clear()
                asyncio.run(w._on_message(json.dumps(msg).encode(), (0,)))
                cases.append({"label": label, "worker_id": "w-1",
                              "caps": ["conformance.add", "conformance.echo"],
                              "input": msg, "replies": list(sent)})
            for label, raw in (("not JSON", b"\x00\x01binary"), ("empty", b"")):
                sent.clear()
                asyncio.run(w._on_message(raw, (0,)))
                cases.append({"label": label, "worker_id": "w-1",
                              "caps": ["conformance.add", "conformance.echo"],
                              "input_hex": raw.hex(), "replies": list(sent)})
    # Error strings for bad args are Python's TypeError text: only the prefix
    # "bad args: " is normative. Mark them so ports can compare loosely.
    for c in cases:
        for r in c["replies"]:
            if isinstance(r.get("error"), str) and r["error"].startswith("bad args: "):
                r["error_prefix"] = "bad args: "
    return doc("messages",
               "How a worker with id `worker_id` owning `caps` answers each inbound band "
               "message: `replies` is the list of JSON messages it sends back (empty = stays "
               "silent). Compare replies as JSON objects (key order free). Where "
               "`error_prefix` is present only that prefix of `error` is normative.", cases)


GENERATORS = {
    "band_crypto": band_crypto, "fragments": fragments, "frames": frames,
    "canonical": canonical, "signatures": signatures, "placement": placement,
    "tiers": tiers, "projection": projection, "core_api": core_api,
    "resources": resources, "messages": messages,
}


def render(obj: dict) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    import logging
    logging.getLogger("rook").setLevel(logging.CRITICAL)  # expected failures log loudly
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if any vector file is stale")
    ns = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, gen in GENERATORS.items():
        text = render(gen())
        path = OUT / f"{name}.json"
        if ns.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(path.name)
        else:
            path.write_text(text, encoding="utf-8")
    if stale:
        print("stale conformance vectors: " + ", ".join(stale)
              + "\nregenerate with: python conformance/generate.py", file=sys.stderr)
        return 1
    if not ns.check:
        print(f"wrote {len(GENERATORS)} vector files to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
