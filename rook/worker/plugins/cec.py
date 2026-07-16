"""cec.* — raw HDMI-CEC control via a network-attached Pico-CEC (Pico W).

Bridges the rook band to a Pico W running the WiFi-enabled Pico-CEC firmware,
which listens on a TCP port for newline-terminated CEC commands and injects
them onto the HDMI CEC bus. This plugin just forwards raw frames.

Config (env):
    CEC_HOST     hostname/IP of the Pico (required; plugin is inert without it)
    CEC_PORT     TCP port the firmware listens on (default 9526)
    CEC_TIMEOUT  socket timeout in seconds (default 5)

Capabilities:
    cec.send(addr, opcode, operands=None)
        Transmit a CEC frame. ``addr`` is the 4-bit destination logical
        address (0-f, e.g. 0 = TV), ``opcode`` a CEC opcode byte, ``operands``
        an optional list of bytes. All accept ints or hex strings.
        e.g. cec.send(addr=0, opcode="0x36")           -> TV standby (off)
             cec.send(addr=0, opcode="0x04")           -> Image View On (wake)
             cec.send(addr=5, opcode="0x44", operands=["0x41"])  -> volume up
    cec.raw(cmd)
        Send a literal firmware command line, e.g. "send 0 36".
    cec.ping()
        Connectivity/identity check; returns the device logical/physical addr.
"""

from __future__ import annotations

import asyncio
import os
import socket

from ..plugin import Plugin, capability


def _cfg() -> tuple[str, int, float]:
    return (
        os.environ.get("CEC_HOST", ""),
        int(os.environ.get("CEC_PORT", "9526")),
        float(os.environ.get("CEC_TIMEOUT", "5")),
    )


def _hex(v) -> str:
    """Normalize an int or hex string to a bare lowercase hex string."""
    if isinstance(v, bool):
        raise ValueError("expected a byte, got bool")
    if isinstance(v, int):
        n = v
    else:
        s = str(v).strip().lower()
        n = int(s[2:] if s.startswith("0x") else s, 16)
    if not (0 <= n <= 0xFF):
        raise ValueError(f"byte out of range: {v}")
    return f"{n:x}"


def _exchange(line: str) -> str:
    """Send one command line to the Pico and return its reply text."""
    host, port, timeout = _cfg()
    if not host:
        raise RuntimeError("CEC_HOST is not set")
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall((line + "\n").encode())
        try:
            data = s.recv(256)
        except socket.timeout:
            data = b""
    return data.decode(errors="replace").strip()


class CecPlugin(Plugin):
    NAMESPACE = "cec"

    @capability("send")
    async def _send(self, addr, opcode, operands=None, **_) -> dict:
        line = f"send {_hex(addr)} {_hex(opcode)}"
        if operands:
            if isinstance(operands, str):
                operands = operands.split()
            line += " " + " ".join(_hex(o) for o in operands)
        reply = await asyncio.to_thread(_exchange, line)
        return {"ok": reply.startswith("OK"), "sent": line, "reply": reply}

    @capability("raw")
    async def _raw(self, cmd, **_) -> dict:
        line = str(cmd).strip()
        reply = await asyncio.to_thread(_exchange, line)
        return {"ok": not reply.startswith("ERR"), "sent": line, "reply": reply}

    @capability("ping")
    async def _ping(self, **_) -> dict:
        reply = await asyncio.to_thread(_exchange, "ping")
        return {"ok": reply.startswith("pong"), "reply": reply}


# Inert unless this host is configured to reach a Pico-CEC device.
PLUGIN = CecPlugin if os.environ.get("CEC_HOST") else None
