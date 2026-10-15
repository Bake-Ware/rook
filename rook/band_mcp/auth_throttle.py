"""Login throttle for the MCP side's token checks (rook.core.throttle).

:class:`ThrottleMiddleware` wraps the whole MCP ASGI app (outside the OAuth
front door). For the paths where a client presents a rook token (``/mcp``,
``/token``, ``/skill/*``, ``/healthz``) it

* answers 429 with ``Retry-After`` before the app runs when the client's
  address is locked out and the request carries a credential (an
  ``Authorization`` header, or any ``POST /token``), so the token is never
  checked; requests without one (OAuth discovery) pass;
* otherwise lets the token checks report their result through :func:`note`.
  The checks run deep inside the MCP SDK (its bearer backend calls
  ``TokenVerifier.verify_token(token)`` with no request), so the client's
  keys travel in a context variable set here.

Only those paths: other routes on the same app take dashboard cookies or the
dashboard's internal bearer tokens (``/jobs/account-api``,
``/internal/mask``), and the SDK's bearer backend tries every Bearer header
against the token store, so counting there would lock the dashboard out.

The throttle instance is the process-wide one, so in ``rook hub serve`` a bad
MCP token and a bad dashboard password from the same address add up.
"""

from __future__ import annotations

import contextvars
import json
from typing import Any

from ..core.throttle import LoginThrottle, client_keys, message, shared

_keys: contextvars.ContextVar = contextvars.ContextVar("rook_auth_throttle_keys", default=None)

GUARDED_PATHS = ("/mcp", "/token", "/healthz")
GUARDED_PREFIXES = ("/mcp/", "/skill/")


def guarded(path: str) -> bool:
    return path in GUARDED_PATHS or path.startswith(GUARDED_PREFIXES)


class _Pending:
    """One guarded request's throttle keys; the first verdict counts."""
    __slots__ = ("throttle", "keys", "done")

    def __init__(self, throttle: LoginThrottle, keys: tuple) -> None:
        self.throttle, self.keys, self.done = throttle, keys, False


def note(ok: bool, what: str = "MCP token") -> None:
    """Report a token check made for the current request. A no-op outside a
    request the middleware guards (background jobs, tests). Only the first
    report of a request counts: the SDK's bearer backend checks the header
    on every route, and a route that checks it again (``/healthz``,
    ``/skill/*``) must not count one bad token twice."""
    pending = _keys.get()
    if pending is None or pending.done:
        return
    pending.done = True
    if ok:
        pending.throttle.success(pending.keys)
    else:
        pending.throttle.failure(pending.keys, what)


class _Headers:
    """Case-insensitive lookup over ASGI raw headers (first value wins)."""

    def __init__(self, raw) -> None:
        self._h: dict[str, str] = {}
        for k, v in raw or ():
            name = k.decode("latin-1").lower()
            if name not in self._h:
                self._h[name] = v.decode("latin-1")

    def get(self, name: str, default: Any = None) -> Any:
        return self._h.get(name.lower(), default)


class ThrottleMiddleware:
    def __init__(self, app, throttle: LoginThrottle | None = None) -> None:
        self.app = app
        self._throttle = throttle

    @property
    def throttle(self) -> LoginThrottle:
        return self._throttle if self._throttle is not None else shared()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not guarded(scope.get("path", "")):
            return await self.app(scope, receive, send)
        throttle = self.throttle
        headers = _Headers(scope.get("headers"))
        client = scope.get("client")
        keys = client_keys(client[0] if client else None, headers)
        credential = bool(headers.get("authorization")) or (
            scope.get("path") == "/token" and scope.get("method") == "POST")
        if credential:
            wait = throttle.retry_after(keys)
            if wait:
                return await self._too_many(send, wait)
        token = _keys.set(_Pending(throttle, keys))
        try:
            return await self.app(scope, receive, send)
        finally:
            _keys.reset(token)

    @staticmethod
    async def _too_many(send, seconds: int) -> None:
        body = json.dumps({"error": "too_many_attempts",
                           "error_description": message(seconds)}).encode()
        await send({"type": "http.response.start", "status": 429, "headers": [
            (b"content-type", b"application/json"),
            (b"retry-after", str(seconds).encode()),
            (b"cache-control", b"no-store"),
            (b"content-length", str(len(body)).encode()),
        ]})
        await send({"type": "http.response.body", "body": body})
