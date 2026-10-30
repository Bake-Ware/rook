"""The hub's side of enrollment (spec 12) and of adding an enrolled worker
to another hub's band (9.1, 9.3). The HTTP door calls these; each returns a
JSON-able dict with ``ok``.

Signing a device in, two ways, both ending in the same config bundle:

- **One-time code** (the QR): a person asks for a code (:meth:`new_code`);
  the QR carries ``rook://enroll?url=…&code=…&hub=…``; the device claims it
  with no token (:meth:`claim`). One use, ten minutes.
- **Device code**, for devices without a camera or keyboard worth using: the
  device asks for one (:meth:`device_start`) and shows the short user code;
  the person approves it from the phone or the dashboard (:meth:`pending`,
  :meth:`approve`); the device polls (:meth:`device_poll`) and gets the
  bundle once.

The person must be allowed to set up a machine: every layer of their caller
permits ``core.enroll`` (admin, persists) as if it were a call. Owners and
adults may; children and guests may not; an agent only for a person who may.
The enrolling person owns the device (9.1).

Other bands **invite, never pull** (9.1): an owner of the other hub makes an
invite (:meth:`invite`: its hub id, root key, band, connection details and a
suggested role and ceiling); an owner of the home hub brings it here and
picks the role and ceiling (:meth:`membership`), which signs a
``rook-membership`` with the root key and sends it with ``core.trust.set``
like any call. The same route narrows the home band's ceiling (role
``home``) or takes a worker off a band (``removed``).

State: pending codes live in memory (a restart drops them; the device asks
again). In ``<data>``: ``enrolled.json`` (each enrolled device: name, owner),
``bands.json`` (other hubs' bands from accepted invites, with their PSKs:
private), ``memberships.json`` (the documents this hub signed).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .. import CORE_VERSION, rights, wire

log = logging.getLogger("rook.hub.enroll")

CODE_SECS = 600               # one-time and device codes
POLL_EVERY = 2                # seconds between a device's polls
MAX_PENDING = 32              # of each kind of code; the device routes take no token
USER_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"     # no vowels (no words), no 0/O or 1/I
ENROLL = {"cap": "core.enroll", "risk": "admin", "effects": ["persists"], "target": "rook"}
FULL = {"grants": ["*"]}
INVITE_ROLES = ("guest", "co-owner")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
HEX32 = re.compile(r"[0-9a-f]{32}")
PSK_REF = re.compile(r"\{\{secret:band-psk-([0-9a-f]{32})\}\}")
OPTIONS = ("url", "relay", "ws_url", "discover", "services")


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _load(path: Path) -> dict:
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def recovery_key(data: Path) -> str:
    """The owner's recovery key (castling-backup): made once, with the
    owner, a plain string to save however they like; owners can see it again
    while the hub lives, and need it only if the hub dies.
    TODO(castling:backup): off-site backups are encrypted with it."""
    path = Path(data) / "keys" / "recovery.key"
    try:
        return path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        pass
    raw = base64.b32encode(secrets.token_bytes(20)).decode("ascii")       # 160 bits, 32 characters
    key = "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(path, key + "\n")
    return key


def user_code() -> str:
    """A short code a person reads off a device and types: ``BCDF-GHJK``."""
    c = "".join(secrets.choice(USER_ALPHABET) for _ in range(8))
    return f"{c[:4]}-{c[4:]}"


def _user_code_of(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    c = re.sub(r"[\s-]", "", text).upper()
    return f"{c[:4]}-{c[4:]}" if len(c) == 8 and all(ch in USER_ALPHABET for ch in c) else None


def check_options(opts: Any) -> None:
    """The hub config's ``enroll:`` block."""
    if not isinstance(opts, dict):
        raise ValueError("enroll must be a mapping")
    unknown = sorted(set(opts) - set(OPTIONS))
    if unknown:
        raise ValueError(f"enroll: unknown keys: {', '.join(unknown)}")
    for k in ("url", "relay", "ws_url"):
        if opts.get(k) is not None and not isinstance(opts[k], str):
            raise ValueError(f"enroll.{k} must be a string")
    if opts.get("discover") and not opts.get("url"):
        raise ValueError("enroll.discover needs enroll.url (the address devices are told)")
    services = opts.get("services") or {}
    if not isinstance(services, dict) or not all(isinstance(s, dict) and isinstance(s.get("url"), str)
                                                 for s in services.values()):
        raise ValueError("enroll.services: each service is {url, key | key_env, presets?}")


