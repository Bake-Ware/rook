"""Negotiated band protocol features (core spec 14): addressed routing, large
replies over Drop, compression, native DROP frames for OTA, caps_hash
announces, the native-WS worker option. Mixed old/new peers throughout.

Real Worker / BandClient / transports over a loopback relay that can emulate
both a routing hub (telesthitium with SPEC §10.1) and an old broadcast hub,
with optional fragment loss. Old peers are emulated faithfully: an "old
worker" is a Worker whose transport strips every extension the round-2 code
adds, an "old receiver" decrypts every frame with empty AAD like the deployed
build and the ESP32 firmware.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import zlib

import pytest
import pytest_asyncio

from telesthete.protocol.crypto import BandCrypto
from telesthete.protocol.fragment import Fragmenter, Reassembler
from telesthete.protocol.framing import ChannelType, pack_packet, unpack_packet

from rook.band_mcp.client import BandClient
from rook.core.plugin import Plugin, capability
from rook.worker import ota_drop
from rook.worker.core import Worker
from rook.worker.transports.telesthete_hub import TelestheteHubTransport
from rook.worker.wire import codec as codec_mod
from rook.worker.wire import envelope
from rook.worker.wire.codec import KEEPALIVE_PAYLOAD, BandCodec, route_id_for

PSK = "band-protocol-test-psk"


# -- loopback relay ---------------------------------------------------------------

class Relay(asyncio.DatagramProtocol):
    """A minimal telesthitium. ``routing=True`` implements SPEC §10.1 (ROUTE
    consumed; an ADDRESSED (0x06) frame whose nonzero id another peer declared
    goes only to the declarers; every other type, CHANNEL included, is
    broadcast whatever its id); ``routing=False`` is the old hub (relays
    everything, ROUTE included, to every other peer). ``loss`` drops that fraction of
    multi-fragment CHANNEL frames and DROP CHUNKs (seeded, reproducible)."""

    def __init__(self, routing: bool = True, loss: float = 0.0, seed: int = 7):
        self.routing = routing
        self.loss = loss
        self.rng = random.Random(seed)
        self.peers: dict[bytes, dict] = {}   # band -> {addr: route or None}
        self.log: list[tuple] = []           # (src, ctype, cid, [dests])
        self.lost = 0

    def connection_made(self, transport):
        self.transport = transport

    @property
    def port(self) -> int:
        return self.transport.get_extra_info("socket").getsockname()[1]

    def _lossy(self, data: bytes) -> bool:
        if not self.loss or len(data) < 1000:
            return False   # keep small control frames: the test is about bulk
        if self.rng.random() < self.loss:
            self.lost += 1
            return True
        return False

    def datagram_received(self, data, addr):
        band, ctype, cid = data[:16], data[16], int.from_bytes(data[17:19], "big")
        peers = self.peers.setdefault(band, {})
        peers.setdefault(addr, None)
        if self.routing and ctype == 0x05:
            peers[addr] = cid or None
            self.log.append((addr, ctype, cid, []))
            return
        dests = [p for p in peers if p != addr]
        if self.routing and ctype == 0x06 and cid:
            declared = [p for p in dests if peers[p] == cid]
            if declared:
                dests = declared
        self.log.append((addr, ctype, cid, dests))
        if self._lossy(data):
            return
        for peer in dests:
            self.transport.sendto(data, peer)


async def _relay(**kw) -> Relay:
    loop = asyncio.get_running_loop()
    _t, relay = await loop.create_datagram_endpoint(lambda: Relay(**kw),
                                                    local_addr=("127.0.0.1", 0))
    return relay


async def _until(cond, timeout=10.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


class Big(Plugin):
    NAMESPACE = "big"

    @capability("blob", risk="read")
    def blob(self, n: int = 0, compressible: bool = False):
        """Return n bytes of payload (as a str)."""
        if compressible:
            return ("abcdefgh" * (n // 8 + 1))[:n]
        return os.urandom(n // 2).hex()[:n]

    @capability("echo", risk="read")
    def echo(self, value=None):
        """Echo."""
        return value


class OldTransport(TelestheteHubTransport):
    """The round-1 transport as deployed peers run it: no route declaration,
    no native send, every frame channel 0."""

    route_id = None
    send_native = None

    def set_route(self, name):
        return None

    async def send(self, payload, peer_id=None, *, route=0):
        await super().send(payload, peer_id, route=0)


def _old_transport(port: int) -> TelestheteHubTransport:
    t = OldTransport(psk=PSK, hub_host="127.0.0.1", hub_port=port)
    return t


def _worker(transport, monkeypatch, tmp_path, name="w", old=False) -> Worker:
    from rook.worker import core
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / f"{name}-id")
    w = Worker(transport, enabled=[], name=name)
    big = Big()
    w.registry.register("big.blob", big.blob)
    w.registry.register("big.echo", big.echo)
    if old:
        # A build that predates every extension: no features, plain replies.
        w.features = lambda: []
        w._reply_drops = None
        w._encode_reply = lambda mid, body, opts: json.dumps(body).encode()
    return w


class Band:
    """One relay + any number of workers and clients, torn down together."""

    def __init__(self, relay: Relay):
        self.relay = relay
        self.tasks: list[asyncio.Task] = []
        self.workers: list[Worker] = []
        self.clients: list[BandClient] = []

    async def worker(self, monkeypatch, tmp_path, name="w", old=False) -> Worker:
        t = (_old_transport(self.relay.port) if old else
             TelestheteHubTransport(psk=PSK, hub_host="127.0.0.1", hub_port=self.relay.port))
        w = _worker(t, monkeypatch, tmp_path, name, old=old)
        self.workers.append(w)
        self.tasks.append(asyncio.create_task(w.run()))
        return w

    async def client(self, old=False) -> BandClient:
        c = BandClient(psk=PSK, hub_host="127.0.0.1", hub_port=self.relay.port)
        if old:
            c.transport.set_route(None)
            c._encode_request = lambda msg, target, feats: (json.dumps(msg).encode(), 0)
        await c.start()
        self.clients.append(c)
        return c

    async def ready(self, client: BandClient, *workers: Worker) -> None:
        async def poke():
            while True:   # workers announce every 30 s; don't wait for it
                await asyncio.sleep(0.2)
                for w in workers:
                    if w.worker_id not in client.workers:
                        try:
                            await w.announce()
                        except Exception:
                            pass
        task = asyncio.create_task(poke())
        try:
            await _until(lambda: all(w.worker_id in client.workers for w in workers))
        finally:
            task.cancel()

    async def close(self) -> None:
        for c in self.clients:
            await c.stop()
        for w in self.workers:
            await w.shutdown()
        for t in self.tasks:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self.relay.transport.close()


@pytest_asyncio.fixture
async def band_factory():
    bands: list[Band] = []

    async def make(**kw) -> Band:
        b = Band(await _relay(**kw))
        bands.append(b)
        return b

    yield make
    for b in bands:
        await b.close()


# -- route ids and ROUTE frames ------------------------------------------------------

# telesthete tests/vectors.json "route" ids (SPEC §10.1), copied verbatim.
ROUTE_VECTORS = [("", 62722), ("rook", 6393), ("kaiju", 38185), ("bakephone", 18922),
                 ("worker-\u00fc", 36495), ("zero-prefix-28041", 65152)]


def test_route_id_matches_library_vectors():
    cases = [{"name": n, "route_id": r} for n, r in ROUTE_VECTORS]
    for c in cases:
        assert route_id_for(c["name"]) == c["route_id"]
    # The fallback (for bundles vendoring an older telesthete) is identical.
    orig = codec_mod._lib_route_id_for
    try:
        codec_mod._lib_route_id_for = None
        for c in cases:
            assert route_id_for(c["name"]) == c["route_id"]
    finally:
        codec_mod._lib_route_id_for = orig


def test_route_frame_is_the_library_declaration():
    from telesthete.protocol.route import pack_route_declaration
    from telesthete.protocol.sequence import SequenceSource
    c = BandCodec(PSK)
    c.seq = SequenceSource(start=41)
    frame = c.route_frame(route_id_for("kaiju"))
    assert frame == pack_route_declaration(BandCrypto(PSK), 41, route_id_for("kaiju"))
    assert len(frame) == 43 and frame[16] == 0x05


def test_addressed_frames_are_the_library_addressed_frames_with_empty_aad():
    """Addressed sends are ADDRESSED (0x06) frames to the destination's route
    id, sealed with empty AAD: byte-identical to the library's
    pack_addressed(..., associated_data=b"") for one §6.6 chunk."""
    from telesthete.protocol.route import pack_addressed
    from telesthete.protocol.sequence import SequenceSource
    c = BandCodec(PSK)
    c.seq = SequenceSource(start=900)
    chunk = c.split(b'{"id":"1","ok":true}')[0]
    rid = route_id_for("kaiju")
    frame = c.seal(chunk, rid)
    assert frame == pack_addressed(BandCrypto(PSK), 900, rid, chunk, associated_data=b"")
    assert frame[16] == 0x06 and int.from_bytes(frame[17:19], "big") == rid
    # Unaddressed traffic stays CHANNEL on channel_id 0.
    plain = c.seal(chunk)
    assert plain[16] == 0x02 and plain[17:19] == b"\0\0"
    with pytest.raises(ValueError):
        c.seal(chunk, 0x10000)


def test_old_receivers_open_addressed_frames_an_old_hub_relays():
    """An old hub broadcasts ADDRESSED frames. Deployed rook receivers (empty
    AAD, channel_type ignored) and this codec both open them; the spec-AAD
    seal would have failed the old receiver's tag check."""
    tx = BandCodec(PSK)
    frames = tx.frames(os.urandom(3000), route=4321)
    assert len(frames) > 1 and {f[16] for f in frames} == {0x06}
    old = BandCrypto(PSK)
    asm = Reassembler()
    out = None
    for f in frames:
        pkt = unpack_packet(f)
        out = asm.feed(old.decrypt(pkt.sequence, pkt.ciphertext)) or out
    assert out is not None and len(out) == 3000
    rx = BandCodec(PSK)
    got = [rx.ingest(f) for f in frames][-1]
    assert got.payload == out and got.channel_type == 0x06 and got.channel_id == 4321
    assert rx.drops["decrypt"] == 0


