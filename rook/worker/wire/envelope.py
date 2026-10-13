"""Negotiated envelope extensions on the band (core spec section 14).

Everything here is opt-in per message, so old peers never see it:

* ``features`` (announce): what a node understands. Callers only use an
  extension toward a target whose announce lists it.
* ``rr`` (request): the caller's own route id (SPEC §10.1 of telesthete). A
  worker sends its reply as ADDRESSED frames to ``rr`` so a routing hub
  delivers it to the caller only. Absent = broadcast CHANNEL reply on 0.
* ``accept`` (request): reply encodings the caller can take — ``"z"``
  (compressed payload), ``"drop"`` (a large reply pulled over telesthete
  Drop) and ``"drop-native"`` (Drop frames as native DROP outer frames).

Compression is applied to the WHOLE serialized message, so base64 blobs in a
result (screenshots, camera frames, file reads) shrink back to roughly their
binary size without any per-field encoding: base64 carries 6 bits per byte,
and deflate recovers most of the other two. A compressed payload is
:data:`MAGIC_Z` followed by a zlib stream; it can never be mistaken for JSON
(which starts with ``{``) or for the 1-byte keepalive, and peers that never
advertised ``z`` are never sent one. Receiving codecs inflate it right after
reassembly (:meth:`rook.worker.wire.codec.BandCodec.ingest`), so handlers only
ever see JSON. A binary sub-protocol payload (a pass-through Drop packet)
starts with the 16-byte band id instead and is told apart by that.

Stdlib only: ships in the worker bundle and the Android source set.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import zlib
from typing import NamedTuple, Optional

log = logging.getLogger("rook.worker.wire.envelope")

# Feature names (announce ``features``).
F_ROUTE = "route"              # declares route_id_for(worker_id); honours rr
F_DROP = "drop"                # serves large replies over Drop on accept "drop"
F_Z = "z"                      # takes compressed requests; compresses replies
F_DROP_NATIVE = "drop-native"  # takes native ChannelType.DROP frames
F_CAPS_HASH = "caps-hash"      # (clients) keep caps across hash-only announces

# Accept tokens (request ``accept``).
A_Z = "z"
A_DROP = "drop"
A_DROP_NATIVE = "drop-native"

MAGIC_Z = b"\x00rz"
Z_MIN_BYTES = 1024        # below this deflate rarely pays for itself
Z_MIN_GAIN = 0.10         # use the compressed form only if it saves >= 10%
# Inflate bound: a compressed message may not expand past this (zip bombs).
MAX_INFLATE_BYTES = 64 * 1024 * 1024

DEFAULT_DROP_THRESHOLD = 32 * 1024
DEFAULT_MAX_REPLY_BYTES = 32 * 1024 * 1024


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("ignoring %s=%r (not an integer)", name, raw)
        return default
    if value < minimum:
        log.warning("ignoring %s=%r (below %d)", name, raw, minimum)
        return default
    return value


def drop_threshold() -> int:
    """Serialized reply size above which a reply rides Drop when the caller
    accepts it (``ROOK_BAND_DROP_THRESHOLD`` bytes; 0 = never)."""
    return _env_int("ROOK_BAND_DROP_THRESHOLD", DEFAULT_DROP_THRESHOLD)


def max_reply_bytes() -> int:
    """Largest reply a worker sends (``ROOK_BAND_MAX_REPLY_BYTES``); a bigger
    result becomes an error reply instead of a message nobody can reassemble."""
    return _env_int("ROOK_BAND_MAX_REPLY_BYTES", DEFAULT_MAX_REPLY_BYTES, 1024)


def drop_native_enabled() -> bool:
    """``ROOK_BAND_DROP_NATIVE=0`` keeps Drop in the pass-through form even
    toward peers that announce ``drop-native``."""
    return os.environ.get("ROOK_BAND_DROP_NATIVE", "1").strip() != "0"


def routing_enabled() -> bool:
    """``ROOK_BAND_ROUTE=0`` turns addressed routing off (no declarations, no
    ``rr``, everything on channel 0) — the round-1 wire behaviour."""
    return os.environ.get("ROOK_BAND_ROUTE", "1").strip() != "0"


# -- compression ---------------------------------------------------------------

def compress(payload: bytes, min_bytes: int = Z_MIN_BYTES,
             min_gain: float = Z_MIN_GAIN) -> bytes:
    """``payload`` compressed (with :data:`MAGIC_Z`) when it is at least
    ``min_bytes`` and the result saves at least ``min_gain``; else unchanged."""
    if len(payload) < min_bytes:
        return payload
    z = MAGIC_Z + zlib.compress(payload, 6)
    return z if len(z) <= len(payload) * (1.0 - min_gain) else payload


def is_compressed(payload: bytes) -> bool:
    return payload[:len(MAGIC_Z)] == MAGIC_Z


def decompress(payload: bytes, limit: int = MAX_INFLATE_BYTES) -> Optional[bytes]:
    """Inverse of :func:`compress` for a :data:`MAGIC_Z` payload. ``None`` if it
    is corrupt or would inflate past ``limit``."""
    try:
        d = zlib.decompressobj()
        out = d.decompress(payload[len(MAGIC_Z):], limit)
        if d.unconsumed_tail or not d.eof:
            return None
        return out
    except zlib.error:
        return None


def decode(payload: bytes) -> bytes:
    """Undo compression if present; anything else is returned unchanged (a bad
    compressed payload becomes ``b""``, which every parser ignores)."""
    if is_compressed(payload):
        return decompress(payload) or b""
    return payload


# -- request-side helpers ---------------------------------------------------------

class ReplyOpts(NamedTuple):
    """How to send the reply to one request (from its ``rr`` and ``accept``)."""
    route: int = 0            # route id to ADDRESS the reply to; 0 = broadcast
    z: bool = False
    drop: bool = False
    drop_native: bool = False


NO_OPTS = ReplyOpts()

# The reply options of the request being dispatched (set by Worker._on_message
# for each request, read by Worker._reply and by caps that answer over a side
# channel, e.g. the in-band OTA receiver routing its Drop REQUESTs back).
current_reply_opts: contextvars.ContextVar[ReplyOpts] = contextvars.ContextVar(
    "rook_reply_opts", default=NO_OPTS)


def reply_opts(msg: dict) -> ReplyOpts:
    """Parse ``rr``/``accept`` from a request. Junk values mean "absent", so a
    malformed request still gets the old-style reply."""
    rr = msg.get("rr")
    route = rr if isinstance(rr, int) and not isinstance(rr, bool) and 0 < rr <= 0xFFFF else 0
    acc = msg.get("accept")
    tokens = {a for a in acc if isinstance(a, str)} if isinstance(acc, (list, tuple)) else set()
    return ReplyOpts(route=route, z=A_Z in tokens, drop=A_DROP in tokens,
                     drop_native=A_DROP_NATIVE in tokens)


def caps_hash(caps) -> str:
    """Short digest of a cap list (announce ``caps_hash``): order-insensitive,
    16 hex chars of SHA-256 over the sorted names joined by newlines."""
    names = sorted({c for c in caps if isinstance(c, str)})
    return hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()[:16]


def caps_hash_announces() -> bool:
    """``ROOK_ANNOUNCE_CAPS_HASH=1``: announces carry only ``caps_hash`` most
    of the time (full ``caps`` on change and every Nth). Off by default until
    every client of the band tolerates it (core spec 14.4)."""
    return os.environ.get("ROOK_ANNOUNCE_CAPS_HASH", "").strip() == "1"


def features_of(entry: Optional[dict]) -> frozenset:
    """Features a roster entry / announce advertised (empty for old nodes)."""
    feats = (entry or {}).get("features")
    if not isinstance(feats, (list, tuple)):
        return frozenset()
    return frozenset(f for f in feats[:64] if isinstance(f, str))


__all__ = [
    "F_ROUTE", "F_DROP", "F_Z", "F_DROP_NATIVE", "F_CAPS_HASH",
    "A_Z", "A_DROP", "A_DROP_NATIVE", "MAGIC_Z", "ReplyOpts", "NO_OPTS",
    "compress", "decompress", "decode", "is_compressed", "reply_opts",
    "features_of", "drop_threshold", "max_reply_bytes", "routing_enabled",
    "current_reply_opts", "caps_hash", "caps_hash_announces", "drop_native_enabled",
]
