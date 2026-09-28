"""USB RP2040 dongle bridge; opt in with ROOK_DONGLE_PORT and pyserial.

Commands control the USB host the dongle is physically attached to. No band
credentials are copied to the microcontroller. Each exchange verifies device
identity before sending a command; failed actions are never retried.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid

from ..plugin import Plugin, capability

_LOCK = threading.Lock()


def _exchange(command: dict) -> dict:
    import serial

    with _LOCK:
        with serial.Serial(os.environ["ROOK_DONGLE_PORT"], 115200,
                           timeout=0.2, write_timeout=2, exclusive=True) as port:
            def request(payload):
                token = uuid.uuid4().hex
                encoded = json.dumps({**payload, "id": token}, ensure_ascii=True).encode() + b"\n"
                if len(encoded) > 1024:
                    raise ValueError("command exceeds dongle line limit")
                port.write(encoded)
                deadline = time.monotonic() + 3
                data = bytearray()
                while time.monotonic() < deadline:
                    data.extend(port.read(1))
                    if len(data) > 2048:
                        raise RuntimeError("oversized dongle response")
                    if data.endswith(b"\n"):
                        response = json.loads(data)
                        data.clear()
                        if response.get("id") == token:
                            return response
                raise TimeoutError("dongle reply timed out; command was not retried")

            identity = request({"cmd": "status"})
            if identity.get("device") != "rook-rp2040-zero" or identity.get("protocol") != 1:
                raise RuntimeError("port is not a compatible Rook RP2040 dongle")
            if command["cmd"] == "status":
                return identity
            try:
                return request(command)
            finally:
                # HID methods are taps, never an unbounded held key/button.
                if command["cmd"] in {"keyboard", "mouse", "consumer"}:
                    request({"cmd": "release"})


class DonglePlugin(Plugin):
    NAMESPACE = "dongle"

    def available(self):
        if not os.environ.get("ROOK_DONGLE_PORT"):
            return False
        try:
            import serial  # noqa: F401
        except ImportError:
            return False
        return True

    async def _call(self, command):
        try:
            return await asyncio.to_thread(_exchange, command)
        except (OSError, ValueError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}

    @capability("status")
    async def status(self, **_):
        return await self._call({"cmd": "status"})

    @capability("display")
    async def display(self, text: str, **_):
        if not isinstance(text, str) or len(text.encode()) > 80:
            return {"ok": False, "error": "text must be at most 80 UTF-8 bytes"}
        return await self._call({"cmd": "display", "text": text})

    @capability("display_probe")
    async def display_probe(self, **_):
        return await self._call({"cmd": "display_probe"})

    @capability("keyboard")
    async def keyboard(self, keys: list[int], mods: int = 0, **_):
        """Tap up to six USB HID usage codes with modifier bitmask (US layout)."""
        return await self._call({"cmd": "keyboard", "keys": keys, "mods": mods})

    @capability("mouse")
    async def mouse(self, x: int = 0, y: int = 0, buttons: int = 0,
                    wheel: int = 0, pan: int = 0, **_):
        """Relative move (-127..127) and/or button tap (bitmask 0..31)."""
        return await self._call({"cmd": "mouse", "x": x, "y": y,
                                 "buttons": buttons, "wheel": wheel, "pan": pan})

    @capability("consumer")
    async def consumer(self, usage: int, **_):
        """Tap a USB consumer usage, e.g. 0xE9 volume up or 0xCD play/pause."""
        return await self._call({"cmd": "consumer", "usage": usage})

    @capability("release")
    async def release(self, **_):
        return await self._call({"cmd": "release"})


PLUGIN = DonglePlugin
