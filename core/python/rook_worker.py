"""A minimal Rook worker core in Python: register, heartbeat, answer calls (W2).

State directory (W7, U2): `key` holds the private key the worker generated,
`identity.json` the GUID, moniker and hub signature it got on approval. Both
survive restarts, so a restarted worker is the same identity (R5).

The worker never sees a transport. `connect(key)` returns its link to the hub:
an object with `async send(bytes)`, `async recv() -> bytes` (raises EOFError
once closed) and `close()`.
"""

import asyncio
import json
import os
import pathlib
import platform
import shutil
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from nacl.signing import SigningKey

from rook_protocol import PROTOCOL, ProtocolError, decode, encode

VERSION = "0.1.0"


@dataclass
class Cap:
    """A cap this worker serves (W3). `handler(args)` is awaited for each call."""
    description: str
    schema: dict
    handler: Callable[[dict], Awaitable[Any]]


class Refused(Exception):
    """The hub refused this worker's key. `.message` is the hub's `error` message."""

    def __init__(self, message):
        super().__init__(message["message"])
        self.message = message


class Worker:
    def __init__(self, state_dir, hub_url, connect, name=None, caps=None, macs=None, knock_every=5.0):
        self.state_dir = pathlib.Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.hub_url = hub_url.rstrip("/")
        self.connect = connect
        self.name = name or socket.gethostname()
        self.caps = dict(caps or {})
        self.macs = own_macs() if macs is None else macs
        self.knock_every = knock_every
        self.signing_key = self._load_key()
        self.key = self.signing_key.verify_key.encode().hex()
        path = self.state_dir / "identity.json"
        self.identity = json.loads(path.read_text()) if path.exists() else None
        self.interval = None
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
        answer calls until the link closes. Raises Refused if the hub refuses the key."""
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

    def changed(self):
        """Something in the worker's state changed: heartbeat now (H1)."""
        self._changed.set()

    # ---------------------------------------------------------------- knock

    async def _knock_until_approved(self):
        knock = encode({"type": "knock", "protocol": PROTOCOL, "name": self.name, "key": self.key,
                        "macs": self.macs, "caps": self._cap_list()})
        while True:
            reply = await asyncio.to_thread(self._post, knock)
            if reply["type"] == "error":
                raise Refused(reply)
            if reply["status"] == "approved":
                return {name: reply[name] for name in ("guid", "moniker", "signature")}
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
            elif message["type"] == "error" and message["code"] in ("unknown_key", "key_revoked"):
                raise Refused(message)

    async def _answer(self, call):
        cap = self.caps.get(call["cap"])
        if cap is None:
            reply = error("unknown_cap", f"{self.name} does not serve {call['cap']}.",
                          f"List the caps {self.name} announces and call one of those.", call["id"])
        else:
            try:
                reply = {"type": "result", "id": call["id"], "result": await cap.handler(call["args"])}
                encode(reply)
            except Exception as e:
                reply = error("cap_failed", f"{call['cap']} failed: {e}",
                              f"Check the arguments against the schema of {call['cap']}.", call["id"])
        await self._send(reply)

    async def _send(self, message):
        try:
            await self._link.send(encode(message))
        except EOFError:
            pass

    # ---------------------------------------------------------------- state

    def _cap_list(self):
        return [{"name": name, "description": cap.description, "schema": cap.schema, "returns": "result"}
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
