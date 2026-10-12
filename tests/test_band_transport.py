"""Band transport internals: codec ingest, the single frame writer (priority,
interleaving, cancellation, pacing), keepalive skipping, bounded dispatch.
All fakes — no sockets, no hub."""

import asyncio
import os
import socket

import pytest

from telesthete.protocol.fragment import MAX_CHUNK_PAYLOAD, Reassembler, parse_chunk
from telesthete.protocol.framing import HEADER_SIZE, pack_packet, unpack_packet

from rook.worker.transports import dispatch as dispatch_mod
from rook.worker.transports import writer as writer_mod
from rook.worker.transports.dispatch import Dispatcher
from rook.worker.transports.telesthete_hub import (
    TelestheteHubTransport, udp_pacer_from_env)
from rook.worker.transports.writer import FrameWriter, TokenBucket
from rook.worker.wire.codec import KEEPALIVE_PAYLOAD, BandCodec

PSK = "band-transport-test-psk"


# -- codec -------------------------------------------------------------------

def test_codec_round_trip_single_and_multi_fragment():
    tx, rx = BandCodec(PSK), BandCodec(PSK)
    for payload in (b"hi", os.urandom(MAX_CHUNK_PAYLOAD * 3 + 7)):
        frames = tx.frames(payload, channel_id=5)
        out = [rx.ingest(f) for f in frames]
        assert all(o is None for o in out[:-1])
        assert out[-1].payload == payload
        assert out[-1].channel_id == 5


def test_codec_filters_short_foreign_tampered_and_keepalive():
    tx, rx = BandCodec(PSK), BandCodec(PSK)
    good = tx.frames(b"x")[0]
    # 27..42 bytes: a header with no room for the 16-byte tag. The old `< 27`
    # check let these through to unpack/decrypt.
    assert rx.ingest(good[:HEADER_SIZE + 15]) is None
    assert rx.drops["short"] == 1
    assert rx.ingest(good[:HEADER_SIZE]) is None
    assert rx.drops["short"] == 2
    other = BandCodec("some-other-band").frames(b"x")[0]
    assert rx.ingest(other) is None and rx.drops["band"] == 1
    bad = bytearray(good)
    bad[-1] ^= 1
    assert rx.ingest(bytes(bad)) is None and rx.drops["decrypt"] == 1
    assert rx.ingest(tx.frames(KEEPALIVE_PAYLOAD)[0]) is None
    assert rx.ingest(good).payload == b"x"


def test_codec_sequences_are_unique_and_monotonic_across_messages():
    c = BandCodec(PSK)
    seqs = [unpack_packet(f).sequence for p in (b"a", os.urandom(5000), b"b")
            for f in c.frames(p)]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_codec_passes_reassembler_limits_it_accepts(monkeypatch):
    from rook.worker.wire import codec as codec_mod

    seen = {}

    class Budgeted(Reassembler):
        def __init__(self, timeout=30.0, buffer_limit=256, max_message_bytes=None,
                     max_total_bytes=None):
            seen.update(timeout=timeout, buffer_limit=buffer_limit,
                        max_message_bytes=max_message_bytes, max_total_bytes=max_total_bytes)
            super().__init__(timeout, buffer_limit)

    monkeypatch.setattr(codec_mod, "Reassembler", Budgeted)
    BandCodec(PSK)
    assert seen["max_message_bytes"] == codec_mod.REASSEMBLY_MAX_MESSAGE_BYTES
    assert seen["max_total_bytes"] == codec_mod.REASSEMBLY_MAX_TOTAL_BYTES
    # And the library as pinned (no byte budgets) still constructs.
    monkeypatch.setattr(codec_mod, "Reassembler", Reassembler)
    assert isinstance(BandCodec(PSK).reassembler, Reassembler)


# -- writer ------------------------------------------------------------------

class Link:
    """Records frames; can be gated to hold the writer mid-message."""

    def __init__(self):
        self.frames: list[bytes] = []
        self.gate: asyncio.Event | None = None

    async def write(self, frame: bytes) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.frames.append(frame)


def _writer(link, pacer=None):
    codec = BandCodec(PSK)
    w = FrameWriter(codec.split, codec.seal, link.write, pacer=pacer)
    w.start()
    return w


