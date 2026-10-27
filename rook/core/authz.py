"""Shared permissions primitives: risk tiers, signed objects, grants, tickets.

Implements the wire-level parts of ``docs/design/permissions.md`` that both
the hub and workers need:

* the tier floor (Appendix A: the core caps' table plus the built-in
  plugins' own declared tiers) and tier resolution (2.2): the effective tier
  is the *maximum* of the floor and what the cap declares, so a worker can
  raise a tier but never lower one; an unknown cap is ``exec``;
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
import threading
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


# The floor (permissions 2.2, Appendix A) has two parts:
#
# * ``CORE``: caps no plugin declares - registered by the worker and hub cores
#   themselves (caps.describe, worker.plugin.*, customcap.*) and the hub's
#   own MCP tools authorized as caps on `rook` (secret.*, band.*, token.admin),
#   plus the Android app's native caps, whose plugin source only the app has.
# * every built-in plugin's ``@capability(risk=..., tags=..., scope="multi-user")``, read from the
#   plugin source shipped with this node (:func:`builtin_table`). The
#   decorator is the one place a plugin cap's tier is written; this node's
#   copy of it is still a floor, so a peer running modified code cannot
#   announce ``shell.exec`` as ``read``.
#
# Letters: R read, W write, X exec, A admin; tags s/d/p.
_CORE_SPEC = {
    # worker core (rook.worker.core, rook.worker.admin)
    "caps.describe": "R", "worker.description_get": "R", "worker.description_set": "W",
    "worker.settings_report": "R",
    # disable/reload stop a running plugin (its jobs, streams, connections).
    "worker.plugin.list": "R", "worker.plugin.enable": "A", "worker.plugin.disable": "A d",
    "worker.plugin.reload": "A d",
    "customcap.list": "R", "customcap.add": "A", "customcap.remove": "A",
    # A.2: hub tools authorized as caps on the reserved worker `rook`
    "identity.whoami": "R", "band.workers": "R", "band.caps": "R",
    "secret.list": "R", "secret.log": "R s", "secret.get": "A s", "secret.set": "A d",
    "secret.delete": "A d", "secret.use": "W s",
    "journal.read": "R s", "handoff.read": "R", "handoff.write": "W",
    "console.read": "R",
    "grants.revocations": "R",
    "token.admin": "A", "band.admin": "A d", "band.deauth": "A d", "member.admin": "A",
    "guidance.write": "A",
    # hub core (rook.hub.node)
    "hub.plugin.enable": "A", "hub.plugin.disable": "A d", "hub.plugin.reload": "A d",
    # The Android app's native caps (android/.../rook_android/plugins). The
    # app adds that package to its own floor, but the hub and Python workers
    # do not ship it, so without these rows the hub would take an Android
    # peer's announced tier at face value. Kept at the tier they resolved to
    # on master (undeclared -> exec) so none is lower than before; lowering
    # one is a deliberate decision made here and in the plugin together.
    # tests/test_tier_snapshot.py pins both against master and each other.
    # (battery.*, hid.*, screenshot.* share their names with Python worker
    # plugins, whose declared tiers already cover them.)
    "calendar.list": "R s",
    # The owner's own calendar, changed for them (2026-10-23); deleting is not undone.
    "calendar.create": "X", "calendar.update": "X", "calendar.delete": "X d",
    # A notification's own actions; acting on one can reply to someone as the owner.
    "notify.actions": "R s", "notify.act": "X",
    "calllog.list": "X s", "contacts.search": "X s", "location.get": "X s",
    "sms.list": "X s", "sms.send": "X", "ui.text": "X s",
    "notify.list": "X s", "notify.dismiss": "X", "notify.post": "X",
    # update_status, info, unlock_status and keep_awake_status only report state:
    # declared read (2026-10-23, with the plugin; see STATUS_READS).
    "device.update": "A d", "device.update_status": "R", "device.info": "R",
    "device.find": "X p", "device.find_stop": "X p", "device.wake": "X p",
    "device.vibrate": "X p", "device.torch": "X p", "device.unlock": "X s,p",
    "device.clipboard_set": "X", "device.clipboard_get": "X s",
    "device.open_url": "X", "device.launch": "X",
    "device.unlock_status": "R", "device.keep_awake": "X p",
    "device.keep_awake_status": "R", "device.keep_awake_settings": "X",
    "ui.nodes": "X s", "ui.click": "X", "ui.scroll": "X", "ui.back": "X",
    # Driving the phone in fewer calls (2026-10-27): ui.screen/ui.wait return
    # the screen like ui.nodes; ui.type types like hid.type. device.apps lists
    # the launcher; media.now what is playing (both reads of private content).
    "ui.screen": "X s", "ui.wait": "X s", "ui.type": "X",
    "device.apps": "R s", "device.intent": "X", "device.sound": "X p",
    "media.now": "R s", "media.control": "X p",
    "voice.speak": "W p", "voice.speak_status": "R", "voice.replies": "R",
    "voice.speak_stop": "W", "voice.speak_voices": "R",
    # The app's attention surface (docs/design/attention.md, attention-phone.md):
    # only the hub's attention plugin should post cards or read taps and notice
    # summaries. A card's Yes is relayed as the person's decision, so an agent
    # that could post one could word the question; admin keeps agents' calls held.
    # The feed and the rules are summaries the phone keeps (like notify.list).
    # attention.remind speaks on the phone after a quiet check (physical, like post).
    "attention.post": "A p", "attention.update": "A", "attention.poll": "A s",
    "attention.remind": "A p",
    "attention.rules_set": "A", "attention.feed": "X s", "attention.rules_get": "X s",
}

CORE: dict[str, tuple[str, tuple[str, ...]]] = {c: _t(s) for c, s in _CORE_SPEC.items()}

#: Reads of a device's state with no private content in them: battery, model and
#: Android version, uptime, app version and update progress, the worker's own caps
#: and description, the voice engine's status. These never need anyone's approval,
#: on any device, family devices included, whatever tier an older app still
#: announces for them (an app released before they were declared read says exec).
#: Private content (messages, notifications, contacts, call log, calendar, location,
#: screen, clipboard, files) is not here: on a protected device those still ask.
#: (Not keep_awake_status or unlock_status: whether a kid's screen is on or locked, and
#: whether a PIN is saved, say more than a version does. Review of #186.)
STATUS_READS = frozenset({
    "battery.status", "device.info", "device.update_status", "info.ping", "info.host",
    "info.uptime", "caps.describe", "worker.description_get", "voice.speak_status",
    "voice.speak_voices",
})


def is_status_read(cap: str) -> bool:
    """A read of a device's state with nothing private in it (:data:`STATUS_READS`)."""
    return str(cap or "") in STATUS_READS
