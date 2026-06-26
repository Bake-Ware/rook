"""Band client: joins a Telesthete band via the hub, tracks workers + caps,
fires capability calls, awaits matching replies via futures.

This is the read-side companion to :mod:`rook.worker.core`. It speaks the
same JSON wire format and reuses the worker's hub transport for I/O.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from ..worker.transports.telesthete_hub import TelestheteHubTransport

log = logging.getLogger("rook.band_mcp.client")

# Idle-worker eviction: drop a worker we haven't heard from in this many
# seconds. Workers re-announce every 30s by default, so 90s tolerates a
# couple missed broadcasts before they disappear from `rook_workers()`.
WORKER_STALE_SECS = 90.0


class WorkerEntry(dict):
    """Just a dict with named keys for readability."""

    @property
    def worker_id(self) -> str:
        return self["worker_id"]

    @property
    def name(self) -> str:
        return self.get("name", self["worker_id"])

    @property
    def caps(self) -> list[str]:
        return self.get("caps", [])

    @property
    def last_seen(self) -> float:
        return self.get("last_seen", 0.0)


class BandClient:
    def __init__(self, psk: str, hub_host: str = "127.0.0.1",
                 hub_port: int = 7474) -> None:
        self.transport = TelestheteHubTransport(
            psk=psk, hub_host=hub_host, hub_port=hub_port,
        )
        self.workers: dict[str, WorkerEntry] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._stopping = False
        self._gc_task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.transport.start(self._on_message)
        self._gc_task = asyncio.create_task(self._gc_loop())
        log.info("band-mcp client up (band_id=%s, hub=%s:%d)",
                 self.transport.band_id.hex()[:16],
                 self.transport._hub[0], self.transport._hub[1])

    async def stop(self) -> None:
        self._stopping = True
        if self._gc_task is not None:
            self._gc_task.cancel()
            try:
                await self._gc_task
            except Exception:
                pass
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        await self.transport.stop()

    # -- inbound -------------------------------------------------------------

    async def _on_message(self, payload: bytes, peer_id: tuple) -> None:
        try:
            msg = json.loads(payload)
        except Exception:
            return
        if not isinstance(msg, dict):
            return

        if msg.get("kind") == "announce":
            self._handle_announce(msg)
            return

        # Looks like a reply if it has an id, "from", and an "ok" boolean.
        if "id" in msg and "ok" in msg and "from" in msg:
            self._handle_reply(msg)
            return

    def _handle_announce(self, msg: dict) -> None:
        wid = msg.get("worker_id")
        if not wid:
            return
        entry = self.workers.get(wid) or WorkerEntry()
        entry.update({
            "worker_id": wid,
            "name": msg.get("name", wid),
            "caps": list(msg.get("caps", [])),
            "plugins": list(msg.get("plugins", [])),
            "last_seen": time.time(),
        })
        if wid not in self.workers:
            log.info("worker joined: id=%s name=%s caps=%d",
                     wid, entry["name"], len(entry["caps"]))
        self.workers[wid] = entry

    def _handle_reply(self, msg: dict) -> None:
        mid = msg.get("id")
        fut = self._pending.pop(mid, None)
        if fut is None:
            return  # late or unsolicited
        if not fut.done():
            fut.set_result(msg)
        # Replies also count as a sign of life.
        from_id = msg.get("from")
        if from_id and from_id in self.workers:
            self.workers[from_id]["last_seen"] = time.time()

    # -- outbound ------------------------------------------------------------

    async def call(self, cap: str, args: dict | None = None,
                   target: str | None = None, timeout: float = 15.0) -> dict:
        """Send a capability call and wait for the first matching reply.

        Returns the reply dict (``{"id", "from", "ok", "result"|"error"}``).
        Raises ``asyncio.TimeoutError`` on no reply.
        """
        mid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        msg: dict = {"id": mid, "cap": cap, "args": args or {}}
        if target:
            msg["target"] = target
        await self.transport.send(json.dumps(msg).encode())
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(mid, None)

    async def _gc_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(15.0)
                cutoff = time.time() - WORKER_STALE_SECS
                stale = [wid for wid, w in self.workers.items()
                         if w["last_seen"] < cutoff]
                for wid in stale:
                    name = self.workers[wid].get("name", wid)
                    log.info("worker stale, evicting: id=%s name=%s", wid, name)
                    self.workers.pop(wid, None)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("gc loop failed")
