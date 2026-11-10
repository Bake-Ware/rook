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

    @capability("blob", risk="read", scope="multi-user")
    def blob(self, n: int = 0, compressible: bool = False):
        """Return n bytes of payload (as a str)."""
        if compressible:
            return ("abcdefgh" * (n // 8 + 1))[:n]
        return os.urandom(n // 2).hex()[:n]

    @capability("echo", risk="read", scope="multi-user")
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


# -- large replies over Drop ----------------------------------------------------------


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


# -- caps_hash announces --------------------------------------------------------------


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


# -- dispatch under bursts ---------------------------------------------------------------


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


# -- device workers (rook.core.devices) ----------------------------------------

