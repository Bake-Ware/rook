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
    POST /plugins/push    {worker, plugin, why}  owners: sign the library's plugin for that worker and install it
    POST /doors/<name>    {open: bool}  owners: the kill switch (close or open a door; kept across restarts)
    GET  /health          no token
    *    /p/<plugin><path>  a hub plugin's ``routes:`` entry: ``auth: user`` takes the
                          bearer token, ``hmac``/``token`` a signature or token under the
                          route's secret (HubPlugins.route_allows), ``none`` anyone

Enrollment (spec 12; rook.core2.enroll.hub):

    POST /enroll/code     {}  a one-time code and its QR text, for a device to claim
    POST /enroll/claim    {code, name}       no token: the device's config bundle, once
    POST /enroll/device   {name}             no token: a device code to poll and a user code to show
    POST /enroll/poll     {device_code}      no token: pending, refused, or the bundle (once)
    GET  /enroll/pending  devices waiting for someone to approve their code
    POST /enroll/approve  {code, decision?}  approve (or refuse) a device's user code
    GET  /recovery-key    owners, in person: the recovery key
    POST /bands/invite    {role?, ceiling?}  owners, in person: an invite to this hub's band
    POST /memberships     {worker, why, invite? | band?, role?, ceiling?}  owners: a worker's other bands

Replies are JSON objects with ``ok``; a refused call is still 200 (the body
says why). Bind to localhost: put TLS in front before exposing it.
Options: ``bind: "127.0.0.1:8740"`` (port 0 picks one); ``web:`` the hub
site on ``/``, ``/login`` and ``/web/`` (:mod:`.web`; ``false`` turns it off,
``{secure_cookie: true}`` behind TLS).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

from rook.kit.ctx import Caller as KitCaller, KitError

from .web import Web

log = logging.getLogger("rook.hub.door.http")

MAX_BODY = 1 << 20
OPEN = {"/enroll/claim": "claim", "/enroll/device": "device_start", "/enroll/poll": "device_poll"}
WAIT = 900.0     # longest a request waits for the hub (dispatch has its own timeouts)


def _reject(token: str):
    raise ValueError(f"{token} is not JSON")


