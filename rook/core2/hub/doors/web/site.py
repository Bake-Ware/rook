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
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from rook.core2 import rights
from rook.core2.hub.identity import NONE, Caller
from rook.core2.hub.oauth import OAuthError, basic_auth
from .caps import _read_json, _write, dock_agent

log = logging.getLogger("rook.hub.door.web")

STATIC = Path(__file__).with_name("static")
COOKIE = "rook_web"
SESSION_SECS = 30 * 86400
TOKEN_SECS = 12 * 3600
FRAME_SECS = 60
TOKENS_PER_SESSION = 32
POLL_SECS = 20.0
EVENTS_KEPT = 500
DOCK_CAPS = ("agents.chat.send", "agents.chat.read")     # the shell's chat dock, with the dock agent only
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}

# frame-src 'self' is load-bearing: it governs every navigation of the page
# frames the shell embeds, including one a page starts itself
# (location = "https://elsewhere/?d=..."), so a page can't carry data off the
# hub that way (review D5; tests/test_core2_page_sandbox_browser.py).
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
        return path in ("/", "/login", "/logout", OAUTH_META) or path.startswith(("/web/", "/oauth/"))

    def handle(self, req, method: str, path: str, query: str) -> tuple[int, dict, bytes]:
        """One request: (status, headers, body)."""
        try:
            return self._route(req, method, path, dict(parse_qsl(query)))
        except _Error as e:
            return _json(e.status, {"ok": False, "error": e.message})

    def _route(self, req, method, path, query):
        if path == OAUTH_META or path.startswith("/oauth/"):
            return self._oauth(req, method, path, query)
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
        if not secrets.compare_digest(req.headers.get("X-Rook-CSRF", "").encode(), session.csrf.encode()):
            raise _Error(403, "missing or wrong X-Rook-CSRF")
        if path == "/logout":
            with self._lock:
                self._drop(sid)
            return 200, {"Set-Cookie": self._cookie("", 0), **_JSON}, b'{"ok": true}'
        body = req._body()
        caller = Caller(person=session.person)
        if path == "/web/api/call":
            cap, args = body.get("cap"), body.get("args") or {}
            agent = dock_agent(self.hub)
            dock = agent is not None and cap in DOCK_CAPS and isinstance(args, dict) and args.get("agent") == agent
            if not (isinstance(cap, str) and cap.startswith("pages.") or dock):
                raise _Error(403, "the shell calls pages.* (and the dock agent's chat) only")
            why = body.get("why") if isinstance(body.get("why"), str) and body["why"].strip() else \
                "Talking with the agent in the site's dock." if dock else "Using the hub site."
            # A message to the dock agent runs in the person's own door session:
            # the agent's turn works with their rights, not the shell's pages.*.
            session = req._run(self._dock_session(caller) if dock else self._shell_session(caller))
            return _json(200, req._run(self.hub.call(caller, {
                "worker": "rook", "cap": cap, "args": args, "why": why, "session": session}, "web")))
        if path == "/web/api/open":
            return _json(200, self._open(req, sid, caller, body.get("page")))
        if path == "/web/api/page":
            return _json(200, self._page_call(req, sid, caller, body))
        raise _Error(404, "not found")

    # -- sign-in ----------------------------------------------------------------

    def _login(self, req):
        # Login CSRF: a form on another site must not sign this browser in
        # as someone else. The browser says where a request comes from.
        site, origin = req.headers.get("Sec-Fetch-Site"), req.headers.get("Origin")
        if site not in (None, "same-origin", "none") or \
                (origin is not None and urlsplit(origin).netloc != req.headers.get("Host", "")):
            raise _Error(403, "sign in from this hub's own page")
        ctype = req.headers.get("Content-Type", "")
        if ctype.startswith("application/x-www-form-urlencoded"):
            try:
                n = int(req.headers.get("Content-Length") or 0)
            except ValueError:
                raise _Error(411, "Content-Length required") from None
            if n < 0 or n > 4096:
                raise _Error(413, "body too large")
            req._read = True
            fields = dict(parse_qsl(req.rfile.read(n).decode("utf-8", "replace")))
            token, form = fields.get("token", ""), True
        else:
            fields = {}
            token, form = req._body().get("token", ""), False
        then = _next(fields.get("next"))
        caller = req._run(_auth(self.hub, str(token).strip()))
        if caller is None or caller.agent or not caller.person:
            log.info("sign-in refused")
            if form:
                return 303, {"Location": "/login?failed=1" + (f"&next={quote(then, safe='')}" if then else "")}, b""
            raise _Error(401, "that is not a person's token")
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self.sessions[_digest(sid)] = Session(caller.person, secrets.token_urlsafe(24), time.time() + SESSION_SECS)
            self._save()
        log.info("signed in", extra={"fields": {"person": caller.person}})
        headers = {"Set-Cookie": self._cookie(sid, SESSION_SECS)}
        if form:
            return 303, {**headers, "Location": then or "/"}, b""
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
        token, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        try:
            page = self.hub.pages.openable(pid)
        except Exception as e:
            raise _Error(404, str(getattr(e, "message", e))) from None
        dsid = req._run(self._page_session(_page_caller(page, caller)))
        now = time.time()
        with self._lock:
            s = self.sessions.get(sid)
            if s is None:
                raise _Error(401, "sign in first")
            self.tokens[token] = PageToken(sid, pid, dsid, now + TOKEN_SECS)
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
        info = {"page": page.id, "title": page.title, "caps": page.caps, "events": page.events}
        return 200, {"Content-Type": TYPES[".html"], "Content-Security-Policy": PAGE_CSP,
                     "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}, \
            inject(doc, info).encode("utf-8")

    def _page_call(self, req, sid: str, caller: Caller, body: dict) -> dict:
        t = self._token(sid, body.get("token"))
        try:
            page = self.hub.pages.openable(t.page)       # as it is now: archived or narrowed since it opened
        except Exception as e:
            return {"ok": False, "error": f"denied: page: {getattr(e, 'message', e)}"}
        cap = body.get("cap")
        if not isinstance(cap, str) or not cap:
            raise _Error(400, "cap is required")
        if page.made and cap == "approval.decide":
            # Agent-written code runs as the viewer: it must never answer the viewer's holds.
            return {"ok": False, "error": "denied: page: a page an agent made can't answer holds"}
        if cap == "approval.decide":
            return self._decide(req, caller, page, body.get("args"))
        worker = body.get("worker") if isinstance(body.get("worker"), str) else None
        if worker is None:
            worker = req._run(_target(self.hub, cap)) or "rook"
        why = body.get("why") if isinstance(body.get("why"), str) and body["why"].strip() else \
            f"Shown on the {t.page} page."
        layers = [["page", {"grants": page.caps}], *self._authors(page)]
        return req._run(self.hub.call(_page_caller(page, caller), {
            "worker": worker, "cap": cap, "args": body.get("args"), "why": why,
            "session": t.dispatch}, "web", layers=layers))

    def _decide(self, req, caller: Caller, page, args) -> dict:
        """The person answering a hold on a page the hub ships (Approvals):
        signed in here, so they are present and may answer a hard stop (10.3),
        which the ``approval.decide`` cap never may. Release and refusal are
        journaled by dispatch."""
        if not rights.check([["page", {"grants": page.caps}]], {"cap": "approval.decide", "risk": "write",
                                                                 "effects": []})["ok"]:
            return {"ok": False, "error": "denied: page: grants"}
        args = args if isinstance(args, dict) else {}
        if not isinstance(args.get("hold"), str) or args.get("decision") not in ("approve", "refuse"):
            return {"ok": False, "error": "bad args: hold, and decision approve or refuse"}
        out = req._run(self.hub.decide(caller, args["hold"], args.get("decision"), present=True))
        if not out.get("ok") and "id" not in out:      # unknown hold, not theirs, bad decision
            return {"ok": False, "error": out["error"]}
        return {"ok": True, "result": out}

    def _authors(self, page) -> list:
        """A page an agent made never does more than any of its authors."""
        if not page.made:
            return []
        out = []
        for a in page.made.get("authors") or [{}]:
            who = Caller(a.get("person"), a.get("agent"))
            out += [[f"author {k}", r] for k, r in self.hub.identities.layers(who)] or [["author", NONE]]
        return out

    async def _shell_session(self, caller: Caller) -> str:
        """The shell's dispatch session: pages.* only."""
        return self.hub._session(self.hub._door_sessions, ("web", caller.person), caller, ["pages.*"])

    async def _dock_session(self, caller: Caller) -> str:
        return self.hub._door_session(caller)

    async def _page_session(self, caller: Caller) -> str:
        """One dispatch session per opened page (its calls group in the
        journal); the page's caps ride as their own layer."""
        return self.hub.dispatcher.start_session(caller, ["*"]).id

    # -- events -------------------------------------------------------------------

    def _on_event(self, event: str, payload: dict, source: str | None) -> None:
        whose = self._whose(event, payload)
        with self._cond:
            self._seq += 1
            self._events.append((self._seq, event, json.loads(json.dumps(payload, default=str)), whose))
            self._cond.notify_all()

    def _whose(self, event: str, payload) -> str | None:
        """Whose data an event is, like a private record (review D6): its
        ``person`` when it carries or declares one (a hold, a linked chat
        message), owners' (``""``) when it brings outside content in
        (``taints``: an inbound message naming nobody), else None: anyone
        whose rights hear it."""
        decl = next((m["emits"][event] for m in self.hub._norm.values() if event in (m.get("emits") or {})), {})
        person = payload.get("person") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and "person" in payload or "person" in (decl.get("payload") or {}):
            return person if isinstance(person, str) and person else ""
        return "" if decl.get("taints") else None

    def _poll(self, sid: str, token: str, since) -> dict:
        t = self._token(sid, token)
        try:
            since = int(since) if since not in (None, "") else None
        except ValueError:
            raise _Error(400, "since is a number") from None
        try:
            page = self.hub.pages.openable(t.page)
        except Exception as e:
            raise _Error(403, str(getattr(e, "message", e))) from None
        with self._lock:
            person = self.sessions[sid].person if sid in self.sessions else None
        owner = self.hub.identities.is_owner(person)
        layers = [["person", self.hub.identities.person_rights(person) if person else NONE],
                  ["page", {"grants": page.events}], *self._authors(page)]

        def visible():
            return [{"seq": s, "event": e, "payload": p} for s, e, p, whose in self._events
                    if s > since and (whose is None or owner or whose == person)
                    and rights.check(layers, {"cap": e, "risk": "read", "effects": []})["ok"]]
        deadline = time.monotonic() + self.poll_secs
        with self._cond:
            if since is None:          # first poll: where the stream is now
                return {"ok": True, "seq": self._seq, "events": []}
            while True:
                got = visible() if page.events else []
                left = deadline - time.monotonic()
                if got or left <= 0:
                    if any(self.hub.taints(e["event"]) for e in got):
                        self.hub.dispatcher.taint(t.dispatch)    # the page's calls now act on outside content (8.4)
                    return {"ok": True, "seq": self._seq, "events": got}
                self._cond.wait(left)

    # -- OAuth: the authorization server for remote MCP clients (rook.core2.hub.oauth) --

    def _oauth(self, req, method, path, query):
        oauth = self.hub.oauth
        if oauth is None:
            raise _Error(404, "not found")
        try:
            if method == "GET" and path == OAUTH_META:
                return _json(200, oauth.metadata())
            if path == "/oauth/authorize" and method in ("GET", "POST"):
                return self._authorize(req, method, query)
            if method != "POST" or path not in ("/oauth/register", "/oauth/token", "/oauth/revoke"):
                raise _Error(404 if path not in ("/oauth/register", "/oauth/token", "/oauth/revoke") else 405,
                             "not found" if method == "POST" else "method not allowed")
            if path == "/oauth/register":
                return _json(201, oauth.register(req._body()))
            form, basic = _form(req), basic_auth(req.headers.get("Authorization", ""))
            if path == "/oauth/revoke":
                oauth.revoke_token(form, basic)
                return _json(200, {})
            reply, grant = oauth.token(form, basic)
            if grant:
                self.hub._journal({"kind": "connector", "ts": self.hub.clock(), "person": grant["person"],
                                   "agent": grant["agent"], "name": grant["name"], "grant": grant["id"],
                                   "client": grant["client"], "ok": True})
                log.info("oauth connector allowed", extra={"fields": {"grant": grant["id"], "person": grant["person"],
                                                                      "agent": grant["agent"]}})
            return _json(200, reply)
        except OAuthError as e:
            if e.redirect:
                return 303, {"Location": e.redirect}, b""
            if path == "/oauth/authorize":
                return self._oauth_page(e.status, "This app can't connect", f"<p>{escape(e.description)}</p>")
            extra = {"WWW-Authenticate": 'Basic realm="rook"'} if e.status == 401 else {}
            return e.status, {**_JSON, **extra}, json.dumps(e.body()).encode("utf-8")

    def _authorize(self, req, method, query):
        """The person's side of the flow: sign in, then allow the app as one
        of the hub's agents (or don't)."""
        oauth = self.hub.oauth
        sid, session = self._session(req)
        if method == "GET":
            if session is None:
                if req.headers.get("Sec-Fetch-Site") == "cross-site":
                    # The sign-in cookie is SameSite=Strict: a link from the app's own site
                    # arrives without it. Load the page again from here, and it comes along;
                    # a browser that still calls that cross-site gets a link to click.
                    again = escape(req.path + "&rook_again=1")
                    if "rook_again" in query:
                        return self._oauth_page(200, "Rook", f'<p><a href="{escape(req.path)}">Continue</a></p>')
                    return self._oauth_page(200, "Rook", '<p class="muted">One moment…</p>',
                                            head=f'<meta http-equiv="refresh" content="0; url={again}">')
                return 303, {"Location": "/login?next=" + quote(req.path, safe="")}, b""
            consent = oauth.begin(query, sid)
            return self._consent(session, consent)
        site, origin = req.headers.get("Sec-Fetch-Site"), req.headers.get("Origin")
        if site not in (None, "same-origin") or \
                (origin is not None and urlsplit(origin).netloc != req.headers.get("Host", "")):
            raise OAuthError("invalid_request", "Answer from this hub's own page.", 403)
        form = _form(req)
        if session is None:
            raise OAuthError("access_denied", "Your sign-in ended. Start again from the app.", 401)
        if not secrets.compare_digest(form.get("csrf", "").encode(), session.csrf.encode()):
            raise OAuthError("invalid_request", "This page is stale. Start again from the app.", 403)
        agent = form.get("agent")
        allow = form.get("decision") == "allow"
        if allow and agent not in self.hub.identities.agents:
            raise OAuthError("invalid_request", "Choose one of the hub's agents.")
        return 303, {"Location": oauth.decide(form.get("consent"), sid, session.person,
                                              f"agent:{agent}" if allow else None)}, b""

    def _consent(self, session: Session, c: dict):
        agents = self.hub.identities.agents
        rows = "".join(
            f'<label class="choice"><input type="radio" name="agent" value="{escape(name)}" required'
            f'{" checked" if len(agents) == 1 else ""}> <b>{escape(name)}</b> '
            f'<span class="muted">{escape(_rights_line(r))}</span></label>'
            for name, r in sorted(agents.items()))
        body = (f'<p><b>{escape(c["client"])}</b> wants to use this hub for you '
                f'({escape(session.person.partition(":")[2])}).</p>'
                f'<p class="muted">When you answer, you go back to <b>{escape(c["return_to"])}</b>. '
                "Only allow an app you just added yourself.</p>")
        if rows:
            body += (f'<fieldset><legend>It acts as</legend>{rows}</fieldset>'
                     '<p class="muted">It can do only what both you and that agent may do. Its calls are '
                     "journaled; end it any time on More &gt; Connectors.</p>")
        else:
            body += ('<p class="bad">This hub has no agents yet. An owner adds one under <code>agents:</code> '
                     "in hub.yaml.</p>")
        body += (f'<input type="hidden" name="consent" value="{escape(c["id"])}">'
                 f'<input type="hidden" name="csrf" value="{escape(session.csrf)}">'
                 + ('<button type="submit" name="decision" value="allow">Allow</button>' if rows else "")
                 + '<button type="submit" name="decision" value="deny" class="link" formnovalidate>'
                   "Don't allow</button>")
        back = urlsplit(c["redirect_uri"])
        # form-action covers the redirect after the answer, so it names the app's address too.
        csp = SHELL_CSP.replace("form-action 'self'", f"form-action 'self' {back.scheme}://{back.netloc}")
        return self._oauth_page(200, "Allow an app", body, form=True, csp=csp)

    def _oauth_page(self, status: int, title: str, body: str, head: str = "", form: bool = False,
                    csp: str = SHELL_CSP):
        inner = (f'<form class="login consent" method="post" action="/oauth/authorize">'
                 f'<div class="brand">{escape(title)}</div>{body}</form>') if form else \
            f'<div class="login consent"><div class="brand">{escape(title)}</div>{body}</div>'
        doc = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
               '<meta name="viewport" content="width=device-width, initial-scale=1">'
               f'{head}<title>Rook: {escape(title)}</title><link rel="stylesheet" href="/web/static/shell.css">'
               f"</head><body>{inner}</body></html>")
        return status, {"Content-Type": TYPES[".html"], "Content-Security-Policy": csp, "X-Frame-Options": "DENY",
                        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}, doc.encode("utf-8")

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


