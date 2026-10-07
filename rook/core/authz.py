"""Shared permissions primitives: risk tiers, signed objects, grants, tickets.

Implements the wire-level parts of ``docs/design/permissions.md`` that both
the hub and workers need:

* the built-in cap tier table (Appendix A) and tier resolution (2.2): the
  effective tier is the *maximum* of the built-in table and what the cap
  declares, so a worker can raise a tier but never lower one; an unknown cap
  is ``exec``;
* canonical JSON and domain-separated ed25519 signatures (4.2). Every signed
  object type has its own prefix, so a body signed as one type can never be
  replayed as another (the deauth-as-manifest gap, 6.3);
* role grants (4.3/4.4), call tickets (3.6), signed announces (proof of
  possession of the grant key), deauth v2 orders and revocation lists;
* a bounded replay cache for ticket message ids.

Stdlib-only: PyNaCl (already what workers use to verify OTA manifests) is
loaded at runtime when a signature is checked, and verification fails closed
without it. Signing takes a nacl ``SigningKey`` from the caller. This module
ships inside the worker bundle.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import json
import logging
import os
import secrets
import time
from typing import Any, Iterable

log = logging.getLogger("rook.core.authz")

# -- tiers -------------------------------------------------------------------

TIERS = ("read", "write", "exec", "admin")
LETTER = {"read": "r", "write": "w", "exec": "x", "admin": "a"}
FROM_LETTER = {v: k for k, v in LETTER.items()}


def norm_tier(value: Any) -> str | None:
    """``"x"`` / ``"exec"`` -> ``"exec"``; anything else -> ``None``."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v in TIERS:
        return v
    return FROM_LETTER.get(v)


def tier_rank(tier: str | None) -> int:
    t = norm_tier(tier)
    return TIERS.index(t) if t else TIERS.index("exec")


def max_tier(*tiers: str | None) -> str:
    known = [norm_tier(t) for t in tiers if norm_tier(t)]
    if not known:
        return "exec"
    return max(known, key=TIERS.index)


def _t(spec: str) -> tuple[str, tuple[str, ...]]:
    """'X s,d' -> ('exec', ('sensitive', 'destructive'))."""
    letter, _, tags = spec.partition(" ")
    names = {"s": "sensitive", "d": "destructive", "p": "physical"}
    return FROM_LETTER[letter.lower()], tuple(names[t] for t in tags.split(",") if t)


