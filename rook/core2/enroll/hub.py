"""The hub's side of enrollment (spec 8). The HTTP door calls these; each
returns a JSON-able dict with ``ok``.

Signing a device in, two ways, both ending in the same config bundle:

- **One-time code** (the QR): anyone with a hub token, a person or an agent
  for one, asks for a code (:meth:`new_code`, also the ``hub.join_code``
  cap); the QR carries ``rook://enroll?url=…&code=…&hub=…``; the device
  claims it with no token (:meth:`claim`). One use, ten minutes.
- **Asking to join**, with no token: the device asks (:meth:`ask`) and
  waits; it shows on the Workers page as wanting to join (:meth:`pending`,
  the ``hub.joins`` cap) until someone taps Join or No (:meth:`approve`,
  the ``hub.join`` cap); the device polls (:meth:`poll`) and gets the
  bundle once.

The person the token is for owns the device.

The device sends its public key (``key``, spec 10.2) with the claim or its
request to join; the hub records it with the worker id it issues, and
from then on takes only requests that key signed from that worker. A
device enrolled without one (before worker keys) gets its key pinned at
its first announce that carries one.

State: pending codes live in memory (a restart drops them; the device asks
again). In ``<data>``: ``enrolled.json`` (each enrolled device: name, owner, key).
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

from .. import CORE_VERSION, wire

log = logging.getLogger("rook.hub.enroll")

CODE_SECS = 600               # one-time codes and requests to join
POLL_EVERY = 2                # seconds between a device's polls
UNPOLLED_SECS = 30            # a request nobody polls for this long is gone (a real device polls every 2 s)
MAX_PENDING = 32              # of codes, and of requests; the device routes take no token
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
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
    Off-site backups are sealed under a key derived from it (rook.core2.hub.backup)."""
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


