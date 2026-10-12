"""Jittered exponential backoff for reconnect and retry loops.

Stdlib only: it ships in the worker bundle (pyz, Android) like the rest of
``rook.core``. One helper so every reconnect loop behaves the same way —
doubling from ``base`` up to ``cap`` with random jitter, so a fleet that lost
its hub at the same moment doesn't come back in lockstep and stampede it.

    backoff = Backoff(base=1.0, cap=30.0)
    while running:
        try:
            await connect_and_serve()
        except Exception:
            pass
        await asyncio.sleep(backoff.next())

Call :meth:`Backoff.reset` once a connection has proved itself (stayed up for
a while), not merely on connect — a link that dies right after connecting
should keep backing off, not retry at ``base`` forever.
"""

from __future__ import annotations

import asyncio
import random
from typing import Callable, Optional


class Backoff:
    """Exponential delays with multiplicative jitter.

    The *n*-th :meth:`next` (from 0) returns ``min(cap, base * factor**n)``
    scaled by a random factor in ``[1 - jitter, 1]``. ``jitter=0`` gives the
    plain doubling sequence; the default 0.5 ("equal jitter") keeps every
    delay at least half the nominal one, so the curve still grows
    monotonically on average while peers spread out.
    """

    def __init__(self, base: float = 1.0, cap: float = 30.0, factor: float = 2.0,
                 jitter: float = 0.5,
                 rng: Optional[Callable[[], float]] = None) -> None:
        if base <= 0 or cap < base or factor < 1.0 or not 0.0 <= jitter <= 1.0:
            raise ValueError("need 0 < base <= cap, factor >= 1, 0 <= jitter <= 1")
        self.base = float(base)
        self.cap = float(cap)
        self.factor = float(factor)
        self.jitter = float(jitter)
        self._rng = rng or random.random
        self.attempt = 0

    def peek(self) -> float:
        """The nominal (un-jittered) delay the next :meth:`next` is based on."""
        # Bound the exponent so a long-running loop can't overflow the float.
        return min(self.cap, self.base * self.factor ** min(self.attempt, 64))

    def next(self) -> float:
        """The delay to wait before the next attempt; advances the curve."""
        nominal = self.peek()
        self.attempt += 1
        return nominal * (1.0 - self.jitter * self._rng())

    def reset(self) -> None:
        """Back to ``base`` (call after a connection has proved stable)."""
        self.attempt = 0

    async def sleep(self) -> float:
        """``await asyncio.sleep(self.next())``; returns the delay used."""
        delay = self.next()
        await asyncio.sleep(delay)
        return delay