# Appendix A.1 (worker caps) and A.2 (hub caps on the reserved worker `rook`).
# Letters: R read, W write, X exec, A admin; tags s/d/p.
_TABLE_SPEC = {
    # core
    "caps.describe": "R", "worker.description_get": "R", "worker.description_set": "W",
    "worker.plugin.list": "R", "worker.plugin.enable": "A", "worker.plugin.disable": "A",
    "customcap.list": "R", "customcap.add": "A", "customcap.remove": "A",
    # self-update
    "worker.status": "R", "worker.check": "A", "worker.apply": "A", "worker.ota_begin": "A",
    "worker.hold": "A", "worker.deauth": "A d", "worker.restart": "A d",
    "worker.reconfigure": "A d", "worker.update": "A d",
    # config
    "worker.config_get": "R", "worker.config_apply": "A d", "worker.config_confirm": "A",
    "worker.config_revert": "A d",
    # enrollment
    "worker.enrollment_status": "R", "worker.enrollment_prepare": "A",
    "worker.enrollment_move_prepare": "A", "worker.enrollment_finish": "A",
    "worker.enrollment_prove": "A",
    # plugins
    "battery.status": "R", "camera.list": "R", "camera.snap": "R s,p",
    "cec.ping": "R p", "cec.send": "W p", "cec.raw": "W p",
    "chat.open": "W", "chat.send": "W", "chat.rooms": "R", "chat.poll": "R s",
    "claude-history.pull": "R s", "claude-history.read": "R s",
    "claude-history.read_page": "R s", "claude-history.read_snapshot": "R s",
    "claude-history.follow": "R s", "claude-history.search": "R s",
    "claude-history.analyze": "R s", "claude-history.export": "R s",
    "claude-history.resumed": "R", "claude-history.send": "X", "claude-history.resume": "X",
    "codex-history.resume": "X",
    "deluge.status": "R", "deluge.list": "R", "deluge.files": "R", "deluge.add": "W",
    "deluge.pause": "W", "deluge.resume": "W", "deluge.remove": "W d",
    "dongle.status": "R p", "dongle.display": "R s,p", "dongle.display_probe": "R p",
    "dongle.keyboard": "X p", "dongle.mouse": "X p", "dongle.consumer": "W p",
    "dongle.release": "W p",
    "file.read": "R s", "file.list": "R", "file.search": "R s", "file.exists": "R",
    "file.write": "X",
    "hermes.status": "R", "hermes.memory.status": "R", "hermes.memory.read": "R s",
    "hermes.skills.list": "R", "hermes.skills.search": "R", "hermes.sessions.list": "R",
    "hermes.sessions.read": "R s", "hermes.mcp.list": "R", "hermes.chat": "X",
    "hermes.run": "X",
    "hid.backend": "R", "hid.type": "X p", "hid.key_combo": "X p", "hid.mouse.move": "X p",
    "hid.mouse.click": "X p", "hid.mouse.drag": "X p",
    "info.host": "R", "info.uptime": "R", "info.ping": "R",
    "log.audit": "R s", "log.tail": "R s",
    "memory.get": "R", "memory.search": "R", "memory.entities": "R", "memory.put": "W",
    "memory.note": "W",
    "msg.send": "W", "msg.read": "R s", "msg.clear": "W d",
    "pikvm.snap": "R s,p", "pikvm.power.status": "R p", "pikvm.api.get": "R s,p",
    "pikvm.type": "X p", "pikvm.key": "X p", "pikvm.mouse.move": "X p",
    "pikvm.mouse.click": "X p", "pikvm.power": "X d,p", "pikvm.api.post": "X d,p",
    "proc.list": "R", "proc.read": "R s", "proc.start": "X", "proc.write": "X",
    "proc.signal": "X d", "proc.close": "X d",
    "screenshot.capture": "R s", "screenshot.capture_region": "R s",
    "screenshot.capture_preview": "R s",
    "shell.which": "R", "shell.env.list": "R s", "shell.env.get": "R s", "shell.exec": "X",
    "agent.wake_info": "R", "agent.wake": "X",
    "work.status": "R", "work.view_page": "R s", "work.adopt_page": "W",
    "work.create": "X", "work.command": "X",
    "work.stream.open": "X", "work.stream.write": "X", "work.stream.signal": "X",
    "work.stream.close": "X", "work.stream.resize": "W",
    "work.stream.read": "R", "work.stream.list": "R", "work.sessions": "R",
    "work.export": "R s",
    "sessions.mirror": "R s",
    # A.2 hub caps (worker `rook`)
    "identity.whoami": "R", "band.workers": "R", "band.caps": "R",
    "secret.list": "R", "secret.log": "R s", "secret.get": "A s", "secret.set": "A d",
    "secret.delete": "A d", "secret.use": "W s",
    "journal.read": "R s", "handoff.read": "R", "handoff.write": "W",
    "chat.read": "R", "chat.write": "W", "chat.delete": "W d", "chat.presence": "R",
    "console.read": "R", "knowledge.read": "R", "knowledge.write": "W",
    "task.read": "R", "task.write": "W",
    "grants.revocations": "R", "policy.explain": "R", "policy.status": "R", "policy.get": "R s", "policy.set": "A",
    "token.admin": "A", "band.admin": "A d", "band.deauth": "A d", "member.admin": "A",
    "guidance.write": "A",
    "hub.info": "R", "hub.plugins": "R",
    # Settings framework caps (settings.md 3.4). Band settings changes are
    # admin: band owners and operator-role tokens (maintainer decision).
    "settings.describe": "R", "settings.get": "R", "settings.history": "R",
    "settings.set": "A", "settings.reset": "A",
}

BUILTIN: dict[str, tuple[str, tuple[str, ...]]] = {c: _t(s) for c, s in _TABLE_SPEC.items()}
# Prefixes whose every cap has a fixed tier (custom command caps are shell).
BUILTIN_PREFIX: tuple[tuple[str, str], ...] = (("cmd.", "exec"),)


