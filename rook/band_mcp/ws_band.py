"""WebSocket transport for remote Telesthete Band workers.

Registers a /band WebSocket endpoint on the MCP server's Starlette app,
bridging WS connections into the Telesthete band via a dedicated UDP socket
to the hub. Remote workers send/receive raw encrypted Band packets over WS —
same protocol as UDP. No double-encryption: packets are forwarded as-is.

Remote workers connect via: ws://mcp.example.com/band
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Dict, Optional, Tuple

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

log = logging.getLogger("rook.band_mcp.ws_band")


class WSBandBridge:
    """Bridges WS connections into the Telesthete band.

    When a worker connects via WS, this class registers it as a peer on the
    Telesthete band by maintaining its own UDP connection to the hub. Incoming
    Band packets from the hub are forwarded to all connected WS peers; outgoing
    packets from WS peers are sent directly to the hub (no re-encryption).

    Args:
        app: The Starlette app to register the /band route on.
        hub_host: Hub UDP host address.
        hub_port: Hub UDP port.
        psk: Band pre-shared key for decryption of incoming packets.
    """

    def __init__(self, app: Starlette, hub_host: str, hub_port: int, psk: str):
        self.app = app
        self.hub_addr: Tuple[str, int] = (hub_host, hub_port)
        self.psk = psk
        self._ws_peers: Dict[str, WebSocket] = {}
        self._running = False
        self._sock: Optional[socket.socket] = None
        self._recv_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Register the /band route and begin relaying."""
        if not self._running:
            # Create UDP socket for hub communication.
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setblocking(False)
            log.info("WS band bridge starting (hub=%s:%d)", self.hub_addr[0], self.hub_addr[1])

            # Register the /band route.
            self.app.router.routes.append(
                WebSocketRoute("/band", self._ws_handler),
            )

            # Start relay loop to forward incoming hub packets to WS peers.
            self._recv_task = asyncio.create_task(self._relay_loop())
            self._running = True
            log.info("WS band bridge started")

    async def stop(self) -> None:
        """Close all WS connections and tear down the UDP socket."""
        if not self._running:
            return
        self._running = False

        # Close all connected peers.
        for ws in list(self._ws_peers.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self._ws_peers.clear()

        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass

        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

        log.info("WS band bridge stopped")

    async def _relay_loop(self):
        """Relay incoming Band packets from the hub to all WS peers."""
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                data, src_addr = await loop.sock_recvfrom(self._sock, 65535)
                # Forward raw encrypted packet to all connected WS peers.
                for ws in list(self._ws_peers.values()):
                    try:
                        await ws.send_bytes(data)
                    except Exception as e:
                        log.warning("WS relay error: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:  # Ignore shutdown errors.
                    log.error("WS relay error: %s", e)

    async def _ws_handler(self, websocket: WebSocket):
        """Handle a new WS connection from a remote worker."""
        await websocket.accept()
        peer_id = f"ws-{id(websocket)}"
        self._ws_peers[peer_id] = websocket
        log.info("WS peer connected: %s", peer_id)

        try:
            while True:
                data = await websocket.receive_bytes()
                await self._send_to_hub(data)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.info("WS peer error: %s", e)
        finally:
            self._ws_peers.pop(peer_id, None)
            log.info("WS peer disconnected: %s", peer_id)

    async def _send_to_hub(self, packet_bytes: bytes):
        """Send a raw encrypted Band packet to the hub."""
        if not self._running or self._sock is None:
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.sock_sendto(self._sock, packet_bytes, self.hub_addr)
        except Exception as e:
            log.error("Failed to send to hub: %s", e)
