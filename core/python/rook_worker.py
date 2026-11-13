"""A minimal Rook worker core in Python: register, heartbeat, answer calls (W2).

State directory (W7, U2): `key` holds the private key the worker generated,
`identity.json` the GUID, moniker, hub signature and hub key it got on approval. Both
survive restarts, so a restarted worker is the same identity (R5).

The worker never sees a transport. `connect(key)` returns its link to the hub:
an object with `async send(bytes)`, `async recv() -> bytes` (raises EOFError
once closed) and `close()`.
"""

import asyncio
import base64
import json
import os
import pathlib
import platform
import shutil
import socket
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from nacl.signing import SigningKey, VerifyKey

import rook_net
from rook_protocol import PROTOCOL, ProtocolError, decode, encode

VERSION = "0.1.0"


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
    (None once closed); `close` it. It lives until one side closes it (S4)."""

    def __init__(self, worker, handle):
        self.worker, self.handle = worker, handle
        self.open = True
        self._input = asyncio.Queue()
        self._ready = asyncio.Event()     # set once the hub has its stream_open, so no data goes first

    async def send(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        await self._ready.wait()
        if self.open:
            await self.worker._send({"type": "stream_data", "stream": self.handle,
                                     "data": base64.b64encode(data).decode("ascii")})

    async def read(self):
        """The next input from a client, as bytes, or None when the stream is closed."""
        return await self._input.get()

    async def close(self):
        if self.open:
            await self._ready.wait()
            await self.worker._send({"type": "stream_close", "stream": self.handle})
            self._closed()

    def _closed(self):
        if self.open:
            self.open = False
            self._input.put_nowait(None)
            self.worker.streams.pop(self.handle, None)
            self.worker.changed()        # the open count is part of the heartbeat (S4)


class Refused(Exception):
    """The hub refused this worker's key. `.message` is the hub's `error` message."""

    def __init__(self, message):
        super().__init__(message["message"])
        self.message = message


class Worker:
    def __init__(self, state_dir, hub_url, connect, name=None, caps=None, macs=None, knock_every=5.0,
                 token=None):
        self.state_dir = pathlib.Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.hub_url = hub_url.rstrip("/")
        self.connect = connect
        self.name = name or socket.gethostname()
        self.caps = {**primitives(), **dict(caps or {})}   # every worker serves the network primitives (D3)
        self.macs = own_macs() if macs is None else macs
        self.knock_every = knock_every
        self.token = token  # a join token: approved at once, no pending step (R3)
        self.signing_key = self._load_key()
        self.key = self.signing_key.verify_key.encode().hex()
        path = self.state_dir / "identity.json"
        self.identity = json.loads(path.read_text()) if path.exists() else None
        self.interval = None
        self.streams = {}                 # handle -> Stream, while open
        self._link = None
        self._changed = asyncio.Event()
        self._interval_known = asyncio.Event()
        self._cpu = None

    def _load_key(self):
        """Generate the keypair on first start; reuse it after (R5, W7)."""
        path = self.state_dir / "key"
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(SigningKey.generate().encode().hex())
        return SigningKey(bytes.fromhex(path.read_text().strip()))

    async def run(self):
        """Knock until approved (first start only), then connect, heartbeat and
        answer calls until the link closes. Raises Refused if the hub refuses the key,
        and BadSignatureError if the approval's signature does not verify."""
        if self.identity is None:
            self.identity = await self._knock_until_approved()
            (self.state_dir / "identity.json").write_text(json.dumps(self.identity))
        self._link = await self.connect(self.key)
        beats = asyncio.create_task(self._heartbeats())
        try:
            await self._serve()
        finally:
            beats.cancel()
            await asyncio.gather(beats, return_exceptions=True)
            self._link.close()

    def stop(self):
        if self._link is not None:
            self._link.close()

    async def emit(self, name, payload):
        """Put an event on the hub's bus (J2), for example from inside a cap."""
        await self._send({"type": "event", "name": name, "payload": payload})

    def changed(self):
        """Something in the worker's state changed: heartbeat now (H1)."""
        self._changed.set()

    # ---------------------------------------------------------------- knock

    async def _knock_until_approved(self):
        knock = {"type": "knock", "protocol": PROTOCOL, "name": self.name, "key": self.key,
                 "macs": self.macs, "caps": self._cap_list()}
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

    def _post(self, body):
        request = urllib.request.Request(self.hub_url + "/knock", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request) as response:
                return decode(response.read())
        except urllib.error.HTTPError as e:
            return decode(e.read())

    # ----------------------------------------------------------------- link

    async def _heartbeats(self):
        while True:
            self._changed.clear()
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
                self.interval = message["interval"]
                self._interval_known.set()
            elif message["type"] == "call":
                asyncio.create_task(self._answer(message))
            elif message["type"] == "stream_data" and message["stream"] in self.streams:
                self.streams[message["stream"]]._input.put_nowait(base64.b64decode(message["data"]))
            elif message["type"] == "stream_close" and message["stream"] in self.streams:
                self.streams[message["stream"]]._closed()
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
                encode(reply)
            except Exception as e:
                reply = error("cap_failed", f"{call['cap']} failed: {e}",
                              f"Check the arguments against the schema of {call['cap']}.", call["id"])
        await self._send(reply)

    async def _open_stream(self, call, cap):
        """A call on a stream cap: the handler names the stream, the hub hears stream_open (S1)."""
        # A fresh random handle, so a stream opened after a restart never takes the
        # handle of one the hub still lists from before it.
        stream = Stream(self, uuid.uuid4().hex[:16])
        try:
            named = await cap.handler(call["args"], stream)
            reply = {"type": "stream_open", "id": call["id"], "stream": stream.handle,
                     "title": str(named["title"]), "description": str(named["description"])}
            encode(reply)
        except Exception as e:
            await self._send(error("cap_failed", f"{call['cap']} failed: {e}",
                                   f"Check the arguments against the schema of {call['cap']}.", call["id"]))
            return
        self.streams[stream.handle] = stream
        await self._send(reply)
        stream._ready.set()
        self.changed()

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


def primitives():
    """The dumb network primitives every worker serves, so it can be a jump box for
    devices on its band (D3): net.http, net.tcp and net.udp (rook_net.py). Built in
    until the worker part of plugins exists (PL7)."""
    def serve(function):
        async def handler(args):
            return await asyncio.to_thread(function, args)
        return handler
    return {name: Cap(description, schema, serve(function))
            for name, (function, description, schema) in rook_net.CAPS.items()}


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
