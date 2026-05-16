"""Telesthete-hub transport — UDP, hub-routed, band-id addressable.

Every outbound packet goes to the configured hub address. The hub forwards to
all other peers in the same band (matched by the 16-byte band_id). The
worker thus auto-registers on first send and stays registered while it
keeps sending (the hub evicts idle peers after ~60s).

This intentionally bypasses the LAN-broadcast discovery in
``telesthete.transport.udp.UDPTransport`` — discovery is the hub's job.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Optional

from telesthete.protocol.crypto import BandCrypto
from telesthete.protocol.framing import (
    ChannelType,
    pack_packet,
    unpack_packet,
)

from .base import OnMessage

log = logging.getLogger("rook.worker.transports.telesthete-hub")


class TelestheteHubTransport:
    NAME = "telesthete-hub"

    def __init__(
        self,
        psk: str,
        hub_host: str,
        hub_port: int = 7474,
        keepalive_secs: float = 20.0,
        bind_port: int = 0,
    ) -> None:
        self._crypto = BandCrypto(psk)
        self.band_id = self._crypto.band_id
        self._hub = (hub_host, hub_port)
        self._keepalive = keepalive_secs
        self._bind_port = bind_port

        self._sock: Optional[socket.socket] = None
        self._on_message: Optional[OnMessage] = None
        self._seq = 0
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    # -- lifecycle -----------------------------------------------------------

    async def start(self, on_message: OnMessage) -> None:
        self._on_message = on_message
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("0.0.0.0", self._bind_port))
        self._sock.setblocking(False)
        log.info(
            "telesthete-hub transport up: hub=%s:%d band_id=%s local=%s",
            self._hub[0], self._hub[1], self.band_id.hex()[:16], self._sock.getsockname(),
        )

        # Implicit registration: send a zero-payload Channel frame so the hub
        # learns our (NAT'd) address before any real traffic.
        await self.send(b"\x00")  # 1-byte payload satisfies min-packet rule

        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._recv_loop()),
            loop.create_task(self._keepalive_loop()),
        ]

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        self._sock = None
        log.info("telesthete-hub transport down")

    # -- send/recv -----------------------------------------------------------

    async def send(self, payload: bytes, peer_id: tuple | None = None) -> None:
        if self._sock is None:
            raise RuntimeError("transport not started")
        self._seq += 1
        seq = self._seq
        ciphertext = self._crypto.encrypt(seq, payload)
        frame = pack_packet(
            band_id=self.band_id,
            channel_type=ChannelType.CHANNEL,
            channel_id=0,
            sequence=seq,
            ciphertext=ciphertext,
        )
        loop = asyncio.get_running_loop()
        await loop.sock_sendto(self._sock, frame, self._hub)

    async def _recv_loop(self) -> None:
        assert self._sock is not None
        loop = asyncio.get_running_loop()
        while not self._stopping:
            try:
                data, _src = await loop.sock_recvfrom(self._sock, 65535)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("recv error: %s", e)
                await asyncio.sleep(0.1)
                continue
            if len(data) < 27:
                continue
            try:
                pkt = unpack_packet(data)
            except Exception as e:
                log.debug("bad frame from hub: %s", e)
                continue
            if pkt.band_id != self.band_id:
                continue  # not our band; hub shouldn't send these but be defensive
            try:
                plaintext = self._crypto.decrypt(pkt.sequence, pkt.ciphertext)
            except Exception as e:
                log.debug("decrypt failed seq=%d: %s", pkt.sequence, e)
                continue
            if plaintext == b"\x00":
                # peer-registration ping; ignore
                continue
            if self._on_message is not None:
                try:
                    # peer_id is opaque — use (band_id, channel_id) as a coarse
                    # bucket. The hub hides the real peer addr.
                    await self._on_message(plaintext, (pkt.channel_id,))
                except Exception:
                    log.exception("on_message handler raised")

    async def _keepalive_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self._keepalive)
                if not self._stopping:
                    await self.send(b"\x00")
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("keepalive failed")


TRANSPORT = TelestheteHubTransport
