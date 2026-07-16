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

    def describe(self) -> dict:
        """Introspect every handler for the UI: for each cap, its docstring plus
        its parameters (name / required / default / type). Skips self and
        *args/**kwargs. Used by the dashboard to build accurate call forms."""
        def _jsonable(v):
            return v if isinstance(v, (str, int, float, bool)) or v is None else str(v)
        out: dict[str, Any] = {}
        for name, fn in self._caps.items():
            params = []
            try:
                sig = inspect.signature(fn)
                for p in sig.parameters.values():
                    if p.name == "self" or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                        continue
                    required = p.default is inspect.Parameter.empty
                    ann = p.annotation
                    typ = (None if ann is inspect.Parameter.empty
                           else getattr(ann, "__name__", None) or str(ann).replace("typing.", ""))
                    params.append({"name": p.name, "required": required,
                                   "default": None if required else _jsonable(p.default),
                                   "type": typ})
            except (ValueError, TypeError):
                pass
            doc = (inspect.getdoc(fn) or "").strip()
            out[name] = {"doc": doc.split("\n\n")[0].replace("\n", " ").strip(),
                         "params": params}
        return out

    async def call(self, dotpath: str, **kwargs: Any) -> Any:
        fn = self._caps.get(dotpath)
        if fn is None:
            raise KeyError(f"no such capability: {dotpath}")
        if inspect.iscoroutinefunction(fn):
            return await fn(**kwargs)
        return await asyncio.to_thread(fn, **kwargs)
