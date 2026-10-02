#!/usr/bin/env python3
"""The Python reference candidate for the live conformance run.

A stock :class:`rook.worker.core.Worker` (the real worker dispatch and
announce code) with the conformance caps added. It proves the harness and
documents the expected behaviour in the reference language:

    python conformance/harness.py --candidate "python conformance/reference_worker.py"

Configuration comes from the environment (see conformance/README.md). State
(worker id, audit log) goes to a throwaway HOME so it never touches a real
worker's ``~/.rook-band-worker``.
"""

from __future__ import annotations

import os
import sys
import tempfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="rook-ref-candidate-")  # before rook.worker imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402

from rook.core import authz  # noqa: E402
from rook.core.plugin import Plugin, capability  # noqa: E402
from rook.worker.core import Worker  # noqa: E402
from rook.worker.transports.telesthete_hub import TelestheteHubTransport  # noqa: E402

IDENTITY = os.environ.get("ROOK_IDENTITY", "agent:conformance-python")
ANCHOR = os.environ.get("ROOK_ANCHOR", "")


class Conformance(Plugin):
    NAMESPACE = "conformance"

    def __init__(self) -> None:
        super().__init__()
        self.worker: Worker | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.rook: dict | None = None

    def bind_worker(self, worker: Worker) -> None:
        self.worker = worker
        worker.register_binary_handler(self._inbound)

    def _inbound(self, payload: bytes, _peer) -> bool:
        """Replies to our own calls and hub announces; everything else goes on
        to the worker's normal dispatch."""
        try:
            msg = json.loads(payload)
        except ValueError:
            return False
        if not isinstance(msg, dict):
            return False
        if msg.get("kind") == "announce" and msg.get("name") == "rook":
            verified = False
            if ANCHOR:
                band = self.worker.transport.band_id.hex()
                verified = "is_hub" in authz.held_roles(msg, [ANCHOR], band=band)
            if verified or not ANCHOR:
                self.rook = {"worker_id": msg.get("worker_id"), "verified": verified}
            return False
        fut = self.pending.pop(str(msg.get("id")), None) if "ok" in msg else None
        if fut is not None and not fut.done():
            fut.set_result(msg)
            return True
        return False

    async def _call_rook(self, cap: str, args: dict, timeout: float = 15.0) -> dict:
        end = time.monotonic() + 40
        while self.rook is None and time.monotonic() < end:
            await asyncio.sleep(0.25)   # the hub announces every ~30 s
        if self.rook is None:
            raise RuntimeError("hub worker 'rook' not seen on the band")
        mid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self.pending[mid] = fut
        msg = {"id": mid, "cap": cap, "args": args, "target": self.rook["worker_id"],
               "identity": IDENTITY}
        try:
            await self.worker.transport.send(json.dumps(msg).encode())
            reply = await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(mid, None)
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error") or f"{cap} failed")
        return reply.get("result")

    @capability("echo", risk="read")
    def echo(self, value=None):
        """Return value unchanged."""
        return value

    @capability("add", risk="read")
    def add(self, a: int, b: int) -> int:
        """Return a + b."""
        return a + b

    @capability("chat_post", risk="write")
    async def chat_post(self, room: str, text: str) -> dict:
        """Post text to a hub chat room (chat.write on worker rook)."""
        hub = await self._call_rook("chat.write", {"action": "send", "room": room, "text": text})
        return {"hub": hub, "rook": self.rook}

    @capability("chat_read", risk="read")
    async def chat_read(self, room: str, since_seq: int = 0) -> dict:
        """Read a hub chat room (chat.read on worker rook)."""
        hub = await self._call_rook("chat.read", {"action": "read", "room": room,
                                                  "since_seq": since_seq})
        return {"hub": hub, "rook": self.rook}


async def main() -> None:
    host, _, port = os.environ["ROOK_RELAY"].rpartition(":")
    transport = TelestheteHubTransport(psk=os.environ["ROOK_PSK"], hub_host=host,
                                       hub_port=int(port))
    worker = Worker(transport, enabled=[], name=os.environ.get("ROOK_NAME", "conformance-py"),
                    announce_interval=float(os.environ.get("ROOK_ANNOUNCE_SECS", "30")))
    plugin = Conformance()
    for dotpath, fn in plugin.caps().items():
        worker.registry.register(dotpath, fn)
    worker.plugins.append(plugin)
    plugin.bind_worker(worker)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