def test_old_receiver_cannot_open_a_route_frame_and_new_receiver_drops_it():
    """An old hub relays declarations to everyone. Deployed receivers (and
    the ESP32 firmware) decrypt with empty AAD: the tag fails. Our codec
    drops ROUTE by type before decrypting."""
    frame = BandCodec(PSK).route_frame(1234)
    pkt = unpack_packet(frame)
    with pytest.raises(Exception):
        BandCrypto(PSK).decrypt(pkt.sequence, pkt.ciphertext)   # old receiver
    rx = BandCodec(PSK)
    assert rx.ingest(frame) is None and rx.drops["route"] == 1
    assert rx.drops["decrypt"] == 0


@pytest.mark.asyncio
async def test_route_frames_never_reach_dispatch():
    t = TelestheteHubTransport(psk=PSK, hub_host="127.0.0.1", hub_port=9)
    got = []

    async def on_message(p, peer):
        got.append((p, peer))

    from rook.worker.transports.dispatch import Dispatcher
    t.dispatcher = Dispatcher(on_message)
    t._on_frame(BandCodec(PSK).route_frame(77))
    for f in BandCodec(PSK).frames(b'{"a":1}', route=77):
        t._on_frame(f)
    await _until(lambda: got)
    await asyncio.sleep(0.02)
    assert got == [(b'{"a":1}', (77,))]   # peer id is (channel_id,), unused
    await t.dispatcher.stop()


class _Link:
    KIND = "fake"
    connected = True

    def __init__(self):
        self.frames = []

    async def write(self, frame):
        self.frames.append(frame)

    async def close(self):
        pass