def builtin_tier(cap: str) -> str | None:
    """The built-in table's tier for ``cap``, or ``None`` if it has none."""
    for prefix, tier in BUILTIN_PREFIX:
        if cap.startswith(prefix):
            return tier
    hit = BUILTIN.get(cap)
    return hit[0] if hit else None


def builtin_tags(cap: str) -> tuple[str, ...]:
    hit = BUILTIN.get(cap)
    return hit[1] if hit else ()


def effective_tier(cap: str, declared: Any = None, override: Any = None,
                   lower: bool = False) -> str:
    """Resolve a cap's tier (permissions 2.2).

    ``declared`` is what the cap/announce says (word or letter). The built-in
    table is a floor: the result is ``max(builtin, declared)``. ``cmd.*`` is
    always exec. An operator ``override`` may raise the tier freely and lower
    it only with ``lower=True``. Unknown everywhere -> ``exec``.
    """
    for prefix, tier in BUILTIN_PREFIX:
        if cap.startswith(prefix):
            return max_tier(tier, override) if not lower else (norm_tier(override) or tier)
    base = builtin_tier(cap)
    dec = norm_tier(declared)
    if base is None and dec is None:
        tier = "exec"
    else:
        tier = max_tier(*(t for t in (base, dec) if t))
    ov = norm_tier(override)
    if ov:
        tier = ov if lower else max_tier(tier, ov)
    return tier


# -- canonical JSON and signatures ----------------------------------------

PREFIX_GRANT = b"rook-grant-v1\n"
PREFIX_TICKET = b"rook-ticket-v1\n"
PREFIX_REVOCATIONS = b"rook-revocations-v1\n"
PREFIX_ROOT_ROTATE = b"rook-root-rotate-v1\n"
PREFIX_ANNOUNCE = b"rook-announce-v1\n"
PREFIX_DEAUTH = b"rook-deauth-v2\n"
PREFIX_MANIFEST_V2 = b"rook-manifest-v2\n"


