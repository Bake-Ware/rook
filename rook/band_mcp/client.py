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
            "version": msg.get("version"),
            "build": msg.get("build"),
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


class MultiBandClient:
    """Join several bands (one PSK each) over a single shared hub.

    The Telesthete hub relays by ``band_id`` and holds no PSK, so one hub
    carries many bands at once. This wraps one :class:`BandClient` per PSK and
    presents the same surface as a single client — a merged ``workers`` roster
    (each entry tagged with the ``band`` it was seen on) and a ``call()`` that
    routes to the band hosting the target worker. Used to run a new band
    alongside an old one during a PSK rotation, then drop the old PSK.

    ``build_server`` treats this interchangeably with :class:`BandClient`.
    """

    def __init__(self, psks, hub_host: str = "127.0.0.1",
                 hub_port: int = 7474) -> None:
        deduped: list[str] = []
        for p in psks:
            p = (p or "").strip()
            if p and p not in deduped:
                deduped.append(p)
        if not deduped:
            raise ValueError("MultiBandClient requires at least one PSK")
        self._clients = [
            BandClient(psk=p, hub_host=hub_host, hub_port=hub_port)
            for p in deduped
        ]
        self.hub_host = hub_host
        self.hub_port = hub_port

    async def start(self) -> None:
        for c in self._clients:
            await c.start()
            # Short band-id fingerprint, for tagging the merged roster.
            c.label = c.transport.band_id.hex()[:8]
        log.info("multi-band client up: %d band(s) [%s] on hub %s:%d",
                 len(self._clients),
                 ", ".join(getattr(c, "label", "?") for c in self._clients),
                 self.hub_host, self.hub_port)

    async def stop(self) -> None:
        for c in self._clients:
            try:
                await c.stop()
            except Exception:
                log.exception("band client stop failed")

    @property
    def workers(self) -> dict[str, WorkerEntry]:
        """Union of every band's roster. If a worker is briefly visible on two
        bands (mid-migration), the freshest sighting wins."""
        merged: dict[str, WorkerEntry] = {}
        for c in self._clients:
            label = getattr(c, "label", "?")
            for wid, w in c.workers.items():
                prev = merged.get(wid)
                if prev is None or w.get("last_seen", 0.0) >= prev.get("last_seen", 0.0):
                    entry = WorkerEntry(w)
                    entry["band"] = label
                    merged[wid] = entry
        return merged

    def _client_for(self, worker_id: str) -> "BandClient | None":
        """The band where ``worker_id`` was most recently seen."""
        best: BandClient | None = None
        best_seen = -1.0
        for c in self._clients:
            w = c.workers.get(worker_id)
            if w and w.get("last_seen", 0.0) > best_seen:
                best, best_seen = c, w.get("last_seen", 0.0)
        return best

    async def call(self, cap: str, args: dict | None = None,
                   target: str | None = None, timeout: float = 15.0) -> dict:
        # Known target → send only on its band.
        if target:
            c = self._client_for(target)
            if c is not None:
                return await c.call(cap=cap, args=args, target=target, timeout=timeout)
        # Otherwise race across all bands; first real reply wins.
        if len(self._clients) == 1:
            return await self._clients[0].call(cap=cap, args=args, target=target, timeout=timeout)
        tasks = [asyncio.create_task(c.call(cap=cap, args=args, target=target, timeout=timeout))
                 for c in self._clients]
        try:
            result: dict | None = None
            err: Exception | None = None
            pending = set(tasks)
            while pending and result is None:
                done, pending = await asyncio.wait(
                    pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    break  # overall timeout
                for t in done:
                    try:
                        result = t.result()
                        break
                    except Exception as e:
                        err = e
            if result is not None:
                return result
            if err is not None:
                raise err
            raise asyncio.TimeoutError
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