def _transport(keepalive, route="w1"):
    from rook.worker.transports.writer import FrameWriter
    t = TelestheteHubTransport(psk=PSK, hub_host="127.0.0.1", keepalive_secs=keepalive)
    link = _Link()
    t.link = link
    t.writer = FrameWriter(t.codec.split, t.codec.seal, link.write)
    t.writer.start()
    if route:
        t.set_route(route)
    return t, link


@pytest.mark.asyncio
async def test_route_redeclared_every_keepalive_even_while_busy():
    """Busy traffic skips the keepalive but must not skip the declaration: a
    hub that restarted lost the route, and only a declaration restores it."""
    t, link = _transport(keepalive=0.1)
    ka = asyncio.create_task(t._keepalive_loop())
    try:
        for _ in range(16):                 # busy for ~0.4 s
            await t.send(b"traffic")
            await asyncio.sleep(0.025)
        kinds = [f[16] for f in link.frames]
        assert kinds.count(0x05) >= 3
        payloads = [BandCodec(PSK).ingest(f) for f in link.frames if f[16] == 0x02]
        assert all(p is not None for p in payloads)   # no keepalive while busy
        # Sequences stay unique and monotonic across ROUTE and CHANNEL frames.
        seqs = [unpack_packet(f).sequence for f in link.frames]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    finally:
        ka.cancel()
        await t.stop()


@pytest.mark.asyncio
async def test_declaration_stands_in_for_the_keepalive_when_idle():
    t, link = _transport(keepalive=0.1)
    ka = asyncio.create_task(t._keepalive_loop())
    try:
        await asyncio.sleep(0.35)
        kinds = [f[16] for f in link.frames]
        assert kinds.count(0x05) >= 2
        assert kinds.count(0x02) == 0       # the declaration kept us registered
    finally:
        ka.cancel()
        await t.stop()


@pytest.mark.asyncio
async def test_no_route_means_round_one_keepalives():
    t, link = _transport(keepalive=0.1, route=None)
    ka = asyncio.create_task(t._keepalive_loop())
    try:
        await asyncio.sleep(0.25)
        assert {f[16] for f in link.frames} == {0x02}
        assert BandCodec(PSK).reassembler  # noqa
        rx = BandCodec(PSK)
        assert all(rx.ingest(f) is None for f in link.frames)   # all keepalives
    finally:
        ka.cancel()
        await t.stop()


@pytest.mark.asyncio
async def test_ws_reconnect_redeclares_route():
    t, link = _transport(keepalive=60)
    t._use_ws = True
    await asyncio.sleep(0.01)
    assert [f[16] for f in link.frames] == [0x05]   # set_route on a live link
    link.frames.clear()
    await t._ws_up()
    assert [f[16] for f in link.frames] == [0x02, 0x05]   # keepalive, declaration
    await t.stop()


def test_routing_env_off(monkeypatch, tmp_path):
    monkeypatch.setenv("ROOK_BAND_ROUTE", "0")
    t = TelestheteHubTransport(psk=PSK, hub_host="127.0.0.1", hub_port=9)
    w = _worker(t, monkeypatch, tmp_path)
    assert t.route_id is None and envelope.F_ROUTE not in w.features()
    c = BandClient(psk=PSK)
    assert c.transport.route_id is None


# -- envelope ------------------------------------------------------------------

def test_compress_only_when_it_pays():
    small = b'{"x": 1}'
    assert envelope.compress(small) == small
    noise = json.dumps({"r": os.urandom(4000).hex()}).encode()
    z = envelope.compress(noise)
    assert z.startswith(envelope.MAGIC_Z) and len(z) < len(noise) * 0.9  # hex is 50% redundant
    rnd = os.urandom(4000)
    assert envelope.compress(rnd) == rnd                                  # incompressible
    assert envelope.decompress(z) == noise
    bomb = envelope.MAGIC_Z + zlib.compress(b"\0" * (2 * 1024 * 1024))
    assert envelope.decompress(bomb, limit=1024 * 1024) is None
    assert envelope.decompress(envelope.MAGIC_Z + b"junk") is None


def test_base64_attachments_shrink_back_toward_binary_size():
    """Item 6: whole-envelope compression is what carries screenshots/camera
    frames/file reads compactly — base64 is 4/3 the bytes, deflate recovers
    most of that without a per-field encoding."""
    import base64
    raw = bytes(random.Random(1).getrandbits(8) for _ in range(30000))
    reply = json.dumps({"ok": True, "result": {"png_b64": base64.b64encode(raw).decode()}})
    z = envelope.compress(reply.encode())
    assert len(z) < len(raw) * 1.05 < len(reply)


def test_reply_opts_parse_and_ignore_junk():
    assert envelope.reply_opts({}) == envelope.NO_OPTS
    o = envelope.reply_opts({"rr": 513, "accept": ["z", "drop", 7, "drop-native"]})
    assert o == envelope.ReplyOpts(513, True, True, True)
    for junk in ({"rr": 0}, {"rr": 70000}, {"rr": True}, {"rr": "5"},
                 {"accept": "z"}, {"accept": {"z": 1}}):
        assert envelope.reply_opts(junk) == envelope.NO_OPTS


def test_codec_inflates_compressed_payloads_but_not_drop_packets():
    tx, rx = BandCodec(PSK), BandCodec(PSK)
    body = json.dumps({"id": "a", "ok": True, "result": "x" * 5000}).encode()
    z = envelope.compress(body)
    assert z != body
    out = [rx.ingest(f) for f in tx.frames(z)]
    assert out[-1].payload == body
    # A pass-through Drop packet starts with the band id; never inflated.
    drop = pack_packet(tx.band_id, ChannelType.DROP, 5, 1, b"\x01{}" + b"\0" * 16)
    assert [rx.ingest(f) for f in tx.frames(drop)][-1].payload == drop
    bad = [rx.ingest(f) for f in tx.frames(envelope.MAGIC_Z + b"garbage")]
    assert bad[-1] is None and rx.drops["inflate"] == 1


