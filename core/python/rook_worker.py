"""A minimal Rook worker core in Python: register, heartbeat, answer calls (W2).

State directory (W7, U2): `key` holds the private key the worker generated,
`link_key` the X25519 secret it makes its link's keys with (docs/link-design.md 2.2),
`identity.json` the GUID, moniker, hub signature and hub key it got on approval. All
survive restarts, so a restarted worker is the same identity (R5).

Plugins (PL3): the hub sends a `plugins` message with every plugin worker part
this worker should have. The worker loads what is new, replaces what changed and
drops what is gone, with no restart, and announces the caps they add like its
own (W5). A part is a Python module defining `CAPS`, a dict of `Cap`, as below
and in docs/plugins.md.

Config (CF2, CF3): the hub sends a `config` message with this worker's config, its
`values` with every secret already put in, and a `version`, when the worker links and
whenever the config changes. The worker applies it: `config` holds the values and
`config_version` their version, and the next heartbeat, sent at once, reports that
version, which is how the hub learns it was applied. The config is kept in memory only,
never written to the state directory: the hub is the source of truth and sends it again
at every link, and secrets are never kept on a worker (X5).

The worker never sees a transport. Each time it connects it first makes a key for
that connection with the hub over HTTPS (`link_open`, docs/link-design.md 2), then
calls `connect(session)` with the result, a `Session`: its identity key, the PSK the
link runs under, and the hub's link URL. `connect` returns the link to the hub: an
object with `async send(bytes)`, `async recv() -> bytes` (raises EOFError once
closed), `close()` and `async wait_closed()` (returns once everything of that link has
stopped). `send` must put messages on the link in the order it is called: the worker
builds each message and hands it to `send` with no await in between, so a heartbeat's
`open_streams` and the stream messages around it arrive in the order they were made
(D5). Without a `connect` of its own, a worker uses the Telesthete carrier over
WebSocket (rook_link.py, build plan L5); the tests pass an in-memory one.

`run` is a loop (link design 3.2): make a session, link under it and serve it until
the link is lost, then start again with a new session. A session key is never reused,
never written and never logged. A session has worked once its link has delivered a
heartbeat_reply; one lost after that is followed by a new exchange at once. A failed
attempt (the exchange failed, or the link was lost before any heartbeat_reply) is
followed by a wait of 0.5 s, doubling to 30 s, each with 20 % jitter either way, as
Telesthete's WebSocket client backs off; the wait resets only after a session that
worked. A request to the hub waits at most HTTP_TIMEOUT seconds. A hub that refuses
the key (`unknown_key`, `key_revoked`), or tells it over the link that its key was revoked
(R5), stops the worker.

A big result is sent in pieces (brief 54; docs/protocol.md, "A big result"). The worker writes a
result's JSON text as it goes. A result whose text ends within PIECE characters is sent whole, as
one `result`, as always. One that goes past PIECE goes as `result_part` messages of about PIECE
characters each, the next sent only once the hub has acknowledged the last (`result_ack`), so
the worker holds one piece at a time, and then a `result` with `parts`. PIECE is where a result
becomes pieces, not a limit: nothing is refused, cut or given a deadline for its size. A cap
whose output can be big returns `Text` (a string) and `Items` (a list) made as they are sent,
and awaitables for what it learns last, so it never holds the whole either; a cap that returns
an ordinary value gets the same pieces, made from the value it holds.

What happens is said in plain words on the `rook_worker` logger (info, and a warning while it
cannot link): waiting for approval, approved, linked, the link lost, why linking fails. Each is
said once, until it changes. The start command (`python -m rook_worker HUB_URL`, rook_start.py)
prints them; nothing else configures that logger.
"""

import asyncio
import base64
import inspect
import json
import logging
import os
import pathlib
import platform
import random
import shutil
import socket
import subprocess
import sys
import threading
import types
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from nacl.public import PrivateKey
from nacl.signing import SigningKey, VerifyKey

import rook_link
from rook_protocol import (MAX_STREAM_DATA, PROTOCOL, ProtocolError, decode, encode, session_psk,
                           sign_link_key, sign_link_open, validate, verify_link_accept)

VERSION = "0.1.0"
RETRY_FIRST, RETRY_MOST, RETRY_JITTER = 0.5, 30.0, 0.2   # a failed attempt: 0.5 s doubling to 30 s, ±20 % (link design 3.2)
HTTP_TIMEOUT = 10.0      # seconds a knock or link_open waits on the hub: Telesthete's WebSocket open timeout (Bake 13 Nov)
SYS = pathlib.Path("/sys")    # where Linux exposes battery, temperature and GPU (H4); a test points it elsewhere
log = logging.getLogger("rook_worker")      # what happens, in plain words (rook_start.py prints it)