def _fids(frames):
    """fragment_id of each frame, decrypted, in wire order."""
    codec = BandCodec(PSK)
    out = []
    for f in frames:
        pkt = unpack_packet(f)
        fid, _seq, _total, _data = parse_chunk(codec.crypto.decrypt(pkt.sequence, pkt.ciphertext))
        out.append(fid)
    return out


async def _until(cond, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_small_message_is_not_blocked_behind_bulk():
    class Signalling(Link):
        def __init__(self):
            super().__init__()
            self.fifth = asyncio.Event()

        async def write(self, frame):
            self.frames.append(frame)
            if len(self.frames) == 5:
                self.fifth.set()

    link = Signalling()
    w = _writer(link)
    big = asyncio.create_task(w.submit(os.urandom(MAX_CHUNK_PAYLOAD * 200)))
    await link.fifth.wait()                  # the bulk message is mid-flight
    await w.submit(b"announce")              # completes long before the bulk
    assert not big.done()
    await big
    assert len(link.frames) == 201
    fids = _fids(link.frames)
    at = next(i for i, f in enumerate(fids) if fids.count(f) == 1)
    # The writer yields every YIELD_EVERY frames; control goes out right after.
    assert at <= 5 + writer_mod.YIELD_EVERY, f"control went out at frame {at}"
    rx = BandCodec(PSK)
    got = [m.payload for m in map(rx.ingest, link.frames) if m is not None]
    assert b"announce" in got and len(got) == 2
    await w.stop()


@pytest.mark.asyncio
async def test_bulk_messages_interleave_round_robin_and_reassemble():
    link = Link()
    w = _writer(link)
    a, b = os.urandom(MAX_CHUNK_PAYLOAD * 20), os.urandom(MAX_CHUNK_PAYLOAD * 20)
    await asyncio.gather(w.submit(a), w.submit(b))
    fids = _fids(link.frames)
    # Neither message is contiguous: their fragments alternate on the wire.
    first = fids[0]
    runs = sum(1 for x, y in zip(fids, fids[1:]) if x != y)
    assert runs > 10, "bulk messages were sent back-to-back, not interleaved"
    assert fids.count(first) == 20
    rx = BandCodec(PSK)
    got = [m.payload for m in map(rx.ingest, link.frames) if m is not None]
    assert sorted(got) == sorted([a, b])
    await w.stop()


@pytest.mark.asyncio
async def test_bulk_interleave_is_bounded(monkeypatch):
    monkeypatch.setattr(writer_mod, "BULK_ACTIVE", 2)
    link = Link()
    w = _writer(link)
    msgs = [os.urandom(MAX_CHUNK_PAYLOAD * 4) for _ in range(5)]
    await asyncio.gather(*(w.submit(m) for m in msgs))
    # Never more than 2 messages open (started, not finished) at once.
    fids, total, open_, seen = _fids(link.frames), {}, set(), {}
    for fid in fids:
        seen[fid] = seen.get(fid, 0) + 1
        open_.add(fid)
        assert len(open_) <= 2
        if seen[fid] == 4:
            open_.discard(fid)
    await w.stop()


@pytest.mark.asyncio
async def test_control_cannot_starve_bulk(monkeypatch):
    monkeypatch.setattr(writer_mod, "CONTROL_BURST", 4)
    link = Link()
    link.gate = asyncio.Event()
    w = _writer(link)
    bulk = asyncio.create_task(w.submit(os.urandom(MAX_CHUNK_PAYLOAD * 3)))
    small = [asyncio.create_task(w.submit(b"k%d" % i)) for i in range(20)]
    await asyncio.sleep(0)
    link.gate.set()
    await asyncio.gather(bulk, *small)
    fids = _fids(link.frames)
    bulk_fid = next(f for f in fids if fids.count(f) == 3)
    first_bulk = fids.index(bulk_fid)
    assert first_bulk <= 5, f"bulk waited behind {first_bulk} control frames"
    await w.stop()


@pytest.mark.asyncio
async def test_cancel_before_start_withdraws_the_message():
    link = Link()
    link.gate = asyncio.Event()
    w = _writer(link)
    first = asyncio.create_task(w.submit(b"first"))       # holds the writer
    await asyncio.sleep(0)
    doomed = asyncio.create_task(w.submit(os.urandom(MAX_CHUNK_PAYLOAD * 5)))
    await asyncio.sleep(0)
    doomed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await doomed
    link.gate.set()
    await first
    await w.submit(b"after")
    assert len(link.frames) == 2, "a withdrawn message must emit nothing"
    await w.stop()


@pytest.mark.asyncio
async def test_cancel_after_start_finishes_the_message():
    class Stepped(Link):
        """Writes one frame per release, so the test can cancel mid-message."""
        def __init__(self):
            super().__init__()
            self.step = asyncio.Semaphore(0)

        async def write(self, frame):
            await self.step.acquire()
            self.frames.append(frame)

    link = Stepped()
    w = _writer(link)
    payload = os.urandom(MAX_CHUNK_PAYLOAD * 50)
    task = asyncio.create_task(w.submit(payload))
    link.step.release()
    link.step.release()
    await _until(lambda: len(link.frames) == 2)
    task.cancel()                       # e.g. band_mcp's asyncio.timeout fired
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(48):
        link.step.release()
    await _until(lambda: len(link.frames) == 50)  # no partial buffers left behind
    rx = BandCodec(PSK)
    assert [m.payload for m in map(rx.ingest, link.frames) if m is not None] == [payload]

    # A caller whose timeout fires before its message starts sends nothing.
    busy = asyncio.create_task(w.submit(b"busy"))   # holds the writer at the link
    await asyncio.sleep(0)

    async def caller():
        async with asyncio.timeout(0.01):
            await w.submit(payload)

    with pytest.raises(TimeoutError):
        await caller()
    for _ in range(10):
        link.step.release()
    await busy
    await asyncio.sleep(0.01)
    assert len(link.frames) == 51          # only "busy"
    await w.stop()


@pytest.mark.asyncio
async def test_write_error_reaches_the_sender_and_writer_survives():
    class Flaky(Link):
        async def write(self, frame):
            if not self.frames and not getattr(self, "failed", False):
                self.failed = True
                raise OSError("boom")
            self.frames.append(frame)

    link = Flaky()
    w = _writer(link)
    with pytest.raises(OSError):
        await w.submit(b"one")
    await w.submit(b"two")
    assert len(link.frames) == 1
    await w.stop()
    with pytest.raises(RuntimeError):
        await w.submit(b"three")


def test_token_bucket_rate_and_burst():
    now = [0.0]
    tb = TokenBucket(rate=100.0, burst=3, clock=lambda: now[0])
    for _ in range(3):
        assert tb.delay() == 0
        tb.consume()
    assert tb.delay() == pytest.approx(0.01)
    now[0] += 0.005
    assert tb.delay() == pytest.approx(0.005)
    now[0] += 1.0
    tb.delay()
    assert tb._tokens == 3  # capped at burst


@pytest.mark.asyncio
async def test_pacing_throttles_bulk_but_not_control():
    link = Link()
    w = _writer(link, pacer=TokenBucket(rate=200.0, burst=5))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    big = asyncio.create_task(w.submit(os.urandom(MAX_CHUNK_PAYLOAD * 25)))
    await asyncio.sleep(0.02)
    c0 = loop.time()
    await w.submit(b"ping")                 # not subject to the pacer
    assert loop.time() - c0 < 0.05
    await big
    # 25 frames at 200/s with a 5-frame burst: >= ~0.1 s.
    assert loop.time() - t0 >= 0.09
    await w.stop()


def test_udp_pacer_env(monkeypatch):
    monkeypatch.delenv("ROOK_BAND_UDP_PACE_RATE", raising=False)
    monkeypatch.delenv("ROOK_BAND_UDP_PACE_BURST", raising=False)
    p = udp_pacer_from_env()
    assert p is not None and p.rate == 2000.0 and p.burst == 64
    monkeypatch.setenv("ROOK_BAND_UDP_PACE_RATE", "500")
    monkeypatch.setenv("ROOK_BAND_UDP_PACE_BURST", "8")
    p = udp_pacer_from_env()
    assert (p.rate, p.burst) == (500.0, 8)
    monkeypatch.setenv("ROOK_BAND_UDP_PACE_RATE", "0")
    assert udp_pacer_from_env() is None
    monkeypatch.setenv("ROOK_BAND_UDP_PACE_RATE", "fast")
    assert udp_pacer_from_env().rate == 2000.0


# -- keepalive ---------------------------------------------------------------

class FakeLink:
    KIND = "fake"
    connected = True

    def __init__(self):
        self.frames = []

    async def write(self, frame):
        self.frames.append(frame)

    async def close(self):
        pass


def _transport_with(link, keepalive):
    t = TelestheteHubTransport(psk=PSK, hub_host="127.0.0.1", keepalive_secs=keepalive)
    t.link = link
    t.writer = FrameWriter(t.codec.split, t.codec.seal, link.write)
    t.writer.start()
    return t


def _payloads(frames):
    rx = BandCodec(PSK)
    out = []
    for f in frames:
        pkt = unpack_packet(f)
        out.append(parse_chunk(rx.crypto.decrypt(pkt.sequence, pkt.ciphertext))[3])
    return out


@pytest.mark.asyncio
async def test_keepalive_skipped_while_traffic_flows():
    link = FakeLink()
    t = _transport_with(link, keepalive=0.1)
    ka = asyncio.create_task(t._keepalive_loop())
    try:
        for _ in range(12):                   # busy for ~0.3 s
            await t.send(b"traffic")
            await asyncio.sleep(0.025)
        assert KEEPALIVE_PAYLOAD not in _payloads(link.frames)
        await asyncio.sleep(0.25)             # idle: keepalives resume
        assert _payloads(link.frames).count(KEEPALIVE_PAYLOAD) >= 1
    finally:
        ka.cancel()
        await t.stop()


@pytest.mark.asyncio
async def test_udp_start_registers_and_stop_is_clean():
    t = TelestheteHubTransport(psk=PSK, hub_host="127.0.0.1", hub_port=9)
    sent = []
    seen = []

    async def on_message(p, peer):
        seen.append(p)

    from rook.worker.transports import telesthete_hub as th
    orig = th.UdpLink.write

    async def capture(self, frame):
        sent.append(frame)

    th.UdpLink.write = capture
    try:
        await t.start(on_message)
        assert len(sent) == 1 and _payloads(sent) == [KEEPALIVE_PAYLOAD]
        assert t.link.sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF) > 0
        # Inbound frames go through the codec and into the dispatcher.
        t._on_frame(BandCodec(PSK).frames(b'{"x":1}')[0])
        await _until(lambda: seen)
        assert seen == [b'{"x":1}']
    finally:
        th.UdpLink.write = orig
        await t.stop()
    with pytest.raises(RuntimeError, match="not started"):
        await t.send(b"x")


