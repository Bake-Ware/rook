"""Serve an ASGI application from aiohttp, in the same process and event loop.

The merged hub (``rook hub serve``, rook/hub/serve.py) runs the dashboard
(aiohttp) and the MCP server in one process. The MCP endpoint must stay on the
official ``mcp`` SDK, whose streamable HTTP transport and auth routes are ASGI
(Starlette) only, so the dashboard's aiohttp server hands MCP paths to the
FastMCP app through this adapter instead of a second HTTP server.

* :class:`AsgiBridge` is an aiohttp handler: one ASGI ``http`` call per
  request with the body streamed in and the response streamed out chunk by
  chunk (SSE and chunked replies reach the client as the app writes them),
  and one ASGI ``websocket`` session per upgrade (the ``/band`` bridge).
* :class:`Lifespan` runs the app's ASGI lifespan (the MCP session manager's
  task group lives inside it) for as long as the hub runs.
* :func:`call` makes one in-memory request to the app with no socket at all:
  the dashboard's proxies to the MCP side's ``*/account-api`` endpoints use it
  (rook/remote/upstream.py).

It mirrors what uvicorn did for the separate ``rook-mcp`` process where that
is visible to the app: the client address and scheme come from
``X-Forwarded-For`` / ``X-Forwarded-Proto`` only when the peer is a trusted
(loopback) proxy, like uvicorn's ``forwarded_allow_ips`` default, and the
WebSocket side pings idle peers and accepts the same 16 MiB messages.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from http.cookies import SimpleCookie
from typing import Any, Awaitable, Callable, Iterable

from aiohttp import WSMsgType, web
from multidict import CIMultiDict

log = logging.getLogger("rook.hub.asgi_bridge")

ASGIApp = Callable[[dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]],
                   Awaitable[None]]

_ASGI = {"version": "3.0", "spec_version": "2.4"}
TRUSTED_PROXIES = ("127.0.0.1", "::1")
# uvicorn's defaults for the websockets implementation the MCP process used.
WS_MAX_MESSAGE = 16 * 1024 * 1024
# aiohttp pings every ``heartbeat`` seconds and drops a peer whose pong does
# not come within half of it: 40 gives uvicorn's 20 s pong deadline.
WS_HEARTBEAT = 40.0
# Request bodies above this get 413 (the MCP paths face the network; uvicorn
# had no limit either, but nothing the MCP side takes comes near it).
MAX_BODY = 16 * 1024 * 1024
# Hop-by-hop headers are aiohttp's to write, never the app's.
_HOP_BY_HOP = frozenset({"transfer-encoding", "connection", "keep-alive"})


class LifespanError(RuntimeError):
    """The app reported ``lifespan.startup.failed`` (or ``shutdown.failed``)."""


class ClientDisconnected(OSError):
    """``send`` after the client went away (ASGI spec: a subclass of OSError)."""


# -- lifespan ---------------------------------------------------------------

class Lifespan:
    """The app's ASGI lifespan, started once and stopped once.

    Like uvicorn's ``lifespan="auto"``: an app that raises before it reads the
    startup event does not support lifespan and is served without it; a
    ``startup.failed`` is an error. The lifespan runs in its own task, so a
    task group the app opens there (the MCP session manager's) outlives any
    one request, as it does under uvicorn.
    """

    def __init__(self, app: ASGIApp, *, timeout: float = 30.0) -> None:
        self.app = app
        self.timeout = timeout
        self.state: dict = {}          # ASGI lifespan state, copied into each request scope
        self.supported: bool | None = None
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._startup: asyncio.Future | None = None
        self._shutdown: asyncio.Future | None = None
        self._received = False
        self._stopping = False

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("lifespan already started")
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._startup = loop.create_future()
        self._shutdown = loop.create_future()
        self._queue.put_nowait({"type": "lifespan.startup"})
        scope = {"type": "lifespan", "asgi": dict(_ASGI), "state": self.state}
        # A fresh context: nothing the starting code set leaks into the app.
        self._task = loop.create_task(self._run(scope), context=contextvars.Context())
        try:
            await asyncio.wait_for(asyncio.shield(self._startup), self.timeout)
        except BaseException:
            await self._abort()
            raise
        self.supported = self._startup.result()

    async def _run(self, scope: dict) -> None:
        try:
            await self.app(scope, self._receive, self._send)
        except BaseException as exc:  # noqa: BLE001 — reported through the futures
            if not self._received:
                log.info("ASGI lifespan unsupported by %r (%s); serving without it",
                         self.app, type(exc).__name__)
                self._resolve(self._startup, False)
            elif not self._startup.done():
                self._fail(self._startup, LifespanError(f"lifespan startup raised {exc!r}"))
            elif self._stopping and not self._shutdown.done():
                self._fail(self._shutdown, LifespanError(f"lifespan shutdown raised {exc!r}"))
            elif not isinstance(exc, asyncio.CancelledError) and self._startup.exception() is None:
                log.error("ASGI lifespan ended with %r while serving", exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self._resolve(self._startup, False)
            self._resolve(self._shutdown, True)

    async def _receive(self) -> dict:
        self._received = True
        return await self._queue.get()

    async def _send(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "lifespan.startup.complete":
            self._resolve(self._startup, True)
        elif kind == "lifespan.startup.failed":
            self._fail(self._startup, LifespanError(message.get("message") or "startup failed"))
        elif kind == "lifespan.shutdown.complete":
            self._resolve(self._shutdown, True)
        elif kind == "lifespan.shutdown.failed":
            self._fail(self._shutdown, LifespanError(message.get("message") or "shutdown failed"))

    @staticmethod
    def _resolve(fut, value) -> None:
        if fut is not None and not fut.done():
            fut.set_result(value)

    @staticmethod
    def _fail(fut, exc) -> None:
        if fut is not None and not fut.done():
            fut.set_exception(exc)

    async def stop(self) -> None:
        """Send ``lifespan.shutdown`` and wait (bounded) for the app to finish."""
        task, self._task = self._task, None
        if task is None:
            return
        if self.supported:
            self._stopping = True
            self._queue.put_nowait({"type": "lifespan.shutdown"})
            try:
                await asyncio.wait_for(asyncio.shield(self._shutdown), self.timeout)
            except asyncio.TimeoutError:
                log.warning("ASGI lifespan shutdown timed out after %.0fs", self.timeout)
            except LifespanError:
                log.exception("ASGI lifespan shutdown failed")
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _abort(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


# -- scope helpers -----------------------------------------------------------

def _raw_headers(request: web.BaseRequest) -> list[tuple[bytes, bytes]]:
    return [(k.lower(), v) for k, v in request.raw_headers]


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for k, v in headers:
        if k == name:
            return v.decode("latin-1")
    return None


def _forwarded_client(xff: str, trusted: Iterable[str]) -> str | None:
    """uvicorn's rule: the right-most address that is not a trusted proxy,
    else the left-most one."""
    hosts = [h.strip() for h in xff.split(",") if h.strip()]
    for host in reversed(hosts):
        if host not in trusted:
            return host
    return hosts[0] if hosts else None


def _scope(request: web.BaseRequest, kind: str, trusted: Iterable[str],
           state: dict | None, root_path: str) -> dict:
    transport = request.transport
    peer = transport.get_extra_info("peername") if transport is not None else None
    sock = transport.get_extra_info("sockname") if transport is not None else None
    client = (str(peer[0]), int(peer[1])) if isinstance(peer, tuple) and len(peer) >= 2 else None
    server = (str(sock[0]), int(sock[1])) if isinstance(sock, tuple) and len(sock) >= 2 else None
    headers = _raw_headers(request)
    scheme = request.scheme
    if client is not None and client[0] in trusted:
        proto = (_header(headers, b"x-forwarded-proto") or "").strip().lower()
        if proto in ("http", "https", "ws", "wss"):
            scheme = proto
        xff = _header(headers, b"x-forwarded-for")
        if xff:
            host = _forwarded_client(xff, trusted)
            if host:
                client = (host, 0)
    if kind == "websocket":
        scheme = {"http": "ws", "https": "wss"}.get(scheme, scheme)
    else:
        scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    raw_path = request.raw_path.split("?", 1)[0]
    scope = {
        "type": kind,
        "asgi": dict(_ASGI),
        "http_version": f"{request.version.major}.{request.version.minor}",
        "scheme": scheme,
        "path": request.path,
        "raw_path": raw_path.encode("latin-1", "replace"),
        "query_string": request.rel_url.raw_query_string.encode("latin-1", "replace"),
        "root_path": root_path,
        "headers": headers,
        "client": client,
        "server": server,
        "extensions": {},
    }
    if kind == "http":
        scope["method"] = request.method
    else:
        proto = _header(headers, b"sec-websocket-protocol") or ""
        scope["subprotocols"] = [p.strip() for p in proto.split(",") if p.strip()]
    if state is not None:
        scope["state"] = dict(state)
    return scope


def _is_websocket(request: web.BaseRequest) -> bool:
    return (request.method == "GET"
            and request.headers.get("Upgrade", "").lower() == "websocket"
            and "upgrade" in request.headers.get("Connection", "").lower())


# -- aiohttp handler ---------------------------------------------------------

class AsgiBridge:
    """aiohttp handler that serves ``app``. Use as ``bridge.handle`` in a
    route or call it from a middleware; ``lifespan`` (optional) supplies the
    lifespan state copied into every scope."""

    def __init__(self, app: ASGIApp, *, lifespan: Lifespan | None = None,
                 trusted_proxies: Iterable[str] = TRUSTED_PROXIES, root_path: str = "",
                 ws_max_message: int = WS_MAX_MESSAGE, ws_heartbeat: float | None = WS_HEARTBEAT,
                 disconnect_poll: float = 1.0, max_body: int | None = MAX_BODY) -> None:
        self.app = app
        self.max_body = max_body   # bytes; None = no limit
        # Set while the host shuts down: new requests get 503 instead of an
        # app whose lifespan (session manager) is going away.
        self.stopping = False
        self.lifespan = lifespan
        self.trusted = frozenset(trusted_proxies)
        self.root_path = root_path
        self.ws_max_message = ws_max_message
        self.ws_heartbeat = ws_heartbeat
        self.disconnect_poll = disconnect_poll

    async def __call__(self, request: web.Request) -> web.StreamResponse:
        return await self.handle(request)

    def _state(self) -> dict | None:
        return self.lifespan.state if self.lifespan is not None else None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if self.stopping:
            return web.Response(status=503, text="Service Unavailable: shutting down",
                                headers={"Retry-After": "5", "Connection": "close"})
        if _is_websocket(request):
            return await self._websocket(request)
        return await self._http(request)

    # -- http --

    async def _http(self, request: web.Request) -> web.StreamResponse:
        scope = _scope(request, "http", self.trusted, self._state(), self.root_path)
        done = asyncio.Event()           # response finished, or the client is gone
        st = {"body_done": False, "response": None, "started": False, "complete": False,
              "gone": False, "received": 0, "too_large": False}
        limit = self.max_body
        if limit is not None and (request.content_length or 0) > limit:
            return self._too_large(limit)

        def client_gone() -> bool:
            t = request.transport
            return t is None or t.is_closing()

        async def receive() -> dict:
            if not st["body_done"]:
                try:
                    chunk = await request.content.readany()
                except (ConnectionError, asyncio.IncompleteReadError, OSError):
                    st["body_done"] = st["gone"] = True
                    done.set()
                    return {"type": "http.disconnect"}
                st["received"] += len(chunk)
                if limit is not None and st["received"] > limit:
                    # A chunked body past the cap: the app sees the client
                    # leave, and the client gets 413 (below).
                    st["body_done"] = st["too_large"] = True
                    done.set()
                    return {"type": "http.disconnect"}
                more = bool(chunk) and not request.content.at_eof()
                if not more:
                    st["body_done"] = True
                return {"type": "http.request", "body": bytes(chunk), "more_body": more}
            # Body read: the next message is the disconnect, which ASGI apps
            # (SSE streams) wait for while they write. aiohttp does not cancel
            # the handler when the peer goes, so watch the transport.
            while not done.is_set():
                if client_gone():
                    st["gone"] = True
                    break
                try:
                    await asyncio.wait_for(done.wait(), self.disconnect_poll)
                except asyncio.TimeoutError:
                    pass
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            if st["too_large"]:
                return   # the client gets 413, whatever the app makes of the cut body
            kind = message.get("type")
            if kind == "http.response.start":
                if st["started"]:
                    raise RuntimeError("http.response.start sent twice")
                st["started"] = True
                status = int(message["status"])
                # No body on HEAD, 1xx, 204 or 304 (Starlette writes one for
                # HEAD; uvicorn dropped it, and so must we).
                st["no_body"] = (request.method == "HEAD" or status in (204, 304)
                                 or 100 <= status < 200)
                resp = web.StreamResponse(status=status)
                hdrs = CIMultiDict()
                for k, v in message.get("headers") or ():
                    name = k.decode("latin-1") if isinstance(k, bytes) else str(k)
                    if name.lower() in _HOP_BY_HOP:
                        continue
                    hdrs.add(name, v.decode("latin-1") if isinstance(v, bytes) else str(v))
                resp.headers.extend(hdrs)
                st["response"] = resp
            elif kind == "http.response.body":
                resp = st["response"]
                if resp is None:
                    raise RuntimeError("http.response.body before http.response.start")
                if st["complete"]:
                    return
                if st["gone"] or client_gone():
                    st["gone"] = True
                    done.set()
                    raise ClientDisconnected("client disconnected")
                try:
                    if not resp.prepared:
                        await resp.prepare(request)
                    body = message.get("body") or b""
                    if body and not st.get("no_body"):
                        await resp.write(body)
                    if not message.get("more_body", False):
                        await resp.write_eof()
                        st["complete"] = True
                        done.set()
                except (ConnectionError, RuntimeError) as exc:
                    # aiohttp: ClientConnectionResetError / "Cannot write to
                    # closing transport" once the peer has gone.
                    st["gone"] = True
                    done.set()
                    raise ClientDisconnected(str(exc)) from exc
            # http.response.trailers / unknown extensions: not advertised, ignored

        try:
            await self.app(scope, receive, send)
        except ClientDisconnected:
            pass
        except Exception:
            if st["too_large"]:
                return self._too_large(limit)
            if st["gone"]:
                # Most apps notice a vanished client as an error of their own.
                log.debug("ASGI app ended after the client left", exc_info=True)
                return st["response"] or web.Response(status=499)
            log.exception("ASGI app failed on %s %s", request.method, request.path)
            if not st["started"]:
                return web.Response(status=500, text="Internal Server Error")
            # Headers already went out: all that is left is to cut the stream.
            raise
        finally:
            done.set()
        resp = st["response"]
        if st["too_large"] and (resp is None or not resp.prepared):
            return self._too_large(limit)
        if resp is None:
            if st["gone"]:
                # Nothing to answer; aiohttp still wants a response object.
                return web.Response(status=499)
            log.error("ASGI app sent no response for %s %s", request.method, request.path)
            return web.Response(status=500, text="Internal Server Error")
        if not resp.prepared:
            try:
                await resp.prepare(request)
            except (ConnectionError, RuntimeError):
                return resp
        if not st["complete"] and not st["gone"]:
            try:
                await resp.write_eof()
            except (ConnectionError, RuntimeError):
                pass
        return resp

    @staticmethod
    def _too_large(limit: int) -> web.Response:
        # Close: the rest of the body is never read.
        return web.Response(status=413, text=f"Request body larger than {limit} bytes",
                            headers={"Connection": "close"})

    # -- websocket --

    async def _websocket(self, request: web.Request) -> web.StreamResponse:
        scope = _scope(request, "websocket", self.trusted, self._state(), self.root_path)
        st: dict[str, Any] = {"ws": None, "connected": False, "denied": None, "closed": False}
        accepted = asyncio.Event()

        async def receive() -> dict:
            if not st["connected"]:
                st["connected"] = True
                return {"type": "websocket.connect"}
            if st["ws"] is None:
                # Reading before accept: wait for the app to accept or refuse.
                await accepted.wait()
                if st["ws"] is None:
                    return {"type": "websocket.disconnect", "code": 1006}
            ws: web.WebSocketResponse = st["ws"]
            while True:
                msg = await ws.receive()
                if msg.type == WSMsgType.BINARY:
                    return {"type": "websocket.receive", "bytes": msg.data}
                if msg.type == WSMsgType.TEXT:
                    return {"type": "websocket.receive", "text": msg.data}
                if msg.type in (WSMsgType.PING, WSMsgType.PONG):
                    continue
                st["closed"] = True
                code = ws.close_code if ws.close_code is not None else 1005
                return {"type": "websocket.disconnect", "code": code}

        async def send(message: dict) -> None:
            kind = message.get("type")
            if kind == "websocket.accept":
                if st["ws"] is not None:
                    raise RuntimeError("websocket.accept sent twice")
                sub = message.get("subprotocol")
                ws = web.WebSocketResponse(protocols=(sub,) if sub else (),
                                           max_msg_size=self.ws_max_message,
                                           heartbeat=self.ws_heartbeat)
                for k, v in message.get("headers") or ():
                    ws.headers.add(k.decode("latin-1") if isinstance(k, bytes) else str(k),
                                   v.decode("latin-1") if isinstance(v, bytes) else str(v))
                await ws.prepare(request)
                st["ws"] = ws
                accepted.set()
            elif kind == "websocket.send":
                ws = st["ws"]
                if ws is None:
                    raise RuntimeError("websocket.send before websocket.accept")
                if ws.closed:
                    raise ClientDisconnected("websocket closed")
                try:
                    if message.get("bytes") is not None:
                        await ws.send_bytes(message["bytes"])
                    else:
                        await ws.send_str(message.get("text") or "")
                except (ConnectionError, RuntimeError) as exc:
                    raise ClientDisconnected(str(exc)) from exc
            elif kind == "websocket.close":
                ws = st["ws"]
                if ws is None:
                    # Closing before accept is a refusal: HTTP 403 (ASGI spec).
                    st["denied"] = web.Response(status=403)
                    accepted.set()
                    return
                st["closed"] = True
                if not ws.closed:
                    reason = message.get("reason") or ""
                    await ws.close(code=int(message.get("code") or 1000),
                                   message=reason.encode("utf-8"))

        try:
            await self.app(scope, receive, send)
        except ClientDisconnected:
            pass
        except Exception:
            log.exception("ASGI websocket app failed on %s", request.path)
        finally:
            accepted.set()
        ws = st["ws"]
        if ws is None:
            return st["denied"] or web.Response(status=403)
        if not ws.closed:
            await ws.close()
        return ws


# -- in-memory request ---------------------------------------------------------

class Reply:
    """What :func:`call` got back."""

    def __init__(self, status: int, headers: CIMultiDict, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def content_type(self) -> str:
        return self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()


def cookie_header(cookies: dict[str, str]) -> str:
    """``Cookie:`` value encoded as aiohttp's client encodes ``cookies=``."""
    jar = SimpleCookie()
    for k, v in cookies.items():
        jar[k] = v
    return "; ".join(f"{m.key}={m.coded_value}" for m in jar.values())


async def call(app: ASGIApp, method: str, path_qs: str, *,
               headers: Iterable[tuple[str, str]] = (), body: bytes = b"",
               host: str = "127.0.0.1", port: int = 80, scheme: str = "http",
               client: tuple[str, int] | None = ("127.0.0.1", 0),
               state: dict | None = None, timeout: float | None = None) -> Reply:
    """One HTTP request to ``app`` in memory: the same scope a loopback
    request to ``scheme://host:port/path_qs`` would get, run in a fresh
    context (no context variable of the caller leaks into the app) and
    cancelled after ``timeout`` seconds (``asyncio.TimeoutError``)."""
    path, _, query = path_qs.partition("?")
    from urllib.parse import unquote
    hdrs = [(k.lower().encode("latin-1"), str(v).encode("latin-1")) for k, v in headers]
    if not any(k == b"host" for k, _ in hdrs):
        default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        hdrs.insert(0, (b"host", (host if default else f"{host}:{port}").encode("latin-1")))
    if body and not any(k == b"content-length" for k, _ in hdrs):
        hdrs.append((b"content-length", str(len(body)).encode("ascii")))
    scope = {
        "type": "http", "asgi": dict(_ASGI), "http_version": "1.1", "method": method.upper(),
        "scheme": scheme, "path": unquote(path), "raw_path": path.encode("latin-1"),
        "query_string": query.encode("latin-1"), "root_path": "", "headers": hdrs,
        "client": client, "server": (host, port), "extensions": {},
    }
    if state is not None:
        scope["state"] = dict(state)
    sent = {"body": False}
    finished = asyncio.Event()
    out: dict[str, Any] = {"status": None, "headers": CIMultiDict(), "chunks": []}

    async def receive() -> dict:
        if not sent["body"]:
            sent["body"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        kind = message.get("type")
        if kind == "http.response.start":
            out["status"] = int(message["status"])
            for k, v in message.get("headers") or ():
                out["headers"].add(k.decode("latin-1") if isinstance(k, bytes) else str(k),
                                   v.decode("latin-1") if isinstance(v, bytes) else str(v))
        elif kind == "http.response.body":
            out["chunks"].append(bytes(message.get("body") or b""))
            if not message.get("more_body", False):
                finished.set()

    async def run() -> None:
        try:
            await app(scope, receive, send)
        finally:
            finished.set()

    task = asyncio.get_running_loop().create_task(run(), context=contextvars.Context())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    if out["status"] is None:
        raise RuntimeError("ASGI app sent no response")
    return Reply(out["status"], out["headers"], b"".join(out["chunks"]))