def canonical(obj: Any) -> bytes:
    """UTF-8, sorted keys, no whitespace, ASCII-escaped (permissions 3.6)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _body(obj: dict) -> dict:
    return {k: v for k, v in obj.items() if k != "sig"}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text)


def key_id(pub: bytes | str) -> str:
    """First 16 hex of sha256(raw public key). Accepts raw bytes, base64 or
    ``ed25519:<b64>``."""
    if isinstance(pub, str):
        pub = unb64(pub.split(":", 1)[1] if pub.startswith("ed25519:") else pub)
    return hashlib.sha256(pub).hexdigest()[:16]


def pub_b64(sk) -> str:
    """Base64 public key of a nacl SigningKey."""
    return b64(bytes(sk.verify_key))


def sign_obj(sk, prefix: bytes, obj: dict) -> dict:
    """Return ``obj`` with ``sig`` = ed25519 over ``prefix + canonical(obj - sig)``."""
    body = _body(obj)
    sig = sk.sign(prefix + canonical(body)).signature
    return {**body, "sig": b64(sig)}


def _nacl_signing():
    """PyNaCl's signing module. An optional runtime dependency, imported by
    name so rook.core stays importable with the standard library alone (the
    Android app bundles it); without it every verification fails closed."""
    import importlib
    return importlib.import_module("nacl.signing")


def verify_obj(pub: str, prefix: bytes, obj: Any) -> bool:
    """True iff ``obj['sig']`` verifies under ``pub`` for this domain. Never raises."""
    if not isinstance(obj, dict) or not isinstance(obj.get("sig"), str) or not pub:
        return False
    try:
        VerifyKey = _nacl_signing().VerifyKey
        raw = pub.split(":", 1)[1] if pub.startswith("ed25519:") else pub
        VerifyKey(unb64(raw)).verify(prefix + canonical(_body(obj)), unb64(obj["sig"]))
        return True
    except Exception:
        return False


def args_hash(args: Any) -> str:
    """b64url(sha256(canonical(args))) without padding: binds a ticket to the
    exact arguments sent (after secret substitution)."""
    digest = hashlib.sha256(canonical(args if args is not None else {})).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# -- trust anchors -----------------------------------------------------------

def default_anchors() -> list[str]:
    """Root public keys this node trusts: ``ROOK_UPDATE_PUBKEY`` (or the key
    baked into the worker bundle), plus ``~/.rook-band-worker/anchors.json``
    (a list of base64 keys or ``{"root": key}`` objects)."""
    out: list[str] = []
    env = os.environ.get("ROOK_UPDATE_PUBKEY", "").strip()
    if env:
        out.append(env)
    else:
        try:
            from ..worker._update_pubkey import PUBKEY_B64
            if PUBKEY_B64:
                out.append(PUBKEY_B64)
        except Exception:
            pass
    path = os.path.join(os.path.expanduser("~"), ".rook-band-worker", "anchors.json")
    try:
        with open(path, encoding="utf-8") as f:
            extra = json.load(f)
        for a in extra if isinstance(extra, list) else []:
            key = a.get("root") if isinstance(a, dict) else a
            if isinstance(key, str) and key and key not in out:
                out.append(key)
    except (OSError, ValueError):
        pass
    return out


# -- grants ------------------------------------------------------------------

KNOWN_ROLES = frozenset({"is_hub"})
RESERVED_HUB_NAME = "rook"
GRANT_LIFETIME = 7 * 86400
GRANT_GRACE = 3600


def make_grant(root_sk, subject_pub: str, role: str, bands: Iterable[str], *,
               name: str | None = None, worker_id: str | None = None,
               device_id: str | None = None, constraints: dict | None = None,
               lifetime: int = GRANT_LIFETIME, now: float | None = None) -> dict:
    now = int(now if now is not None else time.time())
    sub: dict[str, Any] = {"key": f"ed25519:{subject_pub}", "kid": key_id(subject_pub)}
    if worker_id:
        sub["worker_id"] = worker_id
    if device_id:
        sub["device_id"] = device_id
    body: dict[str, Any] = {
        "typ": "rook-grant", "v": 1, "serial": secrets.token_hex(16),
        "iss": key_id(pub_b64(root_sk)), "sub": sub, "role": role,
        "scope": {"bands": sorted(set(bands))},
        "constraints": constraints or {"max_tier": "admin"},
        "iat": now, "nbf": now, "exp": now + int(lifetime),
    }
    if name:
        body["name"] = name
    return sign_obj(root_sk, PREFIX_GRANT, body)


def verify_grant(grant: Any, anchors: Iterable[str], *, band: str | None = None,
                 now: float | None = None, revoked: Iterable[str] = (),
                 worker_id: str | None = None) -> tuple[bool, str]:
    """Check a grant (permissions 4.4). Returns ``(ok, reason)``.

    ``band`` (hex band id) must be in ``scope.bands`` when given. A
    ``sub.worker_id`` binding is checked against ``worker_id`` when both are
    present. Key possession is checked separately (:func:`verify_announce`).
    """
    if not isinstance(grant, dict) or grant.get("typ") != "rook-grant" or grant.get("v") != 1:
        return False, "not a v1 grant"
    iss = grant.get("iss")
    root = next((a for a in anchors if a and key_id(a) == iss), None)
    if root is None:
        return False, "issuer is not a trusted root"
    if not verify_obj(root, PREFIX_GRANT, grant):
        return False, "bad grant signature"
    now = now if now is not None else time.time()
    try:
        nbf, exp = int(grant.get("nbf", 0)), int(grant.get("exp", 0))
    except (TypeError, ValueError):
        return False, "bad validity window"
    if now + GRANT_GRACE < nbf or now > exp + GRANT_GRACE:
        return False, "grant expired or not yet valid"
    if grant.get("role") not in KNOWN_ROLES:
        return False, "unknown role"
    bands = (grant.get("scope") or {}).get("bands") or []
    if band is not None and band not in bands:
        return False, "band not in grant scope"
    if grant.get("serial") in set(revoked):
        return False, "grant revoked"
    sub = grant.get("sub") or {}
    if not isinstance(sub.get("key"), str) or key_id(sub["key"]) != sub.get("kid"):
        return False, "bad grant subject"
    if worker_id and sub.get("worker_id") and sub["worker_id"] != worker_id:
        return False, "grant bound to another worker"
    if grant.get("role") == "is_hub" and grant.get("name") not in (None, RESERVED_HUB_NAME):
        return False, "is_hub grant with a non-reserved name"
    return True, "ok"


def grant_max_tier(grant: dict) -> str:
    return norm_tier((grant.get("constraints") or {}).get("max_tier")) or "admin"


# -- signed announces (proof of possession) ---------------------------------

ANNOUNCE_FRESH_SECS = 90


def announce_body(msg: dict) -> dict:
    return {"worker_id": msg.get("worker_id"), "name": msg.get("name"),
            "caps": list(msg.get("caps") or []), "ts": msg.get("ts"), "seq": msg.get("seq")}


def sign_announce(sk, msg: dict, *, seq: int, now: float | None = None) -> dict:
    """Add ``ts``/``seq`` and ``asig`` (``{kid, sig}``) to an announce, signed
    with the grant subject's key."""
    msg = dict(msg)
    msg["ts"] = int(now if now is not None else time.time())
    msg["seq"] = int(seq)
    sig = sk.sign(PREFIX_ANNOUNCE + canonical(announce_body(msg))).signature
    msg["asig"] = {"kid": key_id(pub_b64(sk)), "sig": b64(sig)}
    return msg


