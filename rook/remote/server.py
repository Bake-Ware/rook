"""WebSocket server for remote workers."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

import websockets
from websockets.server import WebSocketServerProtocol

log = logging.getLogger(__name__)


class RemoteWorker:
    """A connected remote machine."""

    def __init__(self, ws: WebSocketServerProtocol, name: str, platform: str, hostname: str):
        self.id = str(uuid.uuid4())[:8]
        self.ws = ws
        self.name = name
        self.platform = platform  # "windows" or "linux"
        self.hostname = hostname
        self.connected_at = time.time()
        self.last_active = time.time()
        self._pending: dict[str, asyncio.Future] = {}

    @property
    def alive(self) -> bool:
        return self.ws.open

    async def execute(self, command: str, timeout: float = 60) -> dict[str, Any]:
        """Send a command to the worker and wait for the result."""
        req_id = str(uuid.uuid4())[:8]
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        await self.ws.send(json.dumps({
            "type": "exec",
            "id": req_id,
            "command": command,
        }))
        self.last_active = time.time()

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"stdout": "", "stderr": "Command timed out", "returncode": -1}

    def handle_response(self, data: dict) -> None:
        """Handle a response from the worker."""
        req_id = data.get("id", "")
        future = self._pending.pop(req_id, None)
        if future and not future.done():
            future.set_result(data)


class WorkerServer:
    """WebSocket server that accepts remote worker connections."""

    def __init__(self, host: str = "0.0.0.0", port: int = 7005, auth_token: str = ""):
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self._workers: dict[str, RemoteWorker] = {}
        self._server = None

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handle_connection,
            self.host,
            self.port,
        )
        log.info("Worker server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_connection(self, ws: WebSocketServerProtocol) -> None:
        worker = None
        try:
            # First message must be registration
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(raw)

            if data.get("type") != "register":
                await ws.close(1008, "Expected registration")
                return

            # Check auth
            if self.auth_token and data.get("token") != self.auth_token:
                await ws.close(1008, "Invalid token")
                log.warning("Worker rejected: bad token from %s", ws.remote_address)
                return

            worker = RemoteWorker(
                ws=ws,
                name=data.get("name", "unnamed"),
                platform=data.get("platform", "unknown"),
                hostname=data.get("hostname", "unknown"),
            )
            self._workers[worker.id] = worker
            log.info("Worker connected: [%s] %s (%s/%s)", worker.id, worker.name, worker.platform, worker.hostname)

            await ws.send(json.dumps({"type": "registered", "id": worker.id}))

            # Listen for responses
            async for message in ws:
                try:
                    data = json.loads(message)
                    if data.get("type") == "result":
                        worker.handle_response(data)
                    elif data.get("type") == "heartbeat":
                        worker.last_active = time.time()
                except json.JSONDecodeError:
                    pass

        except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError, Exception) as e:
            log.info("Worker disconnected: %s (%s)", worker.name if worker else "unknown", e)
        finally:
            if worker:
                # Cancel any pending futures
                for future in worker._pending.values():
                    if not future.done():
                        future.set_result({"stdout": "", "stderr": "Worker disconnected", "returncode": -1})
                self._workers.pop(worker.id, None)

    def get_worker(self, name: str) -> RemoteWorker | None:
        """Find a worker by name or ID."""
        for w in self._workers.values():
            if w.name == name or w.id == name:
                return w
        return None

    def list_workers(self) -> list[dict[str, Any]]:
        return [
            {
                "id": w.id,
                "name": w.name,
                "platform": w.platform,
                "hostname": w.hostname,
                "alive": w.alive,
                "connected": f"{time.time() - w.connected_at:.0f}s ago",
                "last_active": f"{time.time() - w.last_active:.0f}s ago",
            }
            for w in self._workers.values()
        ]
