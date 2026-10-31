"""Core v2 wire primitives: canonical JSON (RFC 8785), signed objects,
call tickets, elevation leases, hub keys, memberships and version
negotiation. Spec sections 3, 5, 6 and 8 of docs/spec/core-v2.md.

Signing and verification take PyNaCl keys/raw bytes; everything else is
standard library.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from typing import Any

from . import rights

# -- canonical JSON (spec 3.2) ------------------------------------------------

MAX_SAFE_INT = 2**53 - 1


def _number(x: int | float) -> str:
    """ECMAScript Number.prototype.toString, as RFC 8785 requires."""
    if isinstance(x, int):
        if abs(x) > MAX_SAFE_INT:
            raise ValueError(f"integer outside +-(2^53-1): {x}")
        return str(x)
    if not math.isfinite(x):
        raise ValueError("NaN and Infinity have no JSON form")
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    # repr gives the shortest round-trip digits: value = int(digits) * 10**e10.
    mant, _, exp = repr(abs(x)).partition("e")
    whole, _, frac = mant.partition(".")
    digits = (whole + frac).lstrip("0")
    stripped = digits.rstrip("0")
    e10 = int(exp or 0) - len(frac) + (len(digits) - len(stripped))
    digits = stripped
    # ECMAScript form: value = 0.d1..dk * 10**n
    k = len(digits)
    n = k + e10
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * -n + digits
    else:
        e = n - 1
        out = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + out


def _string(s: str) -> str:
    out = json.dumps(s, ensure_ascii=False)
    try:
        out.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("string holds a lone surrogate") from None
    return out


def _canon(v: Any) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return _number(v)
    if isinstance(v, str):
        return _string(v)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_canon(x) for x in v) + "]"
    if isinstance(v, dict):
        for k in v:
            if not isinstance(k, str):
                raise ValueError(f"object key is not a string: {k!r}")
        keys = sorted(v, key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(_string(k) + ":" + _canon(v[k]) for k in keys) + "}"
    raise ValueError(f"not JSON: {type(v).__name__}")


def canonical(v: Any) -> bytes:
    """RFC 8785 (JCS) canonical JSON as UTF-8 bytes. Raises ValueError for
    values that have no canonical form."""
    return _canon(v).encode("utf-8")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def args_hash(args: Any) -> str:
    """base64url(SHA-256(canonical(args))), unpadded; missing or null args
    hash as {}."""
    digest = hashlib.sha256(canonical({} if args is None else args)).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _raw_pub(pub: bytes | str) -> bytes:
    if isinstance(pub, bytes):
        return pub
    return unb64(pub.split(":", 1)[1] if pub.startswith("ed25519:") else pub)


def key_id(pub: bytes | str) -> str:
    """First 16 hex of SHA-256 over the raw 32-byte Ed25519 public key."""
    return hashlib.sha256(_raw_pub(pub)).hexdigest()[:16]


def pub_text(sk) -> str:
    """``ed25519:<base64>`` public key of a nacl SigningKey."""
    return "ed25519:" + b64(bytes(sk.verify_key))


def manifest_hash(manifest: dict) -> str:
    """Hex SHA-256 of a parsed manifest's canonical JSON (spec 5.4).
    TODO(castling:manifest): use rook.kit.manifest's hash once it lands, if
    it hashes the same bytes."""
    return hashlib.sha256(canonical(manifest)).hexdigest()


# -- signed objects (spec 3.3) ----------------------------------------------

PREFIX = {
    "rook-ticket": b"rook-ticket-v2\n",
    "rook-lease": b"rook-lease-v2\n",
    "rook-hubkey": b"rook-hubkey-v2\n",
    "rook-membership": b"rook-membership-v2\n",
    "rook-announce": b"rook-announce-v2\n",
    "rook-plugin": b"rook-plugin-v2\n",
    "rook-release": b"rook-release-v2\n",
}

# Keys that ride on a signed object without being signed (spec 3.3).
UNSIGNED = ("sig", "hubkey")


def signing_input(obj: dict) -> bytes:
    body = {k: v for k, v in obj.items() if k not in UNSIGNED}
    return PREFIX[obj["typ"]] + canonical(body)


def sign(sk, obj: dict) -> dict:
    """Return ``obj`` with ``kid`` and ``sig`` set by nacl SigningKey ``sk``."""
    body = {k: v for k, v in obj.items() if k not in UNSIGNED}
    body["kid"] = key_id(bytes(sk.verify_key))
    sig = sk.sign(signing_input(body)).signature
    return {**obj, "kid": body["kid"], "sig": b64(sig)}


def verify_sig(pub: bytes | str, obj: Any) -> bool:
    """True iff ``obj['sig']`` verifies under ``pub`` with the prefix of
    ``obj['typ']``. Never raises."""
    try:
        from nacl.signing import VerifyKey
        if obj.get("typ") not in PREFIX or not isinstance(obj.get("sig"), str):
            return False
        VerifyKey(_raw_pub(pub)).verify(signing_input(obj), unb64(obj["sig"]))
        return True
    except Exception:
        return False


# -- version negotiation (spec 2.2) ------------------------------------------

_VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")


def negotiate(mine: Any, theirs: Any) -> str | None:
    """The version two peers speak: same major, lower minor. None when the
    majors differ or either side's version is not ``MAJOR.MINOR``."""
    a = _VERSION.fullmatch(mine) if isinstance(mine, str) else None
    b = _VERSION.fullmatch(theirs) if isinstance(theirs, str) else None
    if not a or not b or a[1] != b[1]:
        return None
    return f"{a[1]}.{min(int(a[2]), int(b[2]))}"


