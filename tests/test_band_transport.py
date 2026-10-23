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
from rook.worker.transports import telesthete_hub as hub_mod
from rook.worker.transports.telesthete_hub import (
    TelestheteHubTransport, WsLink, set_socket_buffers, udp_pacer_from_env)
from rook.worker.transports.writer import FrameWriter, TokenBucket
from rook.worker.wire.codec import KEEPALIVE_PAYLOAD, BandCodec

PSK = "band-transport-test-psk"


# -- codec -------------------------------------------------------------------

def test_codec_round_trip_single_and_multi_fragment():
    tx, rx = BandCodec(PSK), BandCodec(PSK)
    for payload in (b"hi", os.urandom(MAX_CHUNK_PAYLOAD * 3 + 7)):
        frames = tx.frames(payload, route=5)
        assert all(f[16] == 0x06 for f in frames)   # ADDRESSED to route 5
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


def test_codec_accepts_channel_and_addressed_only():
    tx, rx = BandCodec(PSK), BandCodec(PSK)
    assert rx.ingest(tx.frames(b"c")[0]).channel_type == 0x02
    assert rx.ingest(tx.frames(b"a", route=9)[0]).channel_type == 0x06
    for ctype in (0x00, 0x01, 0x03, 0x07, 0xFF):
        f = bytearray(tx.frames(b"x")[0])
        f[16] = ctype   # empty AAD: the header isn't authenticated
        assert rx.ingest(bytes(f)) is None
    assert rx.drops["type"] == 5 and rx.drops["decrypt"] == 0


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


class _HungSession:
    """aiohttp session whose handshake never answers (a phone mid network switch)."""
    started = 0

    def __init__(self, *a, **k):
        pass

    async def ws_connect(self, *a, **k):
        _HungSession.started += 1
        await asyncio.Event().wait()

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_ws_handshake_is_bounded(monkeypatch):
    """A handshake that never completes fails into the retry loop instead of
    keeping the worker off the band for hours (bakephone, 2026-10-22)."""
    import aiohttp
    _HungSession.started = 0
    monkeypatch.setattr(aiohttp, "ClientSession", _HungSession)
    monkeypatch.setattr(hub_mod, "_env_num", lambda name, default, cast=float, minimum=0:
                        0.05 if name == "ROOK_BAND_WS_CONNECT_TIMEOUT" else default)
    link = WsLink("wss://hub.example.com/band")
    link.backoff.next = lambda: 0.01
    task = asyncio.create_task(link.run(lambda f: None, _noop_up, lambda: None))
    await asyncio.sleep(0.4)
    assert _HungSession.started >= 2       # timed out and tried again
    await link.close()
    task.cancel()


async def _noop_up():
    pass


@pytest.mark.asyncio
async def test_ws_kick_abandons_hung_connect_and_retries_now(monkeypatch):
    """kick() (network changed) drops the connect in flight and skips the backoff."""
    import aiohttp
    _HungSession.started = 0
    monkeypatch.setattr(aiohttp, "ClientSession", _HungSession)
    link = WsLink("wss://hub.example.com/band")
    link.backoff.next = lambda: 3600.0     # only a kick can get past this
    task = asyncio.create_task(link.run(lambda f: None, _noop_up, lambda: None))
    await asyncio.sleep(0.05)
    assert _HungSession.started == 1
    link.kick()
    await asyncio.sleep(0.05)
    assert _HungSession.started == 2
    assert not task.done()                 # the loop survived the cancelled attempt
    await link.close()
    task.cancel()


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

    shed = []
    d = Dispatcher(handler, concurrency=3, backlog=4, on_shed=lambda p, peer: shed.append(p))
    reqs = [b'{"id":"%d","cap":"x.y"}' % i for i in range(10)]
    accepted = [d.submit(r, ()) for r in reqs]
    assert accepted == [True] * 7 + [False] * 3
    assert d.dropped == 3 and d.queued == 4 and shed == reqs[7:]
    await asyncio.sleep(0.01)
    assert d.running == 3
    release.set()
    await _until(lambda: len(done) == 7)
    assert peak == 3
    assert sorted(done) == sorted(reqs[:7])
    await d.stop()


def test_only_requests_are_sheddable():
    from rook.worker.transports.dispatch import is_request
    assert is_request(b'{"id":"1","cap":"a.b","args":{}}')
    assert not is_request(b'{"id":"1","from":"w","ok":true,"result":1}')     # reply
    assert not is_request(b'{"id":"1","cap":"a.b","ok":false}')              # reply echoing cap
    assert not is_request(b'{"kind":"announce","worker_id":"w","caps":["a.b"],"cap":"x"}')
    assert not is_request(b"\x01" * 16 + b"\x04" + b"\0" * 40)             # Drop packet
    assert not is_request(b"\0rz" + b"junk")                                # compressed (not inflated)
    assert not is_request(b"[1,2]") and not is_request(b"{bad json")
    big_reply = b'{"id":"1","from":"w","ok":true,"result":"' + b"x" * 100_000 + b'"}'
    big_req = b'{"id":"1","cap":"a.b","args":{"v":"' + b"x" * 100_000 + b'"}}'
    assert not is_request(big_reply) and is_request(big_req)


