"""The hub's side of the link exchange (docs/link-design.md, sections 2 and 5): `POST /link`
and the sessions it makes.

A worker makes a new key for every connection: it sends `link_open` over HTTPS, the hub
checks it (2.5), derives the session's PSK with `session_psk`, holds it as a pending session
of that worker and answers `link_accept`, signed by the hub key. Both sides then run the
link under that PSK.

Sessions live in memory only. Neither a session key nor an ephemeral secret is ever
written or logged, so a hub that restarts holds none: a recording of an earlier session
is worthless to it (2.6). The one file is `hub_link.key`, the hub's X25519 link secret,
in the data directory beside `hub.key`.

Pending sessions are kept per worker ephemeral (D10): a repeat of an ephemeral replaces
only its own entry, so a replayed `link_open` never displaces the real worker's fresh one.
The first session used becomes the worker's live session and clears the rest; the live
session stays until a newer one is used, or the key is revoked.

Where the keys go: `Sessions` hands every PSK it holds to `keys`, an object with
`add_key(psk)` and `remove_key(psk)`. The Telesthete carrier (build plan L4) points it at
its Endpoint and calls `Sessions.used(psk)` when a session's Channel 0 opens. Until then
nothing carries a session, `keys` is `NoKeys`, and the tests call `used` themselves.
"""

import threading

from aiohttp import web
from nacl.public import PrivateKey

from rook_protocol import (PROTOCOL, ProtocolError, decode, encode, session_psk, sign_link_accept,
                           verify_link_open)

from .errors import error


class NoKeys:
    """Where session keys go while nothing carries a session (until L4): nowhere."""

    def add_key(self, psk):
        pass

    def remove_key(self, psk):
        pass


class Sessions:
    """The sessions the hub holds, by worker identity key: pending ones per worker
    ephemeral, and at most one live one (link design 2.5, D10). In memory only."""

    def __init__(self, keys=None):
        self.keys = keys or NoKeys()
        self._lock = threading.Lock()
        self._pending = {}      # identity key -> {worker ephemeral: psk}
        self._live = {}         # identity key -> psk
        self._owner = {}        # psk -> identity key, for every session held

    def add(self, key, ephemeral, psk):
        """A new pending session of `key`. One already pending for this ephemeral is replaced;
        no other session changes."""
        with self._lock:
            pending = self._pending.setdefault(key, {})
            old = pending.get(ephemeral)
            if old is not None:
                self._drop(old)
            pending[ephemeral] = psk
            self._owner[psk] = key
            self.keys.add_key(psk)

    def used(self, psk):
        """A session's link was used (its Channel 0 opened). A pending session becomes its
        worker's live session; the worker's other pending sessions and its previous live one
        are removed. Returns the worker's identity key, or None for a session not held."""
        with self._lock:
            key = self._owner.get(psk)
            if key is None or self._live.get(key) == psk:
                return key
            for other in self._pending.pop(key, {}).values():
                if other != psk:
                    self._drop(other)
            previous = self._live.get(key)
            if previous is not None:
                self._drop(previous)
            self._live[key] = psk
            return key

    def remove(self, key):
        """Every session of `key`, pending and live: the key was revoked."""
        with self._lock:
            for psk in self._pending.pop(key, {}).values():
                self._drop(psk)
            live = self._live.pop(key, None)
            if live is not None:
                self._drop(live)

    def pending(self, key):
        """The worker ephemerals `key` has sessions pending for."""
        with self._lock:
            return sorted(self._pending.get(key, {}))

    def live(self, key):
        """The PSK of `key`'s live session, or None."""
        with self._lock:
            return self._live.get(key)

    def _drop(self, psk):
        del self._owner[psk]
        self.keys.remove_key(psk)


def open_session(hub, message):
    """Answer a `link_open` (docs/protocol.md): a `link_accept`, or an `error` and nothing kept.
    The checks run in the order of link design 2.5."""
    key = message["key"]
    with hub._lock:
        row = hub._db.execute("SELECT k.revoked_at, k.link_key, w.moniker FROM keys k "
                              "JOIN workers w ON w.guid = k.worker WHERE k.key = ?", (key,)).fetchone()
        knocking = row is None and hub._db.execute("SELECT 1 FROM knocks WHERE key = ?", (key,)).fetchone()
    if row is None and knocking:
        return error("unknown_key", "This key is still waiting to be approved.",
                     "Wait until a person or agent approves the knock, then open the link.")
    if row is None:
        return error("unknown_key", "This key was never approved.",
                     "Knock with it and wait for a person or agent to approve it.")
    if row["revoked_at"] is not None:
        return error("key_revoked", f"This key was revoked from {row['moniker']}.",
                     "Generate a new key, knock with it, and ask a person or agent to attach it.")
    if not verify_link_open(hub.public_key, message):
        return error("bad_signature", "The link_open signature does not verify under its key for this hub.",
                     "Sign link_open with the identity key, over this hub's key, as docs/protocol.md says.")
    if row["link_key"] is None:
        return error("unknown_key", f"This key of {row['moniker']} was approved before workers had link keys.",
                     f"Generate a new key, knock with it, and ask a person or agent to attach it to {row['moniker']}.")
    ephemeral = PrivateKey.generate()
    hub_ephemeral = ephemeral.public_key.encode().hex()
    try:
        psk = session_psk("hub", key, message["ephemeral"], hub_ephemeral, ephemeral, hub.link_secret,
                          row["link_key"])
    except ProtocolError as e:
        return error("bad_message", str(e), "Make a new ephemeral key and send link_open again.")
    del ephemeral       # the ephemeral secret is not kept (link design 2.2)
    hub.sessions.add(key, message["ephemeral"], psk)
    return {"type": "link_accept", "protocol": PROTOCOL, "ephemeral": hub_ephemeral,
            "hub_link_key": hub.link_key,
            "signature": sign_link_accept(hub._signing_key, key, message["ephemeral"], hub_ephemeral, hub.link_key)}


def add_routes(app, hub):
    async def link(request):
        """`POST /link`: a link_open in, a link_accept or error out (docs/protocol.md)."""
        try:
            message = decode(await request.read())
            if message["type"] != "link_open":
                raise ProtocolError(f"expected a link_open, got {message['type']}")
            reply = open_session(hub, message)
        except ProtocolError as e:
            reply = error("bad_message", str(e), "Send a link_open as docs/protocol.md defines it.")
        status = 200 if reply["type"] == "link_accept" else 400 if reply["code"] == "bad_message" else 403
        return web.Response(body=encode(reply), status=status, content_type="application/json")

    app.router.add_post("/link", link)
