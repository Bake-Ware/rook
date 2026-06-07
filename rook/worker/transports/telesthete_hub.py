"""Telesthete-hub transport — UDP, hub-routed, band-id addressable.

Every outbound packet goes to the configured hub address. The hub forwards
to all other peers in the same band (matched by the 16-byte band_id). The
worker auto-registers on first send and stays registered while it keeps
sending (the hub evicts idle peers after ~60s).

Messages larger than one UDP datagram are fragmented at the Channel layer
per SPEC §6.4 ("Maximum packet payload: 1024 bytes — fragments larger
sends"). Each fragment is a self-contained Telesthete CHANNEL frame with
its own AEAD; the receiver decrypts each frame and feeds the cleartext
chunk through :class:`rook.worker.wire.Reassembler` to recover the full
message.

This intentionally bypasses the LAN-broadcast discovery in
``telesthete.transport.udp.UDPTransport`` — discovery is the hub's job.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Optional

import aiohttp
from telesthete.protocol.crypto import BandCrypto
from telesthete.protocol.framing import (
    ChannelType,
    pack_packet,
    unpack_packet,
)

from ..wire import Fragmenter, Reassembler, HEADER_SIZE as FRAG_HEADER
from .base import OnMessage

log = logging.getLogger("rook.worker.transports.telesthete-hub")


# A bare 1-byte payload still goes through the fragmenter (single fragment)
# so the wire shape is uniform. Receiver detects keepalives by examining
# the assembled payload, not by sniffing inside frames.
_KEEPALIVE_PAYLOAD = b"\x00"


class TelestheteHubTransport:
    NAME = "telesthete-hub"

    def __init__(
        self,
        psk: str,
        hub_host: str,
        hub_port: int = 7474,
        keepalive_secs: float = 20.0,
        bind_port: int = 0,
        use_ws: bool = False,
    ) -> None:
        self._crypto = BandCrypto(psk)
        self.band_id = self._crypto.band_id
        self._hub = (hub_host, hub_port)
        self._keepalive = keepalive_secs
        self._bind_port = bind_port
        self._use_ws = use_ws

        # UDP transport (for LAN peers)
        self._sock: Optional[socket.socket] = None
        
        # WS transport (for remote workers through cloudflare tunnel)
        self._ws_session: Optional[aiohttp.ClientSession] = None
        self._ws_conn: Optional[aiohttp.ClientWebSocketResponse] = None
        # Port 443 (or 8443) is TLS at the edge — must use wss://, not plaintext ws://.
        _scheme = "wss" if hub_port in (443, 8443) else "ws"
        self._ws_url: str = f"{_scheme}://{hub_host}:{hub_port}/band"

        self._on_message: Optional[OnMessage] = None
        self._seq = 0
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

        self._fragmenter = Fragmenter()
        self._reassembler = Reassembler()

    # -- lifecycle -----------------------------------------------------------

    async def start(self, on_message: OnMessage) -> None:
        self._on_message = on_message
        loop = asyncio.get_running_loop()

        if self._use_ws:
            # Use WS transport for remote workers through the cloudflare tunnel.
            log.info(
                "telesthete-hub transport up (WS): hub=%s:%d band_id=%s",
                self._hub[0], self._hub[1], self.band_id.hex()[:16],
            )
            self._ws_session = aiohttp.ClientSession()
            # Establish the first connection now (best-effort); the manage loop
            # owns every subsequent (re)connect so an idle/dropped link recovers.
            await self._ws_connect_once()
            self._tasks.append(loop.create_task(self._ws_manage_loop()))
        else:
            # Use UDP transport for LAN peers
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.bind(("0.0.0.0", self._bind_port))
            self._sock.setblocking(False)
            log.info(
                "telesthete-hub transport up: hub=%s:%d band_id=%s local=%s "
                "frag_overhead=%dB",
                self._hub[0], self._hub[1], self.band_id.hex()[:16],
                self._sock.getsockname(), FRAG_HEADER,
            )
            # Implicit registration: send a zero-payload frame so the hub learns
            # our (NAT'd) address before any real traffic.
            await self.send(_KEEPALIVE_PAYLOAD)
            self._tasks.append(loop.create_task(self._recv_loop()))

        self._tasks.append(loop.create_task(self._keepalive_loop()))

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
        if self._ws_session is not None:
            await self._ws_session.close()
            self._ws_session = None
        self._ws_conn = None
        log.info("telesthete-hub transport down")

    # -- send/recv -----------------------------------------------------------

    async def send(self, payload: bytes, peer_id: tuple | None = None) -> None:
        """Fragment + encrypt + send. Big payloads are split across multiple
        Telesthete CHANNEL frames per SPEC §6.4."""
        if not self._use_ws and self._sock is None:
            raise RuntimeError("transport not started")
        chunks = self._fragmenter.split(payload)
        loop = asyncio.get_running_loop()
        for chunk in chunks:
            self._seq += 1
            seq = self._seq
            ciphertext = self._crypto.encrypt(seq, chunk)
            frame = pack_packet(
                band_id=self.band_id,
                channel_type=ChannelType.CHANNEL,
                channel_id=0,
                sequence=seq,
                ciphertext=ciphertext,
            )
            if self._use_ws:
                conn = self._ws_conn
                if conn is None:
                    # Mid-reconnect: drop this frame. Announces (30s) and
                    # keepalives (20s) retry, so the worker re-registers once
                    # the link is back — no fatal error to the caller.
                    log.debug("WS not connected; dropping outbound frame")
                    return
                try:
                    await conn.send_bytes(frame)
                except Exception as e:
                    log.warning("WS send failed (%s); marking link down", e)
                    self._ws_conn = None  # manage loop reconnects
                    return
            elif self._sock is not None:
                await loop.sock_sendto(self._sock, frame, self._hub)
        if len(chunks) > 1:
            log.debug("sent %d-fragment message (%d B payload)",
                      len(chunks), len(payload))

    async def _ws_connect_once(self) -> bool:
        """(Re)establish the WS connection to the hub and re-register.

        ``heartbeat`` makes aiohttp send WS PING frames so the link survives the
        Cloudflare edge's idle timeout and dead connections surface promptly as
        a close/timeout (which drives a reconnect). Returns True on success.
        """
        if self._ws_session is None or self._stopping:
            return False
        try:
            conn = await self._ws_session.ws_connect(self._ws_url, heartbeat=20.0)
        except Exception as e:
            log.warning("WS connect to %s failed: %s", self._ws_url, e)
            return False
        self._ws_conn = conn
        # Implicit re-registration so the hub re-learns us immediately; the
        # worker's announce loop then re-advertises caps with the same id.
        try:
            await self.send(_KEEPALIVE_PAYLOAD)
        except Exception:
            pass
        return True

    async def _ws_manage_loop(self) -> None:
        """Own the WS connection's whole lifecycle: serve the recv loop while
        connected, and reconnect with capped backoff whenever the link drops.
        Without this, an idle connection closed by the edge never came back and
        the worker silently fell off the band."""
        backoff = 1.0
        while not self._stopping:
            if self._ws_conn is None:
                if not await self._ws_connect_once():
                    await asyncio.sleep(min(backoff, 30.0))
                    backoff = min(backoff * 2, 30.0)
                    continue
                backoff = 1.0
                log.info("WS (re)connected to hub")
            conn = self._ws_conn
            if conn is not None:
                await self._ws_recv_loop(conn)  # returns when the link drops
            self._ws_conn = None
            if not self._stopping:
                log.warning("WS link down — reconnecting")
                await asyncio.sleep(1.0)

    async def _ws_recv_loop(self, ws_conn: aiohttp.ClientWebSocketResponse) -> None:
        """Receive encrypted Band packets from the hub via WS. Returns (rather
        than looping forever) when the connection drops, so the manage loop can
        reconnect."""
        while not self._stopping:
            try:
                msg = await ws_conn.receive()
                if msg.type == aiohttp.WSMsgType.BINARY:
                    data = msg.data
                    if len(data) < 27:
                        continue
                    try:
                        pkt = unpack_packet(data)
                    except Exception as e:
                        log.debug("bad frame from hub: %s", e)
                        continue
                    if pkt.band_id != self.band_id:
                        continue
                    try:
                        cleartext = self._crypto.decrypt(pkt.sequence, pkt.ciphertext)
                    except Exception as e:
                        log.debug("decrypt failed seq=%d: %s", pkt.sequence, e)
                        continue
                    # cleartext is a fragment chunk — feed it through the reassembler.
                    assembled = self._reassembler.feed(cleartext)
                    if assembled is None:
                        continue
                    if assembled == _KEEPALIVE_PAYLOAD:
                        continue
                    if self._on_message is not None:
                        try:
                            await self._on_message(assembled, (pkt.channel_id,))
                        except Exception:
                            log.exception("on_message handler raised")
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("WS recv error: %s — dropping link", e)
                break

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
                continue
            try:
                cleartext = self._crypto.decrypt(pkt.sequence, pkt.ciphertext)
            except Exception as e:
                log.debug("decrypt failed seq=%d: %s", pkt.sequence, e)
                continue
            # cleartext is a fragment chunk — feed it through the reassembler.
            assembled = self._reassembler.feed(cleartext)
            if assembled is None:
                continue
            if assembled == _KEEPALIVE_PAYLOAD:
                continue
            if self._on_message is not None:
                try:
                    await self._on_message(assembled, (pkt.channel_id,))
                except Exception:
                    log.exception("on_message handler raised")

    async def _keepalive_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self._keepalive)
                if not self._stopping:
                    await self.send(_KEEPALIVE_PAYLOAD)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("keepalive failed")


TRANSPORT = TelestheteHubTransport
