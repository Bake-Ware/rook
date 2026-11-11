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
    POST /holds/<name>    {decision: approve | refuse}; a question (q_...): {answer}
    POST /plugins/push    {worker, plugin, why}  owners: sign the library's plugin for that worker and install it
    POST /doors/<name>    {open: bool}  owners: the kill switch (close or open a door; kept across restarts)
    GET  /health          no token
    GET  /updates/desktop.json  no token: the desktop app's files in Rook's latest release (its update check)
    GET  /apk.json[?channel=beta], /apk[?channel=beta]  no token: the phone app's update check and
                          download, from files an owner put in the data folder (:mod:`..apkfeed`)

Enrollment (spec 8; rook.core2.enroll.hub):

    POST /enroll/code     {}  a one-time code and its QR text, for a device to claim (also hub.join_code)
    POST /enroll/claim    {code, name}       no token: the device's config bundle, once
    POST /enroll/ask      {name}             no token: ask to join; a ticket to poll with
    POST /enroll/poll     {ticket}           no token: pending, refused, or the bundle (once)
                          (someone lets it in on the Workers page: hub.joins, hub.join)
    GET  /recovery-key    owners, in person: the recovery key

Replies are JSON objects with ``ok``; a refused call is still 200 (the body
says why). Bind to localhost: put TLS in front before exposing it.
Options: ``bind: "127.0.0.1:8740"`` (port 0 picks one); ``web:`` the hub
site on ``/``, ``/login`` and ``/web/`` (:mod:`.web`; ``false`` turns it off,
``{secure_cookie: true}`` behind TLS); ``band: "ws://127.0.0.1:7475/band"``
the relay's web socket listener: a web socket upgrade on ``/band`` is passed
straight through to it, so a worker whose UDP is blocked joins the band at
the hub's own address (the frames are sealed with the band key; this door
never reads them).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from ..apkfeed import ApkFeed
from .web import Web

log = logging.getLogger("rook.hub.door.http")

MAX_BODY = 1 << 20
OPEN = {"/enroll/claim": "claim", "/enroll/ask": "ask", "/enroll/poll": "poll"}
WAIT = 900.0     # longest a request waits for the hub (dispatch has its own timeouts)
CHUNK = 1 << 20  # a file goes out this much at a time


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
        self.band = options.get("band")
        self._apk: ApkFeed | None = None
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

    @property
    def apk(self) -> ApkFeed:
        if self._apk is None:
            self._apk = ApkFeed(self.hub.data)
        return self._apk

    def challenge(self, host: str | None = None) -> str:
        """The ``WWW-Authenticate`` a 401 carries (``host``: the request's Host header)."""
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
        if (self.door.band and urlsplit(self.path).path == "/band"
                and self.headers.get("Upgrade", "").lower() == "websocket"):
            return self._band()
        self._handle("GET")

    def _band(self) -> None:
        """Replay this upgrade to the relay's web socket, then copy bytes
        both ways until either side closes."""
        to = urlsplit(self.door.band)
        try:
            relay = socket.create_connection((to.hostname, to.port or 80), timeout=10)
        except OSError as e:
            log.warning("band relay unreachable", extra={"fields": {"relay": self.door.band, "error": str(e)}})
            self.send_error(502)
            return
        self.close_connection = True
        relay.settimeout(None)
        self.connection.settimeout(None)
        head = [f"GET {to.path or '/band'} HTTP/1.1", *(f"{k}: {v}" for k, v in self.headers.items())]
        relay.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))

        def back():
            try:
                while data := relay.recv(1 << 16):
                    self.connection.sendall(data)
            except OSError:
                pass
            _shut(self.connection)
        threading.Thread(target=back, name="rook-band-door", daemon=True).start()
        try:
            while data := self.rfile.read1(1 << 16):
                relay.sendall(data)
        except OSError:
            pass
        _shut(relay)
        relay.close()

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method: str) -> None:
        url = urlsplit(self.path)
        self._read = False
        try:
            if self.door.web is not None and self.door.web.owns(url.path):
                status, headers, data = self.door.web.handle(self, method, url.path, url.query)
            else:
                status, body, *extra = self._route(method)     # (status, body[, headers])
                if isinstance(body, _File):
                    return self._send_file(body)
                headers, data = _encode(body)
                headers.update(*extra)
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
        if status == 401:
            self.send_header("WWW-Authenticate", self.door.challenge(self.headers.get("Host")))
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
        if method == "GET" and path == "/updates/desktop.json":     # the desktop app checks before sign-in
            from ..updates import desktop_feed
            return 200, desktop_feed()
        if method == "GET" and path in ("/apk.json", "/apk"):      # the phone app checks before sign-in
            got = self.door.apk.pick(dict(parse_qsl(url.query)).get("channel"))
            if got is None:
                raise _Error(404, "no app update here")
            if path == "/apk.json":
                return 200, got[1]
            return 200, _File(got[0], "application/vnd.android.package-archive", "rook-worker.apk")
        if method == "POST" and path in OPEN:      # a device enrolling has no token yet
            extra = {"address": self._address()} if OPEN[path] == "ask" else {}
            return 200, self._run(getattr(hub.enroll, OPEN[path])(self._body(), **extra))
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
            ("GET", "/recovery-key"): lambda: hub.enroll.recovery(caller),
        }
        if (method, path) in routes:
            return 200, self._run(routes[(method, path)]())
        if method == "POST" and path.startswith("/holds/") and path.count("/") == 2:
            # A person's own token: they are here (a hard stop may be answered).
            return 200, self._run(hub.decide(caller, unquote(path[len("/holds/"):]), body.get("decision"),
                                             present=True, answer=body.get("answer")))
        if any(p == path for _, p in routes) or path in OPEN or path.startswith("/holds/"):
            raise _Error(405, "method not allowed")
        raise _Error(404, "not found")

    def _send_file(self, f: _File) -> None:
        try:
            src = f.path.open("rb")
            size = f.path.stat().st_size
        except OSError:
            log.exception("file not readable")
            data = b'{"ok": false, "error": "internal error"}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        with src:
            self.send_response(200)
            self.send_header("Content-Type", f.type)
            self.send_header("Content-Disposition", f"attachment; filename={f.name}")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            left = size
            try:
                while left > 0:
                    chunk = src.read(min(CHUNK, left))
                    if not chunk:        # the file shrank under us: the client sees a short body
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except OSError:              # the phone went away mid-download
                pass
            if left:
                self.close_connection = True

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


def _shut(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _encode(body: dict | bytes | None) -> tuple[dict, bytes]:
    if body is None:
        return {}, b""
    if isinstance(body, bytes):          # a page (OAuth sign-in); its Content-Type comes with it
        return {}, body
    return {"Content-Type": "application/json"}, json.dumps(body, default=str).encode("utf-8")


class _File:
    """A file a route answers with: streamed, not read into memory."""

    def __init__(self, path: Path, type: str, name: str):
        self.path, self.type, self.name = path, type, name


class _Error(Exception):
    def __init__(self, status: int, message: str, body: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.body = status, message, body


HttpDoor.handler = _Handler
