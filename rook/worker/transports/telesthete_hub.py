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
from telesthete.protocol.sequence import SequenceSource

from ..wire import Fragmenter, Reassembler, HEADER_SIZE as FRAG_HEADER
from .base import OnConnect, OnMessage

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
        self._on_connect: Optional[OnConnect] = None
        # SECURITY: the band key is HKDF(PSK) — one static key shared by every
        # peer — and the AEAD nonce is just 4 zero bytes || an 8-byte counter
        # (telesthete crypto.nonce_from_seq). If every peer started at 0, they'd
        # all reuse the same (key, nonce) pairs, which is catastrophic for
        # ChaCha20-Poly1305/AES-GCM (keystream + one-time-MAC-key reuse → plaintext
        # leak and MAC forgery, all without the PSK). telesthete's SequenceSource
        # is exactly the fix rook prototyped here: a CSPRNG-seeded 63-bit start
        # with a thread-safe monotonic +=1, so cross-peer/cross-restart nonce
        # collisions are negligible (~M·N²/2⁶³). Wire-compatible: the sequence
        # travels in the frame and the receiver derives the nonce from it.
        self._seq_source = SequenceSource()
        self._tasks: list[asyncio.Task] = []
        self._inflight: set[asyncio.Task] = set()
        self._stopping = False
        # Dispatch now runs concurrently (see _dispatch), so several coroutines
        # may call send() at once; serialize frame emission + seq increment.
        self._send_lock = asyncio.Lock()

        self._fragmenter = Fragmenter()
        self._reassembler = Reassembler()

    # -- lifecycle -----------------------------------------------------------

    async def start(self, on_message: OnMessage,
                    on_connect: OnConnect | None = None) -> None:
        self._on_message = on_message
        self._on_connect = on_connect

        loop = asyncio.get_running_loop()

        if self._use_ws:
            # Remote workers reach the hub over a WS tunnel. The whole
            # connect -> receive -> reconnect lifecycle lives in _ws_run_loop,
            # which also (re)registers and fires on_connect each time the link
            # comes up. Implicit registration happens there (we can't send
            # before we're connected).
            log.info(
                "telesthete-hub transport up (WS): hub=%s:%d band_id=%s",
                self._hub[0], self._hub[1], self.band_id.hex()[:16],
            )
            self._tasks.append(loop.create_task(self._ws_run_loop()))
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
        for t in list(self._tasks) + list(self._inflight):
            t.cancel()
        for t in list(self._tasks) + list(self._inflight):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._inflight.clear()
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        self._sock = None
        if self._ws_conn is not None:
            try:
                await self._ws_conn.close()
            except Exception:
                pass
            self._ws_conn = None
        if self._ws_session is not None:
            try:
                await self._ws_session.close()
            except Exception:
                pass
            self._ws_session = None
        log.info("telesthete-hub transport down")

    # -- send/recv -----------------------------------------------------------

    async def send(self, payload: bytes, peer_id: tuple | None = None) -> None:
        """Fragment + encrypt + send. Big payloads are split across multiple
        Telesthete CHANNEL frames per SPEC §6.4.

        Serialized under ``_send_lock``: dispatch runs concurrently now, so
        replies/keepalives/announces can race here. The lock keeps the seq
        counter monotonic, the fragments of one message contiguous on the
        wire, and avoids concurrent ``send_bytes`` on the aiohttp socket
        (which is not safe under concurrency)."""
        if self._use_ws:
            if self._ws_conn is None:
                # Mid-reconnect (or not up yet). Caller loops catch + log;
                # the next announce after reconnect re-registers us.
                raise RuntimeError("WS not connected")
        elif self._sock is None:
            raise RuntimeError("transport not started")
        loop = asyncio.get_running_loop()
        async with self._send_lock:
            chunks = self._fragmenter.split(payload)
            for chunk in chunks:
                seq = self._seq_source.next()
                ciphertext = self._crypto.encrypt(seq, chunk)
                frame = pack_packet(
                    band_id=self.band_id,
                    channel_type=ChannelType.CHANNEL,
                    channel_id=0,
                    sequence=seq,
                    ciphertext=ciphertext,
                )
                if self._use_ws:
                    ws = self._ws_conn
                    if ws is None:
                        raise RuntimeError("WS not connected")
                    await ws.send_bytes(frame)
                else:
                    await loop.sock_sendto(self._sock, frame, self._hub)
            if len(chunks) > 1:
                log.debug("sent %d-fragment message (%d B payload)",
                          len(chunks), len(payload))

    async def _ws_run_loop(self) -> None:
        """Own the WS connection lifecycle: connect, serve, reconnect.

        On every (re)connect we re-register with the hub and fire on_connect
        so the worker re-announces. This is what makes a dropped band link
        self-heal, instead of the old behaviour where one closed socket left
        the worker silently off the band until the process was restarted.
        """
        backoff = 1.0
        while not self._stopping:
            try:
                # total=None: never impose a lifetime cap on the long-lived
                # websocket (aiohttp's default ClientTimeout would force-close
                # it after 5 minutes). sock_connect bounds only the initial TCP
                # connect so an unreachable hub fails fast into the retry below
                # instead of hanging forever. Liveness is handled by heartbeat.
                self._ws_session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=20.0))
                # heartbeat: aiohttp sends WS pings and tears the connection
                # down if pongs stop — proactive dead-peer detection + keeping
                # the Cloudflare tunnel from idling out.
                ws_conn = await self._ws_session.ws_connect(
                    self._ws_url, heartbeat=30.0, autoping=True)
                self._ws_conn = ws_conn
                backoff = 1.0
                log.info("WS connected: %s", self._ws_url)
                # Implicit registration (hub learns our address), then let the
                # worker re-announce its capabilities onto the band.
                try:
                    await self.send(_KEEPALIVE_PAYLOAD)
                    if self._on_connect is not None:
                        await self._on_connect()
                except Exception:
                    log.exception("post-connect (re)announce failed")
                await self._ws_recv_loop(ws_conn)  # returns when the link drops
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("WS connect failed: %s", e)
            finally:
                self._ws_conn = None
                if self._ws_session is not None:
                    try:
                        await self._ws_session.close()
                    except Exception:
                        pass
                    self._ws_session = None
            if self._stopping:
                break
            log.info("WS reconnecting in %.1fs", backoff)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            backoff = min(backoff * 2, 30.0)

    async def _ws_recv_loop(self, ws_conn: aiohttp.ClientWebSocketResponse) -> None:
        """Receive encrypted Band packets from the hub via WS.

        Returns (rather than looping) as soon as the connection closes or
        errors, so :meth:`_ws_run_loop` can reconnect. Capability dispatch is
        handed to a separate task (see :meth:`_dispatch`) so a slow handler
        never stalls this loop — that stall was what stopped pong replies and
        got the connection killed in the first place.
        """
        while not self._stopping:
            try:
                msg = await ws_conn.receive()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("WS recv error: %s", e)
                return  # reconnect
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
                self._dispatch(assembled, (pkt.channel_id,))
            elif msg.type in (aiohttp.WSMsgType.CLOSED,
                              aiohttp.WSMsgType.CLOSING,
                              aiohttp.WSMsgType.ERROR):
                log.info("WS closed by peer (type=%s)", msg.type.name)
                return  # reconnect

    def _dispatch(self, payload: bytes, peer: tuple) -> None:
        """Run on_message in its own task so slow capabilities don't block the
        receive loop. Tracked in ``_inflight`` so the task isn't GC'd mid-run
        and can be cancelled on shutdown."""
        if self._on_message is None:
            return
        task = asyncio.create_task(self._safe_on_message(payload, peer))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _safe_on_message(self, payload: bytes, peer: tuple) -> None:
        try:
            await self._on_message(payload, peer)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("on_message handler raised")

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
            self._dispatch(assembled, (pkt.channel_id,))

    async def _keepalive_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self._keepalive)
                if not self._stopping:
                    await self.send(_KEEPALIVE_PAYLOAD)
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Benign while the WS run-loop is reconnecting; it will recover.
                log.debug("keepalive skipped: %s", e)


TRANSPORT = TelestheteHubTransport
