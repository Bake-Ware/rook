"""Sync plugin code runs on a bounded pool per plugin (review K2): a plugin
whose calls hang only queues its own calls, never other plugins' or the
loop's default executor. Every test runs under a hard timeout."""
from __future__ import annotations

import asyncio
import threading

from rook.kit import ctx as kit_ctx
from rook.kit.ctx import Caller, Ctx, invoke


def test_a_hung_plugin_starves_only_itself():
    gate = threading.Event()
    names = []

    def hang(ctx):
        names.append(threading.current_thread().name)
        gate.wait(10)
        return "late"

    def quick(ctx):
        return threading.current_thread().name

    async def go():
        hung = Ctx({"plugin": "hung"}, Caller(person="person:p"))
        other = Ctx({"plugin": "other"}, Caller(person="person:p"))
        try:
            for _ in range(kit_ctx.PLUGIN_THREADS + 4):     # more than its pool: some queue
                try:
                    await asyncio.wait_for(invoke(hang, hung), 0.05)
                except asyncio.TimeoutError:
                    pass
            assert len(names) == kit_ctx.PLUGIN_THREADS and all(n.startswith("rook-hung") for n in names)
            # another plugin and the loop's default executor still answer at once
            name = await asyncio.wait_for(invoke(quick, other), 2)
            assert name.startswith("rook-other")
            assert await asyncio.wait_for(asyncio.to_thread(lambda: 42), 2) == 42
        finally:
            gate.set()
    asyncio.run(asyncio.wait_for(go(), 20))