def check_invite(inv: Any) -> str | None:
    """Why ``inv`` isn't a usable invite, or None."""
    if not isinstance(inv, dict) or inv.get("typ") != "rook-invite" or inv.get("v") != 2:
        return "not a rook-invite"
    try:
        if inv.get("hub") != wire.key_id(inv.get("root")):
            return "its hub is not key_id(root)"
    except (AttributeError, TypeError, ValueError, IndexError):
        return "bad root key"
    band, connect = inv.get("band"), inv.get("connect")
    if not (isinstance(band, str) and HEX32.fullmatch(band)):
        return "bad band"
    if not (isinstance(connect, dict) and isinstance(connect.get("psk"), str)
            and (connect.get("relay") or connect.get("ws_url"))):
        return "connect needs psk and relay or ws_url"
    from rook.core2.transport import band_of
    if band_of(connect["psk"]) != band:
        return "its PSK is for another band"
    if inv.get("role") not in INVITE_ROLES:
        return "role must be guest or co-owner"
    if inv.get("ceiling") is not None and not rights.valid_rights(inv["ceiling"]):
        return "bad ceiling"
    return None


class Enrollment:
    """One per hub (``hub.enroll``). ``hub`` is the running
    :class:`~rook.core2.hub.runtime.Hub`; ``options`` its ``enroll:`` block."""

    def __init__(self, hub, options: dict | None = None):
        check_options(options or {})
        self.hub, self.opts = hub, dict(options or {})
        self.codes: dict[str, dict] = {}      # digest(code) -> {person, exp}
        self.devices: dict[str, dict] = {}    # digest(device code) -> {user_code, name, exp, by?, refused?}
        self.enrolled = _load(hub.data / "enrolled.json")       # worker_id -> {name, owner, at, via}
        self.bands = _load(hub.data / "bands.json")             # band -> {hub, root, psk, relay?, ws_url?}
        self.issued = _load(hub.data / "memberships.json")      # worker_id -> {"<hub> <band>": doc}

    # -- who may ------------------------------------------------------------------

    def _may_enroll(self, caller) -> str | None:
        ids = self.hub.identities
        if not caller.person or ids.user(caller.person) is None:
            return "denied: setting up a machine needs a person"
        if ids.expired(caller.person, self.hub.clock()):
            return "denied: expired"
        for layer, r in ids.layers(caller):
            if rights.denies(r, ENROLL):
                return f"denied: {layer} may not set up machines"
        return None

    def _owner_alone(self, caller) -> str | None:
        """Things that reveal a secret (the recovery key, a band's PSK) are
        for an owner in person, not an agent with their token."""
        if caller.agent or not self.hub.identities.is_owner(caller.person):
            return "denied: only an owner, signed in as themselves"
        return None

    def _expire(self) -> None:
        now = self.hub.clock()
        for store in (self.codes, self.devices):
            for k in [k for k, v in store.items() if v["exp"] < now]:
                del store[k]

    # -- one-time codes (the QR) ------------------------------------------------------

    def url(self, base: str | None = None) -> str | None:
        """The address devices are told: ``enroll.url``, else the one the request came in on."""
        return self.opts.get("url") or base

    async def new_code(self, caller, base: str | None = None) -> dict:
        if (err := self._may_enroll(caller)):
            return {"ok": False, "error": err}
        self._expire()
        if len(self.codes) >= MAX_PENDING:
            return {"ok": False, "error": "busy: too many codes waiting; try again in a few minutes"}
        code, exp = secrets.token_urlsafe(16), int(self.hub.clock() + CODE_SECS)
        self.codes[_digest(code)] = {"person": caller.person, "exp": exp}
        url = self.url(base)
        qr = "rook://enroll?" + urlencode({"url": url or "", "code": code, "hub": self.hub.hub_id})
        return {"ok": True, "code": code, "exp": exp, "url": url, "hub": self.hub.hub_id, "qr": qr}

    async def claim(self, body: dict) -> dict:
        """``{code, name}`` from the device (no token): the bundle, once."""
        name, code = body.get("name"), body.get("code")
        if not (isinstance(name, str) and NAME.fullmatch(name) and name != "rook"):
            return {"ok": False, "error": "bad request: name is a host-like name (not 'rook')"}
        rec = self.codes.pop(_digest(code), None) if isinstance(code, str) else None
        if rec is None or rec["exp"] < self.hub.clock():
            return {"ok": False, "error": "unknown or expired code"}
        return self._bundle(rec["person"], name, "code")

    # -- device codes ------------------------------------------------------------------

    async def device_start(self, body: dict) -> dict:
        """``{name}`` from the device (no token): a device code to poll with
        and the user code to show."""
        name = body.get("name")
        if not (isinstance(name, str) and NAME.fullmatch(name) and name != "rook"):
            return {"ok": False, "error": "bad request: name is a host-like name (not 'rook')"}
        self._expire()
        if len(self.devices) >= MAX_PENDING:
            return {"ok": False, "error": "busy: too many devices waiting; try again in a few minutes"}
        taken = {d["user_code"] for d in self.devices.values()}
        user = user_code()
        while user in taken:
            user = user_code()
        device, exp = secrets.token_urlsafe(32), int(self.hub.clock() + CODE_SECS)
        self.devices[_digest(device)] = {"user_code": user, "name": name, "exp": exp}
        log.info("device waiting", extra={"fields": {"name": name, "code": user}})
        return {"ok": True, "device_code": device, "user_code": user, "hub": self.hub.hub_id,
                "exp": exp, "interval": POLL_EVERY}

    async def device_poll(self, body: dict) -> dict:
        """``{device_code}``: still pending, refused, or the bundle (once)."""
        key = _digest(body["device_code"]) if isinstance(body.get("device_code"), str) else None
        d = self.devices.get(key) if key else None
        if d is None or d["exp"] < self.hub.clock():
            self.devices.pop(key, None)
            return {"ok": False, "error": "unknown or expired device code"}
        if d.get("refused"):
            del self.devices[key]
            return {"ok": False, "error": "refused by the person"}
        if not d.get("by"):
            return {"ok": True, "pending": True, "interval": POLL_EVERY}
        del self.devices[key]
        return self._bundle(d["by"], d["name"], "device code")

    async def pending(self, caller) -> dict:
        """Devices waiting for someone to approve their code."""
        if (err := self._may_enroll(caller)):
            return {"ok": False, "error": err}
        self._expire()
        return {"ok": True, "devices": [{"user_code": d["user_code"], "name": d["name"], "exp": d["exp"]}
                                        for d in self.devices.values() if not d.get("by") and not d.get("refused")]}

    async def approve(self, caller, body: dict) -> dict:
        """``{code, decision?: approve | refuse}``: the person answers a device's code."""
        if (err := self._may_enroll(caller)):
            return {"ok": False, "error": err}
        decision = body.get("decision", "approve")
        if decision not in ("approve", "refuse"):
            return {"ok": False, "error": "bad request: decision is approve or refuse"}
        code = _user_code_of(body.get("code"))
        self._expire()
        d = next((d for d in self.devices.values() if code and d["user_code"] == code
                  and not d.get("by") and not d.get("refused")), None)
        if d is None:
            return {"ok": False, "error": "unknown or expired code"}
        if decision == "refuse":
            d["refused"] = True
        else:
            d["by"] = caller.person
        log.info("device code answered", extra={"fields": {"name": d["name"], "decision": decision,
                                                           "by": caller.person}})
        return {"ok": True, "name": d["name"], "decision": decision}

    # -- the config bundle (12) --------------------------------------------------------------

    def _band(self) -> dict:
        """The home band as a device joins it: id, PSK, the relay or WebSocket
        address devices use (``enroll.relay``/``ws_url``, else the hub's own)."""
        bands = (self.hub.cfg.transport or {}).get("bands") or []
        first = bands[0] if bands else {}
        psk = first.get("psk") or os.environ.get(first.get("psk_env") or "", "")
        if not psk:
            raise ValueError("this hub has no band to join (transport.bands)")
        out = {"id": self.hub.band, "psk": psk, "relay": self.opts.get("relay") or first.get("relay"),
               "ws_url": self.opts.get("ws_url") or first.get("ws_url")}
        return {k: v for k, v in out.items() if v}

    def _services(self, person: str) -> dict:
        """``enroll.services`` this person's preset may have, keys resolved."""
        preset = self.hub.identities.user(person).preset
        out = {}
        for name, s in (self.opts.get("services") or {}).items():
            if s.get("presets") and preset not in s["presets"]:
                continue
            key = s.get("key") or (os.environ.get(s["key_env"]) if s.get("key_env") else None)
            out[name] = {"url": s["url"], **({"key": key} if key else {})}
        return out

    def _bundle(self, person: str, name: str, via: str) -> dict:
        from ..hub.identity import Caller
        if (err := self._may_enroll(Caller(person=person))):      # rights may have changed since
            return {"ok": False, "error": err}
        try:
            band = self._band()
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        wid, now = secrets.token_hex(16), int(self.hub.clock())
        self.enrolled[wid] = {"name": name, "owner": person, "at": now, "via": via}
        _write_private(self.hub.data / "enrolled.json", json.dumps(self.enrolled, indent=1))
        self.hub._journal({"kind": "enroll", "ts": now, "person": person, "worker": wid,
                           "name": name, "via": via, "ok": True})
        log.info("device enrolled", extra={"fields": {"worker": wid, "name": name, "owner": person, "via": via}})
        return {"ok": True, "bundle": {
            "hub": {"hub_id": self.hub.hub_id, "root": wire.pub_text(self.hub.root), "url": self.opts.get("url")},
            "band": band, "worker_id": wid, "name": name, "owner": person,
            "services": self._services(person), "core": CORE_VERSION}}

    def owner_of(self, worker_id: str) -> str | None:
        return (self.enrolled.get(worker_id) or {}).get("owner")

    async def recovery(self, caller) -> dict:
        if (err := self._owner_alone(caller)):
            return {"ok": False, "error": err}
        return {"ok": True, "recovery_key": recovery_key(self.hub.data)}

    # -- other bands (9.1, 9.3) ---------------------------------------------------------------

    async def invite(self, caller, body: dict) -> dict:
        """``{role?: guest | co-owner, ceiling?}``: an invite to this hub's
        band, for an owner of a worker's home hub to accept there. It carries
        the band's PSK: treat it like one."""
        if (err := self._owner_alone(caller)):
            return {"ok": False, "error": err}
        role = body.get("role", "guest")
        ceiling = body.get("ceiling", FULL if role == "co-owner" else None)
        if role not in INVITE_ROLES:
            return {"ok": False, "error": "bad request: role is guest or co-owner"}
        if not rights.valid_rights(ceiling):
            return {"ok": False, "error": "bad request: a ceiling (rights: {grants, max_risk?, effects?}) is required"}
        try:
            band = self._band()
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        connect = {k: band[k] for k in ("psk", "relay", "ws_url") if k in band}
        return {"ok": True, "invite": {"typ": "rook-invite", "v": 2, "hub": self.hub.hub_id,
                                       "root": wire.pub_text(self.hub.root), "band": band["id"],
                                       "connect": connect, "role": role, "ceiling": ceiling}}

    async def membership(self, caller, body: dict, door: str) -> dict:
        """``{worker, why, invite? | band?, role?, ceiling?}`` (owners): add
        the worker to an invite's band, change its role or ceiling there, take
        it off (``role: removed``), or narrow its home band (``role: home``).
        Signs the membership and sends it with ``core.trust.set``."""
        hub = self.hub
        if not hub.identities.is_owner(caller.person):
            return {"ok": False, "error": "denied: only an owner changes a worker's bands"}
        w = hub.dispatcher.resolve(body.get("worker")) if isinstance(body.get("worker"), str) else None
        if w is None or w.id == hub.node:
            return {"ok": False, "error": f"unknown worker: {body.get('worker')}"}
        invite, role, ceiling = body.get("invite"), body.get("role"), body.get("ceiling")
        if invite is not None:
            if (err := check_invite(invite)):
                return {"ok": False, "error": f"bad invite: {err}"}
            if invite["hub"] == hub.hub_id or invite["band"] == hub.band:
                return {"ok": False, "error": "bad invite: it is for this hub's own band"}
            band, role = invite["band"], role or invite["role"]
            if ceiling is None and role == invite["role"]:
                ceiling = invite.get("ceiling")
            self.bands[band] = {"hub": invite["hub"], "root": invite["root"], **invite["connect"]}
            _write_private(hub.data / "bands.json", json.dumps(self.bands, indent=1))
        else:
            band = body.get("band", hub.band)
            if not isinstance(band, str):
                return {"ok": False, "error": "bad request: band is a band id"}
        if band == hub.band:
            member_hub, root = hub.hub_id, wire.pub_text(hub.root)
            if role != "home":
                return {"ok": False, "error": "bad request: on the home band the role is home"}
        elif band in self.bands:
            member_hub, root = self.bands[band]["hub"], self.bands[band]["root"]
            if role not in (*INVITE_ROLES, "removed"):
                return {"ok": False, "error": "bad request: role is guest, co-owner or removed"}
        else:
            return {"ok": False, "error": "unknown band: bring an invite from its hub"}
        if role in ("home", "co-owner") and ceiling is None:
            ceiling = FULL
        if role != "removed" and not rights.valid_rights(ceiling):
            return {"ok": False, "error": "bad request: a ceiling (rights: {grants, max_risk?, effects?}) is required"}

        key = f"{member_hub} {band}"
        held = self.issued.get(w.id, {})
        iat = max(int(hub.clock()), int((held.get(key) or {}).get("iat", 0)) + 1)   # always newer (9.3)
        doc = {"typ": "rook-membership", "v": 2, "serial": secrets.token_hex(16), "worker": w.id,
               "hub": member_hub, "root": root, "band": band, "role": role, "by": caller.person, "iat": iat}
        if role != "removed":
            doc["ceiling"] = ceiling
        doc = wire.sign(hub.root, doc)
        self.issued.setdefault(w.id, {})[key] = doc
        _write_private(hub.data / "memberships.json", json.dumps(self.issued, indent=1))
        args: dict = {"membership": doc}
        if role in INVITE_ROLES:
            b = self.bands[band]
            args["connect"] = {**{k: b[k] for k in ("relay", "ws_url") if b.get(k)},
                               "psk": "{{secret:band-psk-%s}}" % band}
        from ..hub.dispatch import Call
        return await hub.dispatcher.dispatch(caller, Call(
            cap="core.trust.set", target=w.id, args=args, why=body.get("why") if isinstance(body.get("why"), str)
            else "", session=hub._door_session(caller), door=door))

    def resolve(self, args: Any, caller) -> Any:
        """Dispatch's secret step: ``{{secret:band-psk-<band>}}`` becomes the
        PSK of a band this hub accepted an invite for, in an owner's calls
        only (the journal keeps the reference).
        TODO(castling:vault): the vault resolves every reference."""
        def walk(v: Any) -> Any:
            if isinstance(v, str):
                m = PSK_REF.fullmatch(v)
                if not m:
                    return v
                if not self.hub.identities.is_owner(caller.person):
                    raise ValueError("only an owner's calls carry band keys")
                if m[1] not in self.bands:
                    raise ValueError(f"no key for band {m[1][:8]}")
                return self.bands[m[1]]["psk"]
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x) for x in v]
            return v
        return walk(args)