@pytest.mark.asyncio
async def test_ws_send_raises_when_not_connected():
    t = TelestheteHubTransport(psk=PSK, hub_host="hub.example.com", hub_port=443, use_ws=True)
    with pytest.raises(RuntimeError, match="WS not connected"):
        await t.send(b"x")
    assert t._ws_url.startswith("wss://")


@pytest.mark.asyncio
async def test_ws_link_drop_fails_queued_and_inflight_sends():
    link = FakeLink()
    t = _transport_with(link, keepalive=60)
    blocker = asyncio.Event()

    async def stuck(frame):
        await blocker.wait()
        link.frames.append(frame)

    t.writer._write = stuck
    inflight = asyncio.create_task(t.send(b"inflight"))
    queued = asyncio.create_task(t.send(os.urandom(MAX_CHUNK_PAYLOAD * 3)))
    await asyncio.sleep(0.01)
    t._ws_down()
    for task in (inflight, queued):
        with pytest.raises(RuntimeError, match="WS not connected"):
            await task
    blocker.set()
    await asyncio.sleep(0.01)
    assert len(link.frames) <= 1          # nothing of the queued bulk went out
    await t.stop()


# -- dispatch ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_is_bounded_and_drains_backlog():
    running = 0
    peak = 0
    release = asyncio.Event()
    done = []

    async def handler(p, peer):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1
        done.append(p)

    d = Dispatcher(handler, concurrency=3, backlog=4)
    accepted = [d.submit(b"%d" % i, ()) for i in range(10)]
    assert accepted == [True] * 7 + [False] * 3
    assert d.dropped == 3 and d.queued == 4
    await asyncio.sleep(0.01)
    assert d.running == 3
    release.set()
    await _until(lambda: len(done) == 7)
    assert peak == 3
    assert sorted(done) == sorted(b"%d" % i for i in range(7))
    await d.stop()


