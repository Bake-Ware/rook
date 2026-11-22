"""The worker's carrier (docs/link-design.md sections 3, 4 and 6.1; build plan L5): one session's
link to the hub over Telesthete.

`connect(session)` is the `connect` a `Worker` uses when none is passed. Given a `Session`
(rook_worker.py), it starts a Telesthete `Band(session.psk, reconnect=True)` over a
`WebSocketTransport` to `session.band_url`, the hub's `/band`, waits for the hub to answer the
Band's HELLO, opens Channel 0 (the worker always opens it, section 4.1) and returns the link:

- `send(data)` hands one Rook message to Channel 0, as one Channel message. Nothing is awaited
  first, so messages leave in the order `send` is called (D5 depends on it: a heartbeat's
  `open_streams` and the stream messages around it). A message over 16,000,000 bytes is never
  handed to the Channel.
- `recv()` gives the next message, and raises EOFError once the link has ended.
- `close()` ends the link at once; `await wait_closed()` returns when its Band, the Band's
  transport and every task of this link have stopped. The worker awaits it after each session,
  so nothing of a session outlives `Worker.run`.

The link ends, and the worker's loop makes a new session (section 3.2), when Channel 0 resets for
any reason (`on_reset`: the hub restarted, left, reset it, or stopped acknowledging), and when the
hub is lost: no authenticated word from it for PEER_TIMEOUT seconds. Telesthete does not say when
its Band declares a peer lost; this link reads it from the public `Peer.is_alive()` of the Band's
peers, every WATCH seconds. A WebSocket connection that drops and comes back under the same key,
while the hub still holds it, does not end the link: the Telesthete client reconnects and the
session and Channel 0 carry on (section 3.3).

The link also ends at once when its WebSocket connection comes back to a hub that has started
since (brief 57). A hub gives its start number in `link_accept` (`session.hub_start`), new at every
start. The WebSocket client calls its public `on_connected` hook after every (re)connect; the
Band sets it to send HELLO, and this link wraps it. After each reconnect the link reads the hub's
start number again (`session.read_hub_start`, `GET /link`); a different number means the hub that
held this session is gone, so it can never answer under its key, and the link ends. The worker
then makes a new session at once, instead of waiting for Channel 0 to give up or for
PEER_TIMEOUT. The same number, or none read, changes nothing. A hub that gives no number (one
older than brief 57) is never asked, as before.

A session's key lives only in its Band, and goes when the link ends. It is never written or
logged: Telesthete logs a `band_id` prefix only. `wss://` is verified against the system's roots,
as the knock's HTTPS is.
"""

import asyncio

from telesthete import Band
from telesthete.transport.websocket import WebSocketTransport

from rook_protocol import MAX_MESSAGE, ProtocolError

CHANNEL = 0             # every Rook message rides Channel 0, opened by the worker (section 4.1)
PEER_TIMEOUT = 15.0     # seconds without an authenticated word from the hub: it is lost (Telesthete SPEC 4.5)
WATCH = 1.0             # seconds between looks at whether the hub is still alive


async def within(awaitable, seconds):
    """Await `awaitable` for up to `seconds`; True if it finished then, False if the time ran out (it is
    then cancelled). Its exception, if it raised, is raised here.

    It stands in for asyncio.wait_for, which on Python 3.11 swallows a cancel that comes in the same loop
    step as the awaitable finishing: it returns, and the task that was cancelled carries on (brief 64).
    asyncio.wait never does, so a cancel of the caller always ends this at once, on every Python."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
    except BaseException:                       # cancelled: so is what it waited for
        task.cancel()
        raise
    if not done:
        task.cancel()
        return False
    task.result()
    return True


async def connect(session):
    """One session's link to the hub over Telesthete (section 3.1 steps 2 to 4). If the hub does
    not answer the Band's HELLO within PEER_TIMEOUT, the link returned has already ended."""
    link = TelestheteLink(session)
    try:
        await link._open()
    except BaseException:
        link.close()
        await link.wait_closed()
        raise
    return link