# -- hub keys (spec 5.2) -----------------------------------------------------

GRANT_GRACE = 3600


def verify_hubkey(grant: Any, root_pub: str, band: str, now: int) -> str | None:
    """Verify a ``rook-hubkey`` grant against a hub's pinned root key.
    Returns the operational public key it certifies, or None."""
    if not isinstance(grant, dict) or grant.get("typ") != "rook-hubkey" or grant.get("v") != 2:
        return None
    try:
        if grant.get("hub") != key_id(root_pub) or grant.get("kid") != key_id(root_pub):
            return None
        if not verify_sig(root_pub, grant):
            return None
        if not (grant["iat"] - GRANT_GRACE <= now <= grant["exp"] + GRANT_GRACE):
            return None
        if not isinstance(grant.get("bands"), list) or band not in grant["bands"]:
            return None
        return grant["key"]
    except (KeyError, TypeError, ValueError):
        return None


# -- tickets (spec 6) --------------------------------------------------------

SKEW = 30              # seconds of clock difference tolerated either way
MIN_LIFETIME = 60      # exp - iat bounds
MAX_LIFETIME = 120
REPLAY_KEEP = 900      # replay cache entry lifetime (spec: 600..1200)

_TICKET_STR = ("kid", "hub", "band", "call", "nonce", "worker", "cap", "args_hash", "session", "why")
_HEX32 = re.compile(r"[0-9a-f]{32}")
WHY_MAX = 500

# Core caps every trusted hub of a band may call whatever its ceiling, and
# the ones only the home hub may call (spec 9.2).
ALWAYS = ("core.manifests", "core.lease.drop")
HOME_ONLY = ("core.trust.set",)


class ReplayCache:
    """Nonces seen, each kept until at least its ticket's exp + SKEW."""

    def __init__(self) -> None:
        self.until: dict[str, int] = {}

    def seen(self, nonce: str, now: int) -> bool:
        self.until = {n: t for n, t in self.until.items() if t > now}
        return nonce in self.until

    def add(self, nonce: str, exp: int, now: int) -> None:
        self.until[nonce] = max(exp + SKEW, now + REPLAY_KEEP)


def _ticket_shape(t: Any) -> bool:
    if not isinstance(t, dict) or t.get("typ") != "rook-ticket" or t.get("v") != 2:
        return False
    if not all(isinstance(t.get(k), str) and t[k] for k in _TICKET_STR):
        return False
    if not all(isinstance(t.get(k), int) and not isinstance(t[k], bool) for k in ("iat", "exp")):
        return False
    if not (_HEX32.fullmatch(t["call"]) and _HEX32.fullmatch(t["nonce"])):
        return False
    if not t["why"].strip() or len(t["why"]) > WHY_MAX:
        return False
    if "elevate" in t and not isinstance(t["elevate"], bool):
        return False
    if "contract" in t and not (isinstance(t["contract"], str) and t["contract"]):
        return False
    if "owner" in t and not (isinstance(t["owner"], bool) and t.get("person")):
        return False
    who = [t.get("person"), t.get("agent")]
    return any(who) and all(w is None or (isinstance(w, str) and w) for w in who)