def test_native_drop_frames_round_trip_and_old_receivers_drop_them():
    tx, rx = BandCodec(PSK), BandCodec(PSK)
    frame = tx.seal_spec(b"\x03" + (0).to_bytes(4, "big") + b"x" * 1024, ChannelType.DROP, 9)
    assert len(frame) == 27 + 1029 + 16          # ONE frame per Drop chunk
    got = rx.ingest(frame)
    assert got.channel_type == ChannelType.DROP and got.channel_id == 9
    # Re-wrapped as the pass-through packet the Drop bridge feeds on.
    pkt = unpack_packet(got.payload)
    assert pkt.ciphertext == b"\x03" + (0).to_bytes(4, "big") + b"x" * 1024 + b"\0" * 16
    assert ota_drop.is_drop_packet(got.payload, rx.band_id)
    # Deployed receivers open every frame with empty AAD: authentication fails.
    p = unpack_packet(frame)
    with pytest.raises(Exception):
        BandCrypto(PSK).decrypt(p.sequence, p.ciphertext)


def test_caps_hash_is_order_insensitive():
    assert envelope.caps_hash(["b", "a"]) == envelope.caps_hash(["a", "b", "a"])
    assert envelope.caps_hash(["a"]) != envelope.caps_hash(["a", "b"])


# -- addressed routing end to end ---------------------------------------------------

@pytest.mark.asyncio
async def test_targeted_call_and_reply_are_addressed_on_a_routing_hub(
        band_factory, monkeypatch, tmp_path):
    band = await band_factory(routing=True)
    w1 = await band.worker(monkeypatch, tmp_path, "w1")
    w2 = await band.worker(monkeypatch, tmp_path, "w2")
    c = await band.client()
    await band.ready(c, w1, w2)
    assert set(c.workers[w1.worker_id]["features"]) >= {"route", "drop", "z", "drop-native"}
    band.relay.log.clear()
    seen_by_w2 = []
    w2.register_binary_handler(lambda p, peer: seen_by_w2.append(p) and False)
    r = await c.call("big.echo", {"value": "hi"}, target=w1.worker_id)
    assert r["ok"] and r["result"] == "hi"
    w1_rid, c_rid = route_id_for(w1.worker_id), c.transport.route_id
    data = [e for e in band.relay.log if e[1] == 0x06]
    assert {e[2] for e in data} == {w1_rid, c_rid}
    assert all(e[2] == 0 for e in band.relay.log if e[1] == 0x02)
    for _src, _t, cid, dests in data:
        assert len(dests) == 1                         # nobody else got it
    assert not any(b"big.echo" in p for p in seen_by_w2)


@pytest.mark.asyncio
async def test_old_hub_still_works_with_routing_peers(band_factory, monkeypatch, tmp_path):
    """Routing peer <-> old hub: declarations and addressed frames are
    broadcast; receivers drop the declarations, addressees still get theirs."""
    band = await band_factory(routing=False)
    w = await band.worker(monkeypatch, tmp_path)
    c = await band.client()
    await band.ready(c, w)
    r = await c.call("big.echo", {"value": 5}, target=w.worker_id)
    assert r == {"id": r["id"], "from": w.worker_id, "ok": True, "result": 5}
    assert any(e[1] == 0x05 and e[3] for e in band.relay.log)   # relayed to peers
    assert c.transport.codec.drops["route"] > 0
    assert c.transport.codec.drops["decrypt"] == 0


@pytest.mark.asyncio
async def test_new_client_old_worker(band_factory, monkeypatch, tmp_path):
    band = await band_factory(routing=True)
    w = await band.worker(monkeypatch, tmp_path, old=True)
    c = await band.client()
    await band.ready(c, w)
    assert c.workers[w.worker_id]["features"] == []
    band.relay.log.clear()
    r = await c.call("big.blob", {"n": 100_000}, target=w.worker_id, timeout=20)
    assert r["ok"] and len(r["result"]) == 100_000          # inline, as always
    # Unaddressed request (the worker never declared); its reply on channel 0.
    assert all(e[2] == 0 for e in band.relay.log if e[1] == 0x02)
    assert not any(e[1] == 0x06 for e in band.relay.log)


@pytest.mark.asyncio
async def test_old_client_new_worker(band_factory, monkeypatch, tmp_path):
    band = await band_factory(routing=True)
    w = await band.worker(monkeypatch, tmp_path)
    c = await band.client(old=True)
    await band.ready(c, w)
    band.relay.log.clear()
    r = await c.call("big.blob", {"n": 60_000, "compressible": True},
                     target=w.worker_id, timeout=20)
    # No rr / accept: a plain inline, uncompressed broadcast reply.
    assert r["ok"] and len(r["result"]) == 60_000 and "drop" not in r
    assert all(e[2] == 0 for e in band.relay.log if e[1] == 0x02)
    assert not any(e[1] in (0x04, 0x06) for e in band.relay.log)


@pytest.mark.asyncio
async def test_hub_node_answers_band_calls_addressed(band_factory, monkeypatch, tmp_path):
    """A worker calling `rook` (Worker.request) gets the hub node's reply on
    its own route."""
    class Node:
        worker_id = "hubnode1"
        name = "rook"

        def entry(self):
            return {"worker_id": self.worker_id, "name": "rook", "caps": ["x.y"]}

        def announce_msg(self):
            return {"kind": "announce", "worker_id": self.worker_id, "name": "rook",
                    "caps": ["x.y"]}

        def wants(self, msg):
            return msg.get("target") == self.worker_id

        async def dispatch(self, cap, args, identity, source="local"):
            return {"ok": True, "result": "pong"}

    band = await band_factory(routing=True)
    w = await band.worker(monkeypatch, tmp_path)
    c = await band.client()
    c.attach_local(Node())
    assert c.transport.route_id == route_id_for("hubnode1")
    await band.ready(c, w)
    await asyncio.sleep(0.1)
    band.relay.log.clear()
    r = await w.request("x.y", target="hubnode1", timeout=5)
    assert r["result"] == "pong"
    replies = [e for e in band.relay.log if e[1] == 0x06 and e[2] == w.transport.route_id]
    assert replies and all(len(e[3]) == 1 for e in replies)


# -- large replies over Drop ----------------------------------------------------------

