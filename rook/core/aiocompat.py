"""asyncio pieces newer than the oldest Python a worker still runs.

``pyproject.toml`` asks for 3.11, but the pyz bundle and the Raspberry Pi
installers never checked, and master's worker ran on 3.10. Worker-shipped
code (``rook.core``, ``rook.worker``) uses these instead of the 3.11 APIs so
such a worker keeps working; ``tests/test_py310_compat.py`` enforces it.

Stdlib only: it ships in the worker bundle.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

__all__ = ["timeout"]


class _Timeout:
    """``asyncio.timeout`` for 3.10: cancel the current task after ``delay``
    seconds and turn that cancellation into ``TimeoutError`` on the way out.

    Not ``asyncio.wait_for``: that runs the awaitable in a new task, so
    context variables it sets would not reach the caller and the two kinds of
    TimeoutError (ours, the handler's own) could not be told apart."""

    def __init__(self, delay: float | None) -> None:
        self._delay = delay
        self._expired = False
        self._handle: asyncio.TimerHandle | None = None
        self._task: asyncio.Task | None = None

    def expired(self) -> bool:
        return self._expired

    def _fire(self) -> None:
        self._expired = True
        if self._task is not None:
            self._task.cancel()

    async def __aenter__(self) -> "_Timeout":
        self._task = asyncio.current_task()
        if self._delay is not None:
            self._handle = asyncio.get_running_loop().call_later(self._delay, self._fire)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._handle is not None:
            self._handle.cancel()
        if self._expired and exc_type is asyncio.CancelledError:
            raise TimeoutError from exc
        return False


def _timeout_310(delay: float | None) -> _Timeout:
    return _Timeout(delay)


#: ``async with timeout(seconds) as window: ...``; ``window.expired()`` says
#: whether this timeout (rather than the body) raised the TimeoutError.
timeout = asyncio.timeout if sys.version_info >= (3, 11) else _timeout_310