def verify_ticket(ticket: Any, request: dict, worker: dict, now: int,
                  replay: ReplayCache) -> tuple[bool, str]:
    """Worker-side ticket check (spec 6.3). ``worker`` holds ``worker_id``,
    ``band`` (the band the request arrived on), ``name``, ``trust`` ({hub_id:
    {"band", "role", "keys": {kid: pub}, "ceiling": Rights}}) and ``caps`` ({cap:
    {"risk", "effects", "contracts"}}, from its manifests). Returns (ok, reason); the
    reason is ``ok`` or the first failed check. Records the nonce on success."""
    if ticket is None:
        return False, "no_ticket"
    if not _ticket_shape(ticket):
        return False, "malformed"
    entry = worker["trust"].get(ticket["hub"])
    if not entry or entry["band"] != worker["band"]:
        return False, "unknown_hub"
    pub = entry["keys"].get(ticket["kid"])
    if not pub:
        return False, "unknown_key"
    if not verify_sig(pub, ticket):
        return False, "bad_signature"
    if ticket["band"] != worker["band"]:
        return False, "wrong_band"
    if ticket["worker"] != worker["worker_id"]:
        return False, "wrong_worker"
    if ticket.get("appliance") != request.get("appliance"):
        return False, "wrong_appliance"
    if ticket["call"] != request.get("id"):
        return False, "wrong_call"
    if ticket["cap"] != request.get("cap"):
        return False, "wrong_cap"
    try:
        if ticket["args_hash"] != args_hash(request.get("args")):
            return False, "wrong_args"
    except (ValueError, RecursionError):
        return False, "wrong_args"
    life = ticket["exp"] - ticket["iat"]
    if not MIN_LIFETIME <= life <= MAX_LIFETIME:
        return False, "bad_lifetime"
    if now < ticket["iat"] - SKEW:
        return False, "not_yet_valid"
    if now > ticket["exp"] + SKEW:
        return False, "expired"
    if replay.seen(ticket["nonce"], now):
        return False, "replay"
    meta = worker["caps"].get(ticket["cap"])
    if meta is None or ("contract" in ticket and ticket["contract"] not in (meta.get("contracts") or ())):
        return False, "unknown_cap"
    call = {"cap": ticket["cap"], "risk": meta["risk"], "effects": meta.get("effects", []),
            "target": request.get("appliance") or worker.get("name", "")}
    if "contract" in ticket:
        call["contract"] = ticket["contract"]
    if ticket["cap"] in HOME_ONLY:
        if entry["role"] != "home":
            return False, "outside_ceiling"
    elif ticket["cap"] not in ALWAYS and not rights.permits(entry["ceiling"], call):
        return False, "outside_ceiling"
    replay.add(ticket["nonce"], ticket["exp"], now)
    return True, "ok"


# -- elevation leases (spec 7) -----------------------------------------------

LEASE_SECS = 900


def verify_lease(lease: Any, ticket: dict, keys: dict, now: int) -> tuple[bool, str]:
    """Check a ``rook-lease`` against the verified ticket of the call it
    elevates and the issuing hub's verified op keys ({kid: pub})."""
    if not isinstance(lease, dict) or lease.get("typ") != "rook-lease" or lease.get("v") != 2:
        return False, "malformed"
    pub = keys.get(lease.get("kid"))
    if not pub:
        return False, "unknown_key"
    if not verify_sig(pub, lease):
        return False, "bad_signature"
    for k in ("hub", "band", "worker", "session"):
        if lease.get(k) != ticket.get(k):
            return False, f"wrong_{k}"
    if lease.get("person") != ticket.get("person") or lease.get("agent") != ticket.get("agent"):
        return False, "wrong_identity"
    try:
        if not 0 < lease["exp"] - lease["iat"] <= LEASE_SECS:
            return False, "bad_lifetime"
        if not lease["iat"] - SKEW <= now <= lease["exp"]:
            return False, "expired"
    except (KeyError, TypeError):
        return False, "malformed"
    return True, "ok"


# -- memberships (spec 9.3) --------------------------------------------------

ROLES = ("home", "co-owner", "guest", "removed")


def verify_membership(doc: Any, home_hub: str, home_keys: dict, worker_id: str) -> tuple[bool, str]:
    """Check a ``rook-membership`` document signed by the worker's home hub
    ``home_hub`` (``home_keys``: its verified op keys and its root key,
    {kid: pub}). Role ``home`` is for the home hub's own band and only there."""
    if not isinstance(doc, dict) or doc.get("typ") != "rook-membership" or doc.get("v") != 2:
        return False, "malformed"
    pub = home_keys.get(doc.get("kid"))
    if not pub:
        return False, "unknown_key"
    if not verify_sig(pub, doc):
        return False, "bad_signature"
    if doc.get("worker") != worker_id:
        return False, "wrong_worker"
    try:
        if doc.get("hub") != key_id(doc["root"]):
            return False, "wrong_root"
    except (KeyError, TypeError, ValueError):
        return False, "malformed"
    if doc.get("role") not in ROLES or (doc["role"] == "home") != (doc["hub"] == home_hub):
        return False, "bad_role"
    if doc["role"] != "removed" and not rights.valid_rights(doc.get("ceiling")):
        return False, "bad_ceiling"
    return True, "ok"