@pytest.mark.asyncio
async def test_large_reply_rides_drop_and_is_transparent(band_factory, monkeypatch, tmp_path):
    band = await band_factory(routing=True)
    w = await band.worker(monkeypatch, tmp_path)
    c = await band.client()
    await band.ready(c, w)
    r = await c.call("big.blob", {"n": 200_000}, target=w.worker_id, timeout=30)
    assert r["ok"] and len(r["result"]) == 200_000 and "drop" not in r
    assert set(r) == {"id", "from", "ok", "result"}
    assert any(e[1] == 0x06 and e[2] and len(e[3]) == 1 for e in band.relay.log)
    await _until(lambda: w._reply_drops.active == 0)      # released on the DONE


@pytest.mark.asyncio
async def test_pass_through_drop_packets_arrive_in_sequence_order(
        band_factory, monkeypatch, tmp_path):
    """Pass-through Drop packets are whole band messages; queued together,
    the writer let a window's short (1-fragment) final CHUNK overtake the
    2-fragment CHUNKs before it, and Drop's sequence watermark then rejected
    those as stale (one stalled window per re-request). They now go out one
    at a time, in order: no CHUNK is ever rejected as stale."""
    stale = []
    orig = ota_drop._Receiver._decrypt

    def _decrypt(self, peer, pkt):
        p = unpack_packet(pkt)
        wm = self._recv_watermark.get(peer)
        if wm is not None and p.sequence <= wm:
            stale.append(p.sequence)
        return orig(self, peer, pkt)

    monkeypatch.setattr(ota_drop._Receiver, "_decrypt", _decrypt)
    band = await band_factory(routing=True)
    w = await band.worker(monkeypatch, tmp_path)
    c = await band.client()
    await band.ready(c, w)
    r = await c.call("big.blob", {"n": 300_000}, target=w.worker_id, timeout=30)
    assert r["ok"] and len(r["result"]) == 300_000
    # It rode pass-through Drop (the caller has a route), addressed.
    assert any(e[1] == 0x06 and e[2] == c.transport.route_id and len(e[3]) == 1
               for e in band.relay.log)
    assert stale == []


@pytest.mark.asyncio
async def test_small_replies_stay_inline(band_factory, monkeypatch, tmp_path):
    band = await band_factory(routing=True)
    w = await band.worker(monkeypatch, tmp_path)
    c = await band.client()
    await band.ready(c, w)
    seen = []
    orig = c._handle_reply
    c._handle_reply = lambda m: (seen.append(m), orig(m))
    r = await c.call("big.blob", {"n": 20_000}, target=w.worker_id)
    assert r["ok"] and "drop" not in seen[-1]


@pytest.mark.asyncio
async def test_1mb_reply_completes_under_1pct_fragment_loss(band_factory, monkeypatch, tmp_path):
    """The measurement: 1% loss on bulk frames. Inline, a 1 MB reply is ~1,050
    fragments with no retransmit, so it practically never completes
    (0.99^1050 ~ 0.003%); over Drop the receiver re-requests what was lost."""
    band = await band_factory(routing=True, loss=0.01)
    w = await band.worker(monkeypatch, tmp_path)
    c = await band.client()
    await band.ready(c, w)
    r = await c.call("big.blob", {"n": 1_000_000}, target=w.worker_id, timeout=60)
    assert r["ok"] and len(r["result"]) == 1_000_000
    assert band.relay.lost > 0

    # Same band, same loss, the inline path (an old client): it times out.
    old = await band.client(old=True)
    await band.ready(old, w)
    band.relay.rng.seed(7)
    with pytest.raises(asyncio.TimeoutError):
        await old.call("big.blob", {"n": 1_000_000}, target=w.worker_id, timeout=6)


@pytest.mark.asyncio
async def test_reply_size_cap(band_factory, monkeypatch, tmp_path):
    monkeypatch.setenv("ROOK_BAND_MAX_REPLY_BYTES", "50000")
    band = await band_factory(routing=True)
    w = await band.worker(monkeypatch, tmp_path)
    c = await band.client()
    await band.ready(c, w)
    r = await c.call("big.blob", {"n": 60_000}, target=w.worker_id)
    assert r["ok"] is False and r["error"].startswith("reply too large: ")
    assert "ROOK_BAND_MAX_REPLY_BYTES" in r["error"]


@pytest.mark.asyncio
async def test_drop_threshold_env(band_factory, monkeypatch, tmp_path):
    monkeypatch.setenv("ROOK_BAND_DROP_THRESHOLD", "0")       # never
    band = await band_factory(routing=True)
    w = await band.worker(monkeypatch, tmp_path)
    c = await band.client()
    await band.ready(c, w)
    served = []
    orig = w._reply_drops.serve
    w._reply_drops.serve = lambda *a, **k: served.append(a) or orig(*a, **k)
    r = await c.call("big.blob", {"n": 100_000}, target=w.worker_id, timeout=20)
    assert r["ok"] and len(r["result"]) == 100_000 and served == []


@pytest.mark.asyncio
async def test_bad_drop_descriptor_is_an_error():
    c = BandClient(psk=PSK)
    r = await c._pull_reply({"id": "x", "from": "w", "ok": True,
                             "drop": {"id": 1, "size": 10**12, "sha256": "0" * 64}}, 1.0)
    assert r == {"id": "x", "from": "w", "ok": False, "error": "bad drop descriptor in reply"}


@pytest.mark.asyncio
async def test_caller_timeout_bounds_a_large_reply_pull(band_factory, monkeypatch, tmp_path):
    """The call's timeout covers the Drop pull too (it used to be stretched
    to 15 s + size/256 KB). A pull that can't finish in what is left fails
    with a clear timeout error, promptly, and leaves no state behind."""
    band = await band_factory(routing=True)
    w = await band.worker(monkeypatch, tmp_path)
    c = await band.client()
    await band.ready(c, w)
    # Lose every Drop CHUNK the worker sends: the pull can never complete.
    w._reply_drops._send = lambda *a, **k: asyncio.sleep(0)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    r = await c.call("big.blob", {"n": 200_000}, target=w.worker_id, timeout=1.5)
    took = loop.time() - t0
    assert took < 3.0, took
    assert r["ok"] is False and r["error"].startswith("timeout: large reply")
    assert c._drop_rx == {} and c._drop_locks == {}


