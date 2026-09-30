"""Per-session MCP ``initialize`` instructions: server guidance + persona.

FastMCP builds a session's initialization options once, inside the task it
starts for the request that opens the session. That task inherits the
request's context, so a context variable set around the session manager's
``handle_request`` tells :func:`install`'s hook who is connecting:

* the bearer token -> ``TokenStore.principal_for`` -> user-scope ids
  (``agent_id``, label);
* the agent family from ``X-Rook-Client`` (explicit) or the User-Agent
  (``claude`` -> claude-code, ``codex``, ``hermes``); otherwise ``mcp``.

The persona plugin resolves and renders the persona (trimmed to
``MCP_BUDGET``) and :func:`guidance.compose_instructions` appends it to the
``server`` slot. Nothing assigned, no hub node, or any failure: the
instructions are exactly the server slot, as before.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any, Callable

log = logging.getLogger("rook.band_mcp.persona_connect")

_headers: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "rook_connect_headers", default=None)


def _header(headers: list | None, name: str) -> str:
    want = name.lower().encode()
    for k, v in headers or []:
        if k.lower() == want:
            try:
                return v.decode("latin-1").strip()
            except Exception:
                return ""
    return ""


def connect_identity(headers: list | None, store: Any) -> tuple[list[str], str]:
    """(user ids, family) for the connecting client. Never raises."""
    from ..hub.plugins.persona.model import family, family_from_client
    users: list[str] = []
    auth = _header(headers, "authorization")
    if auth.lower().startswith("bearer ") and store is not None:
        try:
            p = store.principal_for(auth[7:].strip())
        except Exception:
            p = None
        if p:
            users = [x for x in (p.get("agent_id"), p.get("label")) if x]
    fam = family(_header(headers, "x-rook-client")) or family_from_client(
        _header(headers, "user-agent")) or "mcp"
    return users, fam


def install(mcp, store: Any, plugin_getter: Callable[[], Any]) -> None:
    server = mcp._mcp_server
    base = server.create_initialization_options

    def create_initialization_options(*a, **k):
        opts = base(*a, **k)
        try:
            plugin = plugin_getter()
            if plugin is None:
                return opts
            users, fam = connect_identity(_headers.get(), store)
            text = plugin.instructions_for(users, fam)
            if text:
                from .guidance import compose_instructions
                opts = opts.model_copy(update={
                    "instructions": compose_instructions(opts.instructions, text)})
        except Exception:
            log.exception("persona for initialize failed; instructions unchanged")
        return opts

    server.create_initialization_options = create_initialization_options

    make_app = mcp.streamable_http_app

    def streamable_http_app(*a, **k):
        app = make_app(*a, **k)
        sm = mcp.session_manager
        handle = sm.handle_request
        if getattr(handle, "_rook_persona", False):
            return app

        async def handle_request(scope, receive, send):
            tok = _headers.set(list(scope.get("headers") or []))
            try:
                await handle(scope, receive, send)
            finally:
                _headers.reset(tok)
        handle_request._rook_persona = True
        sm.handle_request = handle_request
        return app

    mcp.streamable_http_app = streamable_http_app
