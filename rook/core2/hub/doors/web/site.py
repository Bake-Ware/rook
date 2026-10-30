"""The hub site's routes, sessions and page tokens (see ``__init__``). Runs in
the HTTP door's request threads; hub work goes to the hub's loop through the
handler's ``_run``."""
from __future__ import annotations

import collections
import hashlib
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, unquote

from rook.core2 import rights
from rook.core2.hub.identity import NONE, Caller
from .caps import _read_json, _write

log = logging.getLogger("rook.hub.door.web")

STATIC = Path(__file__).with_name("static")
COOKIE = "rook_web"
SESSION_SECS = 30 * 86400
TOKEN_SECS = 12 * 3600
FRAME_SECS = 60
TOKENS_PER_SESSION = 32
POLL_SECS = 20.0
EVENTS_KEPT = 500
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}

SHELL_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-src 'self'; "
             "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
# A page is its own opaque origin: no cookies, no network, nothing of the shell's.
PAGE_CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; form-action 'none'; "
            "base-uri 'none'; frame-ancestors 'self'")


@dataclass
class Session:
    person: str
    csrf: str
    exp: float
    tokens: list = field(default_factory=list)     # page tokens, oldest first


@dataclass
class PageToken:
    session: str              # web session digest
    page: str
    caps: list
    events: list
    author: list | None       # [[layer, rights]] for an agent-made page
    dispatch: str             # the dispatch session its calls belong to
    exp: float


