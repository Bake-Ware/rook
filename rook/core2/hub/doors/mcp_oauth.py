"""A thin OAuth front for the MCP door, so claude.ai's connectors can attach.

claude.ai's web and mobile connectors only speak OAuth (authorization code +
PKCE); they can't send a pasted bearer header the way Claude Code can. Adding
the connector is: paste the door's address, sign in with your Rook user name
and password, done (castling-switchover-decisions: "a normal username and
password login, like 0.4 is"; "no step may need Bake to ... read a token").

* ``/authorize`` shows a sign-in form (the same user name and password as the
  hub site, :mod:`rook.core2.hub.passwords`). Signed in, it hands back a code
  bound to that person, the redirect, the client id and PKCE.
* ``/token`` checks the code and mints a door token for the person acting
  through the connector's agent (door option ``oauth_agent``, default
  ``claude-web``: an agent in hub.yaml, so the journal names it). That token
  is the access token and the refresh token; a refresh only re-checks it.

**Token clients** (no password typed; castling-switchover-decisions, "Claude
connects itself"): ``token.create`` mints an agent token and names a client
id for it, ``rook-<token id>``. A connector set up with that client id and the
token as its client secret goes straight through ``/authorize`` (no form),
and ``/token`` hands back that same token only when the client secret is it.
The client id alone gets nothing: a code is worth only its token.

**Approve by code** (no password typed, no token pasted; Bake: agents connect
themselves). A browser with no sign-in sees, above the password form, a short
code and "waiting for approval": the page reloads itself (``/authorize/wait``)
until an owner approves that exact code, with ``token.approve`` (any owner's
caller: an agent acting for them, the dashboard) or ``python -m rook.core2.hub
oauth approve CODE`` on the hub's machine (written to
``<data>/oauth-approvals.json``, which the door reads). Approved, the flow
goes on as if that owner had signed in. Codes last 10 minutes, work once,
and every approval is journaled. ``/register`` (RFC 7591, no secret) lets
claude.ai add the connector from its address alone.

What claude.ai then sends on ``/mcp`` is an ordinary door token. Nothing is
stored but codes (memory, 5 minutes, once each) and the token (by hash, in
``<data>/tokens.json``, label "claude.ai connector"); taking it out of that
file cuts the connector off. No client registration: claude.ai takes any
client id typed into the connector's Advanced settings.

On with the door option ``oauth: https://rook.example``: the door's public
address as clients reach it (https; http only on loopback). The door then
answers, beside ``/mcp``::

    GET  /.well-known/oauth-protected-resource[/mcp]   RFC 9728: names the issuer
    GET  /.well-known/oauth-authorization-server       RFC 8414 (no registration)
    GET  /authorize                                   the sign-in form
    POST /authorize                                   user, password -> code + PKCE
    POST /token                                       authorization_code | refresh_token

and a 401 on ``/mcp`` names the protected resource metadata.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import time
from typing import Callable
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

CODE_SECS = 300
MAX_CODES = 1024
WAIT_SECS = 600            # an approval code's life
WAIT_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ23456789"     # no vowels (no words), no 0/O/1/I

MAX_FIELD = 4096
ACCESS_SECS = 31536000     # advertised only: the token itself lasts until it is revoked
SCOPE = "rook"
PRM = "/.well-known/oauth-protected-resource"
ASM = "/.well-known/oauth-authorization-server"

Reply = tuple[int, "dict | bytes", dict]     # status, JSON body (or a page), extra headers
PARAMS = ("response_type", "client_id", "redirect_uri", "code_challenge", "code_challenge_method", "state",
          "scope", "resource")
TOKEN_CLIENT = re.compile(r"rook-[0-9a-f]{12}")
PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"


def check_url(url) -> str:
    """The ``oauth`` door option: an https origin (http only on loopback), no path."""
    if not isinstance(url, str) or not url:
        raise ValueError("doors.mcp.oauth must be the door's public address, e.g. https://rook.example")
    u = urlsplit(url)
    if u.path.strip("/") or u.query or u.fragment or not u.hostname:
        raise ValueError(f"doors.mcp.oauth must be an origin with no path, not {url!r}")
    if u.scheme != "https" and not (u.scheme == "http" and _loopback(u.hostname)):
        raise ValueError(f"doors.mcp.oauth must use https (http only on loopback), not {url!r}")
    return f"{u.scheme}://{u.netloc}"


class OAuthShim:
    """``authenticate(token)`` is the door's own bearer check (a Caller or
    None); ``mint(person)`` makes the connector's token for a signed-in
    person (ValueError: why it can't); ``problem()`` says why sign-in can't
    work on this hub yet (or None)."""

    def __init__(self, base: str, path: str, authenticate: Callable, mint: Callable,
                 problem: Callable = lambda: None, clock: Callable = time.time,
                 token_client: Callable = lambda client_id: False,
                 approvals_file=None, note: Callable = lambda **fields: None):
        self.base, self.path = check_url(base), path
        self.authenticate, self.mint, self.problem, self.clock = authenticate, mint, problem, clock
        self.token_client = token_client      # client id -> True when it names a minted agent token
        self.approvals_file = approvals_file  # <data>/oauth-approvals.json: codes the hub CLI approved
        self.note = note                      # journals an approval
        self._waits: dict[str, dict] = {}     # wait id -> {code, q, person, exp}
        self._codes: dict[str, dict] = {}

    @property
    def resource(self) -> str:
        return self.base + self.path

    def challenge(self) -> str:
        """The 401's ``WWW-Authenticate`` (RFC 9728 5.1)."""
        return f'Bearer resource_metadata="{self.base}{PRM}{self.path}"'

    def owns(self, method: str, path: str) -> bool:
        return (method, path) in (("GET", "/authorize"), ("POST", "/authorize"), ("POST", "/token"),
                                  ("GET", "/authorize/wait"), ("POST", "/register")) or \
            (method == "GET" and path in (PRM, PRM + self.path, ASM, "/.well-known/openid-configuration"))

    def handle(self, method: str, path: str, query: str, body: bytes, auth: str = "",
               person: str | None = None) -> Reply:
        """One request to an owned path; ``auth`` is its Authorization header.
        ``person``: who the POST /authorize form's user name and password
        signed in as (the door checks them off the hub's loop), or None."""
        if path in (PRM, PRM + self.path):
            return 200, {"resource": self.resource, "authorization_servers": [self.base],
                         "bearer_methods_supported": ["header"], "scopes_supported": [SCOPE]}, {}
        if path.startswith("/.well-known/"):
            # No registration_endpoint: claude.ai then takes any client id
            # typed into the connector (Advanced settings); no secret needed.
            return 200, {"issuer": self.base, "authorization_endpoint": self.base + "/authorize",
                         "token_endpoint": self.base + "/token", "response_types_supported": ["code"],
                         "registration_endpoint": self.base + "/register",
                         "grant_types_supported": ["authorization_code", "refresh_token"],
                         "code_challenge_methods_supported": ["S256"],
                         "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
                         "scopes_supported": [SCOPE]}, {}
        if path == "/register":
            return self._register(body)
        if path == "/authorize/wait":
            return self._wait(_fields(query).get("w", ""))
        if path == "/authorize":
            if method == "GET":
                return self._authorize(_fields(query), None)
            return self._authorize(_fields(body.decode("utf-8", "replace")), person, signing_in=True)
        return self._token(_fields(body.decode("utf-8", "replace")), auth)

    def _authorize(self, q: dict, person: str | None, signing_in: bool = False) -> Reply:
        redirect = q.get("redirect_uri", "")
        if not _redirect_ok(redirect):
            return _err("invalid_request", "redirect_uri must be https, or http on a loopback address")
        if q.get("response_type") != "code":
            return _err("unsupported_response_type", "response_type must be code")
        if not q.get("code_challenge") or q.get("code_challenge_method", "S256") != "S256":
            return _err("invalid_request", "PKCE with S256 is required")
        if any(len(v) > MAX_FIELD for v in q.values()):
            return _err("invalid_request", "a field is too long")
        token_id = None
        cid = q.get("client_id", "")
        if person is None and TOKEN_CLIENT.fullmatch(cid) and self.token_client(cid):
            token_id = cid[len("rook-"):]          # no form: /token checks the client secret is that token
        if person is None and token_id is None:
            failed = "Wrong user name or password." if signing_in else ""
            # a GET shows an approval code (unless pw=1 asks for the password form); a failed sign-in, the form
            wait = None if (self.problem() or signing_in or q.get("pw")) else self._new_wait(q)
            return _page(_form(q, self.problem() or failed, wait))
        self._gc()
        if len(self._codes) >= MAX_CODES:
            return _err("temporarily_unavailable", "too many sign-ins waiting; try again shortly", 503)
        code = secrets.token_urlsafe(32)
        self._codes[code] = {"challenge": q["code_challenge"], "redirect_uri": redirect, "person": person,
                             "client_id": cid, "token_id": token_id, "exp": self.clock() + CODE_SECS}
        out = {"code": code, **({"state": q["state"]} if "state" in q else {})}
        return 302, None, {"Location": redirect + ("&" if "?" in redirect else "?") + urlencode(out)}

    def _token(self, form: dict, auth: str) -> Reply:
        client_id, secret = _client(form, auth)   # a password sign-in ignores a typed secret; a token client needs it
        grant = form.get("grant_type")
        if grant == "authorization_code":
            self._gc()
            rec = self._codes.pop(form.get("code", ""), None)      # once, right or wrong
            if rec is None:
                return _err("invalid_grant", "invalid or expired code")
            if form.get("redirect_uri", rec["redirect_uri"]) != rec["redirect_uri"]:
                return _err("invalid_grant", "redirect_uri mismatch")
            if rec["client_id"] and client_id and client_id != rec["client_id"]:
                return _err("invalid_grant", "client_id mismatch")
            calc = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
            if not hmac.compare_digest(calc.rstrip(b"="), rec["challenge"].encode()):   # bytes: any text is safe
                return _err("invalid_grant", "PKCE verification failed")
            if rec["token_id"]:
                if not secret or hashlib.sha256(secret.encode()).hexdigest()[:len(rec["token_id"])] != rec["token_id"]:
                    return _err("invalid_client", "the client secret must be this client's Rook token", 401)
                return self._issue(secret, "invalid_client", 401)
            try:
                token = self.mint(rec["person"])
            except ValueError as e:
                return _err("invalid_grant", str(e))
            return self._issue(token, "invalid_grant", 400)
        if grant == "refresh_token":
            return self._issue(form.get("refresh_token", ""), "invalid_grant", 400)
        return _err("unsupported_grant_type", f"unsupported grant_type {grant!r}")

    def _issue(self, token: str, error: str, status: int) -> Reply:
        caller = self.authenticate(token) if token else None
        if caller is None:
            return _err(error, "not a valid Rook token", status)
        if not caller.agent:
            return _err(error, "not a connector's token", status)
        return 200, {"access_token": token, "token_type": "Bearer", "expires_in": ACCESS_SECS,
                     "refresh_token": token, "scope": SCOPE}, {"Pragma": "no-cache"}

    def _gc(self) -> None:
        now = self.clock()
        for c in [c for c, r in self._codes.items() if r["exp"] < now]:
            del self._codes[c]
        for w in [w for w, r in self._waits.items() if r["exp"] < now]:
            del self._waits[w]

    # -- approve by code -------------------------------------------------------------

    def _new_wait(self, q: dict) -> dict | None:
        self._gc()
        if len(self._waits) >= MAX_CODES:
            return None
        code = "".join(secrets.choice(WAIT_ALPHABET) for _ in range(8))
        wid = secrets.token_urlsafe(24)
        self._waits[wid] = {"code": code, "q": dict(q), "person": None, "exp": self.clock() + WAIT_SECS}
        return {"id": wid, "code": f"{code[:4]}-{code[4:]}"}

    def approve(self, code: str, person: str, by: str = "") -> bool:
        """An owner approves a waiting page's code: its sign-in goes through as
        ``person``. False when no live, unapproved page shows that code."""
        self._gc()
        want = _norm_code(code)
        for wid, rec in self._waits.items():
            if rec["code"] == want and rec["person"] is None:
                rec["person"] = person
                self.note(code=f"{want[:4]}-{want[4:]}", person=person, by=by or person,
                          client=urlsplit(rec["q"].get("redirect_uri", "")).hostname or "")
                return True
        return False

    def _wait(self, wid: str) -> Reply:
        self._gc()
        rec = self._waits.get(wid)
        if rec is None:
            return _page(_message("That code has expired or was used. Start adding the connector again."))
        if rec["person"] is None:
            self._take_file_approvals()
        if rec["person"] is None:
            return _page(_form(rec["q"], "", {"id": wid, "code": f"{rec['code'][:4]}-{rec['code'][4:]}"}))
        del self._waits[wid]                       # once
        return self._authorize(rec["q"], rec["person"])

    def _take_file_approvals(self) -> None:
        """Codes ``python -m rook.core2.hub oauth approve`` wrote while the hub runs."""
        path = self.approvals_file
        if path is None or not path.exists():
            return
        try:
            pending = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        left = {}
        for code, rec in pending.items():
            if not (isinstance(rec, dict) and rec.get("person") and self.clock() - rec.get("ts", 0) < WAIT_SECS):
                continue
            if not self.approve(code, rec["person"], by=rec.get("by") or "hub CLI"):
                left[code] = rec
        if left != pending:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(left), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(path)

    def _register(self, body: bytes) -> Reply:
        """RFC 7591, just enough: any client may register; nothing is kept (any
        client id works at /authorize). No secret: the sign-in is the trust."""
        try:
            meta = json.loads(body or b"{}")
        except ValueError:
            return _err("invalid_client_metadata", "not JSON")
        uris = meta.get("redirect_uris") if isinstance(meta, dict) else None
        if not (isinstance(uris, list) and uris and all(isinstance(u, str) and _redirect_ok(u) for u in uris)):
            return _err("invalid_redirect_uri", "redirect_uris must be https (or http on loopback)")
        return 201, {"client_id": "rook-" + secrets.token_hex(8), "client_id_issued_at": int(self.clock()),
                     "redirect_uris": uris, "token_endpoint_auth_method": "none",
                     "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                     **({"client_name": str(meta["client_name"])[:200]} if meta.get("client_name") else {})}, {}


def _norm_code(code) -> str:
    return "".join(c for c in str(code or "").upper() if c.isalnum())


def _page(body: bytes) -> Reply:
    return 200, body, {"Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": PAGE_CSP,
                       "X-Frame-Options": "DENY", "Cache-Control": "no-store"}


def _message(text: str) -> bytes:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Rook</title></head>'
            f'<body style="font:15px system-ui,sans-serif;margin:40px">{html.escape(text)}</body></html>').encode()


def _form(q: dict, message: str, wait: dict | None = None) -> bytes:
    """The sign-in page: the OAuth fields ride along hidden. With ``wait``,
    the approval code instead, and a link to the password form; that page
    reloads itself (``/authorize/wait``) until the code is approved."""
    e = html.escape
    to = e(urlsplit(q.get("redirect_uri", "")).hostname or "")
    note = f'<p class="bad">{e(message)}</p>' if message else ""
    if wait:
        refresh = f'<meta http-equiv="refresh" content="3;url=/authorize/wait?w={e(wait["id"])}">'
        pw = "/authorize?" + urlencode({**{k: q[k] for k in PARAMS if k in q}, "pw": "1"})
        body = (f'<div class="code"><div class="muted">Ask an owner to approve this code:</div>'
                f'<div class="big">{e(wait["code"])}</div><div class="muted">Waiting for approval…</div></div>'
                f'<a href="{e(pw)}">Sign in with your password instead</a>')
        tag = "div"
    else:
        refresh = ""
        hidden = "".join(f'<input type="hidden" name="{k}" value="{e(q[k])}">' for k in PARAMS if k in q)
        body = f"""{hidden}
<label for="user">User name</label>
<input id="user" name="user" autocomplete="username" autocapitalize="none" spellcheck="false" required autofocus>
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button>"""
        tag = 'form method="post" action="/authorize"'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{refresh}<title>Rook: connect</title>
<style>:root{{color-scheme:light dark}}body{{font:15px/1.45 system-ui,sans-serif;margin:0;display:flex;min-height:100vh}}
.box{{margin:auto;width:min(360px,100% - 32px);display:flex;flex-direction:column;gap:10px}}
input{{font:inherit;padding:8px;border:1px solid #8888;border-radius:6px}}
button{{font:inherit;padding:8px;border:0;border-radius:6px;background:#2563eb;color:#fff}}
.bad{{color:#d33}}.muted{{opacity:.7}}.code{{padding:12px;border:1px solid #8888;border-radius:6px;text-align:center}}
.big{{font:600 28px/1.3 ui-monospace,monospace;letter-spacing:2px;margin:6px 0}}</style></head><body>
<{tag} class="box"><b>Rook</b>
<div>Connect <b>{to or "this app"}</b> to Rook as you.</div>{note}{body}</{tag.split()[0]}></body></html>""".encode("utf-8")


def _fields(raw: str) -> dict:
    """Form or query fields; the first of a repeated one."""
    out: dict = {}
    for k, v in parse_qsl(raw, keep_blank_values=True):
        out.setdefault(k, v)
    return out


def _client(form: dict, auth: str) -> tuple[str, str]:
    """Client id and secret from HTTP Basic or the form (claude.ai uses either)."""
    if auth[:6].lower() == "basic ":
        try:
            cid, _, sec = base64.b64decode(auth[6:].strip()).decode("utf-8").partition(":")
            return unquote(cid), unquote(sec)
        except ValueError:
            pass
    return form.get("client_id", ""), form.get("client_secret", "")


def _err(error: str, description: str, status: int = 400) -> Reply:
    return status, {"error": error, "error_description": description}, {}


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _redirect_ok(uri: str) -> bool:
    """https callbacks, and http ones on loopback for local clients."""
    if not uri or any(ord(c) <= 32 or ord(c) == 127 for c in uri) or "\\" in uri or "#" in uri:
        return False
    try:
        u = urlsplit(uri)
        u.port                                     # a bad port raises
    except ValueError:
        return False
    if not u.hostname or u.username is not None or u.password is not None:
        return False
    return u.scheme == "https" or (u.scheme == "http" and _loopback(u.hostname))