class Enrollment:
    """One per hub (``hub.enroll``). ``hub`` is the running
    :class:`~rook.core2.hub.runtime.Hub`; ``options`` its ``enroll:`` block."""

    def __init__(self, hub, options: dict | None = None):
        check_options(options or {})
        self.hub, self.opts = hub, dict(options or {})
        self.codes: dict[str, dict] = {}      # digest(code) -> {person, exp}
        self.devices: dict[str, dict] = {}    # digest(ticket) -> {name, exp, address, key, by?, refused?}
        self.enrolled = _load(hub.data / "enrolled.json")       # worker_id -> {name, owner, at, via, key?}

    # -- who may ------------------------------------------------------------------

    def _may_enroll(self, caller) -> str | None:
        ids = self.hub.identities
        if not caller.person or ids.user(caller.person) is None:
            return "denied: setting up a machine needs a person"
        if ids.expired(caller.person, self.hub.clock()):
            return "denied: expired"
        return None

    def _owner_alone(self, caller) -> str | None:
        """The recovery key is for an owner in person, not an agent with their token."""
        if caller.agent or not self.hub.identities.is_owner(caller.person):
            return "denied: only an owner, signed in as themselves"
        return None

    def _expire(self) -> None:
        now = self.hub.clock()
        for store in (self.codes, self.devices):
            for k in [k for k, v in store.items() if v["exp"] < now or now - v.get("seen", now) > UNPOLLED_SECS]:
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
        """``{code, name, key?}`` from the device (no token): the bundle, once."""
        name, code, key = body.get("name"), body.get("code"), body.get("key")
        if not (isinstance(name, str) and NAME.fullmatch(name) and name != "rook"):
            return {"ok": False, "error": "bad request: name is a host-like name (not 'rook')"}
        if key is not None and not wire.valid_pub(key):
            return {"ok": False, "error": "bad request: key is an ed25519: public key"}
        rec = self.codes.pop(_digest(code), None) if isinstance(code, str) else None
        if rec is None or rec["exp"] < self.hub.clock():
            return {"ok": False, "error": "unknown or expired code"}
        return self._bundle(rec["person"], name, "code", key)

    # -- asking to join ------------------------------------------------------------------

    async def ask(self, body: dict, address: str | None = None) -> dict:
        """``{name, key?}`` from the device (no token): a ticket to poll with
        while it waits on the Workers page. ``address`` (where the request
        came from, as the door saw it) is shown there with its age: the name
        is whatever the device says."""
        name, key = body.get("name"), body.get("key")
        if not (isinstance(name, str) and NAME.fullmatch(name) and name != "rook"):
            return {"ok": False, "error": "bad request: name is a host-like name (not 'rook')"}
        if key is not None and not wire.valid_pub(key):
            return {"ok": False, "error": "bad request: key is an ed25519: public key"}
        self._expire()
        if len(self.devices) >= MAX_PENDING:
            return {"ok": False, "error": "busy: too many devices waiting; try again in a few minutes"}
        now = self.hub.clock()
        ticket, exp = secrets.token_urlsafe(32), int(now + CODE_SECS)
        self.devices[_digest(ticket)] = {"name": name, "exp": exp, "started": now, "seen": now,
                                         "address": str(address)[:200] if address else None, "key": key}
        log.info("device wants to join", extra={"fields": {"name": name, "address": address}})
        return {"ok": True, "ticket": ticket, "hub": self.hub.hub_id, "exp": exp, "interval": POLL_EVERY}

    async def poll(self, body: dict) -> dict:
        """``{ticket}``: still pending, refused, or the bundle (once)."""
        key = _digest(body["ticket"]) if isinstance(body.get("ticket"), str) else None
        self._expire()
        d = self.devices.get(key) if key else None
        if d is None:
            return {"ok": False, "error": "unknown or expired request"}
        d["seen"] = self.hub.clock()
        if d.get("refused"):
            del self.devices[key]
            return {"ok": False, "error": "refused by the person"}
        if not d.get("by"):
            return {"ok": True, "pending": True, "interval": POLL_EVERY}
        del self.devices[key]
        return self._bundle(d["by"], d["name"], "asked to join", d.get("key"))

    async def pending(self, caller) -> dict:
        """Devices waiting for someone to let them in: ``id`` is what :meth:`approve` takes."""
        if (err := self._may_enroll(caller)):
            return {"ok": False, "error": err}
        self._expire()
        now = self.hub.clock()
        return {"ok": True, "devices": [{"id": k, "name": d["name"], "exp": d["exp"], "address": d.get("address"),
                                         "age": int(now - d.get("started", now))}
                                        for k, d in self.devices.items() if not d.get("by") and not d.get("refused")]}

    async def approve(self, caller, request: str, refuse: bool = False) -> dict:
        """Let the waiting device ``request`` (an ``id`` from :meth:`pending`) in, or refuse it."""
        if (err := self._may_enroll(caller)):
            return {"ok": False, "error": err}
        self._expire()
        d = self.devices.get(request) if isinstance(request, str) else None
        if d is None or d.get("by") or d.get("refused"):
            return {"ok": False, "error": "unknown or expired request"}
        if refuse:
            d["refused"] = True
        else:
            d["by"] = caller.person
        log.info("request to join answered", extra={"fields": {"name": d["name"], "refused": refuse,
                                                               "by": caller.person}})
        return {"ok": True, "name": d["name"], "joined": not refuse}

    # -- the config bundle (8) --------------------------------------------------------------

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

    def _bundle(self, person: str, name: str, via: str, key: str | None = None) -> dict:
        from ..hub.identity import Caller
        if (err := self._may_enroll(Caller(person=person))):      # rights may have changed since
            return {"ok": False, "error": err}
        try:
            band = self._band()
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        wid, now = secrets.token_hex(16), int(self.hub.clock())
        self.enrolled[wid] = {"name": name, "owner": person, "at": now, "via": via, **({"key": key} if key else {})}
        _write_private(self.hub.data / "enrolled.json", json.dumps(self.enrolled, indent=1))
        self.hub._journal({"kind": "enroll", "ts": now, "person": person, "worker": wid,
                           "name": name, "via": via, "ok": True, **({"key": wire.key_id(key)} if key else {})})
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
