"""Cached Rook inventory shared by planner context and read-tool validation."""
import asyncio
import json
import time

from .rookmcp import RookMCP


class WorkerInventory:
    def __init__(self, ttl=60):
        self.ttl = ttl
        self.rows = None
        self.updated = 0
        self.lock = asyncio.Lock()

    @property
    def names(self):
        if self.rows is None or time.monotonic() - self.updated >= self.ttl:
            return ()
        return tuple(sorted(w['name'] for w in self.rows))

    async def refresh(self):
        async with self.lock:
            if self.rows is not None and time.monotonic() - self.updated < self.ttl:
                return self.rows
            raw = await asyncio.wait_for(RookMCP().call('rook_workers', {}), 5)
            rows = json.loads(raw)
            if isinstance(rows, str):
                rows = json.loads(rows)
            if not isinstance(rows, list) or any(not isinstance(w, dict) or
                    not isinstance(w.get('name'), str) or not w['name'] for w in rows):
                raise ValueError('Invalid Rook worker inventory')
            self.rows, self.updated = rows, time.monotonic()
            return rows

    async def validate(self, worker):
        await self.refresh()
        if worker not in self.names:
            raise ValueError(f"no Rook worker named {worker!r}; available: {', '.join(self.names) or '(none)'}")
        return worker


inventory = WorkerInventory()