def _page_caller(page, viewer: Caller) -> Caller:
    """Who a page's calls act as. An agent-made page acts as its (first)
    agent author for the viewer, so whatever it creates (a page, a job, a
    record) is recorded as that agent's, never as the viewer's alone:
    otherwise a made page could make a page or job without the author layer
    and launder its author up to the viewer's full rights."""
    agents = [a.get("agent") for a in (page.made or {}).get("authors") or () if a.get("agent")]
    return Caller(viewer.person, agents[0]) if agents else viewer


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
OAUTH_META = "/.well-known/oauth-authorization-server"


def _next(value) -> str | None:
    """Where sign-in goes next: only back into an OAuth authorization."""
    if isinstance(value, str) and value.startswith("/oauth/authorize?") and \
            not any(c == "\\" or ord(c) <= 32 or ord(c) == 127 for c in value):
        return value
    return None


def _form(req) -> dict:
    """An OAuth form body (application/x-www-form-urlencoded), each field once."""
    if not req.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
        raise OAuthError("invalid_request", "send application/x-www-form-urlencoded")
    pairs = parse_qsl(req._raw().decode("utf-8", "replace"))
    out = dict(pairs)
    if len(out) != len(pairs):
        raise OAuthError("invalid_request", "a field came twice")
    return out


def _rights_line(r: dict) -> str:
    grants = r.get("grants") or []
    text = ", ".join(map(str, grants[:6])) + (f" and {len(grants) - 6} more" if len(grants) > 6 else "")
    return f"{text or 'nothing'}" + (f", up to {r['max_risk']}" if r.get("max_risk") else "")


def _json(status: int, body: dict):
    return status, dict(_JSON), json.dumps(body, default=str).encode("utf-8")


class _Error(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