def verify_announce(msg: dict, grant: dict, *, now: float | None = None) -> bool:
    """True iff ``msg`` is signed by ``grant.sub.key`` and fresh."""
    asig = msg.get("asig")
    sub = (grant or {}).get("sub") or {}
    if not isinstance(asig, dict) or asig.get("kid") != sub.get("kid"):
        return False
    now = now if now is not None else time.time()
    try:
        if abs(now - int(msg.get("ts"))) > ANNOUNCE_FRESH_SECS:
            return False
    except (TypeError, ValueError):
        return False
    return verify_obj(sub.get("key", ""), PREFIX_ANNOUNCE,
                      {**announce_body(msg), "sig": asig.get("sig")})


def held_roles(msg: dict, anchors: Iterable[str], *, band: str | None = None,
               now: float | None = None, revoked: Iterable[str] = ()) -> dict[str, dict]:
    """Roles an announce proves: ``{role: grant}`` for each grant that verifies
    *and* whose key signed this announce. A grant copied into someone else's
    announce proves nothing."""
    grants = msg.get("grants")
    if not isinstance(grants, list):
        return {}
    anchors = list(anchors)
    out: dict[str, dict] = {}
    for g in grants[:4]:
        ok, _ = verify_grant(g, anchors, band=band, now=now, revoked=revoked,
                             worker_id=str(msg.get("worker_id") or "") or None)
        if ok and verify_announce(msg, g, now=now):
            out[g["role"]] = g
    return out


# -- tickets -----------------------------------------------------------------

TICKET_LIFETIME = 30
TICKET_SKEW = 300


def make_ticket(sk, kid: str, *, principal: str, via: Iterable[str] = (), cap: str,
                target: str, msg_id: str, args: Any, tier: str, rev: int,
                now: float | None = None, grant: dict | None = None) -> dict:
    now = int(now if now is not None else time.time())
    body: dict[str, Any] = {
        "v": 1, "kid": kid, "p": principal, "via": list(via), "cap": cap, "t": target,
        "id": msg_id, "ah": args_hash(args), "tier": LETTER.get(tier, "x"),
        "rev": int(rev), "iat": now, "exp": now + TICKET_LIFETIME,
    }
    signed = sign_obj(sk, PREFIX_TICKET, body)
    if grant is not None:
        signed["grant"] = grant  # not covered by sig; verified on its own
    return signed


class ReplayCache:
    """Message ids seen within the ticket window; bounded LRU (3.6)."""

    def __init__(self, max_entries: int = 10000, window: float = TICKET_LIFETIME + TICKET_SKEW):
        self._seen: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._max = max_entries
        self._window = window

    def seen(self, msg_id: str, now: float | None = None) -> bool:
        """Record ``msg_id``; True if it was already present (a replay)."""
        now = now if now is not None else time.time()
        while self._seen:
            first, ts = next(iter(self._seen.items()))
            if now - ts > self._window or len(self._seen) > self._max:
                self._seen.popitem(last=False)
            else:
                break
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = now
        if len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return False


