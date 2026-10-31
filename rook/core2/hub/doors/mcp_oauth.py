"""A thin OAuth front for the MCP door, so claude.ai's connectors can attach.

claude.ai's web and mobile connectors only speak OAuth (authorization code +
PKCE); they can't send a pasted bearer header the way Claude Code can. Rook
doesn't need a second identity system for that: the door's bearer tokens
already name an agent acting for a person. So, as in 0.4's
``rook/band_mcp/oauth_shim.py``, a token doubles as the OAuth credential:

* The **client secret** pasted into claude.ai's connector (Advanced settings)
  IS an agent token: ``python -m rook.core2.hub --config hub.yaml token
  --person bake --agent claude-web``. A person's own token is refused, so the
  journal always names the agent.
* ``/authorize`` hands back a code at once: no sign-in or consent page. The
  code is worthless alone; all trust is in the client secret.
* ``/token`` checks the code (PKCE S256), checks the client secret like the
  door checks a bearer, and returns **that same token** as the access token
  (and as the refresh token, so a refresh only re-checks it).

What claude.ai then sends on ``/mcp`` is the token a header-only client would
send. Nothing is stored but codes (memory, 5 minutes, once each); revoking
the token (taking it out of ``<data>/tokens.json``) cuts the connector off.

On with the door option ``oauth: https://rook.example``: the door's public
address as clients reach it (https; http only on loopback). The door then
answers, beside ``/mcp``::

    GET  /.well-known/oauth-protected-resource[/mcp]   RFC 9728: names the issuer
    GET  /.well-known/oauth-authorization-server       RFC 8414 (no registration)
    GET  /authorize                                   code + PKCE, auto-approved
    POST /token                                       authorization_code | refresh_token

and a 401 on ``/mcp`` names the protected resource metadata.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
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

Reply = tuple[int, dict, dict]     # status, JSON body, extra headers


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
    """``authenticate(token)`` is the door's own bearer check (a Caller or None)."""

    def __init__(self, base: str, path: str, authenticate: Callable, clock: Callable = time.time):
        self.base, self.path = check_url(base), path
        self.authenticate, self.clock = authenticate, clock
        self._codes: dict[str, dict] = {}

    @property
    def resource(self) -> str:
        return self.base + self.path

    def challenge(self) -> str:
        """The 401's ``WWW-Authenticate`` (RFC 9728 5.1)."""
        return f'Bearer resource_metadata="{self.base}{PRM}{self.path}"'

    def owns(self, method: str, path: str) -> bool:
        return (method, path) in (("GET", "/authorize"), ("POST", "/token")) or \
            (method == "GET" and path in (PRM, PRM + self.path, ASM, "/.well-known/openid-configuration"))

    def handle(self, method: str, path: str, query: str, body: bytes, auth: str = "") -> Reply:
        """One request to an owned path; ``auth`` is its Authorization header."""
        if path in (PRM, PRM + self.path):
            return 200, {"resource": self.resource, "authorization_servers": [self.base],
                         "bearer_methods_supported": ["header"], "scopes_supported": [SCOPE]}, {}
        if path.startswith("/.well-known/"):
            # No registration_endpoint: claude.ai then takes the client id and
            # secret typed into the connector, and the secret is the token.
            return 200, {"issuer": self.base, "authorization_endpoint": self.base + "/authorize",
                         "token_endpoint": self.base + "/token", "response_types_supported": ["code"],
                         "grant_types_supported": ["authorization_code", "refresh_token"],
                         "code_challenge_methods_supported": ["S256"],
                         "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
                         "scopes_supported": [SCOPE]}, {}
        if path == "/authorize":
            return self._authorize(_fields(query))
        return self._token(_fields(body.decode("utf-8", "replace")), auth)

    def _authorize(self, q: dict) -> Reply:
        redirect = q.get("redirect_uri", "")
        if not _redirect_ok(redirect):
            return _err("invalid_request", "redirect_uri must be https, or http on a loopback address")
        if q.get("response_type") != "code":
            return _err("unsupported_response_type", "response_type must be code")
        if not q.get("code_challenge") or q.get("code_challenge_method", "S256") != "S256":
            return _err("invalid_request", "PKCE with S256 is required")
        if any(len(v) > MAX_FIELD for v in q.values()):
            return _err("invalid_request", "a field is too long")
        self._gc()
        if len(self._codes) >= MAX_CODES:
            return _err("temporarily_unavailable", "too many sign-ins waiting; try again shortly", 503)
        code = secrets.token_urlsafe(32)
        self._codes[code] = {"challenge": q["code_challenge"], "redirect_uri": redirect,
                             "client_id": q.get("client_id", ""), "exp": self.clock() + CODE_SECS}
        out = {"code": code, **({"state": q["state"]} if "state" in q else {})}
        return 302, None, {"Location": redirect + ("&" if "?" in redirect else "?") + urlencode(out)}

    def _token(self, form: dict, auth: str) -> Reply:
        client_id, secret = _client(form, auth)
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
            if not hmac.compare_digest(calc.rstrip(b"=").decode(), rec["challenge"]):
                return _err("invalid_grant", "PKCE verification failed")
            return self._issue(secret, "invalid_client", 401)
        if grant == "refresh_token":
            return self._issue(form.get("refresh_token", ""), "invalid_grant", 400)
        return _err("unsupported_grant_type", f"unsupported grant_type {grant!r}")

    def _issue(self, token: str, error: str, status: int) -> Reply:
        caller = self.authenticate(token) if token else None
        if caller is None:
            return _err(error, "not a valid Rook token", status)
        if not caller.agent:
            return _err(error, "use an agent's token (token --person <you> --agent <name>), "
                               "not your own", status)
        return 200, {"access_token": token, "token_type": "Bearer", "expires_in": ACCESS_SECS,
                     "refresh_token": token, "scope": SCOPE}, {"Pragma": "no-cache"}

    def _gc(self) -> None:
        now = self.clock()
        for c in [c for c, r in self._codes.items() if r["exp"] < now]:
            del self._codes[c]


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
