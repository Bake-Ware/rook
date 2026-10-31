"""OAuth 2.1 for remote MCP clients (switchover gate R13): claude.ai's web
and mobile connectors only speak OAuth, so the MCP door takes OAuth access
tokens beside its bearer tokens.

The hub site is the authorization server (people sign in there; routes in
``doors/web/site.py``) and the MCP door is the resource server. Turn it on in
``hub.yaml``::

    oauth:
      issuer: https://hub.example          # the hub site's public address (behind TLS)
      resource: https://hub.example/mcp    # the MCP door's public address

The flow (MCP authorization, RFC 6749/7591/7636/8414/9728):

1. The client finds the door's protected resource metadata (a 401 names it),
   then the issuer's metadata, and registers itself (dynamic client
   registration, open: a client is nothing until a person allows it).
2. It sends the person to ``/oauth/authorize`` with a PKCE S256 challenge.
   The person signs in on the hub site and chooses the agent identity the
   connector acts as. A code (5 minutes, once) goes back to the client.
3. ``/oauth/token`` trades it for an access token (1 hour) and a refresh
   token (rotated on every use; a reused one revokes the connector; unused
   for 30 days, the connector lapses).

A **grant** is one connector: a client, the person who allowed it and the
agent it acts as. Its tokens authenticate as that agent acting for that
person, so dispatch intersects both (spec 8.3), like a minted
``token --person --agent``. Revoke one on the dashboard (More > Connectors,
``connector.revoke``), at ``/oauth/revoke``, or by taking the person or the
agent out of the config.

Kept in ``<data>/oauth.json`` (0600): clients (secrets hashed) and grants
(token hashes only). Codes and consents waiting on the person stay in memory.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import unquote_plus, urlencode, urlsplit

log = logging.getLogger("rook.hub.oauth")

ACCESS_SECS = 3600
REFRESH_IDLE = 30 * 86400
CODE_SECS = 300
CONSENT_SECS = 600
MAX_CLIENTS = 500
MAX_WAITING = 256              # codes and consents in memory
OLD_REFRESH = 20               # rotated-out refresh tokens remembered per grant, to catch reuse
AUTH_METHODS = ("none", "client_secret_basic", "client_secret_post")
SCOPE = "rook"
VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")


class OAuthError(Exception):
    """An OAuth error reply. With ``redirect`` the browser goes back to the
    client with it; without, the person sees it (a bad client or redirect
    must never be redirected to)."""

    def __init__(self, error: str, description: str, status: int = 400, redirect: str | None = None):
        super().__init__(description)
        self.error, self.description, self.status, self.redirect = error, description, status, redirect

    def body(self) -> dict:
        return {"error": self.error, "error_description": self.description}


def check_options(opts: dict) -> None:
    """``oauth:`` in hub.yaml: issuer and resource, https (http only on loopback)."""
    if not isinstance(opts, dict):
        raise ValueError("oauth must be a mapping")
    unknown = set(opts) - {"issuer", "resource"}
    if unknown:
        raise ValueError(f"oauth: unknown keys: {', '.join(sorted(unknown))}")
    for key in ("issuer", "resource"):
        v = opts.get(key)
        if not (isinstance(v, str) and _web_uri(v) and not urlsplit(v).query and "#" not in v):
            raise ValueError(f"oauth: {key} must be an https address (http only on localhost)")
    if urlsplit(opts["issuer"]).path not in ("", "/"):
        raise ValueError("oauth: issuer is the hub site's address, without a path")


class OAuth:
    def __init__(self, data: Path, issuer: str, resource: str, clock=time.time, alive=lambda person, agent: True):
        self.issuer, self.resource = issuer.rstrip("/"), resource.rstrip("/")
        self.clock = clock
        self.alive = alive          # (person, agent) -> still configured: no tokens for someone gone
        self.path = Path(data) / "oauth.json"
        self._lock = threading.Lock()
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        self.clients: dict[str, dict] = state.get("clients") or {}
        self.grants: dict[str, dict] = state.get("grants") or {}
        self.access: dict[str, list] = state.get("access") or {}    # sha256(token) -> [grant, exp]
        self._codes: dict[str, dict] = {}                            # sha256(code) -> request
        self._consents: dict[str, dict] = {}                         # consent id -> request

    # -- metadata ---------------------------------------------------------------

    def metadata(self) -> dict:
        """Authorization server metadata (RFC 8414)."""
        methods = list(AUTH_METHODS)
        return {"issuer": self.issuer,
                "authorization_endpoint": self.issuer + "/oauth/authorize",
                "token_endpoint": self.issuer + "/oauth/token",
                "registration_endpoint": self.issuer + "/oauth/register",
                "revocation_endpoint": self.issuer + "/oauth/revoke",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": methods,
                "revocation_endpoint_auth_methods_supported": methods,
                "scopes_supported": [SCOPE],
                "authorization_response_iss_parameter_supported": True}

    def resource_metadata(self) -> dict:
        """Protected resource metadata (RFC 9728), served by the MCP door."""
        return {"resource": self.resource, "authorization_servers": [self.issuer],
                "bearer_methods_supported": ["header"], "scopes_supported": [SCOPE], "resource_name": "Rook"}

    # -- registration (RFC 7591) ------------------------------------------------

    def register(self, body: dict) -> dict:
        uris = body.get("redirect_uris")
        if not (isinstance(uris, list) and 0 < len(uris) <= 10 and all(isinstance(u, str) and _redirect_ok(u)
                                                                         for u in uris)):
            raise OAuthError("invalid_redirect_uri",
                             "redirect_uris: 1 to 10 https addresses (http only on localhost), no fragment")
        method = body.get("token_endpoint_auth_method", "client_secret_basic")
        if method not in AUTH_METHODS:
            raise OAuthError("invalid_client_metadata", f"token_endpoint_auth_method: one of {', '.join(AUTH_METHODS)}")
        grants = body.get("grant_types", ["authorization_code"])
        if not (isinstance(grants, list) and "authorization_code" in grants
                and set(grants) <= {"authorization_code", "refresh_token"}):
            raise OAuthError("invalid_client_metadata", "grant_types: authorization_code (and refresh_token)")
        if body.get("response_types", ["code"]) != ["code"]:
            raise OAuthError("invalid_client_metadata", "response_types: code")
        name = body.get("client_name")
        name = " ".join(name.split())[:80] if isinstance(name, str) and name.strip() else "An MCP client"
        cid, secret, now = "rc_" + secrets.token_urlsafe(16), None, int(self.clock())
        rec = {"name": name, "redirect_uris": uris, "method": method, "created": now}
        if method != "none":
            secret = secrets.token_urlsafe(32)
            rec["secret"] = _digest(secret)
        with self._lock:
            self._make_room()
            self.clients[cid] = rec
            self._save()
        log.info("oauth client registered", extra={"fields": {"client": cid, "name": name}})
        out = {"client_id": cid, "client_id_issued_at": now, "client_name": name, "redirect_uris": uris,
               "token_endpoint_auth_method": method, "grant_types": ["authorization_code", "refresh_token"],
               "response_types": ["code"]}
        if secret:
            out.update(client_secret=secret, client_secret_expires_at=0)
        return out

    def _make_room(self) -> None:
        """Open registration: when full, forget the oldest client nobody allowed."""
        if len(self.clients) < MAX_CLIENTS:
            return
        used = {g["client"] for g in self.grants.values()}
        idle = sorted((c["created"], cid) for cid, c in self.clients.items() if cid not in used)
        if not idle:
            raise OAuthError("temporarily_unavailable", "too many clients", 503)
        del self.clients[idle[0][1]]

    # -- authorization ----------------------------------------------------------

    def begin(self, q: dict, session: str) -> dict:
        """Check an authorization request; the consent the person answers
        (``id`` goes back with their answer). Raises :class:`OAuthError`."""
        client = self.clients.get(q.get("client_id", ""))
        if client is None:
            raise OAuthError("invalid_client", "This app isn't registered with this hub. Add it again.")
        uri = q.get("redirect_uri") or (client["redirect_uris"][0] if len(client["redirect_uris"]) == 1 else "")
        if uri not in client["redirect_uris"]:
            raise OAuthError("invalid_request", "The app sent a return address it didn't register.")
        back = _Back(uri, q.get("state"), self.issuer)
        if q.get("response_type") != "code":
            raise back.error("unsupported_response_type", "response_type must be code")
        if not q.get("code_challenge") or q.get("code_challenge_method") != "S256":
            raise back.error("invalid_request", "PKCE with code_challenge_method S256 is required")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", q["code_challenge"]):
            raise back.error("invalid_request", "code_challenge is a base64url SHA-256")
        if q.get("resource") and q["resource"].rstrip("/") != self.resource:
            raise back.error("invalid_target", f"this hub's MCP door is {self.resource}")
        cid, now = secrets.token_urlsafe(24), self.clock()
        with self._lock:
            self._prune(now)
            if len(self._consents) >= MAX_WAITING:
                raise back.error("temporarily_unavailable", "too many sign-ins waiting", 503)
            self._consents[cid] = {"client": q["client_id"], "redirect_uri": uri, "state": q.get("state"),
                                   "challenge": q["code_challenge"], "session": session, "exp": now + CONSENT_SECS}
        return {"id": cid, "client": client["name"], "client_id": q["client_id"], "redirect_uri": uri,
                "return_to": urlsplit(uri).netloc}

    def decide(self, consent: str, session: str, person: str, agent: str | None) -> str:
        """The person's answer: where the browser goes next. ``agent`` None
        refuses. The caller checks the person and agent exist."""
        with self._lock:
            req = self._consents.get(consent) if isinstance(consent, str) else None
            if req is None or req["session"] != session or req["exp"] < self.clock():
                raise OAuthError("invalid_request", "This sign-in expired. Start again from the app.")
            del self._consents[consent]
            back = _Back(req["redirect_uri"], req["state"], self.issuer)
            if agent is None:
                return back.url(error="access_denied", error_description="the person said no")
            if req["client"] not in self.clients:
                raise OAuthError("invalid_client", "This app was removed. Add it again.")
            if len(self._codes) >= MAX_WAITING:
                return back.url(error="temporarily_unavailable", error_description="too many sign-ins waiting")
            code = secrets.token_urlsafe(32)
            self._codes[_digest(code)] = {"client": req["client"], "redirect_uri": req["redirect_uri"],
                                          "challenge": req["challenge"], "person": person, "agent": agent,
                                          "exp": self.clock() + CODE_SECS}
        log.info("oauth code issued", extra={"fields": {"client": req["client"], "person": person, "agent": agent}})
        return back.url(code=code)

    # -- tokens -----------------------------------------------------------------

    def token(self, form: dict, basic: tuple | None) -> tuple[dict, dict | None]:
        """The token endpoint: (reply, the grant it made or None)."""
        cid = self._client(form, basic)
        kind = form.get("grant_type")
        if form.get("resource") and form["resource"].rstrip("/") != self.resource:
            raise OAuthError("invalid_target", f"this hub's MCP door is {self.resource}")
        now = self.clock()
        with self._lock:
            self._prune(now)
            if kind == "authorization_code":
                d = _digest(form.get("code", ""))
                req = self._codes.get(d)
                if req is None or req["client"] != cid:
                    raise OAuthError("invalid_grant", "unknown or expired code")
                if "grant" in req:          # a code used twice: whoever holds it, the connector ends (RFC 9700)
                    self._revoke(req["grant"], "code reused")
                    self._save()
                    raise OAuthError("invalid_grant", "code already used")
                if form.get("redirect_uri", req["redirect_uri"]) != req["redirect_uri"]:
                    raise OAuthError("invalid_grant", "redirect_uri differs from the authorization request")
                verifier = form.get("code_verifier", "")
                if not VERIFIER.fullmatch(verifier) or not secrets.compare_digest(_s256(verifier), req["challenge"]):
                    raise OAuthError("invalid_grant", "PKCE verification failed")
                gid = "g_" + secrets.token_hex(8)
                req["grant"] = gid
                grant = {"id": gid, "client": cid, "name": self.clients[cid]["name"], "person": req["person"],
                         "agent": req["agent"], "created": int(now), "used": int(now), "old": []}
                self.grants[gid] = grant
            elif kind == "refresh_token":
                d = _digest(form.get("refresh_token", ""))
                grant = next((g for g in self.grants.values() if g.get("refresh") == d), None)
                if grant is None:
                    stolen = next((g for g in self.grants.values() if d in g["old"]), None)
                    if stolen is not None:     # a rotated-out token came back: someone else has a copy
                        self._revoke(stolen["id"], "refresh token reused")
                        self._save()
                    raise OAuthError("invalid_grant", "unknown refresh token")
                if grant["client"] != cid:
                    raise OAuthError("invalid_grant", "that refresh token is another client's")
                grant["old"] = (grant["old"] + [d])[-OLD_REFRESH:]
                grant["used"] = int(now)
            else:
                raise OAuthError("unsupported_grant_type", "grant_type: authorization_code or refresh_token")
            if not self.alive(grant["person"], grant["agent"]):
                self._revoke(grant["id"], "its person or agent is gone")
                self._save()
                raise OAuthError("invalid_grant", "the person or agent this connector acts as is gone")
            access, refresh = "rook_at_" + secrets.token_urlsafe(32), "rook_rt_" + secrets.token_urlsafe(32)
            grant["refresh"] = _digest(refresh)
            self.access[_digest(access)] = [grant["id"], int(now + ACCESS_SECS)]
            self._save()
        made = grant if kind == "authorization_code" else None
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_SECS,
                "refresh_token": refresh, "scope": SCOPE}, made

    def _client(self, form: dict, basic: tuple | None) -> str:
        """The client a token or revocation request authenticates as."""
        cid, secret = basic if basic else (form.get("client_id"), form.get("client_secret"))
        client = self.clients.get(cid or "")
        if client is None or (basic and form.get("client_id") not in (None, cid)):
            raise OAuthError("invalid_client", "unknown client", 401)
        if client["method"] != "none":
            if not (isinstance(secret, str) and secrets.compare_digest(_digest(secret), client["secret"])):
                raise OAuthError("invalid_client", "wrong client secret", 401)
        return cid

    def authenticate(self, token: str) -> tuple[str, str, str] | None:
        """An access token to (person, agent, grant), or None."""
        if not isinstance(token, str) or not token.startswith("rook_at_"):
            return None
        with self._lock:
            hit = self.access.get(_digest(token))
            grant = hit and self.grants.get(hit[0])
            if not grant or hit[1] <= self.clock():
                return None
            return grant["person"], grant["agent"], grant["id"]

    # -- revocation -------------------------------------------------------------

    def revoke_token(self, form: dict, basic: tuple | None) -> None:
        """RFC 7009: a refresh token ends its connector, an access token only
        itself. Unknown tokens are no error."""
        cid = self._client(form, basic)
        d = _digest(form.get("token", ""))
        with self._lock:
            hit = self.access.get(d)
            if hit and self.grants.get(hit[0], {}).get("client") == cid:
                del self.access[d]
            grant = next((g for g in self.grants.values() if g.get("refresh") == d and g["client"] == cid), None)
            if grant:
                self._revoke(grant["id"], "revoked by the client")
            self._save()

    def revoke(self, gid: str, by: str) -> dict | None:
        """End a connector (the dashboard). Its view, or None if unknown."""
        with self._lock:
            grant = self.grants.get(gid)
            if grant is None:
                return None
            self._revoke(gid, f"revoked by {by}")
            self._save()
        return _view(grant)

    def _revoke(self, gid: str, why: str) -> None:
        grant = self.grants.pop(gid, None)
        self.access = {d: v for d, v in self.access.items() if v[0] != gid}
        if grant:
            log.warning("oauth connector revoked", extra={"fields": {"grant": gid, "why": why,
                                                                     "person": grant["person"]}})

    def connectors(self, person: str | None = None) -> list[dict]:
        """Live connectors, newest first: one person's, or everyone's."""
        with self._lock:
            self._prune(self.clock())
            return [_view(g) for g in sorted(self.grants.values(), key=lambda g: -g["created"])
                    if person is None or g["person"] == person]

    def get(self, gid: str) -> dict | None:
        with self._lock:
            g = self.grants.get(gid)
            return _view(g) if g else None

    # -- upkeep -----------------------------------------------------------------

    def _prune(self, now: float) -> None:
        for gid in [gid for gid, g in self.grants.items() if g["used"] + REFRESH_IDLE <= now]:
            self._revoke(gid, "unused for 30 days")
        self.access = {d: v for d, v in self.access.items() if v[1] > now and v[0] in self.grants}
        self._codes = {d: c for d, c in self._codes.items() if c["exp"] > now}
        self._consents = {k: c for k, c in self._consents.items() if c["exp"] > now}

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"clients": self.clients, "grants": self.grants, "access": self.access}, f)
        os.replace(tmp, self.path)


class _Back:
    """The client's redirect: a result or an error, with state and iss (RFC 9207)."""

    def __init__(self, uri: str, state, issuer: str):
        self.uri, self.state, self.issuer = uri, state, issuer

    def url(self, **params) -> str:
        if isinstance(self.state, str):
            params["state"] = self.state
        params["iss"] = self.issuer
        return self.uri + ("&" if urlsplit(self.uri).query else "?") + urlencode(params)

    def error(self, error: str, description: str, status: int = 400) -> OAuthError:
        return OAuthError(error, description, status,
                          redirect=self.url(error=error, error_description=description))


