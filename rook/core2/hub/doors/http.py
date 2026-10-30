"""A local HTTP JSON door. Every request but ``/health`` carries
``Authorization: Bearer <token>``; the token says who calls (a person, an
agent, or an agent for a person).

    POST /call            {worker, cap, args?, why, session?, appliance?, elevate?}
    POST /session         {caps, project?, task?}  -> {session, exp}
    POST /session/end     {session}
    GET  /workers         the registry, online or not
    GET  /caps[?worker=]  what each worker serves, from its manifests
    GET  /journal[?cap=&worker=&project=&kind=&session=&since=&until=&limit=]
    GET  /holds           calls waiting for a person
    POST /holds/<name>    {decision: approve | refuse}
    GET  /health          no token

Replies are JSON objects with ``ok``; a refused call is still 200 (the body
says why). Bind to localhost: put TLS in front before exposing it.
Options: ``bind: "127.0.0.1:8740"`` (port 0 picks one).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

log = logging.getLogger("rook.hub.door.http")

MAX_BODY = 1 << 20
WAIT = 900.0     # longest a request waits for the hub (dispatch has its own timeouts)


def _reject(token: str):
    raise ValueError(f"{token} is not JSON")


class HttpDoor:
    name = "http"

    def __init__(self, hub, options: dict):
        self.hub = hub
        host, _, port = str(options.get("bind", "127.0.0.1:8740")).rpartition(":")
        self.host, self.port = host or "127.0.0.1", int(port)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    async def start(self) -> None:
        door, loop = self, asyncio.get_running_loop()

        class Handler(_Handler):
            pass
        Handler.door, Handler.loop = door, loop
        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="rook-http-door", daemon=True)
        self._thread.start()
        log.info("door open", extra={"fields": {"door": self.name, "bind": f"{self.host}:{self.port}"}})

    async def stop(self) -> None:
        if self._server is not None:
            await asyncio.to_thread(self._server.shutdown)
            self._server.server_close()
            self._server = None
        log.info("door closed", extra={"fields": {"door": self.name}})


class _Handler(BaseHTTPRequestHandler):
    door: HttpDoor
    loop: asyncio.AbstractEventLoop
    protocol_version = "HTTP/1.1"
    server_version = "rook-hub"

    def log_message(self, fmt, *args):   # our logger, not stderr
        log.debug("request", extra={"fields": {"line": fmt % args}})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method: str) -> None:
        try:
            status, body = self._route(method)
        except _Error as e:
            status, body = e.status, {"ok": False, "error": e.message}
        except Exception:
            log.exception("door request failed")
            status, body = 500, {"ok": False, "error": "internal error"}
        data = json.dumps(body, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _route(self, method: str):
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        hub = self.door.hub
        if method == "GET" and path == "/health":
            return 200, {"ok": True, "hub": hub.hub_id}
        body = self._body() if method == "POST" else None
        caller = self._run(self._auth())
        query = dict(parse_qsl(url.query))
        routes = {
            ("POST", "/call"): lambda: hub.call(caller, body, self.door.name),
            ("POST", "/session"): lambda: hub.start_session(caller, body),
            ("POST", "/session/end"): lambda: hub.end_session(caller, body.get("session")),
            ("GET", "/workers"): lambda: hub.workers_view(caller),
            ("GET", "/caps"): lambda: hub.caps_view(caller, query.get("worker")),
            ("GET", "/journal"): lambda: hub.journal_view(caller, query),
            ("GET", "/holds"): lambda: hub.holds_view(caller),
        }
        if (method, path) in routes:
            return 200, self._run(routes[(method, path)]())
        if method == "POST" and path.startswith("/holds/") and path.count("/") == 2:
            return 200, self._run(hub.decide(caller, unquote(path[len("/holds/"):]), body.get("decision")))
        if any(p == path for _, p in routes) or path.startswith("/holds/"):
            raise _Error(405, "method not allowed")
        raise _Error(404, "not found")

    async def _auth(self):
        header = self.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        caller = self.door.hub.authenticate(token.strip()) if scheme.lower() == "bearer" else None
        if caller is None:
            raise _Error(401, "a valid bearer token is required")
        return caller

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise _Error(411, "Content-Length required") from None
        if n < 0 or n > MAX_BODY:
            raise _Error(413, "body too large")
        try:
            body = json.loads(self.rfile.read(n) or b"{}", parse_constant=_reject)
        except (ValueError, UnicodeDecodeError) as e:
            raise _Error(400, f"bad JSON: {e}") from None
        if not isinstance(body, dict):
            raise _Error(400, "the body must be a JSON object")
        return body

    def _run(self, coro):
        """Run a door API coroutine on the hub's loop and wait for it."""
        if self.loop.is_closed() or self.door._server is None:
            coro.close()
            raise _Error(503, "hub unavailable")
        try:
            return asyncio.run_coroutine_threadsafe(coro, self.loop).result(WAIT)
        except (concurrent.futures.CancelledError, TimeoutError):
            raise _Error(503, "hub unavailable") from None


class _Error(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message
