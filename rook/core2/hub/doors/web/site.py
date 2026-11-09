"""The hub site's routes, sessions and page tokens (see ``__init__``). Runs in
the HTTP door's request threads; hub work goes to the hub's loop through the
handler's ``_run``."""
from __future__ import annotations

import base64
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
from urllib.parse import parse_qsl, unquote, urlsplit

from rook.core2 import rights
from rook.core2.hub import login_links, passwords
from rook.core2.hub.identity import NONE, Caller
from .caps import _read_json, _write, dock_agent, setup_state

log = logging.getLogger("rook.hub.door.web")

STATIC = Path(__file__).with_name("static")
COOKIE = "rook_web"
SESSION_SECS = 30 * 86400
TOKEN_SECS = 12 * 3600
FRAME_SECS = 60
TOKENS_PER_SESSION = 32
PUBLIC = "public"            # the "session" of page tokens a public page's viewers hold
PUBLIC_TOKENS = 512
POLL_SECS = 20.0
EVENTS_KEPT = 500
ACTS_KEPT = 200
DOCK_CAPS = ("agent.chat.send", "agent.chat.read")     # the shell's chat dock, with the dock agent only
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".woff2": "font/woff2", ".ico": "image/x-icon"}

# frame-src 'self' is load-bearing: it governs every navigation of the page
# frames the shell embeds, including one a page starts itself
# (location = "https://elsewhere/?d=..."), so a page can't carry data off the
# hub that way (review D5; tests/test_core2_page_sandbox_browser.py).
# The brand's fonts (DM Sans, IBM Plex Mono) are the hub's own files
# (static/fonts, the ones Google Fonts serves the 0.4 dashboard); Google Fonts
# stays allowed for pages saved before.
SHELL_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
             "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; frame-src 'self'; "
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
        self.public_tokens: list = []                        # page tokens public viewers hold, oldest first
        self._events: collections.deque = collections.deque(maxlen=EVENTS_KEPT)
        self._seq = 0
        self._cond = threading.Condition()
        hub.listeners.append(self._on_event)
        self._acts: collections.deque = collections.deque(maxlen=ACTS_KEPT)
        self._act_seq = 0
        if getattr(hub, "dispatcher", None) is not None:
            hub.dispatcher.watchers.append(self._on_call)

    # -- routing ----------------------------------------------------------------

    @staticmethod
    def owns(path: str) -> bool:
        return path in ("/", "/login", "/login/link", "/logout", "/account", "/favicon.ico") or \
            path.startswith(("/web/", "/public/"))

    def handle(self, req, method: str, path: str, query: str) -> tuple[int, dict, bytes]:
        """One request: (status, headers, body)."""
        try:
            return self._route(req, method, path, dict(parse_qsl(query)))
        except _Error as e:
            return _json(e.status, {"ok": False, "error": e.message})

    def _route(self, req, method, path, query):
        if method == "GET" and path.startswith("/web/static/"):
            return self._static(path[len("/web/static/"):])
        if method == "GET" and path == "/favicon.ico":     # the rook, for any tab, signed in or not
            return self._file("favicon.ico")
        public = self._public_route(req, method, path, query)
        if public is not None:
            return public
        setup = self._setup_owner()
        if path == "/login":
            if method == "POST":
                return self._login(req)
            if setup:                  # still in setup: no sign-in, straight to the setup chat
                return 303, {"Location": "/"}, b""
            return self._file("login.html", csp=SHELL_CSP)
        if method == "GET" and path == "/login/link":
            return self._link(query.get("code"))
        sid, session = self._session(req)
        if method == "GET" and path in ("/", "/account"):
            cookie = {}
            if session is None and setup:
                # Setup isn't completed: whoever opens the hub is the owner
                # setting it up (Bake, Nov 6: "It is simple. If configuration
                # isn't completed assume we are still in setup mode").
                sid = self._start(setup)
                cookie = {"Set-Cookie": self._cookie(sid, SESSION_SECS)}
                log.info("setup mode: signed in as the owner", extra={"fields": {"person": setup}})
            elif session is None:
                return 303, {"Location": "/login"}, b""
            status, headers, body = self._file("shell.html" if path == "/" else "account.html", csp=SHELL_CSP)
            return status, {**headers, **cookie}, body
        if method == "GET" and path.startswith("/web/frame/"):
            return self._frame(sid, session, unquote(path[len("/web/frame/"):]))
        if session is None:
            raise _Error(401, "sign in first")
        if method == "GET" and path == "/web/api/me":
            return _json(200, {"ok": True, "person": session.person, "csrf": session.csrf,
                               "connector": self._connector(),
                               "password": passwords.set_by(self.hub.data, session.person.partition(":")[2])
                               is not None})
        if method == "GET" and path == "/web/api/events":
            return _json(200, self._poll(sid, query.get("token", ""), query.get("since")))
        if method == "GET" and path == "/web/api/secrets":       # the Vault work view: names, never values
            try:
                return _json(200, {"ok": True, "secrets": self.hub.vault.list(), "locked": self.hub.vault.locked})
            except Exception as e:
                return _json(200, {"ok": False, "error": str(e)})
        if method == "GET" and path == "/web/api/activity":
            return _json(200, self._activity(session.person, query.get("since")))
        if method != "POST":
            raise _Error(404, "not found")
        if not secrets.compare_digest(req.headers.get("X-Rook-CSRF", "").encode(), session.csrf.encode()):
            raise _Error(403, "missing or wrong X-Rook-CSRF")
        if path == "/logout":
            with self._lock:
                self._drop(sid)
            return 200, {"Set-Cookie": self._cookie("", 0), **_JSON}, b'{"ok": true}'
        body = req._body()
        if path == "/web/api/password":
            return _json(200, self._password(session.person, body))
        if path == "/web/api/secret":
            return _json(200, self._secret(session.person, body))
        caller = Caller(person=session.person)
        if path == "/web/api/call":
            cap, args = body.get("cap"), body.get("args") or {}
            agent = dock_agent(self.hub)
            dock = agent is not None and cap in DOCK_CAPS and isinstance(args, dict) and args.get("agent") == agent
            if not (isinstance(cap, str) and cap.startswith("page.") or dock):
                raise _Error(403, "the shell calls page.* (and the dock agent's chat) only")
            why = body.get("why") if isinstance(body.get("why"), str) and body["why"].strip() else \
                "Talking with the agent in the site's dock." if dock else "Using the hub site."
            # A message to the dock agent runs in the person's own door session:
            # the agent's turn works with their rights, not the shell's page.*.
            session = req._run(self._dock_session(caller) if dock else self._shell_session(caller))
            return _json(200, req._run(self.hub.call(caller, {
                "worker": "rook", "cap": cap, "args": args, "why": why, "session": session}, "web")))
        if path == "/web/api/open":
            return _json(200, self._open(req, sid, caller, body.get("page")))
        if path == "/web/api/page":
            return _json(200, self._page_call(req, sid, caller, body))
        raise _Error(404, "not found")

    # -- public pages -------------------------------------------------------------

    def _public_route(self, req, method, path, query):
        """A public page (``page.public``; Bake, Nov 9: "a way to make a given
        page publicly accessible for things like reports or reference"):
        anyone opens /public/<page> without signing in. Its calls act as the
        person who made it public, within the page's own grants. None: not
        a public route."""
        if method == "GET" and path.startswith("/public/"):
            return self._file("public.html", csp=SHELL_CSP)
        if not path.startswith("/web/api/public/"):
            return None
        what = path[len("/web/api/public/"):]
        if method == "GET" and what == "events":
            return _json(200, self._poll(PUBLIC, query.get("token", ""), query.get("since")))
        if method != "POST":
            raise _Error(404, "not found")
        body = req._body()
        if what == "open":
            pid = body.get("page")
            caller = self._publisher(pid)
            return _json(200, self._open(req, PUBLIC, caller, pid))
        if what == "page":
            t = self._token(PUBLIC, body.get("token"))
            return _json(200, self._page_call(req, PUBLIC, self._publisher(t.page), body))
        raise _Error(404, "not found")

    def _publisher(self, pid) -> Caller:
        """Who a public page acts as: the person who made it public."""
        rec = self.hub.pages.public.get(pid) if isinstance(pid, str) else None
        if rec is None:
            raise _Error(404, "This page isn't public.")
        person = rec.get("person")
        if not person or self.hub.identities.user(person) is None:
            raise _Error(404, "This page isn't public any more.")
        return Caller(person=person)

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
            fields, form = dict(parse_qsl(req.rfile.read(n).decode("utf-8", "replace"))), True
        else:
            fields, form = req._body(), False
        person = self._check(req, fields)
        if person is None:
            log.info("sign-in refused")
            if form:
                return 303, {"Location": "/login?failed=1"}, b""
            raise _Error(401, "wrong user name or password")
        sid = self._start(person)
        log.info("signed in", extra={"fields": {"person": person}})
        headers = {"Set-Cookie": self._cookie(sid, SESSION_SECS)}
        if form:
            # A password made for them at setup: offer to change it first.
            first = passwords.set_by(self.hub.data, person.partition(":")[2]) == passwords.GENERATED
            return 303, {**headers, "Location": "/account?first=1" if first else "/"}, b""
        return 200, {**headers, **_JSON}, b'{"ok": true}'

    def _link(self, code) -> tuple[int, dict, bytes]:
        """A sign-in link (``login_links``; the installer opens one): a
        session, then the site. An unknown link goes to /login."""
        person = login_links.take(self.hub.data, code)
        if person is None or self.hub.identities.user(person) is None:
            log.info("sign-in link refused")
            return 303, {"Location": "/login?link=1", "Cache-Control": "no-store"}, b""
        sid = self._start(person)
        log.info("signed in with a link", extra={"fields": {"person": person}})
        return 303, {"Set-Cookie": self._cookie(sid, SESSION_SECS), "Location": "/",
                     "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}, b""

    def _setup_owner(self) -> str | None:
        """While first-run setup isn't completed (``hub.setup``), the owner
        the site signs everyone in as (the first one); else None."""
        if setup_state(self.hub)["done"]:
            return None
        return next((f"person:{u.name}" for u in self.hub.identities.users.values() if u.preset == "owner"), None)

    def _start(self, person: str) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self.sessions[_digest(sid)] = Session(person, secrets.token_urlsafe(24), time.time() + SESSION_SECS)
            self._save()
        return sid

    def _check(self, req, fields: dict) -> str | None:
        """The person signing in (``person:<name>``), or None. A person signs
        in with their user name and password (``passwords``); an app that
        holds a person's own door token (the desktop app) may send
        ``{"token"}`` instead. An agent's token never signs in."""
        user = fields.get("user")
        if isinstance(user, str) and user.strip():
            return passwords.sign_in(self.hub, user, fields.get("password"))
        token = fields.get("token")
        if not isinstance(token, str) or not token.strip():
            return None
        caller = req._run(_auth(self.hub, token.strip()))
        return caller.person if caller is not None and caller.person and not caller.agent else None

    def _password(self, person: str, body: dict) -> dict:
        """The signed-in person sets their own password (the Account page); the
        current one isn't asked for (Bake, Nov 6: "Remove all restrictions and
        gates")."""
        name, new = person.partition(":")[2], body.get("password")
        if not isinstance(new, str) or not new:
            return {"ok": False, "error": "Type a new password."}
        passwords.set_password(self.hub.data, name, new, by="web")
        log.info("password changed", extra={"fields": {"person": person}})
        return {"ok": True}

    def _secret(self, person: str, body: dict) -> dict:
        """The Vault work view's Save (the house agent opens it with the key
        filled in for a sudo password or an API key): straight into the
        hub's vault; the chat only hears that it was saved (Bake, Nov 9:
        "this chat interface needs to be able to pull in other aspects like
        add a secret")."""
        name, value = body.get("name"), body.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            return {"ok": False, "error": "Give a name and a value."}
        try:
            self.hub.vault.set(name.strip(), value, by=person, description=body.get("description") or None)
        except Exception as e:                 # a locked vault, a name it can't take
            return {"ok": False, "error": str(e)}
        log.info("secret saved from the vault view", extra={"fields": {"person": person, "name": name}})
        return {"ok": True, "name": name.strip()}

    def _connector(self) -> str | None:
        """The MCP door's address for claude.ai's connector, when it speaks OAuth."""
        return next((d.oauth.resource for d in getattr(self.hub, "doors", ())
                     if getattr(d, "oauth", None) is not None), None)

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
        """``page.view`` as the person (rights, journal); on success a page
        token for the shell and a one-time frame URL."""
        if not isinstance(pid, str) or not pid:
            raise _Error(400, "page is required")
        reply = req._run(self.hub.call(caller, {
            "worker": "rook", "cap": "page.view", "args": {"page": pid}, "why": f"Opening the page {pid}.",
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
            held = self.public_tokens if sid == PUBLIC else getattr(self.sessions.get(sid), "tokens", None)
            if held is None:
                raise _Error(401, "sign in first")
            self.tokens[token] = PageToken(sid, pid, dsid, now + TOKEN_SECS)
            held.append(token)
            while len(held) > (PUBLIC_TOKENS if sid == PUBLIC else TOKENS_PER_SESSION):
                self.tokens.pop(held.pop(0), None)
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
        t = self.tokens.get(token) if token else None
        if t is not None and t.session == PUBLIC:      # a public page's viewer: no sign-in
            sid, session = PUBLIC, True
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
        worker = body.get("worker") if isinstance(body.get("worker"), str) else None
        if worker is None:
            worker = req._run(_target(self.hub, cap)) or "rook"
        why = body.get("why") if isinstance(body.get("why"), str) and body["why"].strip() else \
            f"Shown on the {t.page} page."
        layers = [["page", {"grants": page.caps}], *self._authors(page)]
        return req._run(self.hub.call(_page_caller(page, caller), {
            "worker": worker, "cap": cap, "args": body.get("args"), "why": why,
            "session": t.dispatch}, "web", layers=layers))

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
        """The shell's dispatch session: page.* only."""
        return self.hub._session(self.hub._door_sessions, ("web", caller.person), caller, ["page.*"])

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

    def _on_call(self, phase: str, entry: dict) -> None:
        """Every call starting or ending (the dispatcher's watchers), as
        journaled, args and why included; the dock's Rookie picks its agent's."""
        if entry.get("kind") not in (None, "call"):
            return
        act = {k: v for k, v in entry.items() if k not in ("kind", "hub")}
        act = json.loads(json.dumps({"phase": phase, **act}, default=str))
        with self._cond:
            self._act_seq += 1
            self._acts.append((self._act_seq, act))
            self._cond.notify_all()

    def _activity(self, person: str, since) -> dict:
        """Every call, start and end, after ``since`` (long-polls; the first
        poll says where the feed is now)."""
        try:
            since = int(since) if since not in (None, "") else None
        except ValueError:
            raise _Error(400, "since is a number") from None
        deadline = time.monotonic() + self.poll_secs
        with self._cond:
            if since is None or since > self._act_seq:      # first poll, or the hub restarted
                return {"ok": True, "seq": self._act_seq, "events": []}
            while True:
                got = [{"seq": n, **a} for n, a in self._acts if n > since]
                left = deadline - time.monotonic()
                if got or left <= 0:
                    return {"ok": True, "seq": self._act_seq, "events": got}
                self._cond.wait(left)

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
        if sid == PUBLIC:
            person = self._publisher(t.page).person
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

    # -- files --------------------------------------------------------------------

    def _static(self, name: str):
        if not re.fullmatch(r"[a-z0-9-]+\.(js|css|svg)|fonts/[a-z0-9-]+\.woff2", name):
            raise _Error(404, "not found")
        return self._file(name)

    def _file(self, name: str, csp: str | None = None):
        path = STATIC / name
        if not path.is_file():
            raise _Error(404, "not found")
        headers = {"Content-Type": TYPES[path.suffix], "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "same-origin"}
        if path.suffix in (".woff2", ".ico") or path.name == "rook-scene.js":     # big and never changes within a version
            headers["Cache-Control"] = "max-age=86400"
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
            f"<style>{_page_fonts()}\n{_asset('page.css')}</style>\n")
    doc = LIB.sub(lambda m: _lib(m.group(1)), doc)
    m = re.search(r"<head\b[^>]*>", doc, re.I)
    if m:
        return doc[:m.end()] + "\n" + head + doc[m.end():]
    m = re.search(r"<html\b[^>]*>", doc, re.I)
    if m:
        return doc[:m.end()] + "<head>" + head + "</head>" + doc[m.end():]
    return "<!doctype html><head><meta charset=utf-8>" + head + "</head>" + doc


def _asset(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


# A page has no network, so the brand fonts go in as data: URLs (font-src
# data: in PAGE_CSP), made once.
_FONTS: list = []


def _page_fonts() -> str:
    if not _FONTS:
        css = _asset("fonts.css")
        for name in re.findall(r"/web/static/fonts/([a-z0-9-]+\.woff2)", css):
            data = base64.b64encode((STATIC / "fonts" / name).read_bytes()).decode("ascii")
            css = css.replace(f"/web/static/fonts/{name}", f"data:font/woff2;base64,{data}")
        _FONTS.append(css)
    return _FONTS[0]


# Shared libraries a page can ask for (it can't load scripts): a
# <script type="module" data-rook-lib="NAME"></script> becomes the library,
# inline, with its exports on window.RookLib[NAME] and a "rooklib" event once
# it ran. rook-scene: the 0.4 dashboard's three.js rook and fleet view
# (rook/web/scene; mountRook, mountFleet).
LIBS = {"rook-scene": "rook-scene.js"}
LIB = re.compile(r'<script\b[^>]*\bdata-rook-lib="([a-z0-9-]+)"[^>]*>\s*</script>', re.I)
_LIB_CACHE: dict = {}


def _lib(name: str) -> str:
    if name not in LIBS:
        return f"<!-- no library {name} -->"
    if name not in _LIB_CACHE:
        code = _asset(LIBS[name])
        m = re.search(r"export\s*\{([^}]*)\}\s*;?", code)
        pairs = []
        for part in (m.group(1).split(",") if m else ()):
            local, _, exported = part.strip().partition(" as ")
            pairs.append(f"{json.dumps(exported.strip() or local.strip())}:{local.strip()}")
        exports = (f";(window.RookLib=window.RookLib||{{}})[{json.dumps(name)}]={{{','.join(pairs)}}};"
                   f"window.dispatchEvent(new CustomEvent('rooklib',{{detail:{json.dumps(name)}}}));")
        body = code[:m.start()] + exports + code[m.end():] if m else code
        _LIB_CACHE[name] = '<script type="module">' + body.replace("</script", "<\\/script") + "</script>"
    return _LIB_CACHE[name]


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


