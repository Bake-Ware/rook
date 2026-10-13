"""HTTP + WebSocket client for the Rook KVM Bridge (T-Dongle-S3)."""

import asyncio
import json
import logging
import os
import random
import httpx

try:  # shared helper when rook is installed alongside (same repo)
    from rook.core.backoff import Backoff
except ImportError:  # rook-kvm is packaged on its own; same curve, inline
    class Backoff:
        def __init__(self, base=1.0, cap=30.0, factor=2.0, jitter=0.5):
            self.base, self.cap, self.factor, self.jitter = base, cap, factor, jitter
            self.attempt = 0

        def next(self):
            nominal = min(self.cap, self.base * self.factor ** min(self.attempt, 64))
            self.attempt += 1
            return nominal * (1.0 - self.jitter * random.random())

        def reset(self):
            self.attempt = 0

log = logging.getLogger("rook_kvm.bridge")

# A stream that stayed up this long resets the reconnect backoff.
WS_STABLE_SECS = 10.0


class RookBridge:
    """Async client for the ESP32-S3 firmware's REST + WebSocket API."""

    def __init__(
        self,
        host: str | None = None,
        port: int = 80,
        timeout: float = 10.0,
        admin_user: str | None = None,
        admin_pass: str | None = None,
    ):
        host = host or os.environ.get("ROOK_BRIDGE_HOST", "192.168.4.1")
        admin_user = admin_user or os.environ.get("ROOK_ADMIN_USER", "admin")
        admin_pass = admin_pass or os.environ.get("ROOK_ADMIN_PASS", "")
        self.base_url = f"http://{host}:{port}"
        self.ws_url = f"ws://{host}:{port}/telesthete/stream"
        # Basic auth: single round-trip, avoids re-upload on multipart /ota.
        # Server accepts both Basic and Digest via request->authenticate().
        self._auth = httpx.BasicAuth(admin_user, admin_pass)
        self._client = httpx.AsyncClient(timeout=timeout)
        self._ws = None
        self._serial_buffer: list[str] = []
        self._ws_task = None
        self._file_ready_events: list[dict] = []
        self._closing = False
        self._backoff = Backoff(base=1.0, cap=30.0)

    # ---- WebSocket Serial Stream ----

    async def _connect_ws(self):
        import websockets
        return await websockets.connect(self.ws_url)

    async def connect_stream(self):
        """Establish persistent WebSocket for real-time serial I/O.

        The first connect is synchronous so a caller learns immediately that
        the dongle is unreachable. After that a background task keeps the
        stream up: when the dongle reboots (mode switch, OTA, config save) or
        WiFi blips, it reconnects with jittered backoff. While it is down
        ``_ws`` is None, so callers fall back to the HTTP serial endpoints."""
        self._closing = False
        self._ws = await self._connect_ws()
        self._ws_task = asyncio.create_task(self._ws_run())

    async def _ws_run(self):
        """Serve the current stream, then reconnect until :meth:`close`."""
        loop = asyncio.get_running_loop()
        while not self._closing:
            began = loop.time()
            ws = self._ws
            await self._ws_reader()
            self._ws = None
            # The reader also returns on errors that leave the socket open;
            # close it so a reconnect never leaks the old connection.
            await self._close_quietly(ws)
            if self._closing:
                return
            if loop.time() - began >= WS_STABLE_SECS:
                self._backoff.reset()
            while not self._closing:
                delay = self._backoff.next()
                log.info("bridge stream lost; reconnecting in %.1fs", delay)
                await asyncio.sleep(delay)
                if self._closing:
                    return
                try:
                    self._ws = await self._connect_ws()
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("bridge stream reconnect failed: %s", e)

    @staticmethod
    async def _close_quietly(ws) -> None:
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:
            pass

    def _on_ws_message(self, message) -> None:
        """Buffer one stream message. Anything malformed (not a JSON object,
        ``data`` without a string ``payload``, ``file_ready`` without a
        string ``path``) is skipped: one bad message from the dongle must not
        end the stream."""
        try:
            data = json.loads(message)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        if kind == "data":
            payload = data.get("payload")
            if isinstance(payload, str):
                self._serial_buffer.append(payload)
        elif kind == "file_ready":
            if isinstance(data.get("path", ""), str):
                self._file_ready_events.append(data)

    async def _ws_reader(self):
        """Read WS messages into the buffers until the stream closes."""
        ws = self._ws
        if ws is None:
            return
        try:
            async for message in ws:
                self._on_ws_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Connection closed or broke; _ws_run closes it and reconnects.
            log.debug("bridge stream ended: %s", e)

    async def write_serial_ws(self, data: str):
        """Write to serial via WebSocket (HTTP while the stream is down)."""
        ws = self._ws
        if ws:
            try:
                await ws.send(json.dumps({"type": "data", "payload": data}))
                return
            except Exception:
                pass  # stream just dropped; the reconnect task takes over
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

    async def consumer(self, key: str | None = None, code: int | None = None) -> dict:
        body: dict = {}
        if key is not None:
            body["key"] = key
        if code is not None:
            body["code"] = code
        resp = await self._client.post(f"{self.base_url}/consumer", json=body)
        resp.raise_for_status()
        return resp.json()

    async def ota(self, firmware_path: str, timeout: float = 180.0) -> dict:
        """Wireless flash. Posts firmware.bin as raw body to /ota.
        Device reboots into new image on success."""
        from pathlib import Path
        p = Path(firmware_path)
        size = p.stat().st_size
        data_bytes = p.read_bytes()  # buffer fully so Content-Length is set
        try:
            resp = await self._client.post(
                f"{self.base_url}/ota",
                content=data_bytes,
                headers={"Content-Type": "application/octet-stream"},
                auth=self._auth,
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except (
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.ReadTimeout,
            httpx.ConnectError,
        ) as e:
            data = {"ok": True, "response_lost": True, "exception": f"{type(e).__name__}: {e}"}
        data["uploaded_bytes"] = size
        return data

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

    # ---- HTTP: Device Mode + HID kill-switch ----

    async def get_mode(self) -> dict:
        resp = await self._client.get(f"{self.base_url}/mode")
        resp.raise_for_status()
        return resp.json()

    async def set_mode(self, mode: str) -> dict:
        """Switch storage mode. Causes the device to reboot — the HTTP
        response is sent before reboot, but if it doesn't arrive cleanly,
        treat as success (dongle is rebooting into the new mode)."""
        try:
            resp = await self._client.post(
                f"{self.base_url}/mode", json={"mode": mode},
                timeout=3.0,
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError):
            # Device rebooted before HTTP could finalize — that's a success
            return {"mode": mode, "rebooting": True, "response_lost": True}

    async def get_hid_enabled(self) -> dict:
        resp = await self._client.get(f"{self.base_url}/hid")
        resp.raise_for_status()
        return resp.json()

    async def set_hid_enabled(self, enabled: bool) -> dict:
        resp = await self._client.post(
            f"{self.base_url}/hid", json={"enabled": bool(enabled)},
        )
        resp.raise_for_status()
        return resp.json()

    async def reboot_to_bootloader(self) -> dict:
        """Trigger the device into ROM bootloader mode (USB download mode)
        via software, so the host can flash via esptool without holding the
        physical BOOT button. The HTTP response may be lost mid-reboot;
        treat that as success."""
        try:
            resp = await self._client.post(
                f"{self.base_url}/flash_mode", timeout=3.0, auth=self._auth,
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError):
            return {"ok": True, "response_lost": True}

    async def get_config(self) -> str:
        """Fetch the rendered HTML config page (admin auth)."""
        resp = await self._client.get(f"{self.base_url}/config", auth=self._auth)
        resp.raise_for_status()
        return resp.text

    async def set_config(self, **fields) -> dict:
        """Update one or more settings (admin auth).

        Accepts: ap_ssid, ap_pass, sta_ssid, sta_pass, admin_user, admin_pass.
        Device reboots after save — connection drop expected.
        """
        try:
            resp = await self._client.post(
                f"{self.base_url}/config",
                data=fields,
                auth=self._auth,
                timeout=5.0,
            )
            resp.raise_for_status()
            return {"ok": True, "updated": list(fields.keys())}
        except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError):
            return {"ok": True, "updated": list(fields.keys()), "response_lost": True}

    async def factory_reset(self) -> dict:
        """Wipe NVS settings and reboot (admin auth)."""
        try:
            resp = await self._client.post(
                f"{self.base_url}/factory_reset", auth=self._auth, timeout=5.0,
            )
            resp.raise_for_status()
            return {"ok": True}
        except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError):
            return {"ok": True, "response_lost": True}

    async def close(self):
        self._closing = True
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ws_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        await self._client.aclose()
