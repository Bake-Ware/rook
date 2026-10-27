"""Paths hub plugins serve on the hub's own site (``rook hub serve``).

A hub plugin that has a web face of its own (the voice plugin's ``/voice/ws``
and browser client) mounts it here, and the single-process hub
(rook/hub/serve.py) hands matching requests to it before any dashboard
middleware runs, the same way it hands ``/mcp`` to the MCP app: the plugin
brings its own authentication (voice keys, not the dashboard login).

* :meth:`SiteMounts.mount` claims a prefix (``/voice`` serves ``/voice`` and
  everything under ``/voice/``);
* :meth:`SiteMounts.alias` claims one exact path (``/api/transcribe``), for
  clients that only know an older layout;
* :meth:`SiteMounts.unmount` drops everything one owner claimed (call it from
  the plugin's ``stop``, so a switched-off plugin stops answering at once).

Handlers are aiohttp handlers (``async (request) -> StreamResponse``); an
ASGI app goes through :class:`rook.hub.asgi_bridge.AsgiBridge`. The dashboard
keeps its own paths: a mount or alias under a reserved root is refused.

Mounts are served by ``rook hub serve`` only, which calls
:func:`enable_serving` before it builds the hub node, so a plugin can tell
(:func:`serving`) whether anything will hand it requests. In the older
two-process layout (``rook-dashboard`` + ``rook-mcp``) the hub node lives in
the MCP process, whose port a public proxy does not route these paths to;
there nothing serves the mounts, plugins that need them stay off and say
why, and the plugin's own standalone server (``python -m
services.voice.server``) keeps serving.

The single-process hub also lets a path the dashboard's router already
resolves win over a mount (defence in depth on top of :data:`RESERVED`), and
:meth:`SiteMounts.match` never matches a path with a ``.`` or ``..``
segment (aiohttp does not normalise them away).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable

log = logging.getLogger("rook.hub.site")

Handler = Callable[[Any], Awaitable[Any]]

#: Roots the dashboard and the MCP side serve; no plugin may claim them or
#: anything below them.
RESERVED = ("/account", "/auth", "/api/band", "/api/bands", "/api/chat", "/api/avatar",
            "/api/avatars", "/api/presence", "/api/policy", "/assets", "/login", "/logout",
            "/register", "/setup", "/install", "/mcp", "/band", "/tokens", "/authorize",
            "/token", "/.well-known", "/skill", "/healthz", "/health", "/internal",
            "/static", "/web", "/apk", "/apk.json", "/band-worker.pyz", "/band-worker.json",
            "/band-worker-enrollment.pyz", "/rook", "/rook.py", "/worker", "/enroll", "/hub",
            "/permissions", "/telesthete-hub", "/show")
_PATH = re.compile(r"^(/[A-Za-z0-9._~-]+)+$")

_SERVED = False


def enable_serving() -> None:
    """Say that this process hands requests to the mounts (``rook hub
    serve`` calls it before it builds the hub node and its plugins)."""
    global _SERVED
    _SERVED = True


def serving() -> bool:
    """Whether anything in this process serves the mounts. False in the
    two-process layout, where the hub node lives in ``rook-mcp``."""
    return _SERVED


def has_dot_segment(path: str) -> bool:
    return any(part in (".", "..") for part in (path or "").split("/"))


class SiteError(ValueError):
    pass


def _check(path: str) -> str:
    path = (path or "").rstrip("/")
    if not _PATH.match(path) or has_dot_segment(path):
        raise SiteError(f"not a mountable path: {path!r} (use /name[/more])")
    for root in RESERVED:
        if path == root or path.startswith(root + "/"):
            raise SiteError(f"{path} is the dashboard's ({root}); pick another path")
    return path


class SiteMounts:
    def __init__(self) -> None:
        self._prefixes: dict[str, tuple[str, Handler]] = {}
        self._exact: dict[str, tuple[str, Handler]] = {}

    def _claim(self, table: dict, owner: str, path: str, handler: Handler) -> str:
        path = _check(path)
        held = table.get(path)
        if held is not None and held[0] != owner:
            raise SiteError(f"{path} is already served by {held[0]}")
        table[path] = (owner, handler)
        log.info("hub site: %s serves %s", owner, path)
        return path

    def mount(self, owner: str, prefix: str, handler: Handler) -> str:
        """Serve ``prefix`` and everything below it with ``handler``."""
        return self._claim(self._prefixes, owner, prefix, handler)

    def alias(self, owner: str, path: str, handler: Handler) -> str:
        """Serve exactly ``path`` with ``handler``."""
        return self._claim(self._exact, owner, path, handler)

    def unmount(self, owner: str) -> int:
        n = 0
        for table in (self._prefixes, self._exact):
            for path in [p for p, (o, _) in table.items() if o == owner]:
                del table[path]
                n += 1
        if n:
            log.info("hub site: %s no longer serves its %d path(s)", owner, n)
        return n

    def match(self, path: str) -> Handler | None:
        if has_dot_segment(path):
            # /voice/../x is not under /voice: never hand it to a plugin.
            return None
        hit = self._exact.get(path)
        if hit is not None:
            return hit[1]
        best = None
        for prefix, (_, handler) in self._prefixes.items():
            if (path == prefix or path.startswith(prefix + "/")) and (
                    best is None or len(prefix) > len(best[0])):
                best = (prefix, handler)
        return best[1] if best else None

    def describe(self) -> list[dict]:
        rows = [{"path": p + "/*", "owner": o, "kind": "prefix"}
                for p, (o, _) in self._prefixes.items()]
        rows += [{"path": p, "owner": o, "kind": "alias"} for p, (o, _) in self._exact.items()]
        return sorted(rows, key=lambda r: r["path"])
