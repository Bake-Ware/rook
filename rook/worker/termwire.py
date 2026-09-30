"""Compact framing for raw terminal bytes on the band.

Band messages are JSON, so raw PTY bytes need an encoding. Terminal output is
mostly ASCII sprinkled with escape sequences, and each ESC costs six bytes as a
JSON ``\\u001b`` escape. No single encoding wins everywhere, so the sender picks
the smallest of the ones the receiver accepts, per chunk:

``t``  UTF-8 text (only when the chunk is valid UTF-8 on its own)
``b``  base64 of the raw bytes
``z``  base64 of zlib-deflated raw bytes (worth it past a few hundred bytes;
       redraw-heavy TUI output typically shrinks 3-6x)

Every band packet is fragmented at ~1 KB with no retransmit, so fewer bytes
means fewer fragments that all have to arrive. Both ends import this module
(stdlib only; it ships in the worker bundle).
"""

from __future__ import annotations

import base64
import json
import zlib

ENCODINGS = ("t", "b", "z")
_Z_MIN = 256   # below this, deflate overhead rarely pays


def encode(raw: bytes, accept=ENCODINGS) -> tuple[str, str]:
    """Return ``(enc, data)`` for ``raw``: the smallest accepted encoding."""
    if not raw:
        return "t", ""
    accept = tuple(a for a in (accept or ()) if a in ENCODINGS) or ("b",)
    options: list[tuple[int, str, str]] = []
    if "t" in accept:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is not None:
            # Cost on the wire is the JSON-escaped length, not len(text).
            options.append((len(json.dumps(text)) - 2, "t", text))
    if "b" in accept or not options:
        b = base64.b64encode(raw).decode("ascii")
        options.append((len(b), "b", b))
    if "z" in accept and len(raw) >= _Z_MIN:
        z = base64.b64encode(zlib.compress(raw, 6)).decode("ascii")
        options.append((len(z), "z", z))
    _, enc, data = min(options, key=lambda o: o[0])
    return enc, data


def decode(enc: str, data: str, limit: int = 4 * 1024 * 1024) -> bytes:
    """Inverse of :func:`encode`. ``limit`` bounds a decompressed chunk so a
    malicious peer can't hand the hub a zip bomb."""
    if not data:
        return b""
    if enc == "t":
        return data.encode("utf-8")
    if enc == "b":
        return base64.b64decode(data)
    if enc == "z":
        d = zlib.decompressobj()
        out = d.decompress(base64.b64decode(data), limit)
        if d.unconsumed_tail:
            raise ValueError("terminal chunk exceeds the decompression limit")
        return out
    raise ValueError(f"unknown terminal encoding: {enc!r}")
