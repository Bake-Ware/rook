"""Per-dispatch call context.

The band envelope carries a caller ``identity`` (stamped from the bearer-token
name — see ``rook.band_mcp``). The capability registry only forwards the call
*args* to a handler, not envelope metadata, so identity would otherwise be
invisible to a cap. This contextvar bridges that gap: the plugin host (worker or hub) sets it
around each dispatch, and any capability that cares about who is calling —
memory write-ownership, chat attribution, later ACLs — reads it here.

``asyncio.to_thread`` (how the registry runs sync handlers) copies the current
context into the worker thread, so a value set before dispatch is visible to
both sync and async handlers.
"""

from __future__ import annotations

import contextvars

caller_identity: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "rook_caller_identity", default=None)


# Result of the worker's ticket check for the call being dispatched (see
# rook.worker.authz_guard): ``{"verified": bool, "reason", "kid", "p", ...}``
# or ``None`` when no check ran (local invocation, hub node). Caps that need a
# hub-signed order (hub/PSK repointing) read it.
call_ticket: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "rook_call_ticket", default=None)


def current_identity() -> str | None:
    """The identity of the agent/user behind the call currently being handled,
    or ``None`` if unknown (anonymous / local invocation)."""
    return caller_identity.get()
