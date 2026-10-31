"""OTA-over-Drop bridge: in-band bundle transfer over a (lossy) relay.

Exercises rook.worker.ota_drop end to end without a real hub: a fake relay
shuttles DROP packets between an OtaDropSender and an OtaDropReceiver, with an
optional loss filter to prove the receiver-driven re-request recovers.
"""

import asyncio
import hashlib
import os

import pytest

from telesthete.protocol.crypto import BandCrypto
from rook.worker.ota_drop import OtaDropReceiver, OtaDropSender, is_drop_packet


class Relay:
    """Cross-wires a sender and receiver. Each side's outbound packet is
    delivered to the other side's feed(). `drop` may filter packets (return
    True to drop) to simulate loss."""

    def __init__(self, drop=None):
        self.sender = None
        self.receiver = None
        self._drop = drop or (lambda pkt, to: False)
        self.sent = 0
        self.dropped = 0

    def to_receiver(self):
        async def _send(pkt):
            self.sent += 1
            if self._drop(pkt, "receiver"):
                self.dropped += 1
                return
            if self.receiver is not None:
                self.receiver.feed(pkt)
        return _send

    def to_sender(self):
        async def _send(pkt):
            self.sent += 1
            if self._drop(pkt, "sender"):
                self.dropped += 1
                return
            if self.sender is not None:
                self.sender.feed(pkt)
        return _send


def _make_pair(band_id, drop_id, name, data, drop=None, have=None):
    relay = Relay(drop=drop)
    sender = OtaDropSender(band_id, drop_id, name, data, relay.to_receiver())
    receiver = OtaDropReceiver(band_id, drop_id, relay.to_sender(), have=have)
    relay.sender, relay.receiver = sender, receiver
    return relay, sender, receiver


@pytest.mark.asyncio
async def test_small_bundle_transfers_and_verifies():
    band_id = BandCrypto("ota-psk").band_id
    data = b"#!/bin/sh\necho hello from a pushed bundle\n" * 3
    got = {}
    relay, sender, receiver = _make_pair(band_id, 7, "band-worker.pyz", data)
    receiver.on_complete(lambda d, ok: got.update(data=d, ok=ok))
    receiver.start()
    sender.offer()
    assert await receiver.wait(timeout=5.0)
    assert got["ok"] is True and got["data"] == data
    assert receiver.verified is True
    receiver.stop()


@pytest.mark.asyncio
async def test_multi_chunk_bundle_over_relay():
    band_id = BandCrypto("ota-psk2").band_id
    data = os.urandom(1024 * 200 + 17)  # ~200 KB, many windows
    relay, sender, receiver = _make_pair(band_id, 11, "big.pyz", data)
    done = {}
    sender.on_complete(lambda ok: done.update(sender_ok=ok))
    receiver.on_complete(lambda d, ok: done.update(data=d, ok=ok))
    receiver.start()
    sender.offer()
    assert await receiver.wait(timeout=15.0)
    assert done["ok"] is True
    assert hashlib.sha256(done["data"]).hexdigest() == sender.sha256
    # The sender learns the receiver's verdict via DONE.
    assert await sender.wait(timeout=2.0) is True
    receiver.stop()


@pytest.mark.asyncio
async def test_a_finished_transfer_is_freed_without_the_cycle_collector():
    """Wrapper and Drop receiver/sender must not form a reference cycle: each
    pulled large reply's chunks (and each served blob) stayed in memory until
    a full gc, hundreds of MB on a hub under steady large replies (soak)."""
    import gc
    import weakref
    band_id = BandCrypto("ota-psk-gc").band_id
    data = os.urandom(1024 * 100)
    relay, sender, receiver = _make_pair(band_id, 9, "gc.pyz", data)
    receiver.start()
    sender.offer()
    assert await receiver.wait(timeout=15.0) and await sender.wait(timeout=2.0)
    receiver.stop()
    await asyncio.sleep(0)       # the tick task finishes its cancellation
    refs = [weakref.ref(o) for o in (sender, receiver, sender._sender, receiver._recv)]
    gc.disable()
    try:
        relay.sender = relay.receiver = None
        del sender, receiver
        assert [r() for r in refs] == [None] * 4
    finally:
        gc.enable()


@pytest.mark.asyncio
async def test_lossy_relay_recovers_via_rerequest():
    band_id = BandCrypto("ota-psk3").band_id
    data = os.urandom(1024 * 80)  # spans > 1 window

    # Drop ~30% of CHUNK packets heading to the receiver, on first sight only.
    seen = {"n": 0}

    def drop(pkt, to):
        if to != "receiver":
            return False
        seen["n"] += 1
        return seen["n"] % 3 == 0  # every third packet to the receiver is lost

    relay, sender, receiver = _make_pair(band_id, 3, "lossy.pyz", data, drop=drop)
    got = {}
    receiver.on_complete(lambda d, ok: got.update(data=d, ok=ok))
    receiver.start()
    sender.offer()
    # Despite loss, tick()-driven re-request must eventually complete it.
    assert await receiver.wait(timeout=20.0)
    assert got["ok"] is True and got["data"] == data
    assert relay.dropped > 0, "test should actually have dropped packets"
    receiver.stop()