@pytest.mark.asyncio
async def test_dispatch_drop_warning_is_rate_limited(caplog):
    now = [0.0]
    block = asyncio.Event()

    async def handler(p, peer):
        await block.wait()

    d = Dispatcher(handler, concurrency=1, backlog=0, clock=lambda: now[0])
    d.submit(b"a", ())
    with caplog.at_level("WARNING", logger=dispatch_mod.log.name):
        for _ in range(50):
            d.submit(b"b", ())
        now[0] += dispatch_mod.WARN_EVERY + 1
        d.submit(b"c", ())
    warnings = [r for r in caplog.records if "backlog full" in r.message]
    assert len(warnings) == 2 and d.dropped == 51
    block.set()
    await d.stop()


@pytest.mark.asyncio
async def test_slow_handler_does_not_stall_ingest():
    """The receive path only enqueues: a handler that never returns doesn't
    stop later frames from being reassembled and dispatched."""
    t = TelestheteHubTransport(psk=PSK, hub_host="127.0.0.1")
    got = []
    hang = asyncio.Event()

    async def handler(p, peer):
        if p == b"slow":
            await hang.wait()
        got.append(p)

    t.dispatcher = Dispatcher(handler, concurrency=2, backlog=8)
    tx = BandCodec(PSK)
    for p in (b"slow", b"fast1", b"fast2"):
        for f in tx.frames(p):
            t._on_frame(f)
    await _until(lambda: b"fast1" in got)
    hang.set()
    await _until(lambda: len(got) == 3)
    await t.dispatcher.stop()