# What a cap's handler, or a plugin part as it loads, may raise and only fail that call or
# that part: any Exception, and SystemExit, so code that calls sys.exit() cannot stop the
# worker. KeyboardInterrupt and cancellation are not caught: they still stop it.
FAILS = (Exception, SystemExit)

# Where a result becomes pieces, and about how big each piece is, in characters of its JSON text
# (brief 54; Bake: "anything big should be chunked", about a megabyte). Not a limit: a result of
# any size is sent whole, in as many pieces as it takes.
PIECE = 1_000_000


@dataclass
class Cap:
    """A cap this worker serves (W3). `handler(args)` is awaited for each call.

    A stream cap (`opens_stream=True`, S1) is called as `handler(args, stream)` and
    returns {"title": ..., "description": ...} (S3); it keeps `stream` to send on."""
    description: str
    schema: dict
    handler: Callable[..., Awaitable[Any]]
    opens_stream: bool = False


class Text:
    """A string in a cap's result that is made while it is sent (brief 54): `pieces` is an async
    iterable of str, taken in order as the sending reaches it. A cap whose output can be big
    returns one, so the worker never holds the whole string."""

    def __init__(self, pieces):
        self.pieces = pieces


class Items:
    """A list in a cap's result that is made while it is sent (brief 54): `items` is an async
    iterable of ordinary JSON values, taken in order as the sending reaches it."""

    def __init__(self, items):
        self.items = items


# What an awaitable in a result's object gives to leave its field out, such as `truncated` when
# nothing was cut (brief 54).
LEFT_OUT = object()