@pytest.mark.asyncio
async def test_pull_with_no_time_left_fails_at_once_and_prunes_its_lock():
    c = BandClient(psk=PSK)
    desc = {"id": 0x8123, "size": 5000, "sha256": "0" * 64}
    r = await c._pull_reply({"id": "x", "from": "w", "ok": True, "drop": desc}, 0.0)
    assert r["ok"] is False and r["error"].startswith("timeout:")
    assert c._drop_locks == {} and c._drop_rx == {}
    # A pull waiting behind another of the same drop_id times out cleanly too.
    lock = asyncio.Lock()
    await lock.acquire()
    c._drop_locks[0x8123] = [lock, 1]
    r = await c._pull_reply({"id": "y", "from": "w", "ok": True, "drop": desc}, 0.05)
    assert r["error"].startswith("timeout:") and c._drop_locks[0x8123][1] == 1


@pytest.mark.asyncio
async def test_reply_server_expires_and_bounds_memory():
    now = [0.0]
    sent = []

    async def send(p, route=0):
        sent.append(p)

    srv = ota_drop.DropReplyServer(b"\x01" * 16, send, ttl=5, max_bytes=3000,
                                   clock=lambda: now[0])
    a = srv.serve("a", b"x" * 2000)
    b = srv.serve("b", b"y" * 2000)            # evicts a (budget)
    assert a and b and srv.active == 1
    assert srv.serve("c", b"z" * 4000) is None  # too big to hold: inline
    now[0] = 10
    srv.serve("d", b"w")                        # prunes b (expired)
    assert srv.active == 1
    srv.close()


@pytest.mark.asyncio
async def test_reply_server_expires_without_further_serves():
    """An idle worker releases unpulled replies at their TTL; it used to hold
    them until the next reply was served."""
    async def send(p, route=0):
        pass

    srv = ota_drop.DropReplyServer(b"\x01" * 16, send, ttl=0.1)
    srv.serve("a", b"x" * 5000)
    srv.serve("b", b"y" * 5000)
    assert srv.active == 2
    await _until(lambda: srv.active == 0, timeout=2)
    assert srv._timer is None
    srv.serve("c", b"z")
    srv.close()
    assert srv.active == 0 and srv._timer is None
    assert ota_drop.REPLY_DROP_MAX_BYTES == 32 * 1024 * 1024


# -- compression ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compression_negotiated_both_ways(band_factory, monkeypatch, tmp_path):
    band = await band_factory(routing=True)
    w = await band.worker(monkeypatch, tmp_path)
    c = await band.client()
    await band.ready(c, w)
    band.relay.log.clear()
    seen = []
    w.register_binary_handler(lambda p, peer: seen.append(p) and False)
    big_arg = "q" * 20_000
    r = await c.call("big.echo", {"value": big_arg}, target=w.worker_id)
    assert r["ok"] and r["result"] == big_arg
    # The worker saw plain JSON (its codec inflated the request) ...
    assert any(big_arg.encode() in p for p in seen)
    # ... while the wire carried far fewer frames than 2 x 20 KB uncompressed.
    data = [e for e in band.relay.log if e[1] == 0x06]
    assert len(data) <= 4


@pytest.mark.asyncio
async def test_no_compression_toward_a_target_without_z(band_factory, monkeypatch, tmp_path):
    band = await band_factory(routing=True)
    w = await band.worker(monkeypatch, tmp_path, old=True)
    c = await band.client()
    await band.ready(c, w)
    payload, channel = c._encode_request({"id": "1", "cap": "big.echo",
                                          "args": {"value": "q" * 5000}},
                                         w.worker_id, frozenset())
    assert not envelope.is_compressed(payload) and channel == 0
    # And a tiny request to a feature-less node stays one fragment.
    small = {"id": "a" * 32, "cap": "serial.read", "args": {"x": "y" * 850},
             "target": w.worker_id}
    payload, _ = c._encode_request(small, w.worker_id, frozenset())
    assert len(Fragmenter().split(payload)) == 1


# -- caps_hash announces --------------------------------------------------------------

@pytest.mark.asyncio
async def test_hash_only_announces_keep_known_caps(monkeypatch, tmp_path):
    monkeypatch.setenv("ROOK_ANNOUNCE_CAPS_HASH", "1")
    sent = []

    class T:
        band_id = b"\x02" * 16

        async def send(self, p, peer_id=None, **kw):
            sent.append(json.loads(p))

    w = _worker(T(), monkeypatch, tmp_path)
    for _ in range(12):
        await w.announce()
    with_caps = [i for i, m in enumerate(sent) if "caps" in m]
    assert with_caps == [0, 10]                  # first, then every 10th
    assert all(m["caps_hash"] == envelope.caps_hash(sent[0]["caps"]) for m in sent)
    w.registry.register("new.cap", lambda: 1)    # a change: full caps at once
    await w.announce()
    assert "new.cap" in sent[-1]["caps"]
    await w.announce(full=True)                  # reconnect: full
    assert "caps" in sent[-1]

    c = BandClient(psk=PSK)
    c._handle_announce(sent[0])
    assert c.workers[w.worker_id]["caps"] == sent[0]["caps"]
    c._handle_announce(sent[1])                  # hash-only, matches: kept
    assert c.workers[w.worker_id]["caps"] == sent[0]["caps"]


@pytest.mark.asyncio
async def test_unknown_caps_hash_fetches_caps_describe(monkeypatch):
    c = BandClient(psk=PSK)
    calls = []

    async def fake_call(cap, args=None, target=None, timeout=15.0, **kw):
        calls.append((cap, target))
        return {"ok": True, "result": {"a.b": {}, "c.d": {}}}

    c.call = fake_call
    msg = {"kind": "announce", "worker_id": "w9", "name": "w9",
           "caps_hash": envelope.caps_hash(["a.b", "c.d"])}
    c._handle_announce(msg)
    assert c.workers["w9"]["caps"] == []
    await _until(lambda: c.workers["w9"]["caps"] == ["a.b", "c.d"])
    assert calls == [("caps.describe", "w9")]
    c._handle_announce(msg)                      # now matches: no refetch
    await asyncio.sleep(0.02)
    assert len(calls) == 1


