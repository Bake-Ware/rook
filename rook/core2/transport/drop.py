"""Pulling a large reply or a bundle over Drop with several windows in flight.

The Drop receiver Rook shares with 0.4 (``rook.worker.ota_drop``) keeps one
64-chunk window outstanding, so a pull pays a round trip per 64 KiB. Here
the receiver keeps WINDOWS windows requested ahead: one REQUEST may name
several ranges and every Drop sender serves each of them (telesthete SPEC
8.2). When the oldest window drains it asks for the next one; after
``rerequest`` without progress it asks again for every chunk still missing
below the frontier. Built on the 0.4 classes, which stay unchanged.

TODO(castling:transport): when ``rook.worker.ota_drop`` moves under here,
this becomes an option of its receiver.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Optional

from telesthete.protocol.drop import MAX_RANGE_CHUNKS, DropFrameType
from telesthete.protocol.sequence import SequenceSource

from rook.worker.ota_drop import (REPLY_REREQUEST, REPLY_TICK, OtaDropReceiver, _Receiver,
                                  _weak)

log = logging.getLogger("rook.core2.transport")

WINDOWS = 8          # 64-chunk windows in flight: 512 KiB


class _Windowed(_Receiver):
    def __init__(self, *a, windows: int = WINDOWS, **kw) -> None:
        super().__init__(*a, **kw)
        self.windows = max(1, int(windows))
        self._next = 0          # first chunk not yet requested

    def tick(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        if (self.offer is not None and self.verified is None
                and now - self._last_progress > (self.rerequest or 1.0)):
            self._last_progress = now
            self.request_missing(gaps=True)

    def request_missing(self, gaps: bool = False) -> None:
        if self.offer is None or self.sender_addr is None or self.verified is not None:
            return
        if self._next_missing_range() is None:      # advances _low
            return
        total, low = self.offer["total_chunks"], self._low
        limit = min(total, low + self.windows * MAX_RANGE_CHUNKS)
        start = max(self._next, low)
        ranges: list[tuple[int, int]] = []
        if gaps:            # stalled: everything still missing below the frontier
            run = None
            for i in range(low, start + 1):
                lacking = i < start and i not in self.have
                if lacking and run is None:
                    run = i
                elif not lacking and run is not None:
                    ranges += [(s, min(s + MAX_RANGE_CHUNKS, i)) for s in range(run, i, MAX_RANGE_CHUNKS)]
                    run = None
        ranges += [(s, min(s + MAX_RANGE_CHUNKS, limit)) for s in range(start, limit, MAX_RANGE_CHUNKS)]
        if not ranges:
            return
        self._next = max(self._next, limit)
        # The library calls us again once this (the oldest) window drains.
        self._outstanding = (low, min(low + MAX_RANGE_CHUNKS, total))
        self._send_json(self.sender_addr, DropFrameType.REQUEST, {"ranges": [list(r) for r in ranges]})


class WindowedPull(OtaDropReceiver):
    """An :class:`OtaDropReceiver` whose receiver keeps ``windows`` windows
    in flight. One OFFER per object (a pull makes a new one per attempt)."""

    def __init__(self, band_id: bytes, drop_id: int, send_coro: Callable[..., Awaitable[None]], *,
                 route: int = 0, rerequest: float = REPLY_REREQUEST, tick_secs: float = REPLY_TICK,
                 seq_source: Optional[SequenceSource] = None, windows: int = WINDOWS) -> None:
        super().__init__(band_id, drop_id, send_coro, route=route, rerequest=rerequest,
                         tick_secs=tick_secs, seq_source=seq_source)
        old = self._recv
        self._recv = _Windowed(band_id, drop_id, crypto=self._crypto, transport=self._shim,
                               seq_source=old._seq_source, rerequest=rerequest, windows=windows)
        self._recv.on_complete(_weak(self._on_complete))


async def pull(band_id: bytes, desc: tuple[int, int, str], send_coro: Callable[..., Awaitable[None]], *,
               route: int = 0, timeout: float, register: Callable[[int, Any], None],
               windows: int = WINDOWS) -> Optional[bytes]:
    """``rook.worker.ota_drop.pull_reply`` with windows in flight: the
    verified bytes, or None on timeout or a hash mismatch (pulled twice)."""
    did, size, sha = desc
    deadline = time.monotonic() + timeout
    seqs = SequenceSource()
    for _attempt in range(2):
        rx = WindowedPull(band_id, did, send_coro, route=route, seq_source=seqs, windows=windows)
        register(did, rx)
        try:
            rx.start()
            rx.accept_offer("reply", size, sha)
            if await rx.wait(max(deadline - time.monotonic(), 0.0)):
                return rx.data
            if rx.verified is None:
                return None   # timed out
            log.warning("drop %d failed its sha256 check; pulling again", did)
        finally:
            rx.stop()
            register(did, None)
    return None