class HttpDoor:
    name = "http"
    bind = "127.0.0.1:8740"

    def __init__(self, hub, options: dict):
        self.hub = hub
        host, _, port = str(options.get("bind", self.bind)).rpartition(":")
        self.host, self.port = host or "127.0.0.1", int(port)
        self._server: ThreadingHTTPServer | None = None
        web = options.get("web", {} if self.name == "http" else False)    # the site is on the plain door only
        self.web = None if web is False else Web(hub, web if isinstance(web, dict) else {})
        self._thread: threading.Thread | None = None

    async def start(self) -> None:
        door, loop = self, asyncio.get_running_loop()

        class Handler(self.handler):
            pass
        Handler.door, Handler.loop = door, loop
        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="rook-http-door", daemon=True)
        self._thread.start()
        log.info("door open", extra={"fields": {"door": self.name, "bind": f"{self.host}:{self.port}"}})

    def challenge(self) -> str:
        """The ``WWW-Authenticate`` a 401 carries."""
        return "Bearer"

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
    # Headers and body go out in two writes: with Nagle on, the body waits for the
    # client's delayed ACK, ~40 ms on every request of a busy keep-alive connection.
    disable_nagle_algorithm = True
    timeout = 60          # seconds a socket may sit idle or half-sent: no thread held forever

    def log_message(self, fmt, *args):   # our logger, not stderr
        log.debug("request", extra={"fields": {"line": fmt % args}})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method: str) -> None:
        url = urlsplit(self.path)
        self._read = False
        try:
            if self.door.web is not None and self.door.web.owns(url.path):
                status, headers, data = self.door.web.handle(self, method, url.path, url.query)
            else:
                status, body = self._route(method)
                headers, data = _encode(body)
        except _Error as e:
            status, (headers, data) = e.status, _encode(e.body or {"ok": False, "error": e.message})
        except Exception:
            log.exception("door request failed")
            status, (headers, data) = 500, _encode({"ok": False, "error": "internal error"})
        sent = self.headers.get("Content-Length", "0") != "0" or "Transfer-Encoding" in self.headers
        if status >= 400 or (sent and not self._read):
            self.close_connection = True    # a body we didn't read must not become the next request
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        if status == 401 and "WWW-Authenticate" not in headers:
            self.send_header("WWW-Authenticate", self.door.challenge())
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
        if self.door.name in hub.dispatcher.closed_doors and not path.startswith("/doors/"):
            raise _Error(503, f"denied: door {self.door.name} closed")
        if method == "POST" and path in OPEN:      # a device enrolling has no token yet
            extra = {"address": self._address()} if OPEN[path] == "device_start" else {}
            return 200, self._run(getattr(hub.enroll, OPEN[path])(self._body(), **extra))
        if path.startswith("/p/"):
            return self._plugin_route(method, url)
        caller = self._run(self._auth())
        body = self._body() if method == "POST" else None
        if method == "POST" and path.startswith("/doors/") and path.count("/") == 2:
            # The switch itself: an owner reopens a door even while this one is closed.
            return 200, self._run(hub.set_door(caller, unquote(path[len("/doors/"):]), body.get("open")))
        refused = hub.door_refused(caller, self.door.name)
        if refused:
            raise _Error(503, refused)
        query = dict(parse_qsl(url.query))
        routes = {
            ("POST", "/call"): lambda: hub.call(caller, body, self.door.name),
            ("POST", "/session"): lambda: hub.start_session(caller, body),
            ("POST", "/session/end"): lambda: hub.end_session(caller, body.get("session")),
            ("GET", "/workers"): lambda: hub.workers_view(caller),
            ("GET", "/caps"): lambda: hub.caps_view(caller, query.get("worker")),
            ("GET", "/journal"): lambda: hub.journal_view(caller, query),
            ("GET", "/holds"): lambda: hub.holds_view(caller),
            ("POST", "/plugins/push"): lambda: hub.push_plugin(caller, body, self.door.name),
            ("POST", "/enroll/code"): lambda: hub.enroll.new_code(caller, self._base()),
            ("GET", "/enroll/pending"): lambda: hub.enroll.pending(caller),
            ("POST", "/enroll/approve"): lambda: hub.enroll.approve(caller, body),
            ("GET", "/recovery-key"): lambda: hub.enroll.recovery(caller),
            ("POST", "/bands/invite"): lambda: hub.enroll.invite(caller, body),
            ("POST", "/memberships"): lambda: hub.enroll.membership(caller, body, self.door.name),
        }
        if (method, path) in routes:
            return 200, self._run(routes[(method, path)]())
        if method == "POST" and path.startswith("/holds/") and path.count("/") == 2:
            # A person's own token: they are here (a hard stop may be answered).
            return 200, self._run(hub.decide(caller, unquote(path[len("/holds/"):]), body.get("decision"),
                                             present=True))
        if any(p == path for _, p in routes) or path in OPEN or path.startswith("/holds/"):
            raise _Error(405, "method not allowed")
        raise _Error(404, "not found")

    def _address(self) -> str:
        """Where a request came from, for a person to judge: the peer, and
        what a proxy in front says (X-Forwarded-For, as sent: a client
        without a proxy can write anything there)."""
        peer = self.client_address[0] if self.client_address else "?"
        fwd = self.headers.get("X-Forwarded-For", "").strip()
        return f"{peer} (forwarded for {fwd[:100]})" if fwd else peer

    def _base(self) -> str | None:
        """The address this request came in on, for a QR when the hub has no
        ``enroll.url`` (plain http: this door has no TLS of its own)."""
        host = self.headers.get("Host")
        return f"http://{host}" if host else None

    def _plugin_route(self, method: str, url):
        hub = self.door.hub
        plugin, _, rest = url.path[len("/p/"):].partition("/")
        path = "/" + rest.rstrip("/")
        route = hub.plugins.route(plugin, path)
        if route is None:
            raise _Error(404, "not found")
        if method not in route.methods:
            raise _Error(405, "method not allowed")
        if route.auth not in ("user", "none") and route.secret is None:
            raise _Error(501, f"this {route.auth} route names no secret")
        body = self._raw()
        headers = {k.lower(): v for k, v in self.headers.items()}
        if route.auth in ("hmac", "token") and not hub.plugins.route_allows(plugin, route, headers, body):
            raise _Error(401, "unauthorized")
        caller = None
        if route.auth == "user":
            who = self._run(self._auth())
            if (refused := hub.door_refused(who, self.door.name)):
                raise _Error(503, refused)
            caller = KitCaller(person=who.person, agent=who.agent, band=hub.band, why=f"{plugin} route {path}",
                               owner=hub.identities.is_owner(who.person))
        request = {"method": method, "path": path, "query": dict(parse_qsl(url.query)),
                   "headers": headers, "body": body}
        try:
            out = self._run(hub.plugins.serve(plugin, path, request, caller))
        except PermissionError as e:
            raise _Error(401, str(e) or "unauthorized") from None
        except KitError as e:
            raise _Error(400, e.message) from None
        return 200, out if isinstance(out, dict) else {"ok": True, "result": out}

    async def _auth(self):
        header = self.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        caller = self.door.hub.authenticate(token.strip()) if scheme.lower() == "bearer" else None
        if caller is None:
            raise _Error(401, "a valid bearer token is required")
        return caller

    def _raw(self) -> bytes:
        if self.command == "GET" and "Content-Length" not in self.headers:
            return b""
        self._read = True
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise _Error(411, "Content-Length required") from None
        if n < 0 or n > MAX_BODY:
            raise _Error(413, "body too large")
        return self.rfile.read(n)

    def _body(self) -> dict:
        raw = self._raw()
        try:
            body = json.loads(raw or b"{}", parse_constant=_reject)
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


def _encode(body: dict | None) -> tuple[dict, bytes]:
    if body is None:
        return {}, b""
    return {"Content-Type": "application/json"}, json.dumps(body, default=str).encode("utf-8")


class _Error(Exception):
    def __init__(self, status: int, message: str, body: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.body = status, message, body


HttpDoor.handler = _Handler