@pytest.mark.asyncio
async def test_dispatch_env_override(monkeypatch):
    monkeypatch.setenv("ROOK_BAND_DISPATCH_CONCURRENCY", "5")
    monkeypatch.setenv("ROOK_BAND_DISPATCH_BACKLOG", "7")
    t = TelestheteHubTransport(psk=PSK, hub_host="127.0.0.1", hub_port=9)
    try:
        await t.start(lambda p, peer: asyncio.sleep(0))
        assert (t.dispatcher.concurrency, t.dispatcher.backlog_limit) == (5, 7)
    finally:
        await t.stop()


def test_wire_channel_shim_is_gone():
    import importlib.util
    assert importlib.util.find_spec("rook.worker.wire.channel") is None
    assert pack_packet  # framing still importable via the library


# -- loopback relay (no network beyond 127.0.0.1) ------------------------------

class _Relay(asyncio.DatagramProtocol):
    """Minimal telesthitium: forward each datagram to the band's other peers."""

    def __init__(self):
        self.peers: dict[bytes, set] = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        band = data[:16]
        self.peers.setdefault(band, set()).add(addr)
        for peer in self.peers[band]:
            if peer != addr:
                self.transport.sendto(data, peer)


@pytest.mark.asyncio
async def test_udp_large_and_small_messages_cross_the_relay(monkeypatch):
    # Default pacing: an unpaced burst of ~300 datagrams overflows the test
    # relay's (default-sized) receive buffer — the real hub's failure mode.
    monkeypatch.delenv("ROOK_BAND_UDP_PACE_RATE", raising=False)
    monkeypatch.delenv("ROOK_BAND_UDP_PACE_BURST", raising=False)
    loop = asyncio.get_running_loop()
    relay_t, _ = await loop.create_datagram_endpoint(_Relay, local_addr=("127.0.0.1", 0))
    port = relay_t.get_extra_info("socket").getsockname()[1]
    a = TelestheteHubTransport(psk=PSK, hub_host="127.0.0.1", hub_port=port)
    b = TelestheteHubTransport(psk=PSK, hub_host="127.0.0.1", hub_port=port)
    got = []

    async def on_b(p, peer):
        got.append(p)

    try:
        await b.start(on_b)
        await a.start(lambda p, peer: asyncio.sleep(0))
        big = os.urandom(300 * 1024)
        big_send = asyncio.create_task(a.send(big))
        await asyncio.sleep(0.005)
        await a.send(b"small")
        assert not big_send.done(), "small reply waited for the whole bulk send"
        await big_send
        await _until(lambda: big in got and b"small" in got)
        assert got.index(b"small") < got.index(big)
    finally:
        await a.stop()
        await b.stop()
        relay_t.close()
