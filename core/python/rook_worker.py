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
the key (`unknown_key`, `key_revoked`) stops the worker.
"""

import asyncio
import base64
import json
import os
import pathlib
import platform
import random
import shutil
import socket
import sys
import types
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from nacl.public import PrivateKey
from nacl.signing import SigningKey, VerifyKey

import rook_link
from rook_protocol import (MAX_MESSAGE, MAX_STREAM_DATA, PROTOCOL, ProtocolError, decode, encode, session_psk,
                           sign_link_key, sign_link_open, validate, verify_link_accept)

VERSION = "0.1.0"
RETRY_FIRST, RETRY_MOST, RETRY_JITTER = 0.5, 30.0, 0.2   # a failed attempt: 0.5 s doubling to 30 s, ±20 % (link design 3.2)
HTTP_TIMEOUT = 10.0      # seconds a knock or link_open waits on the hub: Telesthete's WebSocket open timeout (Bake 13 Nov)

# What a cap's handler, or a plugin part as it loads, may raise and only fail that call or
# that part: any Exception, and SystemExit, so code that calls sys.exit() cannot stop the
# worker. KeyboardInterrupt and cancellation are not caught: they still stop it.
FAILS = (Exception, SystemExit)


@dataclass
class Cap:
    """A cap this worker serves (W3). `handler(args)` is awaited for each call.

    A stream cap (`opens_stream=True`, S1) is called as `handler(args, stream)` and
    returns {"title": ..., "description": ...} (S3); it keeps `stream` to send on."""
    description: str
    schema: dict
    handler: Callable[..., Awaitable[Any]]
    opens_stream: bool = False


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
    with scheme ws or wss and path /band (2.3). One session per connection; never reused."""
    key: str
    psk: str = field(repr=False)
    band_url: str


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
        self._loads = 0
        self._link = None
        self._stopped = asyncio.Event()
        self._replied = False             # this session's link has delivered a heartbeat_reply
        self._sleep = asyncio.sleep       # a test replaces it, so backoff waits are asked for, not slept
        self._changed = asyncio.Event()
        self._interval_known = asyncio.Event()
        self._cpu = None

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
            self.identity = await self._knock_until_approved()
            (self.state_dir / "identity.json").write_text(json.dumps(self.identity))
        wait = None
        while not self._stopped.is_set():
            if await self._session():             # it worked: a new exchange at once
                wait = None
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
        return self._replied

    def stop(self):
        """End run(): during a session, during the exchange or while waiting to try again."""
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
            if reply["status"] == "approved":
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
        except (OSError, ProtocolError):
            return None
        if reply["type"] == "error":
            if reply["code"] in ("unknown_key", "key_revoked"):
                raise Refused(reply)
            return None
        if reply["type"] != "link_accept" or not verify_link_accept(hub_key, self.key, public, reply):
            return None
        try:
            psk = session_psk("worker", self.key, public, reply["ephemeral"], ephemeral, self.link_secret,
                              reply["hub_link_key"])
        except ProtocolError:                     # a small-order point
            return None
        scheme, rest = self.hub_url.split("://", 1)
        return Session(self.key, psk, f"{'wss' if scheme == 'https' else 'ws'}://{rest}/band")

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
        while True:
            try:
                data = await self._link.recv()
            except EOFError:
                return
            try:
                message = decode(data)
            except ProtocolError as e:
                await self._send(error("bad_message", str(e), "Send messages as docs/protocol.md defines them."))
                continue
            if message["type"] == "heartbeat_reply":
                self._replied = True
                self.interval = message["interval"]
                self._interval_known.set()
            elif message["type"] == "call":
                asyncio.create_task(self._answer(message))
            elif message["type"] == "stream_data" and message["stream"] in self.streams:
                self.streams[message["stream"]]._input.put_nowait(base64.b64decode(message["data"]))
            elif message["type"] == "stream_resize" and message["stream"] in self.streams:
                self.streams[message["stream"]]._resize(message["cols"], message["rows"])
            elif message["type"] == "stream_close" and message["stream"] in self.streams:
                self.streams[message["stream"]]._closed()
            elif message["type"] == "plugins":
                await self._plugins(message["plugins"])
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
            try:
                reply = {"type": "result", "id": call["id"], "result": await cap.handler(call["args"])}
                if (size := len(encode(reply))) > MAX_MESSAGE:     # docs/protocol.md, "Sizes"
                    raise ValueError(f"its result is {size} bytes encoded, over the {MAX_MESSAGE} a message may be")
            except FAILS as e:
                reply = error("cap_failed", f"{call['cap']} failed: {reason(e)}",
                              f"Check the arguments against the schema of {call['cap']}.", call["id"])
        await self._send(reply)

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
            await self._send(error("cap_failed", f"{call['cap']} failed: {reason(e)}",
                                   f"Check the arguments against the schema of {call['cap']}.", call["id"]))
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

    async def _send(self, message):
        try:
            await self._link.send(encode(message))
        except EOFError:
            pass

    # ---------------------------------------------------------------- state

    def _cap_list(self):
        return [{"name": name, "description": cap.description, "schema": cap.schema,
                 "returns": "stream" if cap.opens_stream else "result"}
                for name, cap in sorted(self.caps.items())]

    def heartbeat(self):
        """The full state, as one heartbeat (H1)."""
        return {
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

    def health(self):
        """Health, as far as this machine can tell without extra packages (H4)."""
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
        return health


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