def basic_auth(header: str) -> tuple | None:
    """``Authorization: Basic`` to (client_id, secret), form-decoded (RFC 6749 2.3.1)."""
    scheme, _, value = (header or "").partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        cid, sep, secret = base64.b64decode(value.strip(), validate=True).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        raise OAuthError("invalid_client", "bad Basic credentials", 401) from None
    if not sep:
        raise OAuthError("invalid_client", "bad Basic credentials", 401)
    return unquote_plus(cid), unquote_plus(secret)


def _view(g: dict) -> dict:
    return {"id": g["id"], "name": g["name"], "client": g["client"], "person": g["person"], "agent": g["agent"],
            "created": g["created"], "used": g["used"], "expires": g["used"] + REFRESH_IDLE}


def _web_uri(uri: str) -> bool:
    """https anywhere, http only on loopback; no user info, no odd characters."""
    if any(ord(c) <= 32 or ord(c) == 127 for c in uri) or "\\" in uri:
        return False
    try:
        u = urlsplit(uri)
        host = u.hostname
        u.port            # malformed or out of range raises
    except ValueError:
        return False
    if not host or u.username is not None or u.password is not None:
        return False
    if u.scheme == "https":
        return True
    if u.scheme != "http":
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _redirect_ok(uri: str) -> bool:
    return len(uri) <= 2048 and "#" not in uri and _web_uri(uri)


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


def _digest(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()