class Web:
    def __init__(self, hub, options: dict | None = None):
        options = options or {}
        self.hub = hub
        self.secure = bool(options.get("secure_cookie"))
        self.poll_secs = POLL_SECS
        self._lock = threading.Lock()
        self._path = Path(hub.data) / "web-sessions.json"
        self.sessions: dict[str, Session] = {}
        now = time.time()
        for d, s in _read_json(self._path).items():
            if isinstance(s, dict) and s.get("exp", 0) > now and isinstance(s.get("person"), str):
                self.sessions[d] = Session(s["person"], s.get("csrf") or secrets.token_urlsafe(24), s["exp"])
        self.tokens: dict[str, PageToken] = {}
        self.frames: dict[str, tuple[str, float]] = {}       # nonce -> (page token, exp)
        self._events: collections.deque = collections.deque(maxlen=EVENTS_KEPT)
        self._seq = 0
        self._cond = threading.Condition()
        hub.listeners.append(self._on_event)

    # -- routing ----------------------------------------------------------------

    @staticmethod
    def owns(path: str) -> bool:
        return path in ("/", "/login", "/logout") or path.startswith("/web/")

    def handle(self, req, method: str, path: str, query: str) -> tuple[int, dict, bytes]:
        """One request: (status, headers, body)."""
        try:
            return self._route(req, method, path, dict(parse_qsl(query)))
        except _Error as e:
            return _json(e.status, {"ok": False, "error": e.message})

    def _route(self, req, method, path, query):
        if method == "GET" and path.startswith("/web/static/"):
            return self._static(path[len("/web/static/"):])
        if path == "/login":
            if method == "POST":
                return self._login(req)
            return self._file("login.html", csp=SHELL_CSP)
        sid, session = self._session(req)
        if method == "GET" and path == "/":
            if session is None:
                return 303, {"Location": "/login"}, b""
            return self._file("shell.html", csp=SHELL_CSP)
        if method == "GET" and path.startswith("/web/frame/"):
            return self._frame(sid, session, unquote(path[len("/web/frame/"):]))
        if session is None:
            raise _Error(401, "sign in first")
        if method == "GET" and path == "/web/api/me":
            return _json(200, {"ok": True, "person": session.person, "csrf": session.csrf})
        if method == "GET" and path == "/web/api/events":
            return _json(200, self._poll(sid, query.get("token", ""), query.get("since")))
        if method != "POST":
            raise _Error(404, "not found")
        if not secrets.compare_digest(req.headers.get("X-Rook-CSRF", ""), session.csrf):
            raise _Error(403, "missing or wrong X-Rook-CSRF")
        if path == "/logout":
            with self._lock:
                self._drop(sid)
            return 200, {"Set-Cookie": self._cookie("", 0), **_JSON}, b'{"ok": true}'
        body = req._body()
        caller = Caller(person=session.person)
        if path == "/web/api/call":
            cap = body.get("cap")
            if not (isinstance(cap, str) and cap.startswith("pages.")):
                raise _Error(403, "the shell calls pages.* only")
            return _json(200, req._run(self.hub.call(caller, {
                "worker": "rook", "cap": cap, "args": body.get("args") or {},
                "why": body.get("why") or "Using the hub site.",
                "session": req._run(self._shell_session(caller))}, "web")))
        if path == "/web/api/open":
            return _json(200, self._open(req, sid, caller, body.get("page")))
        if path == "/web/api/page":
            return _json(200, self._page_call(req, sid, caller, body))
        raise _Error(404, "not found")

    # -- sign-in ----------------------------------------------------------------

    def _login(self, req):
        ctype = req.headers.get("Content-Type", "")
        if ctype.startswith("application/x-www-form-urlencoded"):
            n = int(req.headers.get("Content-Length") or 0)
            if n > 4096:
                raise _Error(413, "body too large")
            token = dict(parse_qsl(req.rfile.read(n).decode("utf-8", "replace"))).get("token", "")
            form = True
        else:
            token, form = req._body().get("token", ""), False
        caller = req._run(_auth(self.hub, str(token).strip()))
        if caller is None or caller.agent or not caller.person:
            log.info("sign-in refused")
            if form:
                return 303, {"Location": "/login?failed=1"}, b""
            raise _Error(401, "that is not a person's token")
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self.sessions[_digest(sid)] = Session(caller.person, secrets.token_urlsafe(24), time.time() + SESSION_SECS)
            self._save()
        log.info("signed in", extra={"fields": {"person": caller.person}})
        headers = {"Set-Cookie": self._cookie(sid, SESSION_SECS)}
        if form:
            return 303, {**headers, "Location": "/"}, b""
        return 200, {**headers, **_JSON}, b'{"ok": true}'

    def _session(self, req) -> tuple[str | None, Session | None]:
        sid = _cookie_value(req.headers.get("Cookie", ""), COOKIE)
        if not sid:
            return None, None
        d = _digest(sid)
        with self._lock:
            s = self.sessions.get(d)
            if s is None:
                return None, None
            if s.exp < time.time() or self.hub.identities.user(s.person) is None:
                self._drop(d)
                return None, None
        return d, s

    def _drop(self, d: str) -> None:
        s = self.sessions.pop(d, None)
        if s is not None:
            for t in s.tokens:
                self.tokens.pop(t, None)
            self._save()

    def _save(self) -> None:
        _write(self._path, json.dumps({d: {"person": s.person, "csrf": s.csrf, "exp": s.exp}
                                       for d, s in self.sessions.items()}))

    def _cookie(self, value: str, max_age: int) -> str:
        return (f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict"
                + ("; Secure" if self.secure else ""))

    # -- pages --------------------------------------------------------------------

    def _open(self, req, sid: str, caller: Caller, pid) -> dict:
        """``pages.view`` as the person (rights, journal); on success a page
        token for the shell and a one-time frame URL."""
        if not isinstance(pid, str) or not pid:
            raise _Error(400, "page is required")
        reply = req._run(self.hub.call(caller, {
            "worker": "rook", "cap": "pages.view", "args": {"page": pid}, "why": f"Opening the page {pid}.",
            "session": req._run(self._shell_session(caller))}, "web"))
        if not reply.get("ok"):
            return reply
        page = self.hub.pages.openable(pid)
        author = None
        if page.made:
            who = Caller(page.made.get("person"), page.made.get("agent"))
            author = [[f"author {k}", r] for k, r in self.hub.identities.layers(who)] or [["author", NONE]]
        token, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        dsid = req._run(self._page_session(caller))
        now = time.time()
        with self._lock:
            s = self.sessions.get(sid)
            if s is None:
                raise _Error(401, "sign in first")
            self.tokens[token] = PageToken(sid, pid, list(page.caps), list(page.events), author, dsid, now + TOKEN_SECS)
            s.tokens.append(token)
            while len(s.tokens) > TOKENS_PER_SESSION:
                self.tokens.pop(s.tokens.pop(0), None)
            self.frames = {n: f for n, f in self.frames.items() if f[1] > now}
            self.frames[nonce] = (token, now + FRAME_SECS)
        return {"ok": True, "token": token, "frame": f"/web/frame/{nonce}", "page": reply["result"]}

    def _token(self, sid: str, token) -> PageToken:
        with self._lock:
            t = self.tokens.get(token) if isinstance(token, str) else None
        if t is None or t.session != sid or t.exp < time.time():
            raise _Error(403, "unknown page token")
        return t

    def _frame(self, sid, session, nonce: str):
        with self._lock:
            token, exp = self.frames.pop(nonce, (None, 0))
        if session is None or token is None or exp < time.time():
            return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"This page link has been used or expired."
        t = self._token(sid, token)
        try:
            page = self.hub.pages.openable(t.page)
            doc = self.hub.pages.html(page)
        except Exception as e:
            raise _Error(404, str(getattr(e, "message", e))) from None
        info = {"page": page.id, "title": page.title, "caps": t.caps, "events": t.events}
        return 200, {"Content-Type": TYPES[".html"], "Content-Security-Policy": PAGE_CSP,
                     "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}, \
            inject(doc, info).encode("utf-8")

    def _page_call(self, req, sid: str, caller: Caller, body: dict) -> dict:
        t = self._token(sid, body.get("token"))
        try:
            self.hub.pages.openable(t.page)
        except Exception as e:
            return {"ok": False, "error": f"denied: page: {getattr(e, 'message', e)}"}
        cap = body.get("cap")
        if not isinstance(cap, str) or not cap:
            raise _Error(400, "cap is required")
        worker = body.get("worker") if isinstance(body.get("worker"), str) else None
        if worker is None:
            worker = req._run(_target(self.hub, cap)) or "rook"
        why = body.get("why") if isinstance(body.get("why"), str) and body["why"].strip() else \
            f"Shown on the {t.page} page."
        layers = [["page", {"grants": t.caps}], *(t.author or [])]
        return req._run(self.hub.call(caller, {"worker": worker, "cap": cap, "args": body.get("args"), "why": why,
                                               "session": t.dispatch}, "web", layers=layers))

    async def _shell_session(self, caller: Caller) -> str:
        """The shell's dispatch session: pages.* only."""
        return self.hub._session(self.hub._door_sessions, ("web", caller.person), caller, ["pages.*"])

    async def _page_session(self, caller: Caller) -> str:
        """One dispatch session per opened page (its calls group in the
        journal); the page's caps ride as their own layer."""
        return self.hub.dispatcher.start_session(caller, ["*"]).id

    # -- events -------------------------------------------------------------------

    def _on_event(self, event: str, payload: dict, source: str | None) -> None:
        with self._cond:
            self._seq += 1
            self._events.append((self._seq, event, json.loads(json.dumps(payload, default=str))))
            self._cond.notify_all()

    def _poll(self, sid: str, token: str, since) -> dict:
        t = self._token(sid, token)
        try:
            since = int(since) if since not in (None, "") else None
        except ValueError:
            raise _Error(400, "since is a number") from None
        person = self.sessions[sid].person if sid in self.sessions else None
        layers = [["person", self.hub.identities.person_rights(person) if person else NONE],
                  ["page", {"grants": t.events}], *(t.author or [])]

        def visible():
            return [{"seq": s, "event": e, "payload": p} for s, e, p in self._events
                    if s > since and rights.check(layers, {"cap": e, "risk": "read", "effects": []})["ok"]]
        deadline = time.monotonic() + self.poll_secs
        with self._cond:
            if since is None:          # first poll: where the stream is now
                return {"ok": True, "seq": self._seq, "events": []}
            while True:
                got = visible() if t.events else []
                left = deadline - time.monotonic()
                if got or left <= 0:
                    return {"ok": True, "seq": self._seq, "events": got}
                self._cond.wait(left)

    # -- files --------------------------------------------------------------------

    def _static(self, name: str):
        if not re.fullmatch(r"[a-z0-9-]+\.(js|css|svg)", name):
            raise _Error(404, "not found")
        return self._file(name)

    def _file(self, name: str, csp: str | None = None):
        path = STATIC / name
        if not path.is_file():
            raise _Error(404, "not found")
        headers = {"Content-Type": TYPES[path.suffix], "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "same-origin"}
        if csp:
            headers.update({"Content-Security-Policy": csp, "X-Frame-Options": "DENY"})
        return 200, headers, path.read_bytes()


def inject(doc: str, info: dict) -> str:
    """The page with the bridge and the base style ahead of its own."""
    data = json.dumps(info).replace("<", "\\u003c")
    head = (f"<script>window.__rookPage={data};\n{_asset('bridge.js')}</script>\n"
            f"<style>{_asset('page.css')}</style>\n")
    m = re.search(r"<head\b[^>]*>", doc, re.I)
    if m:
        return doc[:m.end()] + "\n" + head + doc[m.end():]
    m = re.search(r"<html\b[^>]*>", doc, re.I)
    if m:
        return doc[:m.end()] + "<head>" + head + "</head>" + doc[m.end():]
    return "<!doctype html><head><meta charset=utf-8>" + head + "</head>" + doc


def _asset(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


async def _auth(hub, token: str):
    return hub.authenticate(token) if token else None


async def _target(hub, cap: str):
    target = hub._target_for(cap)
    w = hub.dispatcher.workers.get(target) if target else None
    return w.name if w else None


def _digest(sid: str) -> str:
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


def _cookie_value(header: str, name: str) -> str | None:
    for part in header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name and v:
            return v
    return None


_JSON = {"Content-Type": "application/json"}


def _json(status: int, body: dict):
    return status, dict(_JSON), json.dumps(body, default=str).encode("utf-8")


class _Error(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


