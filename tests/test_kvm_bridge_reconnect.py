"""rook_kvm bridge: the serial WebSocket stream reconnects with backoff."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from rook.core.backoff import Backoff


class FakeWS:
    def __init__(self, messages, then_close=True):
        self.q: asyncio.Queue = asyncio.Queue()
        for m in messages:
            self.q.put_nowait(m)
        if then_close:
            self.q.put_nowait(None)
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        m = await self.q.get()
        if m is None:
            raise StopAsyncIteration
        return m

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True
        self.q.put_nowait(None)


@pytest.mark.asyncio
async def test_kvm_bridge_reconnects_after_the_stream_drops(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
    try:
        from rook_kvm import bridge as bridge_mod
    finally:
        sys.path.pop(0)
    b = bridge_mod.RookBridge(host="192.0.2.1")
    b._backoff = Backoff(base=0.001, cap=0.002)
    second = FakeWS([json.dumps({"type": "data", "payload": "again"})], then_close=False)
    conns = [FakeWS([json.dumps({"type": "data", "payload": "first"})]),
             OSError("dongle rebooting"), second]
    attempts = []

    async def connect():
        attempts.append(1)
        c = conns.pop(0)
        if isinstance(c, Exception):
            raise c
        return c

    monkeypatch.setattr(b, "_connect_ws", connect)
    await b.connect_stream()
    async with asyncio.timeout(5):
        while b._ws is not second or "again" not in "".join(b._serial_buffer):
            await asyncio.sleep(0.005)
    assert len(attempts) == 3            # first, a failed retry, then success
    assert "first" in "".join(b._serial_buffer)
    await b.write_serial_ws("hello")
    assert json.loads(second.sent[-1])["payload"] == "hello"
    await b.close()
    assert second.closed and b._ws is None and b._ws_task is None