class Stream:
    """One open stream, owned by this worker (S2). `send` output; `read` input
    (None once closed); `close` it. It lives until one side closes it (S4).

    A stream that is a terminal learns the size it is shown at (Bake 13 Nov): `size`
    is the latest (cols, rows) a client sent, or None, and `await resized()` gives the
    next new size. A handler that has no use for sizes ignores both; nothing piles up."""

    def __init__(self, worker, handle, cap):
        self.worker, self.handle, self.cap = worker, handle, cap   # the cap that opened it
        self.open = True
        self.size = None
        self._input = asyncio.Queue()
        self._resized = asyncio.Event()
        self._ready = asyncio.Event()     # set once the hub has its stream_open, so no data goes first

    async def send(self, data):
        """Output, in order. More than 65,536 bytes goes as several stream_data messages
        (docs/protocol.md, "Sizes"), so no one message holds the link for long."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        await self._ready.wait()
        for start in range(0, len(data), MAX_STREAM_DATA):
            if not self.open:
                return
            await self.worker._send({"type": "stream_data", "stream": self.handle,
                                     "data": base64.b64encode(data[start:start + MAX_STREAM_DATA]).decode("ascii")})

    async def read(self):
        """The next input from a client, as bytes, or None when the stream is closed."""
        return await self._input.get()

    async def resized(self):
        """The next size a client shows the stream at, as (cols, rows), or None once the stream is
        closed. Sizes that arrive before this is awaited again are not queued: it gives the latest."""
        await self._resized.wait()
        self._resized.clear()
        return self.size if self.open else None

    def _resize(self, cols, rows):
        self.size = (cols, rows)
        self._resized.set()

    async def close(self):
        if self.open:
            await self._ready.wait()
            await self.worker._send({"type": "stream_close", "stream": self.handle})
            self._closed()

    def _closed(self):
        if self.open:
            self.open = False
            self._input.put_nowait(None)
            self._resized.set()
            self.worker.streams.pop(self.handle, None)
            self.worker.changed()        # the open count is part of the heartbeat (S4)


@dataclass
class Part:
    """A plugin's worker part as this worker has it (PL3): loaded, with the caps it
    added and the module name it runs under, or failed, with why."""
    version: str
    source: str
    caps: tuple = ()
    module: str | None = None
    error: str | None = None


@dataclass
class Session:
    """What one link exchange gives the carrier (link design 3.1): the worker's identity key,
    the PSK the link runs under (never printed), and where the hub serves links: the hub URL
    with scheme ws or wss and path /band (2.3). One session per connection; never reused.

    `hub_start` is the start number the hub gave in link_accept, and `read_hub_start` reads the
    hub's start number now (GET /link; None if it cannot be read). When the carrier's connection to
    /band drops and comes back, it reads the number again: a different one means the hub that held
    this session is gone, and the link ends at once (brief 57). A hub that gives no number (one
    older than brief 57) leaves `hub_start` None, and the carrier never reads it."""
    key: str
    psk: str = field(repr=False)
    band_url: str
    hub_start: int | None = None
    read_hub_start: Callable[[], Awaitable[int | None]] | None = field(default=None, repr=False)


class Refused(Exception):
    """The hub refused this worker's key. `.message` is the hub's `error` message."""

    def __init__(self, message):
        super().__init__(message["message"])
        self.message = message


class Worker:
    def __init__(self, state_dir, hub_url, connect=None, name=None, caps=None, macs=None, knock_every=5.0,
                 token=None):
        self.state_dir = pathlib.Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.hub_url = hub_url.rstrip("/")
        self.connect = connect or rook_link.connect     # the Telesthete carrier unless one is given
        self.name = name or socket.gethostname()
        self.caps = dict(caps or {})     # the network primitives (D3) come as the default plugin `net` (PL7)
        self.macs = own_macs() if macs is None else macs
        self.knock_every = knock_every
        self.token = token  # a join token: approved at once, no pending step (R3)
        self.signing_key = self._load_key("key", SigningKey)
        self.key = self.signing_key.verify_key.encode().hex()
        self.link_secret = self._load_key("link_key", PrivateKey)
        path = self.state_dir / "identity.json"
        self.identity = json.loads(path.read_text()) if path.exists() else None
        self.interval = None
        self.streams = {}                 # handle -> Stream, while open
        self.plugins = {}                 # plugin name -> Part, as the hub's last plugins message left them
        self.config = {}                  # the config the hub last sent, secrets put in (CF2); memory only
        self.config_version = None        # its version, reported in every heartbeat once there is one
        self._loads = 0
        self._link = None
        self._acks = {}                   # call id -> (its link, a future set when the hub acknowledges a piece)
        self._stopped = asyncio.Event()
        self._replied = False             # this session's link has delivered a heartbeat_reply
        self._told = None                 # the last thing said on `log` about linking
        self._sleep = asyncio.sleep       # a test replaces it, so backoff waits are asked for, not slept
        self._changed = asyncio.Event()
        self._interval_known = asyncio.Event()
        self._cpu = None
        self._read = {}                   # battery, temperature and GPU as last read (H4)
        self._reading = None              # the thread reading them, while it runs

    def _load_key(self, name, kind):
        """Generate a key of `kind` on first start; reuse it after (R5, W7)."""
        path = self.state_dir / name
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(kind.generate().encode().hex())
        return kind(bytes.fromhex(path.read_text().strip()))

    async def run(self):
        """Knock until approved (first start only). Then, until stop(): make a session with the
        hub, link under it, heartbeat and answer calls until the link is lost, and start again
        with a new session (link design 3.1, 3.2). Raises Refused if the hub refuses the key, and
        BadSignatureError if the approval's signature does not verify."""
        self._stopped.clear()
        if self.identity is None:
            identity = await self._unless_stopped(self._knock_until_approved())
            if identity is None:                  # stop() came while knocking
                return
            self.identity = identity
            (self.state_dir / "identity.json").write_text(json.dumps(self.identity))
            log.info("Approved as %s.", self.identity["moniker"])
        wait = None
        while not self._stopped.is_set():
            if await self._session():             # it worked: a new exchange at once
                wait = None
                if not self._stopped.is_set():
                    self._tell("lost", log.warning, "The link to the hub was lost; linking again.")
                continue
            if self._stopped.is_set():
                break
            wait = RETRY_FIRST if wait is None else min(wait * 2, RETRY_MOST)
            await self._unless_stopped(self._sleep(wait * random.uniform(1 - RETRY_JITTER, 1 + RETRY_JITTER)))

    async def _session(self):
        """One attempt: an exchange, then a link under its key, served until it is lost. True if the
        session worked (its link delivered a heartbeat_reply), False if the attempt failed."""
        session = await self._unless_stopped(self._open_session())
        if session is None:                       # the exchange failed, or stop() was called
            return False
        band_url = session.band_url
        link = await self._unless_stopped(self.connect(session))
        del session                               # the carrier has it; the worker keeps no session key
        if link is None:                          # stop() came while the carrier was connecting
            return False
        self._link, self._replied = link, False
        try:
            if self._stopped.is_set():
                return False
            beats = asyncio.create_task(self._heartbeats())
            try:
                await self._serve()
            finally:
                beats.cancel()
                await asyncio.gather(beats, return_exceptions=True)
        finally:
            link.close()
            await link.wait_closed()              # nothing of this session outlives it
        if not self._replied and not self._stopped.is_set():
            self._cannot_link(f"no answer on {band_url}")
        return self._replied

    def stop(self):
        """End run(): while knocking, during a session, during the exchange or while waiting to try again."""
        self._stopped.set()
        if self._link is not None:
            self._link.close()

    async def _unless_stopped(self, awaitable):
        """Await `awaitable`, unless stop() comes first: then it is cancelled and this gives None."""
        task = asyncio.ensure_future(awaitable)
        stopping = asyncio.ensure_future(self._stopped.wait())
        try:
            await asyncio.wait({task, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return None
        return task.result()

    async def emit(self, name, payload):
        """Put an event on the hub's bus (J2), for example from inside a cap."""
        await self._send({"type": "event", "name": name, "payload": payload})

    def changed(self):
        """Something in the worker's state changed: heartbeat now (H1)."""
        self._changed.set()

    def _tell(self, what, say, text, *args):
        """Say `text` on `log` unless `what` is what was said last."""
        if what != self._told:
            self._told = what
            say(text, *args)

    def _cannot_link(self, why):
        self._tell(why, log.warning, "Cannot link to the hub: %s. Trying again.", why)

    # ---------------------------------------------------------------- knock

    async def _knock_until_approved(self):
        link_key = self.link_secret.public_key.encode().hex()
        knock = {"type": "knock", "protocol": PROTOCOL, "name": self.name, "key": self.key,
                 "macs": self.macs, "caps": self._cap_list(),
                 "link_key": link_key, "link_signature": sign_link_key(self.signing_key, link_key)}
        if self.token is not None:
            knock["token"] = self.token
        knock = encode(knock)
        while True:
            reply = await asyncio.to_thread(self._post, knock)
            if reply["type"] == "error":
                raise Refused(reply)
            if reply["status"] != "approved":
                self._tell("pending", log.info, "Waiting for approval: on the hub, approve the pending worker "
                           "named %s.", self.name)
            else:
                # R5: check the hub signed our key before keeping the identity.
                # Raises nacl.exceptions.BadSignatureError if it did not.
                VerifyKey(bytes.fromhex(reply["hub_key"])).verify(
                    b"rook-worker-key:" + bytes.fromhex(self.key), bytes.fromhex(reply["signature"]))
                return {name: reply[name] for name in ("guid", "moniker", "signature", "hub_key")}
            await asyncio.sleep(self.knock_every)

    def _post(self, body, path="/knock"):
        request = urllib.request.Request(self.hub_url + path, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return decode(response.read())
        except urllib.error.HTTPError as e:
            return decode(e.read())

    # ------------------------------------------------------------- session

    async def _open_session(self):
        """One link exchange (link design 2, 3.1): a new ephemeral key, `link_open` signed for
        the hub key stored at approval, and the checks of 2.5 on the answer. Returns a Session,
        or None when the attempt failed (the hub unreachable, a refusal worth retrying, or an
        answer that fails a check: it is never used). Raises Refused on unknown_key or key_revoked."""
        hub_key = self.identity["hub_key"]
        ephemeral = PrivateKey.generate()
        public = ephemeral.public_key.encode().hex()
        body = encode({"type": "link_open", "protocol": PROTOCOL, "key": self.key, "ephemeral": public,
                       "signature": sign_link_open(self.signing_key, hub_key, public)})
        try:
            reply = await asyncio.to_thread(self._post, body, "/link")
        except OSError as e:
            return self._cannot_link(f"cannot reach {self.hub_url} ({unreachable(e)})")
        except ProtocolError:
            return self._cannot_link(f"{self.hub_url} did not answer as a Rook hub")
        if reply["type"] == "error":
            if reply["code"] in ("unknown_key", "key_revoked"):
                raise Refused(reply)
            return self._cannot_link(f"the hub answered {reply['code']}: {reply['message']}")
        if reply["type"] != "link_accept" or not verify_link_accept(hub_key, self.key, public, reply):
            return self._cannot_link("the hub's link_accept did not pass the checks of link design 2.5")
        try:
            psk = session_psk("worker", self.key, public, reply["ephemeral"], ephemeral, self.link_secret,
                              reply["hub_link_key"])
        except ProtocolError:                     # a small-order point
            return self._cannot_link("the hub's link_accept did not pass the checks of link design 2.5")
        scheme, rest = self.hub_url.split("://", 1)
        return Session(self.key, psk, f"{'wss' if scheme == 'https' else 'ws'}://{rest}/band",
                       reply.get("hub_start"), self._read_hub_start)

    async def _read_hub_start(self):
        """The hub's start number now, from `GET /link` (brief 57), or None if it cannot be read: the
        hub unreachable, or one that has no such route."""
        try:
            reply = await asyncio.to_thread(self._get, "/link")
        except (OSError, ProtocolError):
            return None
        return reply["hub_start"] if reply["type"] == "hub_start" else None

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.hub_url + path, timeout=HTTP_TIMEOUT) as response:
                return decode(response.read())
        except urllib.error.HTTPError as e:
            return decode(e.read())

    # ----------------------------------------------------------------- link

    async def _heartbeats(self):
        while True:
            self._changed.clear()
            # Built and handed to the link with no await in between, so `open_streams` matches the
            # stream messages before and after it on the one ordered link (D5; see the module docstring).
            await self._link.send(encode(self.heartbeat()))
            await self._interval_known.wait()
            try:
                await asyncio.wait_for(self._changed.wait(), self.interval)
            except TimeoutError:
                pass

    async def _serve(self):
        link = self._link
        try:
            await self._serve_link(link)
        finally:
            for call_id, (on, acked) in list(self._acks.items()):   # its link is gone: no piece waits on it
                if on is link and not acked.done():
                    acked.set_result(False)

    async def _serve_link(self, link):
        while True:
            try:
                data = await link.recv()
            except EOFError:
                return
            try:
                message = decode(data)
            except ProtocolError as e:
                await self._send(error("bad_message", str(e), "Send messages as docs/protocol.md defines them."))
                continue
            if message["type"] == "heartbeat_reply":
                if not self._replied:
                    self._tell("linked", log.info, "Linked to %s as %s.", self.hub_url, self.identity["moniker"])
                self._replied = True
                if self.interval is not None and message["interval"] != self.interval:
                    self._changed.set()         # a new interval (H2): heartbeat now, then wait the new one
                self.interval = message["interval"]
                self._interval_known.set()
            elif message["type"] == "call":
                asyncio.create_task(self._answer(message))
            elif message["type"] == "result_ack":          # the hub has the last piece: send the next
                waiting = self._acks.get(message["id"])
                if waiting is not None and not waiting[1].done():
                    waiting[1].set_result(True)
            elif message["type"] == "stream_data" and message["stream"] in self.streams:
                self.streams[message["stream"]]._input.put_nowait(base64.b64decode(message["data"]))
            elif message["type"] == "stream_resize" and message["stream"] in self.streams:
                self.streams[message["stream"]]._resize(message["cols"], message["rows"])
            elif message["type"] == "stream_close" and message["stream"] in self.streams:
                self.streams[message["stream"]]._closed()
            elif message["type"] == "plugins":
                await self._plugins(message["plugins"])
            elif message["type"] == "config":           # applied, then acknowledged by a heartbeat (CF2)
                self.config, self.config_version = message["values"], message["version"]
                self.changed()
            elif message["type"] == "error" and message["code"] in ("unknown_key", "key_revoked"):
                raise Refused(message)

    async def _answer(self, call):
        cap = self.caps.get(call["cap"])
        if cap is None:
            reply = error("unknown_cap", f"{self.name} does not serve {call['cap']}.",
                          f"List the caps {self.name} announces and call one of those.", call["id"])
        elif cap.opens_stream:
            await self._open_stream(call, cap)
            return
        else:
            link = self._link       # the call's link: every message of its answer goes on it
            try:
                reply = await self._result(call["id"], await cap.handler(call["args"]), link)
                encode(reply)       # a whole result that cannot be sent fails the call here, as always
            except FAILS as e:      # before any piece, or after some: the call ends with this either way
                reply = self._failed(call, e)
            await self._send(reply, link)
            return
        await self._send(reply)

    async def _result(self, call_id, value, link):
        """The `result` that answers call `call_id` with `value` (brief 54). Its JSON text is made as
        it goes. Ending within PIECE characters, it is sent whole, in the `result` returned, exactly
        as before. Going past PIECE, it goes as result_part messages of PIECE characters (the last
        one less), each sent once the hub has acknowledged the one before; the `result` returned
        then says how many. If the link is lost meanwhile, the rest of the text is still made, and
        dropped, as a whole result is dropped when its link is gone."""
        text, held, size, parts, lost = _json(value), [], 0, 0, False
        try:
            async for chunk in text:
                held.append(chunk)
                size += len(chunk)
                while size > PIECE:             # past PIECE: it goes in pieces, each sent as it is full
                    joined = "".join(held)
                    if not lost:
                        lost = not await self._piece(call_id, joined[:PIECE], link)
                    parts += 1
                    held, size = [joined[PIECE:]], len(joined) - PIECE
        finally:
            await text.aclose()
        if parts == 0:          # it ended within PIECE: whole, as one message, as always
            return {"type": "result", "id": call_id, "result": json.loads("".join(held))}
        if size:
            if not lost:
                await self._piece(call_id, "".join(held), link)
            parts += 1
        return {"type": "result", "id": call_id, "result": None, "parts": parts}

    async def _piece(self, call_id, data, link):
        """Send one piece of a result and wait until the hub acknowledges it, or the link is gone
        (then False). The wait has no deadline: a hub that never acknowledges is waited for."""
        acked = asyncio.get_running_loop().create_future()
        self._acks[call_id] = (link, acked)
        try:
            await link.send(encode({"type": "result_part", "id": call_id, "data": data}))
            return await acked
        except EOFError:
            return False
        finally:
            self._acks.pop(call_id, None)

    def _failed(self, call, e):
        """`cap_failed` for a handler that raised. The worker cannot tell whether the arguments or
        the machine caused it, so the next step points at the reason, not at the schema (C3)."""
        why = reason(e).rstrip()
        return error("cap_failed", f"{call['cap']} failed: {why}{'' if why.endswith(('.', '!', '?')) else '.'}",
                     f"Change what the reason names and call again, or call {call['cap']} on another worker "
                     "that announces it.", call["id"])

    async def _open_stream(self, call, cap):
        """A call on a stream cap: the handler names the stream, the hub hears stream_open (S1)."""
        # A fresh random handle, so a stream opened after a restart never takes the
        # handle of one the hub still lists from before it.
        stream = Stream(self, uuid.uuid4().hex[:16], call["cap"])
        try:
            named = await cap.handler(call["args"], stream)
            reply = {"type": "stream_open", "id": call["id"], "stream": stream.handle,
                     "title": str(named["title"]), "description": str(named["description"])}
            encode(reply)
        except FAILS as e:
            await self._send(self._failed(call, e))
            return
        self.streams[stream.handle] = stream
        await self._send(reply)
        stream._ready.set()
        self.changed()

    # -------------------------------------------------------------- plugins

    async def _plugins(self, parts):
        """Make this worker's plugin worker parts match the hub's list (PL3): drop what is
        no longer listed, has changed or failed last time; load what is not loaded; then
        heartbeat, so the caps are announced (W5). Parts load in the order the hub lists them."""
        wanted = {part["name"]: part for part in parts}
        for name, have in list(self.plugins.items()):
            part = wanted.get(name)
            if part is None or have.error is not None or (have.version, have.source) != (part["version"], part["source"]):
                await self._drop(name)
        for part in parts:
            if part["name"] not in self.plugins:
                self.plugins[part["name"]] = self._load(part)
        self.changed()

    async def _drop(self, name):
        """A plugin's worker part goes: its caps are no longer served or announced, and the
        streams they opened are closed (W5, S4)."""
        part = self.plugins.pop(name)
        for cap in part.caps:
            del self.caps[cap]
        for stream in [s for s in self.streams.values() if s.cap in part.caps]:
            await stream.close()
        if part.module is not None:
            sys.modules.pop(part.module, None)

    def _load(self, part):
        """Run a part's module under a name of its own and take its CAPS. A part that fails,
        or names a cap this worker already serves, is kept as failed with the reason and adds nothing."""
        self._loads += 1
        module_name = f"rook_plugin_{part['name'].replace('-', '_')}_{self._loads}"
        module = types.ModuleType(module_name)
        sys.modules[module_name] = module
        try:
            exec(compile(part["source"], f"<plugin {part['name']}>", "exec"), module.__dict__)
            caps = getattr(module, "CAPS", None)
            if not isinstance(caps, dict):
                raise ValueError("the module defines no CAPS dict of name to Cap (docs/plugins.md)")
            for name, cap in caps.items():
                validate({"type": "call", "id": "-", "cap": name, "args": {}})     # a cap name as W3 has it
                if not isinstance(cap, Cap) or not callable(cap.handler) or not isinstance(cap.schema, dict) \
                        or not isinstance(cap.description, str) or not cap.description or "\n" in cap.description:
                    raise ValueError(f"CAPS[{name!r}] must be a Cap with a one-line description, "
                                     "a JSON Schema object and a handler")
                if name in self.caps:
                    raise ValueError(f"{name} is already served by {self._owner(name)}")
        except FAILS as e:
            sys.modules.pop(module_name, None)
            return Part(part["version"], part["source"], error=f"{type(e).__name__}: {e}")
        self.caps.update(caps)
        return Part(part["version"], part["source"], tuple(caps), module_name)

    def _owner(self, cap):
        for name, part in self.plugins.items():
            if cap in part.caps:
                return f"plugin {name}"
        return "this worker"

    async def _send(self, message, link=None):
        try:
            await (link or self._link).send(encode(message))
        except EOFError:
            pass

    # ---------------------------------------------------------------- state

    def _cap_list(self):
        return [{"name": name, "description": cap.description, "schema": cap.schema,
                 "returns": "stream" if cap.opens_stream else "result"}
                for name, cap in sorted(self.caps.items())]

    def heartbeat(self):
        """The full state, as one heartbeat (H1)."""
        beat = {
            "type": "heartbeat", "protocol": PROTOCOL,
            "identity": {"name": self.name, "key": self.key},
            "facts": {"os": platform.system().lower(), "arch": platform.machine(),
                      "hostname": socket.gethostname(), "form": "python", "version": VERSION,
                      "macs": self.macs},
            "caps": self._cap_list(),
            "band": {"gateway_mac": gateway_mac()},
            "health": self.health(),
            "streams": len(self.streams),
            # D5: the handles of every stream this worker opened and still has open, built here, as the
            # heartbeat goes on the link, from the same `streams` that stream_open and stream_close change.
            "open_streams": sorted(self.streams),
            "plugins": [dict(name=name, version=part.version, **({"error": part.error} if part.error else {}))
                        for name, part in sorted(self.plugins.items())],
        }
        if self.config_version is not None:
            beat["config_version"] = self.config_version      # the config it applied (CF2)
        return beat

    def health(self):
        """Health, as far as this machine can tell without extra packages (H4). Each field is read, never
        set, and left out where the machine does not expose it: battery, temperature and GPU as
        `battery()`, `temperature()` and `gpus()` say."""
        health = {}
        disk = shutil.disk_usage(self.state_dir)
        health["disk"] = {"total": disk.total, "free": disk.free}
        try:
            meminfo = dict(line.split(":", 1) for line in pathlib.Path("/proc/meminfo").read_text().splitlines())
            health["ram"] = {"total": int(meminfo["MemTotal"].split()[0]) * 1024,
                             "free": int(meminfo["MemAvailable"].split()[0]) * 1024}
            health["uptime"] = float(pathlib.Path("/proc/uptime").read_text().split()[0])
            ticks = [int(t) for t in pathlib.Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
            idle, total = ticks[3] + ticks[4], sum(ticks)
            if self._cpu and total > self._cpu[1]:
                health["cpu"] = round(100 * (1 - (idle - self._cpu[0]) / (total - self._cpu[1])), 1)
            self._cpu = (idle, total)
        except (OSError, KeyError, ValueError, IndexError):
            pass
        health.update(self._devices())
        return health

    def _devices(self):
        """Battery, temperature and GPU as last read, and a new reading started on a thread of its own if
        none is running. Reading them takes milliseconds (a Wi-Fi card's sensor, `nvidia-smi`): on the
        event loop at every heartbeat, that holds up the link the heartbeats travel on. So a heartbeat
        carries the latest reading, never waits for one, and the first after a start has none yet. With no
        event loop running (health() called on its own), they are read here and now."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return devices()
        if self._reading is None or not self._reading.is_alive():
            self._reading = threading.Thread(target=self._read_devices, daemon=True)
            self._reading.start()
        return self._read

    def _read_devices(self):
        self._read = devices()


def _read(path):
    return path.read_text().strip()


def devices():
    """The H4 fields Linux exposes beyond CPU, RAM and disk: those found, by name."""
    found = {"battery": battery(), "temperature": temperature()}
    found["gpu"], found["vram"] = gpus()
    return {name: value for name, value in found.items() if value is not None}


def battery():
    """Charge, percent: the mean `capacity` of the machine's batteries, the power supplies Linux lists
    under /sys/class/power_supply with type Battery. A device's own battery (scope Device: a mouse, a
    headset) is not the machine's. None without one."""
    charges = []
    for supply in sorted((SYS / "class" / "power_supply").glob("*")):
        try:
            if _read(supply / "type") == "Battery" and not (
                    (supply / "scope").exists() and _read(supply / "scope") == "Device"):
                charges.append(float(_read(supply / "capacity")))
        except (OSError, ValueError):
            continue
    return round(sum(charges) / len(charges), 1) if charges else None


def temperature():
    """Degrees Celsius: the hottest sensor Linux exposes under /sys/class/hwmon (`temp*_input`, in
    thousandths of a degree): the CPU's, a disk's, a GPU's, whichever runs hottest. None without one."""
    readings = []
    for sensor in sorted((SYS / "class" / "hwmon").glob("hwmon*/temp*_input")):
        try:
            readings.append(int(_read(sensor)) / 1000)
        except (OSError, ValueError):
            continue
    return round(max(readings), 1) if readings else None


def gpus():
    """(busy percent, VRAM size) over the machine's GPUs: the mean busy and the summed VRAM. AMD's are
    read from /sys/class/drm/card*/device (gpu_busy_percent, mem_info_vram_total, mem_info_vram_used);
    NVIDIA's do not show there, so they are asked of `nvidia-smi` when it is installed. Either part is
    None when no GPU gives it."""
    busy, total, free = [], [], []
    for device in sorted((SYS / "class" / "drm").glob("card*/device")):
        if not device.parent.name[4:].isdigit():          # card1-DP-1 and the like are its outputs
            continue
        try:
            busy.append(float(_read(device / "gpu_busy_percent")))
        except (OSError, ValueError):
            pass
        try:
            size, used = int(_read(device / "mem_info_vram_total")), int(_read(device / "mem_info_vram_used"))
            total.append(size)
            free.append(size - used)
        except (OSError, ValueError):
            pass
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.total,memory.free",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
        except OSError:
            out = ""
        for line in out.splitlines():
            fields = [f.strip() for f in line.split(",")]
            try:
                busy.append(float(fields[0]))
            except (ValueError, IndexError):
                pass
            try:
                total.append(int(fields[1]) * 1024 * 1024)            # MiB
                free.append(int(fields[2]) * 1024 * 1024)
            except (ValueError, IndexError):
                pass
    return (round(sum(busy) / len(busy), 1) if busy else None,
            {"total": sum(total), "free": sum(free)} if total else None)


def unreachable(e):
    """Why the hub could not be reached, in a few words, from what urllib raised."""
    e = getattr(e, "reason", e)
    return getattr(e, "strerror", None) or str(e) or type(e).__name__


async def _json(value):
    """A result's JSON text, as str chunks, made as it goes (brief 54). The result, or a field of a
    result that is an object, may be `Text` or `Items`, taken as the sending reaches it, or an
    awaitable, awaited there; one in a field that gives LEFT_OUT leaves the field out. An object's
    fields go in the order the cap gave them. Anything that is not JSON fails the call, as an
    ordinary result that is not JSON always has."""
    if not isinstance(value, dict):
        async for chunk in _value(value):
            yield chunk
        return
    yield "{"
    first = True
    for key, item in value.items():
        if inspect.isawaitable(item):
            item = await item
            if item is LEFT_OUT:
                continue
        yield ("" if first else ",") + json.dumps({key: 0}, ensure_ascii=False, separators=(",", ":"))[1:-3] + ":"
        first = False
        async for chunk in _value(item):
            yield chunk
    yield "}"


async def _value(value):
    """One value's JSON text, in chunks of about PIECE characters: Text and Items as they come."""
    if inspect.isawaitable(value):
        value = await value
    if isinstance(value, Text):
        yield '"'
        async for piece in value.pieces:
            if not isinstance(piece, str):
                raise TypeError(f"a Text gives str pieces, not {type(piece).__name__}")
            for chunk in _string(piece):
                yield chunk
        yield '"'
    elif isinstance(value, Items):
        held, size, first = ["["], 1, True
        async for item in value.items:
            for chunk in _plain(item, "" if first else ","):
                held.append(chunk)
                size += len(chunk)
            first = False
            if size >= PIECE:
                yield "".join(held)
                held, size = [], 0
        yield "".join(held) + "]"
    else:
        held, size = [], 0
        for chunk in _plain(value):
            held.append(chunk)
            size += len(chunk)
            if size >= PIECE:
                yield "".join(held)
                held, size = [], 0
        yield "".join(held)


def _plain(value, before=""):
    """An ordinary JSON value's text, as str chunks, `before` first; a long string in chunks."""
    if isinstance(value, str):
        yield before + '"'
        yield from _string(value)
        yield '"'
    elif isinstance(value, dict):
        yield before + "{"
        for i, (key, item) in enumerate(value.items()):
            yield from _plain(item, ("," if i else "") + json.dumps({key: 0}, ensure_ascii=False,
                                                                    separators=(",", ":"))[1:-3] + ":")
        yield "}"
    elif isinstance(value, (list, tuple)):
        yield before + "["
        for i, item in enumerate(value):
            yield from _plain(item, "," if i else "")
        yield "]"
    else:
        yield before + json.dumps(value, allow_nan=False)    # a number, true, false or null; TypeError otherwise


def _string(text):
    """The inside of a JSON string, escaped, in chunks of PIECE characters of `text`."""
    for start in range(0, len(text), PIECE):
        yield json.dumps(text[start:start + PIECE], ensure_ascii=False)[1:-1]


def reason(e):
    """Why a handler failed, in a few words."""
    return f"it called sys.exit({e.code!r})" if isinstance(e, SystemExit) else str(e)


def error(code, message, next_step, call_id=None):
    reply = {"type": "error", "code": code, "message": message, "next": next_step}
    if call_id is not None:
        reply["id"] = call_id
    return reply


def own_macs():
    """This machine's interface MAC addresses (Linux), for W7."""
    macs = []
    for iface in sorted(pathlib.Path("/sys/class/net").glob("*")):
        try:
            mac = (iface / "address").read_text().strip().lower()
        except OSError:
            continue
        if iface.name != "lo" and mac and mac != "00:00:00:00:00:00":
            macs.append(mac)
    return macs


def gateway_mac():
    """The MAC address of the default gateway (Linux), or None: the band fingerprint (B2)."""
    try:
        for line in pathlib.Path("/proc/net/route").read_text().splitlines()[1:]:
            fields = line.split()
            if fields[1] == "00000000" and fields[2] != "00000000":
                gateway = socket.inet_ntoa(bytes.fromhex(fields[2])[::-1])
                for arp in pathlib.Path("/proc/net/arp").read_text().splitlines()[1:]:
                    parts = arp.split()
                    if parts[0] == gateway and parts[3] != "00:00:00:00:00:00":
                        return parts[3].lower()
    except (OSError, IndexError, ValueError):
        pass
    return None


if __name__ == "__main__":
    # The start command (R1): `python -m rook_worker HUB_URL`. It runs from the module named rook_worker,
    # the one plugin parts import Cap from (docs/plugins.md), not from this copy named __main__.
    import rook_start
    sys.exit(rook_start.main())
