"""Talking to a device from a hub script through a worker's streams: the
worker carries bytes (``net.open``, ``net.udp``, ``serial.open``, then
``stream.read`` / ``stream.write`` / ``stream.close``), the script speaks the
protocol. What the device plugins share, and no more: a stream with
read-until, one HTTP/1.1 request, and WebSocket framing.

    async with await Stream.tcp(ctx, worker, host, 8060) as s:
        await s.send("ping\\n")
        line = await s.read_until(b"\\n", timeout=5)
    status, headers, body = await http(ctx, worker, "GET", host, 443, "/api/info", tls=True, verify=False)

A plugin using these lists the caps in its ``uses``.
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct
import time
from typing import Any

from .ctx import KitError

READ_WAIT = 20.0          # longest one stream.read waits (the cap allows 50)


def values(appliance: dict | None) -> dict:
    """An appliance instance as one mapping: its name, host and port, then its
    settings, which win (a ``port`` setting overrides the discovered port)."""
    a = dict(appliance or {})
    settings = a.pop("settings", None) or {}
    return {**a, **{k: v for k, v in settings.items() if v is not None}}


class Stream:
    """One stream on a worker, read as a byte buffer (or datagrams)."""

    def __init__(self, ctx, worker: str, sid: str):
        self.ctx, self.worker, self.id = ctx, worker, sid
        self.buf = bytearray()
        self.after = 0
        self.closed = False
        self.datagrams: list[tuple[bytes, str | None]] = []
        self.max_bytes: int | None = None
        self.exit: int | None = None

    @classmethod
    async def open(cls, ctx, worker: str, cap: str, **args) -> "Stream":
        got = await ctx.call_on(worker, cap, **args)
        return cls(ctx, worker, got["stream"])

    @classmethod
    async def tcp(cls, ctx, worker: str, host: str, port: int, tls: bool = False, verify: bool = True):
        return await cls.open(ctx, worker, "net.open", host=host, port=int(port), tls=tls, verify=verify)

    @classmethod
    async def udp(cls, ctx, worker: str):
        return await cls.open(ctx, worker, "net.udp")

    @classmethod
    async def serial(cls, ctx, worker: str, port: str, baud: int):
        return await cls.open(ctx, worker, "serial.open", port=port, baud=int(baud))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def send(self, data: bytes | str, to: str | None = None) -> None:
        body: dict[str, Any] = ({"text": data} if isinstance(data, str)
                                else {"bytes": base64.b64encode(data).decode("ascii")})
        if to:
            body["to"] = to
        await self.ctx.call_on(self.worker, "stream.write", stream=self.id, body=body)

    async def _more(self, wait: float) -> bool:
        """One stream.read: True when something arrived."""
        got = await self.ctx.call_on(self.worker, "stream.read", stream=self.id, after=self.after,
                                     wait=max(0.0, min(wait, READ_WAIT)))
        self.after = got.get("next", self.after)
        new = False
        for ev in got.get("events") or ():
            if ev.get("type") == "bytes" and "b64" in ev:
                data = base64.b64decode(ev["b64"])
                self.buf += data
                self.datagrams.append((data, ev.get("by")))
                if self.max_bytes is not None and len(self.buf) > self.max_bytes:
                    raise KitError(f"the answer is larger than {self.max_bytes >> 20} MiB (a stream?)")
                new = True
            elif ev.get("type") == "status" and ev.get("error"):
                raise KitError(str(ev["error"]))
            elif ev.get("type") == "status" and "exit" in (ev.get("data") or {}):
                self.exit = ev["data"]["exit"]          # a process's exit code (shell.open)
        self.closed = self.closed or bool(got.get("closed"))
        return new

    async def fill(self, done, timeout: float) -> None:
        """Read until ``done()`` holds; KitError when the stream closes first
        or ``timeout`` seconds pass."""
        deadline = time.monotonic() + timeout
        while not done():
            left = deadline - time.monotonic()
            if self.closed:
                raise KitError("the device closed the connection")
            if left <= 0:
                raise KitError(f"no answer from the device in {timeout:g}s", retry=True)
            await self._more(left)

    async def read_until(self, sep: bytes, timeout: float = 10.0) -> bytes:
        """Up to and including the first ``sep``, taken from the buffer."""
        await self.fill(lambda: sep in self.buf, timeout)
        end = self.buf.index(sep) + len(sep)
        out = bytes(self.buf[:end])
        del self.buf[:end]
        return out

    async def read_exactly(self, n: int, timeout: float = 10.0) -> bytes:
        await self.fill(lambda: len(self.buf) >= n, timeout)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    async def read_to_close(self, timeout: float = 10.0) -> bytes:
        """Everything until the far end closes."""
        deadline = time.monotonic() + timeout
        while not self.closed:
            left = deadline - time.monotonic()
            if left <= 0:
                raise KitError(f"the device didn't finish in {timeout:g}s", retry=True)
            await self._more(left)
        out = bytes(self.buf)
        self.buf.clear()
        return out

    async def listen(self, seconds: float) -> list[tuple[bytes, str | None]]:
        """The datagrams that arrive in ``seconds`` (a UDP stream): [(bytes, sender)]."""
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0 and not self.closed:
            await self._more(left)
        out, self.datagrams = self.datagrams, []
        self.buf.clear()
        return out

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                await self.ctx.call_on(self.worker, "stream.close", stream=self.id)
            except KitError:
                pass


# -- HTTP/1.1, one request per connection ----------------------------------------------

async def http(ctx, worker: str, method: str, host: str, port: int, path: str, *, headers: dict | None = None,
               body: bytes | str = b"", tls: bool = False, verify: bool = True, timeout: float = 10.0,
               max_bytes: int = 16 << 20) -> tuple[int, dict, bytes]:
    """One request through the worker: (status, headers with lower-case
    names, body). The connection is closed after it. An answer that takes
    longer than ``timeout`` or is larger than ``max_bytes`` (a stream that
    never ends) is a KitError."""
    data = body.encode() if isinstance(body, str) else body
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}:{port}", "Connection: close",
             *(f"{k}: {v}" for k, v in (headers or {}).items())]
    if data or method in ("POST", "PUT", "PATCH"):
        lines.append(f"Content-Length: {len(data)}")
    async with await Stream.tcp(ctx, worker, host, port, tls=tls, verify=verify) as s:
        s.max_bytes = max_bytes
        await s.send(("\r\n".join(lines) + "\r\n\r\n").encode() + data)
        head = (await s.read_until(b"\r\n\r\n", timeout)).decode("latin-1")
        status_line, *rest = head.split("\r\n")
        try:
            status = int(status_line.split()[1])
        except (IndexError, ValueError):
            raise KitError(f"not an HTTP answer: {status_line[:80]!r}") from None
        got = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in rest if ":" in h)}
        if method == "HEAD" or status in (204, 304):
            return status, got, b""
        if got.get("transfer-encoding", "").lower() == "chunked":
            out = bytearray()
            while True:
                size = int((await s.read_until(b"\r\n", timeout)).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    break
                out += await s.read_exactly(size, timeout)
                await s.read_exactly(2, timeout)
            return status, got, bytes(out)
        if "content-length" in got:
            return status, got, await s.read_exactly(int(got["content-length"]), timeout)
        return status, got, await s.read_to_close(timeout)


# -- WebSocket (RFC 6455), client side ----------------------------------------------------

class WebSocket:
    """A WebSocket over a worker's TCP (or TLS) stream: masked frames out,
    text or binary messages in (control frames answered: ping gets pong)."""

    def __init__(self, stream: Stream):
        self.stream = stream

    @classmethod
    async def connect(cls, ctx, worker: str, host: str, port: int, path: str = "/", *, tls: bool = False,
                      verify: bool = True, timeout: float = 10.0) -> "WebSocket":
        s = await Stream.tcp(ctx, worker, host, port, tls=tls, verify=verify)
        key = base64.b64encode(os.urandom(16)).decode()
        try:
            await s.send(f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                         f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
            head = (await s.read_until(b"\r\n\r\n", timeout)).decode("latin-1")
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode())
                                      .digest()).decode()
            if head.split()[1:2] != ["101"] or accept not in head:
                raise KitError(f"the device refused the WebSocket: {head.splitlines()[0][:80]}")
        except BaseException:
            await s.close()
            raise
        return cls(s)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _frame(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 1 << 16:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        await self.stream.send(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    async def send(self, text: str) -> None:
        await self._frame(0x1, text.encode())

    async def recv(self, timeout: float = 10.0) -> str | bytes:
        """The next message (text as str, binary as bytes)."""
        parts, kind = [], None
        while True:
            b0, b1 = await self.stream.read_exactly(2, timeout)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", await self.stream.read_exactly(2, timeout))[0]
            elif n == 127:
                n = struct.unpack(">Q", await self.stream.read_exactly(8, timeout))[0]
            mask = await self.stream.read_exactly(4, timeout) if b1 & 0x80 else b""
            data = await self.stream.read_exactly(n, timeout)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            opcode = b0 & 0x0F
            if opcode == 0x8:
                raise KitError("the device closed the WebSocket")
            if opcode == 0x9:
                await self._frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                kind = opcode
            parts.append(data)
            if b0 & 0x80:
                whole = b"".join(parts)
                return whole.decode("utf-8", "replace") if kind == 0x1 else whole

    async def close(self) -> None:
        if not self.stream.closed:
            try:
                await self._frame(0x8, b"")
            except KitError:
                pass
        await self.stream.close()
