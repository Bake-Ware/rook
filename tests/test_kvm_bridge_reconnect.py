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


def _bridge_mod():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
    try:
        from rook_kvm import bridge as bridge_mod
    finally:
        sys.path.pop(0)
    return bridge_mod


@pytest.mark.asyncio
async def test_kvm_bridge_reconnects_after_the_stream_drops(monkeypatch):
    bridge_mod = _bridge_mod()
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


@pytest.mark.asyncio
async def test_malformed_stream_messages_are_skipped_not_fatal(monkeypatch):
    """Non-object JSON or a data message without a payload used to raise in
    the reader, ending the stream; the old socket was then never closed."""
    bridge_mod = _bridge_mod()
    b = bridge_mod.RookBridge(host="192.0.2.1")
    b._backoff = Backoff(base=0.001, cap=0.002)
    junk = ["[1, 2]", "42", '"str"', "null", "{bad", json.dumps({"type": "data"}),
            json.dumps({"type": "data", "payload": {"x": 1}}),
            json.dumps({"type": "file_ready", "path": 7})]
    ws = FakeWS(junk + [json.dumps({"type": "data", "payload": "ok"}),
                        json.dumps({"type": "file_ready", "path": "/a.bin"})],
                then_close=False)
    conns = [ws]

    async def connect():
        if not conns:
            raise OSError("no more")
        return conns.pop(0)

    monkeypatch.setattr(b, "_connect_ws", connect)
    await b.connect_stream()
    async with asyncio.timeout(5):
        while not b._file_ready_events:
            await asyncio.sleep(0.005)
    assert b._serial_buffer == ["ok"] and b._ws is ws and not ws.closed
    assert [e["path"] for e in b._file_ready_events] == ["/a.bin"]
    await b.close()


class BrokenWS(FakeWS):
    """A socket whose iterator fails without closing the connection."""

    async def __anext__(self):
        raise RuntimeError("protocol error")


@pytest.mark.asyncio
async def test_old_socket_is_closed_before_reconnecting(monkeypatch):
    bridge_mod = _bridge_mod()
    b = bridge_mod.RookBridge(host="192.0.2.1")
    b._backoff = Backoff(base=0.001, cap=0.002)
    broken = BrokenWS([], then_close=False)
    good = FakeWS([json.dumps({"type": "data", "payload": "hi"})], then_close=False)
    conns = [broken, good]

    async def connect():
        return conns.pop(0)

    monkeypatch.setattr(b, "_connect_ws", connect)
    await b.connect_stream()
    async with asyncio.timeout(5):
        while b._ws is not good or not b._serial_buffer:
            await asyncio.sleep(0.005)
    assert broken.closed
    await b.close()