@pytest.mark.asyncio
async def test_replies_announces_and_drop_packets_are_never_shed():
    """With every request slot and the backlog full, everything that isn't a
    new request still runs, at once."""
    block = asyncio.Event()
    seen = []

    async def handler(p, peer):
        if b'"cap"' in p and b'"ok"' not in p:
            await block.wait()
        seen.append(p)

    d = Dispatcher(handler, concurrency=1, backlog=1)
    assert d.submit(b'{"id":"a","cap":"x.y"}', ())
    assert d.submit(b'{"id":"b","cap":"x.y"}', ())
    assert not d.submit(b'{"id":"c","cap":"x.y"}', ())          # shed
    others = [b'{"id":"r%d","from":"w","ok":true}' % i for i in range(500)]
    others += [b'{"kind":"announce","worker_id":"w"}', b"\x07" * 16 + b"\x04" + b"\0" * 30]
    assert all(d.submit(p, ()) for p in others)
    await _until(lambda: len(seen) == len(others))
    assert d.dropped == 1
    block.set()
    await d.stop()


@pytest.mark.asyncio
async def test_dispatch_backpressure_instead_of_dropping(monkeypatch):
    monkeypatch.setattr(dispatch_mod, "URGENT_HIGH", 4)
    monkeypatch.setattr(dispatch_mod, "URGENT_LOW", 1)
    gate = asyncio.Event()

    async def handler(p, peer):
        await gate.wait()

    d = Dispatcher(handler)
    for i in range(4):
        d.submit(b'{"id":"%d","from":"w","ok":true}' % i, ())
    assert d.saturated
    waiter = asyncio.create_task(d.room())
    await asyncio.sleep(0.01)
    assert not waiter.done()
    gate.set()
    await asyncio.wait_for(waiter, 1)
    assert not d.saturated and d.dropped == 0
    await d.stop()


async def _burst(n, payload_for, *, handler_delay=0.0):
    """Queue ``n`` single-fragment messages in a transport's UDP socket
    while the loop is blocked, then let it run (the probe from the audit:
    3,000 queued replies used to lose 1,912)."""
    import time as _time
    hub = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    hub.bind(("127.0.0.1", 0))
    got = []

    async def on_msg(p, peer):
        if handler_delay:
            await asyncio.sleep(handler_delay)
        got.append(p)

    t = TelestheteHubTransport(PSK, "127.0.0.1", hub.getsockname()[1])
    await t.start(on_msg)
    try:
        me = t.link.sock.getsockname()
        tx = BandCodec(PSK)
        frames = [f for i in range(n) for f in tx.frames(payload_for(i))]
        for f in frames:          # the loop is blocked: all of it queues
            hub.sendto(f, ("127.0.0.1", me[1]))
        _time.sleep(0.2)
        deadline = asyncio.get_running_loop().time() + 10
        while len(got) < n and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
            if t.dispatcher.dropped and not t.dispatcher.running and not t.dispatcher.queued:
                break
        return got, t.dispatcher.dropped
    finally:
        await t.stop()
        hub.close()


def _kernel_fits(n, payload_for):
    """How many of these ``n`` frames the kernel queues for one unread UDP
    socket with the transport's buffer size. The requested SO_RCVBUF is capped
    by net.core.rmem_max, which differs per host (GitHub's runners queue about
    2,200 of these, a 4 MiB rmem_max all 3,000); datagrams beyond it are
    dropped by the kernel before the transport could see them, which is not
    what these tests measure."""
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        set_socket_buffers(rx)
        rx.bind(("127.0.0.1", 0))
        rx.setblocking(False)
        codec = BandCodec(PSK)
        for i in range(n):
            for f in codec.frames(payload_for(i)):
                tx.sendto(f, rx.getsockname())
        fits = 0
        while True:
            try:
                rx.recv(65535)
            except BlockingIOError:
                return fits
            fits += 1
    finally:
        rx.close()
        tx.close()


def _burst_size(n, payload_for):
    fits = _kernel_fits(n, payload_for)
    if fits < 1000:
        pytest.skip(f"this host's UDP receive buffer queues only {fits} frames (net.core.rmem_max)")
    return min(n, fits)


@pytest.mark.asyncio
async def test_burst_of_queued_replies_is_delivered_in_full():
    payload = lambda i: b'{"id":"%d","ok":true,"from":"w"}' % i  # noqa: E731
    n = _burst_size(3000, payload)
    got, dropped = await _burst(n, payload)
    assert dropped == 0 and len(got) == n


@pytest.mark.asyncio
async def test_burst_of_fast_requests_is_served_not_shed():
    # The receive loop yields every RECV_YIELD_EVERY frames, so handlers run
    # while a backlog is being read and the dispatch backlog never fills.
    payload = lambda i: b'{"id":"%d","cap":"x.y"}' % i  # noqa: E731
    n = _burst_size(3000, payload)
    got, dropped = await _burst(n, payload)
    assert dropped == 0 and len(got) == n


@pytest.mark.asyncio
async def test_burst_of_slow_requests_sheds_only_requests(monkeypatch):
    monkeypatch.setenv("ROOK_BAND_DISPATCH_CONCURRENCY", "4")
    monkeypatch.setenv("ROOK_BAND_DISPATCH_BACKLOG", "16")
    got, dropped = await _burst(200, lambda i: b'{"id":"%d","cap":"x.y"}' % i,
                                handler_delay=0.5)
    assert dropped > 0 and len(got) + dropped == 200


@pytest.mark.asyncio
async def test_dispatch_drop_warning_is_rate_limited(caplog):
    now = [0.0]
    block = asyncio.Event()

    async def handler(p, peer):
        await block.wait()

    d = Dispatcher(handler, concurrency=1, backlog=0, clock=lambda: now[0])
    req = b'{"id":"1","cap":"x.y"}'
    d.submit(req, ())
    with caplog.at_level("WARNING", logger=dispatch_mod.log.name):
        for _ in range(50):
            d.submit(req, ())
        now[0] += dispatch_mod.WARN_EVERY + 1
        d.submit(req, ())
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