def test_default_announces_still_carry_full_caps(monkeypatch, tmp_path):
    monkeypatch.delenv("ROOK_ANNOUNCE_CAPS_HASH", raising=False)
    sent = []

    class T:
        band_id = None

        async def send(self, p, peer_id=None, **kw):
            sent.append(json.loads(p))

    w = _worker(T(), monkeypatch, tmp_path)
    for _ in range(3):
        asyncio.run(w.announce())
    assert all("caps" in m and "caps_hash" in m for m in sent)
    # A transport without routing / a band id advertises nothing it can't do.
    assert sent[0]["features"] == []


# -- native DROP for OTA -------------------------------------------------------------

def _signed_manifest(bundle: bytes, build: int):
    import base64
    import hashlib
    from nacl.signing import SigningKey
    from rook.worker._update_verify import canonical_payload
    sk = SigningKey.generate()
    manifest = {"schema": 1, "build": build, "version": f"{build}.test",
                "filename": "band-worker.pyz",
                "sha256": hashlib.sha256(bundle).hexdigest(), "size": len(bundle)}
    manifest["sig"] = base64.b64encode(sk.sign(canonical_payload(manifest)).signature).decode()
    return manifest, base64.b64encode(sk.verify_key.encode()).decode()


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.asyncio
async def test_inband_ota_native_drop_on_and_off(native, band_factory, monkeypatch, tmp_path):
    from rook.worker import core
    from rook.worker._build_info import BUILD
    from rook.worker._update_verify import verify_manifest as real_verify
    if not native:
        monkeypatch.setenv("ROOK_BAND_DROP_NATIVE", "0")
    bundle = os.urandom(1024 * 30 + 77)
    manifest, pub = _signed_manifest(bundle, BUILD + 1)
    monkeypatch.setattr("rook.worker._update_verify.verify_manifest",
                        lambda m, pk=None: real_verify(m, pub))
    monkeypatch.setattr("rook.worker.plugins.selfupdate._WORKER_DIR", tmp_path)
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "ota-id")

    band = await band_factory(routing=True)
    t = TelestheteHubTransport(psk=PSK, hub_host="127.0.0.1", hub_port=band.relay.port)
    w = Worker(t, enabled=["selfupdate"], name="ota")
    su = next(p for p in w.plugins if p.NAMESPACE == "worker")
    swapped = {}

    async def fake_swap(tmp, target, from_build):
        swapped["bytes"] = tmp.read_bytes()
        return {"ok": True}

    monkeypatch.setattr(su, "_swap_and_restart", fake_swap)
    band.workers.append(w)
    band.tasks.append(asyncio.create_task(w.run()))
    c = await band.client()
    await band.ready(c, w)
    band.relay.log.clear()
    res = await c.push_update(w.worker_id, bundle, manifest, drop_id=3,
                              begin_timeout=10, transfer_timeout=30)
    assert res["ok"] and res["verified"] and res["native"] is native
    await _until(lambda: "bytes" in swapped)
    assert swapped["bytes"] == bundle
    drop_frames = [e for e in band.relay.log if e[1] == 0x04]
    chan_frames = [e for e in band.relay.log if e[1] == 0x06]
    chunks = -(-len(bundle) // 1024)
    if native:
        # One outer frame per Drop packet: ~chunks + a few control frames.
        assert chunks <= len(drop_frames) <= chunks + 10
    else:
        assert not drop_frames
        # Pass-through: every CHUNK costs two ADDRESSED frames.
        assert len([e for e in chan_frames if e[2] == route_id_for(w.worker_id)]) >= 2 * chunks


@pytest.mark.asyncio
async def test_ota_drop_ids_in_the_reply_range_are_refused(monkeypatch, tmp_path):
    """Reply drops use 0x8000|...; an OTA push there would cross-feed with a
    large-reply transfer of the same id. Both ends refuse it."""
    assert ota_drop.valid_ota_drop_id(0) and ota_drop.valid_ota_drop_id(0x7FFF)
    for bad in (0x8000, 0xFFFF, -1, 0x10000, True, "1", None):
        assert not ota_drop.valid_ota_drop_id(bad)
    c = BandClient(psk=PSK)

    async def no_call(*a, **k):
        raise AssertionError("must not reach the worker")

    c.call = no_call
    r = await c.push_update("w", b"x", {}, drop_id=0x8001)
    assert r["ok"] is False and "drop_id must be 0-32767" in r["error"]

    from rook.worker import core
    from rook.worker.plugins.selfupdate import SelfUpdatePlugin
    monkeypatch.setattr(core, "_WORKER_ID_FILE", tmp_path / "ota-id")
    su = SelfUpdatePlugin()
    su._worker = object()
    for bad in (0x8000, 0xFFFF, "junk"):
        res = await su._ota_begin({"build": 1}, drop_id=bad)
        assert res["ok"] is False and "drop_id must be" in res["error"]
    assert su._ota_rx is None


@pytest.mark.asyncio
async def test_old_worker_gets_pass_through_ota(band_factory, monkeypatch, tmp_path):
    """No drop-native in the worker's announce: the controller keeps the old
    wrapping (and the old worker sees nothing it can't parse)."""
    band = await band_factory(routing=True)
    w = await band.worker(monkeypatch, tmp_path, old=True)
    c = await band.client()
    await band.ready(c, w)
    sent = []

    async def send(p, route=0):
        sent.append((p, route))

    c._send = send
    feats = envelope.features_of(c.workers[w.worker_id])
    assert envelope.F_DROP_NATIVE not in feats
    s = ota_drop.OtaDropSender(c.transport.band_id, 1, "x", b"abc", c._send,
                               route=0, native=None)
    s.offer()
    await asyncio.sleep(0.01)
    assert sent and sent[0][0][16] == 0x04 and sent[0][1] == 0


# -- dispatch under bursts ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_shed_targeted_request_gets_busy_reply(band_factory, monkeypatch, tmp_path):
    """A request the dispatcher sheds is answered "busy" (addressed to the
    caller's rr) when it targeted this worker; open calls stay silent."""
    band = await band_factory(routing=True)
    w = await band.worker(monkeypatch, tmp_path)
    c = await band.client()
    await band.ready(c, w)
    assert w.transport.dispatcher.on_shed == w._on_shed
    sent = []

    async def send(p, route=0):
        sent.append((json.loads(p), route))

    w._send = send
    w._on_shed(json.dumps({"id": "q1", "cap": "big.echo", "target": w.worker_id,
                           "rr": 4242}).encode(), ())
    w._on_shed(json.dumps({"id": "q2", "cap": "big.echo"}).encode(), ())          # open
    w._on_shed(json.dumps({"id": "q3", "cap": "big.echo", "target": "other"}).encode(), ())
    await _until(lambda: not w._busy_tasks)
    assert len(sent) == 1
    body, route = sent[0]
    assert route == 4242 and body["id"] == "q1" and body["ok"] is False
    assert body["error"].startswith("busy")


# -- native WS endpoint option ---------------------------------------------------------

def test_ws_url_overrides_the_bridge_url():
    t = TelestheteHubTransport(psk=PSK, hub_host="hub.example.com", hub_port=443,
                               ws_url="wss://relay.example.com:8443/band")
    assert t._use_ws and t._ws_url == "wss://relay.example.com:8443/band"
    t = TelestheteHubTransport(psk=PSK, hub_host="hub.example.com", hub_port=443, use_ws=True)
    assert t._ws_url == "wss://hub.example.com:443/band"


def test_installer_public_ws_url(monkeypatch):
    from rook.remote import bootstrap
    monkeypatch.setenv("ROOK_HUB_PUBLIC_WS_URL", "wss://relay.example.com/band")
    assert bootstrap.public_ws_url() == "wss://relay.example.com/band"
    monkeypatch.setenv("ROOK_HUB_PUBLIC_WS_URL", "wss://x.example.com/band; rm -rf ~")
    assert bootstrap.public_ws_url() == ""
    monkeypatch.delenv("ROOK_HUB_PUBLIC_WS_URL")
    assert bootstrap.public_ws_url() == ""


def test_ws_bridge_sockets_get_larger_buffers(monkeypatch):
    import socket
    from rook.band_mcp import ws_band
    got = []
    monkeypatch.setattr(ws_band, "set_socket_buffers", lambda s, n: got.append(n))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ws_band.set_socket_buffers(s, ws_band.BRIDGE_SOCKET_BUFFER)
    finally:
        s.close()
    assert got == [ws_band.BRIDGE_SOCKET_BUFFER] and got[0] >= 1024 * 1024


def test_bundle_builders_require_the_route_module(tmp_path):
    import importlib.util
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("bbw2", root / "rook/remote/build_band_worker.py")
    bbw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bbw)
    assert "route.py" in bbw.REQUIRED_TELESTHETE
    # The installed one is new enough. (Its revision is checked by builds;
    # the dev sibling checkout may legitimately be elsewhere.)
    bbw.check_telesthete(bbw.TELESTHETE_ROOT, verify_rev=False)
    (tmp_path / "protocol").mkdir()
    for m in bbw.REQUIRED_TELESTHETE[:-1]:
        (tmp_path / "protocol" / m).write_text("")
    with pytest.raises(FileNotFoundError, match="route.py"):
        bbw.check_telesthete(tmp_path)
    assert "check_telesthete" in (root / "android/stage_worker.py").read_text(encoding="utf-8")
    pin = (root / "pyproject.toml").read_text(encoding="utf-8")
    docker = (root / "Dockerfile").read_text(encoding="utf-8")
    rev = pin.split("telesthete@")[1].split('"')[0]
    assert len(rev) == 40 and f"TELESTHETE_REV={rev}" in docker
    assert bbw.pinned_telesthete_rev(root / "pyproject.toml") == rev