@pytest.mark.asyncio
async def test_resume_from_persisted_chunks():
    band_id = BandCrypto("ota-psk4").band_id
    data = os.urandom(1024 * 10)
    CHUNK = 1024
    # Pretend a prior interrupted transfer persisted half the chunks.
    have = {i: data[i * CHUNK:(i + 1) * CHUNK] for i in (0, 1, 2, 5, 8)}
    relay, sender, receiver = _make_pair(band_id, 4, "resume.pyz", data, have=have)
    got = {}
    receiver.on_complete(lambda d, ok: got.update(data=d, ok=ok))
    receiver.start()
    sender.offer()
    assert await receiver.wait(timeout=5.0)
    assert got["ok"] is True and got["data"] == data
    receiver.stop()


def test_is_drop_packet_discriminates_json_from_drop():
    band_id = BandCrypto("disc-psk").band_id
    assert not is_drop_packet(b'{"cap": "worker.status"}', band_id)
    assert not is_drop_packet(b"", band_id)
    assert not is_drop_packet(b"\x00" * 30, band_id)  # wrong band_id
    # A real DROP packet is recognised.
    from telesthete.protocol.framing import pack_packet, ChannelType
    pkt = pack_packet(band_id, ChannelType.DROP, 9, 1, b"x" * 20)
    assert is_drop_packet(pkt, band_id)


def test_reply_receiver_handles_chunks_in_linear_time():
    """rook's _Receiver inherits the library's handle_packet, which used to
    rescan every chunk index per CHUNK (32 MiB pull: ~28 s of CPU on the
    band client's loop). Count membership checks on a 4,096-chunk pull."""
    import json
    from telesthete.protocol.drop import CHUNK_SIZE, DropFrameType
    from telesthete.protocol.framing import ChannelType, pack_packet
    from rook.worker.ota_drop import _BAND_PEER, _FAKE_TAG, _PassthroughCrypto, _Receiver

    class Counting(dict):
        n = 0

        def __contains__(self, k):
            Counting.n += 1
            return super().__contains__(k)

    class T:
        def send(self, d, p):
            pass

    band = b"\x05" * 16
    total = 4096
    rx = _Receiver(band, 0x8001, crypto=_PassthroughCrypto(band), transport=T(),
                   rerequest=0.25)
    offer = {"name": "r", "size": total * CHUNK_SIZE, "chunk_size": CHUNK_SIZE,
             "total_chunks": total, "sha256": "0" * 64}
    rx.handle_packet(_BAND_PEER, pack_packet(
        band, ChannelType.DROP, 0x8001, 0,
        bytes([DropFrameType.OFFER]) + json.dumps(offer).encode() + _FAKE_TAG))
    rx.have = Counting(rx.have)
    chunk = b"x" * CHUNK_SIZE
    for i in range(total):
        rx.handle_packet(_BAND_PEER, pack_packet(
            band, ChannelType.DROP, 0x8001, i + 1,
            bytes([DropFrameType.CHUNK]) + i.to_bytes(4, "big") + chunk + _FAKE_TAG))
    assert rx.verified is False          # fake sha: complete, mismatched
    assert Counting.n < total * 80, Counting.n


@pytest.mark.asyncio
async def test_several_windows_in_flight_and_lost_chunks_asked_again():
    """windows=4: one REQUEST asks for four 64-chunk windows, each drained
    window opens the next, and after ``rerequest`` without progress every
    chunk still missing below the frontier is asked again."""
    import json
    from telesthete.protocol.framing import unpack_packet
    band_id = BandCrypto("ota-psk-win").band_id
    data = os.urandom(1024 * 600 + 5)               # 587 chunks, ~9 windows
    requests, n = [], {"chunks": 0}

    def drop(pkt, to):
        frame = unpack_packet(pkt).ciphertext
        if to == "sender" and frame[0] == 0x02:
            requests.append(json.loads(frame[1:-16])["ranges"])
        if to == "receiver" and frame[0] == 0x03:
            n["chunks"] += 1
            return n["chunks"] > 300 and n["chunks"] % 50 == 7      # then 2 % lost, on every pass
        return False

    relay = Relay(drop=drop)
    sender = OtaDropSender(band_id, 21, "w.pyz", data, relay.to_receiver())
    receiver = OtaDropReceiver(band_id, 21, relay.to_sender(), rerequest=0.05, tick_secs=0.02, windows=4)
    relay.sender, relay.receiver = sender, receiver
    receiver.start()
    receiver.accept_offer("w.pyz", len(data), sender.sha256)
    assert await receiver.wait(timeout=15.0) and receiver.data == data
    assert requests[0] == [[0, 64], [64, 128], [128, 192], [192, 256]]
    assert requests[1:4] == [[[256, 320]], [[320, 384]], [[384, 448]]]      # each drained window: the next
    assert any(len(r) > 1 for r in requests[4:])          # a stall asked for the gaps at once
    receiver.stop()