class TelestheteLink:
    """Channel 0 of one session, each Rook message one Channel message: what the worker takes of
    any link (`send`, `recv`, `close`, `wait_closed`)."""

    def __init__(self, session):
        self.key = session.key
        self.closed = False
        # A wss:// link is checked against the system's roots (ssl=True); a ws:// link has no TLS. Said
        # outright: the websockets library refuses ssl=None, Telesthete's default, with a wss:// address
        # ("ssl=None is incompatible with a wss:// URI"), so a worker never linked to a hub reached by
        # https (found 22 Nov 2026 joining a worker to a hub behind a Cloudflare tunnel).
        self.transport = WebSocketTransport(session.band_url,
                                            ssl=True if session.band_url.startswith("wss://") else None)
        self.band = Band(session.psk, transport=self.transport, reconnect=True)
        self._channel = None
        self._hub_start, self._read_hub_start = session.hub_start, session.read_hub_start
        self._connections = 0
        self._checks = set()            # the reads of the hub's start number running after reconnects
        if self._hub_start is not None and self._read_hub_start is not None:
            hello = self.transport.on_connected         # the Band's: HELLO on every (re)connect

            def connected():
                hello()
                self._connected()
            self.transport.on_connected = connected
        self._inbox = asyncio.Queue()
        self._watching = None
        self._stopping = None

    async def _open(self):
        await self.band.start()                 # connects, and sends HELLO on every (re)connect
        if not await within(self._answered(), PEER_TIMEOUT):
            self.close()                        # never answered: a failed attempt (section 3.2)
            return
        if self.closed:                         # ended meanwhile: a reconnect found the hub had restarted (brief 57)
            return
        channel = self._channel = self.band.channel(CHANNEL, self.transport.peer_address)
        channel.on_message(self._inbox.put_nowait)
        channel.on_reset(lambda reason: self.close())          # any reset ends the link (section 3.2)
        await channel.open()                    # SYN; messages sent before it is acknowledged wait in order
        self._watching = asyncio.get_running_loop().create_task(self._watch())

    async def _answered(self):
        while not self.band.get_peers() and not self.closed:    # HELLO_ACK: the hub holds this session's key
            await asyncio.sleep(0.02)

    def _connected(self):
        """The WebSocket client connected to /band. Every connection after the first is a reconnect:
        read the hub's start number, to learn whether the hub that holds this session still answers."""
        self._connections += 1
        if self._connections > 1 and not self.closed:
            task = asyncio.get_running_loop().create_task(self._check_hub())
            self._checks.add(task)
            task.add_done_callback(self._checks.discard)

    async def _check_hub(self):
        start = await self._read_hub_start()
        if start is not None and start != self._hub_start:
            self.close()                        # that hub is gone, and this session with it (brief 57)

    async def _watch(self):
        """The hub is lost once no peer of the Band is alive: PEER_TIMEOUT without a word."""
        while not self.closed:
            await asyncio.sleep(WATCH)
            if not any(peer.is_alive() for peer in self.band.get_peers()):
                self.close()

    async def send(self, data):
        """Hand one message to Channel 0, with nothing awaited first."""
        if self.closed or self._channel is None:
            raise EOFError
        if len(data) > MAX_MESSAGE:
            raise ProtocolError(f"a message is at most {MAX_MESSAGE} bytes encoded; this one is {len(data)}")
        try:
            self._channel.send_message(data)
        except RuntimeError:                    # the Channel is not open: it reset
            raise EOFError from None

    async def recv(self):
        """The next message; EOFError once the link has ended."""
        data = await self._inbox.get()
        if data is None:
            self._inbox.put_nowait(None)
            raise EOFError
        # Telesthete also keeps every message in the Channel's own two receive queues; take this
        # one from each, so neither grows for the life of the link. Both return at once.
        await self._channel.recv()
        await self._channel.recv_message()
        return data

    def close(self):
        """End the link: recv ends, and the Band and its transport stop (wait_closed waits for it).
        The session's key is never used again."""
        if self.closed:
            return
        self.closed = True
        while not self._inbox.empty():          # nothing more is read from a closed link
            self._inbox.get_nowait()
        self._inbox.put_nowait(None)
        self._stopping = asyncio.get_running_loop().create_task(self._stop())

    async def _stop(self):
        tasks = [task for task in (self._watching, *self._checks)
                 if task is not None and task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.band.stop()                  # GOODBYE, then the Band's tasks and its transport

    async def wait_closed(self):
        if self._stopping is not None:
            await asyncio.shield(self._stopping)
