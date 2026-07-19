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
