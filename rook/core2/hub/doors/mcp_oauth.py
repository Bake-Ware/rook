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
import secrets
import time
from typing import Callable
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

CODE_SECS = 300
MAX_CODES = 1024
MAX_FIELD = 4096
ACCESS_SECS = 31536000     # advertised only: the token itself lasts until it is revoked
SCOPE = "rook"
PRM = "/.well-known/oauth-protected-resource"
ASM = "/.well-known/oauth-authorization-server"

Reply = tuple[int, "dict | bytes", dict]     # status, JSON body (or a page), extra headers
PARAMS = ("response_type", "client_id", "redirect_uri", "code_challenge", "code_challenge_method", "state",
          "scope", "resource")
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
                 problem: Callable = lambda: None, clock: Callable = time.time):
        self.base, self.path = check_url(base), path
        self.authenticate, self.mint, self.problem, self.clock = authenticate, mint, problem, clock
        self._codes: dict[str, dict] = {}

    @property
    def resource(self) -> str:
        return self.base + self.path

    def challenge(self) -> str:
        """The 401's ``WWW-Authenticate`` (RFC 9728 5.1)."""
        return f'Bearer resource_metadata="{self.base}{PRM}{self.path}"'

    def owns(self, method: str, path: str) -> bool:
        return (method, path) in (("GET", "/authorize"), ("POST", "/authorize"), ("POST", "/token")) or \
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
                         "grant_types_supported": ["authorization_code", "refresh_token"],
                         "code_challenge_methods_supported": ["S256"],
                         "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
                         "scopes_supported": [SCOPE]}, {}
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
        if person is None:
            failed = "Wrong user name or password." if signing_in else ""
            return 200, _form(q, self.problem() or failed), {"Content-Type": "text/html; charset=utf-8",
                                                               "Content-Security-Policy": PAGE_CSP,
                                                               "X-Frame-Options": "DENY"}
        self._gc()
        if len(self._codes) >= MAX_CODES:
            return _err("temporarily_unavailable", "too many sign-ins waiting; try again shortly", 503)
        code = secrets.token_urlsafe(32)
        self._codes[code] = {"challenge": q["code_challenge"], "redirect_uri": redirect, "person": person,
                             "client_id": q.get("client_id", ""), "exp": self.clock() + CODE_SECS}
        out = {"code": code, **({"state": q["state"]} if "state" in q else {})}
        return 302, None, {"Location": redirect + ("&" if "?" in redirect else "?") + urlencode(out)}

    def _token(self, form: dict, auth: str) -> Reply:
        client_id, _ = _client(form, auth)      # a typed client secret is ignored: the sign-in is the trust
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


def _form(q: dict, message: str) -> bytes:
    """The sign-in page: the OAuth fields ride along hidden."""
    e = html.escape
    hidden = "".join(f'<input type="hidden" name="{k}" value="{e(q[k])}">' for k in PARAMS if k in q)
    to = e(urlsplit(q.get("redirect_uri", "")).hostname or "")
    note = f'<p class="bad">{e(message)}</p>' if message else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Rook: sign in</title>
<style>:root{{color-scheme:light dark}}body{{font:15px/1.45 system-ui,sans-serif;margin:0;display:flex;min-height:100vh}}
form{{margin:auto;width:min(360px,100% - 32px);display:flex;flex-direction:column;gap:10px}}
input{{font:inherit;padding:8px;border:1px solid #8888;border-radius:6px}}
button{{font:inherit;padding:8px;border:0;border-radius:6px;background:#2563eb;color:#fff}}
.bad{{color:#d33}}.muted{{opacity:.7}}</style></head><body>
<form method="post" action="/authorize"><b>Rook</b>
<div>Sign in to connect <b>{to or "this app"}</b> to Rook as you.</div>{note}{hidden}
<label for="user">User name</label>
<input id="user" name="user" autocomplete="username" autocapitalize="none" spellcheck="false" required autofocus>
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button></form></body></html>""".encode("utf-8")


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