def _bbw():
    import importlib.util
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("bbw3", root / "rook/remote/build_band_worker.py")
    bbw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bbw)
    return bbw


def test_bundle_builders_require_the_pinned_telesthete_checkout(tmp_path, monkeypatch):
    import shutil
    import subprocess
    if shutil.which("git") is None:
        pytest.skip("no git")
    bbw = _bbw()
    checkout = tmp_path / "telesthete"
    pkg = checkout / "telesthete"
    (pkg / "protocol").mkdir(parents=True)
    for m in bbw.REQUIRED_TELESTHETE:
        (pkg / "protocol" / m).write_text("", encoding="utf-8")

    def git(*a):
        return subprocess.check_output(
            ["git", "-C", str(checkout), "-c", "user.email=t@example.com",
             "-c", "user.name=t", *a], text=True).strip()

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "one")
    first = git("rev-parse", "HEAD")
    (pkg / "x.py").write_text("", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "two")
    head = git("rev-parse", "HEAD")
    monkeypatch.delenv(bbw.ANY_REV_ENV, raising=False)
    bbw.check_telesthete(pkg, pin=head)               # at the pin
    bbw.check_telesthete(pkg, pin=head[:12])          # short pins work too
    with pytest.raises(RuntimeError, match=f"pins {first[:12]}"):
        bbw.check_telesthete(pkg, pin=first)
    bbw.check_telesthete(pkg, pin=first, verify_rev=False)
    monkeypatch.setenv(bbw.ANY_REV_ENV, "1")
    bbw.check_telesthete(pkg, pin=first)              # warns only
    # A pip install (not the top of a git checkout) is checked for modules only.
    monkeypatch.delenv(bbw.ANY_REV_ENV)
    plain = tmp_path / "site" / "telesthete"
    shutil.copytree(pkg, plain)
    bbw.check_telesthete(plain, pin=first)
