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
`add_key(psk)` and `remove_key(psk)`. The hub points it at its `Carrier`, which holds them
in its Telesthete Endpoint and calls `Sessions.used(psk)` when a session's Channel 0 opens.

The carrier (section 5; build plan L4). One Telesthete `Endpoint` in path mode holds every
session key. It is fed by `GET /band`, on the hub's own HTTP server: each WebSocket
connection is one path, and the hub opens no UDP socket (D3). When a worker opens Channel 0
of a session, the session becomes its live one and the Channel is wrapped as a
`TelestheteLink`, which `Hub.serve_link` serves as it serves any link (`tests/memory_link.py`
is another). Closing a link removes its key from the Endpoint, so frames under that session
are dropped before decryption from then on. A pending session not used within PENDING_WAIT
seconds of its `link_open` is removed (the reviewer's decision, told to Bake on 13 Nov).

Telesthete's Endpoint is called from the event loop's thread only. Every caller here runs
there: `/link` and `/band` are aiohttp handlers, the accept hook and Channel callbacks are
Telesthete's own, the removal timer is the loop's, `Hub.attach` runs in a handler or a hub
cap (both on the loop), and the hub stops the carrier in `stop_serving`, on the loop.
Session keys are never written or logged; Telesthete logs a `band_id` prefix only.
"""

import asyncio
import itertools
import threading
import time

from aiohttp import WSMsgType, web
from nacl.public import PrivateKey
from telesthete import Endpoint

from rook_protocol import (MAX_MESSAGE, PROTOCOL, ProtocolError, decode, encode, session_psk, sign_link_accept,
                           verify_link_open)

from .errors import HubError, error

CHANNEL = 0             # every Rook message rides Channel 0, opened by the worker (section 4.1)
PENDING_WAIT = 60       # seconds a pending session may wait for its Channel 0 (reviewer, told to Bake 13 Nov)
SILENT = 60             # /band: a connection that sends nothing for this long is closed (Telesthete SPEC 9.3)
QUEUE = 256             # /band: frames waiting to go out on one connection; past this they are dropped


class Sessions:
    """The sessions the hub holds, by worker identity key: pending ones per worker
    ephemeral, and at most one live one (link design 2.5, D10). In memory only."""

    def __init__(self, keys):
        self.keys = keys        # the hub's Carrier: add_key(psk) and remove_key(psk)
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

    def expire(self, psk):
        """A session not used within PENDING_WAIT of its link_open: removed if it is still pending.
        A live session is kept."""
        with self._lock:
            self._unpend(psk)

    def close(self, psk):
        """A session's link closed: the session is removed, live or pending."""
        with self._lock:
            key = self._owner.get(psk)
            if key is not None and self._live.get(key) == psk:
                del self._live[key]
                self._drop(psk)
            else:
                self._unpend(psk)

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

    def _unpend(self, psk):
        """Remove `psk` if it is a pending session. The lock is held."""
        key = self._owner.get(psk)
        pending = self._pending.get(key, {})
        for ephemeral, held in list(pending.items()):
            if held == psk:
                del pending[ephemeral]
                self._drop(psk)
        if key in self._pending and not pending:
            del self._pending[key]


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


class Carrier:
    """The hub's Telesthete side (section 5): one Endpoint in path mode holding every session key,
    fed by `/band`. It is where `Sessions` puts keys (`add_key`, `remove_key`), and it starts
    `Hub.serve_link` for each session whose worker opens Channel 0."""

    def __init__(self, hub):
        self.hub = hub
        # Session epoch: milliseconds since the Unix epoch at start, so it increases across
        # restarts on a synced clock (section 5.1; Telesthete SPEC 4.3).
        self.endpoint = Endpoint(hostname="hub", send=self._send, session_epoch=int(time.time() * 1000))
        self._bands = {}        # psk -> its Band in the Endpoint
        self._timers = {}       # psk -> its PENDING_WAIT timer, while the session is pending
        self._links = {}        # psk -> its TelestheteLink, once the worker opened Channel 0
        self._queues = {}       # path -> frames waiting to go out on that /band connection
        self._numbers = itertools.count(1)
        self._served = set()    # the serve_link tasks

    async def start(self):
        await self.endpoint.start()

    async def stop(self):
        """Close every link (removing its key), then stop the Endpoint."""
        for link in list(self._links.values()):
            link.close()
        await asyncio.gather(*self._served, return_exceptions=True)
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        await self.endpoint.stop()

    # ------------------------------------------------- where Sessions puts keys

    def add_key(self, psk):
        """Hold a new pending session's key; remove it if it is not used within PENDING_WAIT."""
        band = self._bands[psk] = self.endpoint.add_key(psk)
        band.on_channel(lambda channel: self._opened(psk, channel))      # the accept hook (section 3.1 step 4)
        self._timers[psk] = asyncio.get_running_loop().call_later(PENDING_WAIT, self.hub.sessions.expire, psk)

    def remove_key(self, psk):
        """Stop holding a session's key: from now on its frames are dropped before decryption. Its
        link, if it has one, ends."""
        band = self._bands.pop(psk, None)
        if band is None:
            return
        timer = self._timers.pop(psk, None)
        if timer is not None:
            timer.cancel()
        self.endpoint.remove_key(band.band_id)
        link = self._links.pop(psk, None)
        if link is not None:
            link._ended()

    # ------------------------------------------------------------- links

    def _opened(self, psk, channel):
        """The worker opened a Channel of this session. Channel 0 makes the session the worker's
        live one (D10) and becomes its link, served as any link is."""
        if channel.channel_id != CHANNEL or psk in self._links:
            return
        key = self.hub.sessions.used(psk)
        if key is None:
            return
        timer = self._timers.pop(psk, None)
        if timer is not None:
            timer.cancel()
        link = self._links[psk] = TelestheteLink(key, channel, lambda: self._close(psk))
        task = asyncio.get_running_loop().create_task(self.hub.serve_link(link))
        self._served.add(task)
        task.add_done_callback(self._served.discard)

    def _close(self, psk):
        self.hub.sessions.close(psk)        # the session table drops it, which removes its key here
        self.remove_key(psk)                # and if the table no longer held it

    # -------------------------------------------------------------- /band

    def _send(self, path, frame):
        """The Endpoint's send callback: must not block or raise. A connection whose queue is full
        loses the frame, as UDP would; the Channel retransmits (Telesthete seam, T9)."""
        queue = self._queues.get(path)
        if queue is not None and not queue.full():
            queue.put_nowait(frame)

    async def band(self, request):
        """`GET /band`: one WebSocket connection is one path into the Endpoint (section 5.1)."""
        ws = web.WebSocketResponse(receive_timeout=SILENT)      # pings are answered (autoping)
        await ws.prepare(request)
        path = ("ws", next(self._numbers))
        queue = self._queues[path] = asyncio.Queue(QUEUE)

        async def pump():
            while True:
                await ws.send_bytes(await queue.get())     # one frame, one binary message

        writer = asyncio.create_task(pump())
        try:
            while True:
                try:
                    message = await ws.receive()
                except TimeoutError:                        # silent for SILENT seconds
                    break
                if message.type == WSMsgType.BINARY:
                    self.endpoint.receive(path, message.data)   # under 43 bytes: dropped by the Endpoint
                elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
                # Text messages are ignored.
        finally:
            self.endpoint.close_path(path)
            del self._queues[path]
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            await ws.close()
        return ws


class TelestheteLink:
    """A worker's link over Telesthete (section 5.2): Channel 0 of its session, each Rook message
    one Channel message. It has what `Hub.serve_link` takes of any link: `key`, `send`, `recv`
    and `close`."""

    def __init__(self, key, channel, close):
        self.key = key                      # the identity key that made the session's exchange
        self.closed = False
        self._channel, self._close = channel, close
        self._inbox = asyncio.Queue()
        channel.on_message(self._inbox.put_nowait)
        channel.on_reset(lambda reason: self.close())    # any reset ends the link (section 3.3)

    async def send(self, data):
        """Hand one message to the Channel. Nothing is awaited first, so messages leave in the
        order send is called."""
        if self.closed:
            raise EOFError
        if len(data) > MAX_MESSAGE:
            raise HubError("message_too_large", f"The message is {len(data)} bytes encoded; a link carries at most "
                                                f"{MAX_MESSAGE}.", "Send less at once.")
        try:
            self._channel.send_message(data)
        except RuntimeError:                # the Channel is not open
            raise EOFError from None

    async def recv(self):
        """The next message; EOFError once the link is closed or its Channel reset."""
        data = await self._inbox.get()
        if data is None:
            self._inbox.put_nowait(None)
            raise EOFError
        # Telesthete also keeps every message in the Channel's own two receive queues; take this
        # one from each, so that neither grows for the life of the link. Both return at once.
        await self._channel.recv()
        await self._channel.recv_message()
        return data

    def close(self):
        """Remove the session's key from the Endpoint, then end recv."""
        if not self.closed:
            self._close()
        self._ended()

    def _ended(self):
        if self.closed:
            return
        self.closed = True
        while not self._inbox.empty():      # nothing more is read from a closed link
            self._inbox.get_nowait()
        self._inbox.put_nowait(None)


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
    app.router.add_get("/band", hub.carrier.band)
