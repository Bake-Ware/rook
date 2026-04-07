"""HTTP + WebSocket client for the Rook KVM Bridge (T-Dongle-S3)."""

import asyncio
import json
import httpx


class RookBridge:
    """Async client for the ESP32-S3 firmware's REST + WebSocket API."""

    def __init__(self, host: str = "192.168.1.138", port: int = 80, timeout: float = 10.0):
        self.base_url = f"http://{host}:{port}"
        self.ws_url = f"ws://{host}:{port}/telesthete/stream"
        self._client = httpx.AsyncClient(timeout=timeout)
        self._ws = None
        self._serial_buffer: list[str] = []
        self._ws_task = None
        self._file_ready_events: list[dict] = []

    # ---- WebSocket Serial Stream ----

    async def connect_stream(self):
        """Establish persistent WebSocket for real-time serial I/O."""
        import websockets
        self._ws = await websockets.connect(self.ws_url)
        self._ws_task = asyncio.create_task(self._ws_reader())

    async def _ws_reader(self):
        """Background task: read WS messages, buffer serial data."""
        try:
            async for message in self._ws:
                data = json.loads(message)
                if data["type"] == "data":
                    self._serial_buffer.append(data["payload"])
                elif data["type"] == "file_ready":
                    self._file_ready_events.append(data)
        except Exception:
            pass  # connection closed

    async def write_serial_ws(self, data: str):
        """Write to serial via WebSocket."""
        if self._ws:
            await self._ws.send(json.dumps({"type": "data", "payload": data}))
        else:
            await self.write_serial(data)

    async def read_serial_ws(self, timeout: float = 5.0) -> str:
        """Read accumulated serial data from WS buffer."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if self._serial_buffer:
                result = "".join(self._serial_buffer)
                self._serial_buffer.clear()
                return result
            await asyncio.sleep(0.05)
        return ""

    async def wait_for_file(self, filename: str, timeout: float = 30.0) -> dict | None:
        """Wait for a file_ready WebSocket notification."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            for evt in self._file_ready_events:
                if filename in evt.get("path", ""):
                    self._file_ready_events.remove(evt)
                    return evt
            await asyncio.sleep(0.25)
        return None

    # ---- HTTP: HID Keyboard ----

    async def status(self) -> dict:
        resp = await self._client.get(f"{self.base_url}/status")
        resp.raise_for_status()
        return resp.json()

    async def type_text(self, text: str, delay_ms: int = 10) -> dict:
        resp = await self._client.post(
            f"{self.base_url}/type",
            json={"text": text, "delay_ms": delay_ms},
        )
        resp.raise_for_status()
        return resp.json()

    async def key_combo(self, modifiers: list[str], key: str) -> dict:
        resp = await self._client.post(
            f"{self.base_url}/key",
            json={"modifiers": modifiers, "key": key},
        )
        resp.raise_for_status()
        return resp.json()

    # ---- HTTP: Serial (legacy, kept for backwards compat) ----

    async def read_serial(self) -> dict:
        resp = await self._client.get(f"{self.base_url}/serial")
        resp.raise_for_status()
        return resp.json()

    async def write_serial(self, data: str) -> dict:
        resp = await self._client.post(
            f"{self.base_url}/serial",
            json={"data": data},
        )
        resp.raise_for_status()
        return resp.json()

    async def clear_serial(self) -> dict:
        resp = await self._client.post(f"{self.base_url}/serial/clear")
        resp.raise_for_status()
        return resp.json()

    # ---- HTTP: File/Drop Operations ----

    async def list_files(self) -> list[dict]:
        resp = await self._client.get(f"{self.base_url}/telesthete/drop")
        resp.raise_for_status()
        return resp.json()

    async def download_file(self, path: str) -> bytes:
        resp = await self._client.get(f"{self.base_url}/telesthete/drop/{path}")
        resp.raise_for_status()
        return resp.content

    async def delete_file(self, path: str):
        resp = await self._client.delete(f"{self.base_url}/telesthete/drop/{path}")
        resp.raise_for_status()

    async def close(self):
        if self._ws:
            await self._ws.close()
        if self._ws_task:
            self._ws_task.cancel()
        await self._client.aclose()
