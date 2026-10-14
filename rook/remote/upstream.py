"""The dashboard's calls to the MCP side's HTTP APIs.

The account pages proxy ``/account/<x>/api`` to the MCP side's
``/<x>/account-api`` endpoints (knowledge_web.py and its subclasses,
jobs_web.py, token_web.py, work_web.py), and the dashboard masks secrets
through ``/internal/mask`` (mask_client.py). Every one of those calls goes
through :func:`request_json`:

* Two processes (``rook-dashboard`` + ``rook-mcp``): plain HTTP to the
  operator-configured URL (``ROOK_*_ADMIN_URL``, default
  ``http://127.0.0.1:8765/...``), as before.
* One process (``rook hub serve``, rook/hub/serve.py): the MCP app is in this
  process and :func:`register_local` maps its loopback origins to it, so the
  same request (method, path, query, cookies, headers, JSON body) is handed to
  the same handler in memory (rook.hub.asgi_bridge.call): identical session,
  operator and CSRF checks, no socket and no second HTTP server. A URL that
  points anywhere else (an operator override to another host) still goes over
  HTTP.
"""
from __future__ import annotations

import json as _json
import logging
from typing import Any, Callable
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger("rook.remote.upstream")
# origin ("http://127.0.0.1:8765") -> ASGI app served in this process
_local: dict[str, Any] = {}


class Unavailable(Exception):
    """The upstream could not be reached, timed out, or did not answer JSON."""


def _origin(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    except ValueError:
        return None
    if not parts.hostname or port is None:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}:{port}"


def register_local(origins, app) -> Callable[[], None]:
    """Serve requests to ``origins`` (``http://host:port``) from ``app`` in
    memory. Returns the function that undoes it."""
    keys = [o for o in (_origin(x) for x in origins) if o]
    for key in keys:
        _local[key] = app

    def unregister() -> None:
        for key in keys:
            if _local.get(key) is app:
                del _local[key]
    return unregister


def local_app(url: str):
    """The in-process app that serves ``url``, or None (go over HTTP)."""
    origin = _origin(url)
    return _local.get(origin) if origin else None


async def request_json(method: str, url: str, *, params=None, json: Any = None,
                       cookies: dict | None = None, headers: dict | None = None,
                       timeout: float = 20) -> tuple[int, Any]:
    """``(status, decoded JSON body)`` of one request; never follows
    redirects. ``json=None`` sends no body (as aiohttp's ``json=`` does).
    Raises :class:`Unavailable` when there is no JSON answer."""
    app = local_app(url)
    if app is not None:
        return await _in_process(app, method, url, params=params, json=json,
                                 cookies=cookies, headers=headers, timeout=timeout)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
            async with http.request(method, url, params=params, json=json, cookies=cookies,
                                    headers=headers, allow_redirects=False) as response:
                return response.status, await response.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        raise Unavailable(type(error).__name__) from error


async def _in_process(app, method, url, *, params, json, cookies, headers, timeout):
    import asyncio

    from yarl import URL

    from ..hub.asgi_bridge import call, cookie_header
    target = URL(url)
    if params:
        target = target.extend_query(params)
    hdrs: list[tuple[str, str]] = [(k, str(v)) for k, v in (headers or {}).items()]
    if cookies:
        hdrs.append(("Cookie", cookie_header(cookies)))
    body = b""
    if json is not None:
        body = _json.dumps(json).encode("utf-8")
        hdrs.append(("Content-Type", "application/json"))
    try:
        reply = await call(app, method, target.raw_path_qs, headers=hdrs, body=body,
                           host=target.raw_host or "127.0.0.1", port=target.port or 80,
                           scheme=target.scheme or "http", timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError) as error:
        raise Unavailable(type(error).__name__) from error
    except Exception as error:  # noqa: BLE001 — over HTTP this was a 500, i.e. unavailable
        log.exception("in-process %s %s failed", method, target.path)
        raise Unavailable(type(error).__name__) from error
    # aiohttp's response.json() accepts only application/json.
    if reply.content_type != "application/json":
        raise Unavailable(f"not JSON ({reply.content_type or 'no content type'})")
    try:
        return reply.status, _json.loads(reply.body.decode("utf-8"))
    except ValueError as error:
        raise Unavailable("bad JSON") from error
