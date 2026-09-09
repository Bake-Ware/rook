"""Shared, bounded chat-summary polling for the operator dashboard and CLI."""
from __future__ import annotations

import asyncio
import math
import logging
import time


class BandOverview:
    INTERVAL = 15.0
    MAX_AGE = 60.0
    IDLE_AFTER = 90.0
    CONCURRENCY = 4

    def __init__(self, roster, call):
        self._roster = roster
        self._call = call
        self._cache = {}
        self._task = None
        self._wake = asyncio.Event()
        self._last_request = 0.0

    def snapshot(self, band: str = "") -> dict:
        """Return immediately; HTTP readers never wait for a worker call."""
        self._last_request = time.monotonic()
        self._wake.set()
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        rows = self._roster(band)
        now = time.time()
        chats = []
        for worker in rows:
            if worker.get("banned") or worker["last_seen_age_secs"] >= 90:
                continue
            cached = self._cache.get((worker.get("band"), worker["worker_id"]))
            if not cached or now - cached[0] > self.MAX_AGE:
                continue
            updated, rooms = cached
            for room in rooms:
                chats.append({**room, "wid": worker["worker_id"],
                              "name": worker.get("name") or worker["worker_id"],
                              "band": worker.get("band"), "updated_at": updated,
                              "stale": now - updated > self.INTERVAL * 2})
        chats.sort(key=lambda r: r["last_ts"], reverse=True)
        return {"workers": rows, "chats": chats, "generated_at": now,
                "chat_refresh_seconds": self.INTERVAL}

    async def refresh(self):
        workers = [w for w in self._roster("") if not w.get("banned")
                   and w["last_seen_age_secs"] < 90 and "chat.rooms" in w.get("caps", [])]
        keys = {(w.get("band"), w["worker_id"]) for w in workers}
        self._cache = {key: value for key, value in self._cache.items() if key in keys}

        async def poll(worker):
            try:
                response = await asyncio.wait_for(
                    self._call("chat.rooms", target=worker["worker_id"], args={}, timeout=3), timeout=4)
                result = response.get("result")
                if not response.get("ok") or not isinstance(result, dict) or result.get("ok") is False:
                    return
                rooms = result.get("rooms")
                if not isinstance(rooms, list):
                    return
                clean = []
                for room in rooms[:40]:
                    if not isinstance(room, dict) or not isinstance(room.get("room"), str):
                        continue
                    ts = room.get("last_ts") or 0
                    if not isinstance(ts, (int, float)) or not math.isfinite(ts):
                        ts = 0
                    clean.append({"room": room["room"][:128], "last_ts": ts,
                                  "last_text": str(room.get("last_text") or "")[:280],
                                  "last_sender": str(room.get("last_sender") or "")[:128]})
                self._cache[(worker.get("band"), worker["worker_id"])] = (time.time(), clean)
            except Exception:
                pass  # Keep previous data briefly; snapshots label/expire it.

        for start in range(0, len(workers), self.CONCURRENCY):
            await asyncio.gather(*(poll(w) for w in workers[start:start + self.CONCURRENCY]))

    async def _run(self):
        while True:
            await self._wake.wait()
            self._wake.clear()
            while time.monotonic() - self._last_request < self.IDLE_AFTER:
                try:
                    await self.refresh()
                except Exception:
                    logging.getLogger(__name__).exception("Band overview refresh failed")
                await asyncio.sleep(self.INTERVAL)

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
