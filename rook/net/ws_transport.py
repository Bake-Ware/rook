"""WebSocket transport for Telesthete Band.

Drop-in replacement for UDPTransport that works over WebSocket.
Same interface: register_handler, send, start, run, stop.

Carries the same encrypted Band packets — WS is just the pipe.
Band crypto still handles authentication and encryption.

Use for:
- Internet peers through Cloudflare tunnel
- Corp VPN where UDP is blocked/NATted
- Fallback when UDP discovery fails
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Callable, Dict, Optional, Tuple

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)


class WSTransportServer:
    """WebSocket transport — server side (hub).

    Listens for WS connections and routes Band packets to handlers,
    same interface as UDPTransport.
    """

    def __init__(self, bind_address: str = "0.0.0.0", bind_port: int = 9999):
        self.bind_address = bind_address
        self.bind_port = bind_port
        self.local_address: Optional[Tuple[str, int]] = None

        self._handlers: Dict[int, list] = defaultdict(list)
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._tasks = []

        # Connected WS clients
        self._addr_to_peer: Dict[Tuple[str, int], str] = {}
        self._peer_to_ws: Dict[str, web.WebSocketResponse] = {}

        # Raw message handler (bypasses channel_type routing for non-Band packets)
        self._raw_handler: Optional[Callable] = None

        self._app = web.Application()
        self._app.router.add_get("/band", self._ws_handler)
        self._runner: Optional[web.AppRunner] = None

    def register_handler(self, channel_type: int, handler: Callable):
        self._handlers[channel_type].append(handler)

    def on_raw_message(self, handler: Callable):
        """Register handler for raw binary messages (non-Band-framed)."""
        self._raw_handler = handler

    def start(self):
        self.local_address = (self.bind_address, self.bind_port)
        logger.info("WS transport server configured on %s:%d", self.bind_address, self.bind_port)

    async def run(self):
        self._running = True
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.bind_address, self.bind_port)
        await site.start()
        logger.info("WS transport server listening on %s:%d", self.bind_address, self.bind_port)

        # Run send loop
        self._tasks = [asyncio.create_task(self._send_loop())]
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        # Close all WS connections
        for ws in list(self._peer_to_ws.values()):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()
        logger.info("WS transport server stopped")

    def send(self, destination: Tuple[str, int], packet_bytes: bytes):
        try:
            self._send_queue.put_nowait((destination, packet_bytes))
        except asyncio.QueueFull:
            logger.warning("WS send queue full")

    async def _send_loop(self):
        while self._running:
            try:
                dest, packet_bytes = await self._send_queue.get()
                peer_id = self._addr_to_peer.get(dest)
                if peer_id and peer_id in self._peer_to_ws:
                    ws = self._peer_to_ws[peer_id]
                    if not ws.closed:
                        await ws.send_bytes(packet_bytes)
                else:
                    # Broadcast to all connected clients
                    for ws in list(self._peer_to_ws.values()):
                        if not ws.closed:
                            await ws.send_bytes(packet_bytes)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("WS send error: %s", e)

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        peer_id = f"ws-{id(ws)}"
        # Assign a virtual address for this peer
        virtual_addr = (request.remote or "0.0.0.0", hash(peer_id) % 65535)

        self._peer_to_ws[peer_id] = ws
        self._addr_to_peer[virtual_addr] = peer_id
        logger.info("WS peer connected: %s from %s", peer_id, request.remote)

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    packet_bytes = msg.data
                    if len(packet_bytes) >= 17:
                        # Try Band-framed routing first
                        channel_type = packet_bytes[16]
                        handlers = self._handlers.get(channel_type, [])
                        if handlers:
                            for handler in handlers:
                                try:
                                    result = handler(virtual_addr, packet_bytes)
                                    if asyncio.iscoroutine(result):
                                        await result
                                except Exception as e:
                                    logger.error("WS handler error: %s", e)
                        elif self._raw_handler:
                            # Not a known channel_type — treat as raw RPC
                            try:
                                result = self._raw_handler(virtual_addr, packet_bytes)
                                if asyncio.iscoroutine(result):
                                    await result
                            except Exception as e:
                                logger.error("WS raw handler error: %s", e)
                    elif self._raw_handler:
                        # Short packet — raw RPC
                        try:
                            result = self._raw_handler(virtual_addr, packet_bytes)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as e:
                            logger.error("WS raw handler error: %s", e)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        except Exception as e:
            logger.info("WS peer error: %s", e)
        finally:
            self._peer_to_ws.pop(peer_id, None)
            self._addr_to_peer.pop(virtual_addr, None)
            logger.info("WS peer disconnected: %s", peer_id)

        return ws


class WSTransportClient:
    """WebSocket transport — client side (remote machines).

    Connects to hub's WS endpoint and routes Band packets,
    same interface as UDPTransport.
    """

    def __init__(self, hub_url: str = "ws://localhost:9999/band"):
        self.hub_url = hub_url
        self.local_address: Optional[Tuple[str, int]] = None

        self._handlers: Dict[int, list] = defaultdict(list)
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._tasks = []
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None

        # Virtual address of the hub (for Band's peer tracking)
        self.hub_addr: Tuple[str, int] = ("hub", 0)

        # Raw message handler
        self._raw_handler: Optional[Callable] = None

    def register_handler(self, channel_type: int, handler: Callable):
        self._handlers[channel_type].append(handler)

    def on_raw_message(self, handler: Callable):
        """Register handler for raw binary messages from hub."""
        self._raw_handler = handler

    def start(self):
        self.local_address = ("client", 0)
        logger.info("WS transport client configured for %s", self.hub_url)

    async def run(self):
        self._running = True
        self._session = aiohttp.ClientSession()

        self._tasks = [
            asyncio.create_task(self._connect_loop()),
            asyncio.create_task(self._send_loop()),
        ]

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()
        logger.info("WS transport client stopped")

    def send(self, destination: Tuple[str, int], packet_bytes: bytes):
        try:
            self._send_queue.put_nowait((destination, packet_bytes))
        except asyncio.QueueFull:
            logger.warning("WS client send queue full")

    async def _connect_loop(self):
        """Connect to hub, auto-reconnect on failure."""
        while self._running:
            try:
                logger.info("Connecting to hub: %s", self.hub_url)
                self._ws = await self._session.ws_connect(
                    self.hub_url, heartbeat=30, receive_timeout=60,
                )
                logger.info("Connected to hub")

                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        packet_bytes = msg.data
                        # Try raw handler first, then channel_type routing
                        if self._raw_handler:
                            try:
                                result = self._raw_handler(self.hub_addr, packet_bytes)
                                if asyncio.iscoroutine(result):
                                    await result
                            except Exception as e:
                                logger.error("WS client raw handler error: %s", e)
                        elif len(packet_bytes) >= 17:
                            channel_type = packet_bytes[16]
                            for handler in self._handlers.get(channel_type, []):
                                try:
                                    result = handler(self.hub_addr, packet_bytes)
                                    if asyncio.iscoroutine(result):
                                        await result
                                except Exception as e:
                                    logger.error("WS client handler error: %s", e)
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                        break

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Hub connection failed: %s — retrying in 5s", e)

            if self._running:
                await asyncio.sleep(5)

    async def _send_loop(self):
        while self._running:
            try:
                dest, packet_bytes = await self._send_queue.get()
                if self._ws and not self._ws.closed:
                    await self._ws.send_bytes(packet_bytes)
                else:
                    # Queue it back — will retry when connected
                    await asyncio.sleep(0.1)
                    self._send_queue.put_nowait((dest, packet_bytes))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("WS client send error: %s", e)
                await asyncio.sleep(0.5)

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed
