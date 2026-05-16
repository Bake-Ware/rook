"""Flat dot-namespaced capability registry."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("rook.worker.registry")

Handler = Callable[..., Any]


class CapabilityRegistry:
    """Maps dot-namespaced names to handlers and dispatches calls.

    Handlers may be sync or async. Async results are awaited; sync results are
    returned as-is (wrapped in `asyncio.to_thread` so a blocking handler can't
    stall the event loop).
    """

    def __init__(self) -> None:
        self._caps: dict[str, Handler] = {}

    def register(self, dotpath: str, fn: Handler) -> None:
        if not dotpath:
            raise ValueError("capability dotpath cannot be empty")
        if dotpath in self._caps:
            raise ValueError(f"capability already registered: {dotpath}")
        self._caps[dotpath] = fn
        log.debug("registered capability %s", dotpath)

    def has(self, dotpath: str) -> bool:
        return dotpath in self._caps

    def list(self) -> list[str]:
        return sorted(self._caps.keys())

    async def call(self, dotpath: str, **kwargs: Any) -> Any:
        fn = self._caps.get(dotpath)
        if fn is None:
            raise KeyError(f"no such capability: {dotpath}")
        if inspect.iscoroutinefunction(fn):
            return await fn(**kwargs)
        return await asyncio.to_thread(fn, **kwargs)
