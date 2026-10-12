"""Bridge each WebSocket peer to its own UDP endpoint on the blind band hub.

The hub excludes a datagram's sender from its fanout. Sharing one UDP socket
between WebSockets therefore prevents those peers from talking to each other.
Packets remain encrypted end to end; this bridge never needs the band PSK.

This duplicates the relay's own WebSocket listener (telesthitium
``HUB_WS_BIND``), which registers each connection as a first-class peer (no
extra UDP hop, no TTL eviction). It stays the default because deployed
remote workers dial ``wss://<hub_public>/band`` here; workers move to the
native endpoint with ``--ws-url`` / ``ROOK_HUB_WS_URL`` (see
docs/spec/core-v1.md 2.6 for the migration). Addressed routing works through
either: a ROUTE declaration rides the peer's own UDP socket here.
"""
from __future__ import annotations

import asyncio
import logging
import socket

from ..worker.transports.telesthete_hub import set_socket_buffers

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

log = logging.getLogger("rook.band_mcp.ws_band")

# Per-peer UDP socket buffers. The kernel default (~200 KB) drops a burst of
# relayed frames (a large reply fanned out, paced at ~2 MB/s) while the WS side
# is momentarily slow; this absorbs about half a second of it. Best effort
# (clamped by net.core.rmem_max/wmem_max).
BRIDGE_SOCKET_BUFFER = 1024 * 1024


class WSBandBridge:
    def __init__(self, app: Starlette, hub_host: str, hub_port: int, psk: str = ""):
        # psk is retained for compatibility with existing startup code, unused.
        self.app = app
        self.hub_addr = (hub_host, hub_port)
        self._ws_peers: dict[int, tuple[WebSocket, asyncio.Task]] = {}
        self._running = False
        self._route_registered = False

    def start(self) -> None:
        if not self._route_registered:
            self.app.router.routes.append(WebSocketRoute("/band", self._ws_handler))
            self._route_registered = True
        self._running = True
        log.info("WS band bridge started (hub=%s:%d)", *self.hub_addr)

    async def stop(self) -> None:
        self._running = False
        peers = list(self._ws_peers.values())
        for ws, task in peers:
            try:
                await ws.close(code=1001)
            except Exception:
                pass
            task.cancel()
        await asyncio.gather(*(task for _, task in peers), return_exceptions=True)
        self._ws_peers.clear()

    async def _ws_handler(self, websocket: WebSocket):
        if not self._running:
            await websocket.close(code=1001)
            return
        await websocket.accept()
        peer_id = id(websocket)
        self._ws_peers[peer_id] = (websocket, asyncio.current_task())
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        set_socket_buffers(sock, BRIDGE_SOCKET_BUFFER)
        sock.setblocking(False)
        tasks = []
        loop = asyncio.get_running_loop()
        try:
            # Connected UDP accepts replies only from the configured hub.
            await loop.sock_connect(sock, self.hub_addr)

            async def to_hub():
                while self._running:
                    data = await websocket.receive_bytes()
                    if len(data) > 65507:
                        await websocket.close(code=1009)
                        return
                    await loop.sock_sendall(sock, data)

            async def from_hub():
                while self._running:
                    data = await loop.sock_recv(sock, 65535)
                    await websocket.send_bytes(data)

            tasks = [asyncio.create_task(to_hub()), asyncio.create_task(from_hub())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.info("WS peer disconnected (%s)", type(error).__name__)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            sock.close()
            self._ws_peers.pop(peer_id, None)
            try:
                await websocket.close()
            except Exception:
                pass