# Prefixes whose every cap has a fixed tier (custom command caps are shell).
BUILTIN_PREFIX: tuple[tuple[str, str], ...] = (("cmd.", "exec"),)

#: Plugin packages whose declared tiers join the floor, when present on this
#: node (a worker bundle has no rook.hub; the Android app adds its own).
FLOOR_PACKAGES = ["rook.worker.plugins", "rook.hub.plugins"]

_floor: dict[str, tuple[str, tuple[str, ...]]] | None = None
_floor_lock = threading.RLock()
_floor_loading = False


def declared_floor(packages: Iterable[str]) -> dict[str, tuple[str, tuple[str, ...]]]:
    """``{cap: (tier, tags)}`` from every Plugin and Device class under
    ``packages`` (loaded or not; a Device's caps are those of the device
    workers its delegate plugin announces). A cap declared by several classes
    gets the highest tier and every tag. Packages or modules that do not
    import are skipped."""
    import importlib
    import inspect
    import pkgutil
    from .devices import Device
    from .plugin import Plugin
    out: dict[str, tuple[str, tuple[str, ...]]] = {}
    for package in packages:
        try:
            pkg = importlib.import_module(package)
        except Exception:
            continue
        for info in pkgutil.walk_packages(pkg.__path__, package + "."):
            try:
                mod = importlib.import_module(info.name)
            except Exception:
                log.warning("tier floor: %s does not import here; its caps fall back to "
                            "what they announce", info.name)
                continue
            for _, cls in inspect.getmembers(mod, inspect.isclass):
                if not (issubclass(cls, (Plugin, Device)) and cls not in (Plugin, Device)
                        and cls.__module__ == mod.__name__ and cls.NAMESPACE):
                    continue
                for cap, fn in cls.declared_caps().items():
                    meta = getattr(fn, "_rook_cap_meta", None)
                    tier = norm_tier(getattr(meta, "risk", None))
                    if tier is None:
                        continue
                    tags = tuple(getattr(meta, "tags", ()) or ())
                    if cap in out:
                        old_tier, old_tags = out[cap]
                        tier = max_tier(tier, old_tier)
                        tags = tuple(dict.fromkeys(old_tags + tags))
                    out[cap] = (tier, tags)
    return out


def builtin_table() -> dict[str, tuple[str, tuple[str, ...]]]:
    """The whole floor: :data:`CORE` plus the declared tiers of the built-in
    plugins (:data:`FLOOR_PACKAGES`). Built once per process, on first use."""
    global _floor, _floor_loading
    if _floor is not None:
        return _floor
    with _floor_lock:
        if _floor is not None:
            return _floor
        if _floor_loading:
            return CORE  # a plugin module resolving a tier while it imports
        _floor_loading = True
        try:
            table = declared_floor(FLOOR_PACKAGES)
        finally:
            _floor_loading = False
        for cap, (tier, tags) in CORE.items():
            if cap in table:
                tier = max_tier(tier, table[cap][0])
                tags = tuple(dict.fromkeys(tags + table[cap][1]))
            table[cap] = (tier, tags)
        _floor = table
        return _floor


def add_floor_package(package: str) -> None:
    """Add a plugin package to the floor (the Android app's native plugins).
    Takes effect immediately, even if the floor was already built."""
    global _floor
    with _floor_lock:
        if package not in FLOOR_PACKAGES:
            FLOOR_PACKAGES.append(package)
        _floor = None


def __getattr__(name: str) -> Any:
    # ``BUILTIN`` was the hand-written table; it is now built from the plugins
    # on first use, so it is served lazily (importing this module stays cheap).
    if name == "BUILTIN":
        return builtin_table()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def builtin_tier(cap: str) -> str | None:
    """The floor's tier for ``cap``, or ``None`` if it has none."""
    for prefix, tier in BUILTIN_PREFIX:
        if cap.startswith(prefix):
            return tier
    hit = builtin_table().get(cap)
    return hit[0] if hit else None


def builtin_tags(cap: str) -> tuple[str, ...]:
    hit = builtin_table().get(cap)
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
    baked into the worker bundle), plus ``anchors.json`` in the worker state directory
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
    from .paths import worker_state_path
    path = str(worker_state_path("anchors.json"))
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