def verify_ticket(ticket: Any, *, cap: str, target: str, msg_id: str, args: Any,
                  keys: dict[str, dict], now: float | None = None,
                  replay: ReplayCache | None = None) -> tuple[bool, str]:
    """Check a ticket against the envelope it arrived in (3.6).

    ``keys`` maps an op-key ``kid`` to its verified ``is_hub`` grant. Returns
    ``(ok, reason)``; the replay cache is only updated for otherwise-valid
    tickets."""
    if not isinstance(ticket, dict) or ticket.get("v") != 1:
        return False, "no ticket"
    grant = keys.get(ticket.get("kid"))
    if grant is None:
        return False, "unknown ticket key"
    pub = ((grant.get("sub") or {}).get("key")) or ""
    body = {k: v for k, v in ticket.items() if k != "grant"}
    if not verify_obj(pub, PREFIX_TICKET, body):
        return False, "bad ticket signature"
    if body.get("t") != target:
        return False, "ticket for another worker"
    if body.get("id") != msg_id:
        return False, "ticket for another message"
    if body.get("cap") != cap:
        return False, "ticket for another cap"
    if body.get("ah") != args_hash(args):
        return False, "ticket args mismatch"
    now = now if now is not None else time.time()
    try:
        iat, exp = int(body["iat"]), int(body["exp"])
    except (KeyError, TypeError, ValueError):
        return False, "bad ticket window"
    if not (iat - TICKET_SKEW <= now <= exp + TICKET_SKEW):
        return False, "ticket expired"
    tier = norm_tier(body.get("tier")) or "exec"
    if tier_rank(tier) > tier_rank(grant_max_tier(grant)):
        return False, "ticket tier above grant constraint"
    if replay is not None and replay.seen(msg_id, now):
        return False, "ticket replayed"
    return True, "ok"


# -- deauth v2 ---------------------------------------------------------------

DEAUTH_MAX_AGE = 86400


def make_deauth(root_sk, worker_id: str, name: str = "", reason: str = "",
                now: float | None = None) -> dict:
    body = {"typ": "rook-deauth", "v": 2, "worker_id": worker_id, "name": name,
            "issued_at": int(now if now is not None else time.time()),
            "reason": (reason or "")[:500]}
    return sign_obj(root_sk, PREFIX_DEAUTH, body)


def verify_deauth(payload: Any, anchors: Iterable[str], worker_id: str | None,
                  now: float | None = None) -> tuple[bool, str]:
    """A deauth v2 order: own domain prefix, and ``worker_id`` and
    ``issued_at`` are required (6.3). Accepts the order itself or a legacy
    payload carrying it under ``v2``."""
    if isinstance(payload, dict) and isinstance(payload.get("v2"), dict):
        payload = payload["v2"]
    if not isinstance(payload, dict) or payload.get("typ") != "rook-deauth" or payload.get("v") != 2:
        return False, "not a signed deauth v2 order"
    if not any(verify_obj(a, PREFIX_DEAUTH, payload) for a in anchors if a):
        return False, "invalid or missing signature"
    target = payload.get("worker_id")
    if not isinstance(target, str) or not target:
        return False, "deauth order names no worker_id"
    if not worker_id or target != worker_id:
        return False, "worker_id mismatch (not this worker)"
    issued = payload.get("issued_at")
    if not isinstance(issued, int) or isinstance(issued, bool):
        return False, "deauth order has no issued_at"
    now = now if now is not None else time.time()
    if abs(now - issued) > DEAUTH_MAX_AGE:
        return False, "signed order too old"
    return True, "ok"


# -- revocation lists --------------------------------------------------------

def make_revocations(root_sk, seq: int, serials: Iterable[str], now: float | None = None) -> dict:
    return sign_obj(root_sk, PREFIX_REVOCATIONS, {
        "typ": "rook-revocations", "seq": int(seq),
        "iat": int(now if now is not None else time.time()), "serials": sorted(set(serials))})


def verify_revocations(obj: Any, anchors: Iterable[str]) -> bool:
    return (isinstance(obj, dict) and obj.get("typ") == "rook-revocations"
            and isinstance(obj.get("seq"), int)
            and any(verify_obj(a, PREFIX_REVOCATIONS, obj) for a in anchors if a))
