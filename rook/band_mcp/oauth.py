"""Static bearer-token verifier + minimal single-user OAuth provider.

By default the band MCP uses :class:`StaticTokenVerifier` — claude.ai's
custom-connector UI lets you paste a bearer header, no OAuth dance. The
:class:`InMemoryProvider` below is the OAuth path kept around as an
opt-in if you ever flip the switch back.

Minimal single-user OAuth 2.1 provider for the band MCP.

Single shared admin password gates the authorize step. All authenticated
sessions get the same set of scopes — there is exactly one user (you).
Tokens, codes, and registered clients live in memory; a restart invalidates
everything and the next claude.ai connector use will re-register and
re-authorize, which costs the user one password prompt.

Adequate for "just me" use. NOT adequate for multi-tenant.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenVerifier,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

log = logging.getLogger("rook.band_mcp.oauth")


_CODE_TTL = 300            # 5 min
_ACCESS_TTL = 3600         # 1 hr
_REFRESH_TTL = 86400 * 30  # 30 days
_ADMIN_SESSION_TTL = 1800  # 30 min — UI-only session for /tokens page


class StaticTokenVerifier(TokenVerifier):
    """Accept exactly one bearer token. No expiry, no scope check beyond
    presence. Sized for "I just need a working header on claude.ai"."""

    def __init__(self, token: str, scopes: list[str] | None = None) -> None:
        if not token or len(token) < 16:
            raise ValueError("static token must be at least 16 characters")
        self._token = token
        self._scopes = scopes or ["rook"]

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        if not secrets.compare_digest(self._token, token):
            return None
        return AccessToken(
            token=token, client_id="static", scopes=self._scopes,
            expires_at=None, resource=None,
        )


class InMemoryProvider(OAuthAuthorizationServerProvider):
    """OAuth state with optional JSON persistence.

    A single admin password gates the authorize step. Clients, access
    tokens, and refresh tokens are persisted to ``persist_path`` (atomic
    write) so the connector survives service restarts. In-flight
    authorization codes and pending password sessions stay in memory —
    they're short-lived and regenerating them costs nothing.
    """

    def __init__(self, auth_password: str,
                 persist_path: str | None = None,
                 preset_client: tuple[str, str, list[str]] | None = None,
                 static_token: str | None = None) -> None:
        if not auth_password:
            raise ValueError("auth_password required")
        self._password = auth_password
        self._persist_path = persist_path
        # Optional fixed bearer accepted alongside OAuth/API tokens, so a
        # header-only client can authenticate without the OAuth flow. Set via
        # ROOK_MCP_STATIC_TOKEN. Ignored if too short to be a real secret.
        self._static_token = static_token if (static_token and len(static_token) >= 16) else None
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._access: dict[str, AccessToken] = {}
        self._refresh: dict[str, RefreshToken] = {}
        self._pending: dict[str, tuple[OAuthClientInformationFull, AuthorizationParams]] = {}
        # API tokens for headless agents. Keyed by full token. Each value:
        #   {token, id, name, scopes, created_at, last_used_at, expires_at}
        # expires_at = None means no expiry.
        self._api_tokens: dict[str, dict[str, Any]] = {}
        # Admin UI sessions for /tokens page. session_id -> expires_at.
        self._admin_sessions: dict[str, int] = {}
        self._lock = asyncio.Lock()

        if persist_path:
            self._load()

        if preset_client is not None:
            cid, csecret, redirect_uris = preset_client
            from pydantic import AnyUrl
            self._clients[cid] = OAuthClientInformationFull(
                client_id=cid,
                client_secret=csecret,
                redirect_uris=[AnyUrl(u) for u in redirect_uris],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="client_secret_basic",
                scope="rook",
                client_name="preset",
            )
            log.info("preset OAuth client installed: %s", cid)
            self._save()

    def _load(self) -> None:
        import os, json as _json
        if not self._persist_path or not os.path.exists(self._persist_path):
            return
        try:
            with open(self._persist_path) as f:
                data = _json.load(f)
        except Exception:
            log.exception("failed to load %s", self._persist_path)
            return
        for c in data.get("clients", []):
            try:
                self._clients[c["client_id"]] = OAuthClientInformationFull(**c)
            except Exception:
                log.exception("bad client entry, skipping: %s", c.get("client_id"))
        for t in data.get("access", []):
            self._access[t["token"]] = AccessToken(**t)
        for t in data.get("refresh", []):
            self._refresh[t["token"]] = RefreshToken(**t)
        for t in data.get("api_tokens", []):
            tok = t.get("token")
            if tok:
                self._api_tokens[tok] = t
        log.info("loaded oauth state: clients=%d access=%d refresh=%d api=%d",
                 len(self._clients), len(self._access), len(self._refresh),
                 len(self._api_tokens))

    def _save(self) -> None:
        if not self._persist_path:
            return
        import os, json as _json, tempfile
        try:
            data = {
                "clients": [c.model_dump(mode="json") for c in self._clients.values()],
                "access": [t.model_dump(mode="json") for t in self._access.values()],
                "refresh": [t.model_dump(mode="json") for t in self._refresh.values()],
                "api_tokens": list(self._api_tokens.values()),
            }
            d = os.path.dirname(self._persist_path) or "."
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".oauth-", suffix=".json")
            with os.fdopen(fd, "w") as f:
                _json.dump(data, f, indent=2)
            os.replace(tmp, self._persist_path)
            os.chmod(self._persist_path, 0o600)
        except Exception:
            log.exception("oauth persist failed")

    # -- helpers -------------------------------------------------------------

    def _now(self) -> int:
        return int(time.time())

    def verify_password(self, candidate: str) -> bool:
        return secrets.compare_digest(self._password, candidate or "")

    # -- client registration -------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        log.info("OAuth client registered: %s (redirect_uris=%s)",
                 client_info.client_id, [str(u) for u in client_info.redirect_uris])
        self._clients[client_info.client_id] = client_info
        self._save()

    # -- authorize -----------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        # Stash the request; the user lands on /authorize?session=<id> next.
        async with self._lock:
            sid = secrets.token_urlsafe(24)
            self._pending[sid] = (client, params)
        # Where to send the user to enter the password.
        return f"/oauth/authorize?session={sid}"

    async def consume_authorize(self, session_id: str, password: str
                                 ) -> tuple[str, str] | None:
        """Verify password, mint auth code, return ``(redirect_url, code)`` or
        ``None`` if password failed."""
        if not self.verify_password(password):
            return None
        async with self._lock:
            pending = self._pending.pop(session_id, None)
        if pending is None:
            return None
        client, params = pending
        code = secrets.token_urlsafe(32)
        ac = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=self._now() + _CODE_TTL,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        self._codes[code] = ac
        qs = urlencode({"code": code, "state": params.state or ""})
        sep = "&" if "?" in str(params.redirect_uri) else "?"
        return (f"{params.redirect_uri}{sep}{qs}", code)

    # -- code exchange -------------------------------------------------------

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                       authorization_code: str
                                       ) -> AuthorizationCode | None:
        ac = self._codes.get(authorization_code)
        if ac is None or ac.expires_at < self._now() or ac.client_id != client.client_id:
            return None
        return ac

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                           authorization_code: AuthorizationCode
                                           ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        self._access[access] = AccessToken(
            token=access, client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=self._now() + _ACCESS_TTL,
            resource=authorization_code.resource,
        )
        self._refresh[refresh] = RefreshToken(
            token=refresh, client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=self._now() + _REFRESH_TTL,
        )
        self._save()
        return OAuthToken(access_token=access, token_type="Bearer",
                          expires_in=_ACCESS_TTL, refresh_token=refresh,
                          scope=" ".join(authorization_code.scopes))

    # -- refresh -------------------------------------------------------------

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                  refresh_token: str) -> RefreshToken | None:
        rt = self._refresh.get(refresh_token)
        if rt is None or rt.expires_at < self._now() or rt.client_id != client.client_id:
            return None
        return rt

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                      refresh_token: RefreshToken,
                                      scopes: list[str]) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        self._access[access] = AccessToken(
            token=access, client_id=client.client_id,
            scopes=refresh_token.scopes,
            expires_at=self._now() + _ACCESS_TTL,
            resource=None,
        )
        self._save()
        return OAuthToken(access_token=access, token_type="Bearer",
                          expires_in=_ACCESS_TTL, refresh_token=refresh_token.token,
                          scope=" ".join(refresh_token.scopes))

    # -- access tokens -------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Fixed static bearer (no OAuth dance) — checked first, constant-time.
        if self._static_token and secrets.compare_digest(self._static_token, token or ""):
            return AccessToken(token=token, client_id="static",
                               scopes=["rook"], expires_at=None, resource=None)
        at = self._access.get(token)
        if at is not None and at.expires_at >= self._now():
            return at
        # Fall through to API tokens — long-lived bearers for headless agents.
        api = self._api_tokens.get(token)
        if api is None:
            return None
        exp = api.get("expires_at")
        if exp is not None and exp < self._now():
            return None
        api["last_used_at"] = self._now()
        # Skip _save() here — would write on every authenticated request.
        return AccessToken(
            token=token,
            client_id=api.get("client_id", "api"),
            scopes=api.get("scopes") or ["rook"],
            expires_at=exp,
            resource=None,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._access.pop(token.token, None)
        self._refresh.pop(token.token, None)
        self._api_tokens.pop(token.token, None)
        self._save()

    # -- API tokens (headless-agent bearers) ---------------------------------

    def list_api_tokens(self) -> list[dict[str, Any]]:
        """Return token metadata (token secret elided) for the management UI."""
        out = []
        for t in self._api_tokens.values():
            out.append({
                "id": t.get("id"),
                "name": t.get("name"),
                "created_at": t.get("created_at"),
                "last_used_at": t.get("last_used_at"),
                "expires_at": t.get("expires_at"),
                "preview": (t.get("token", "")[:8] + "…"
                            + t.get("token", "")[-4:]) if t.get("token") else "",
            })
        out.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
        return out

    def mint_api_token(self, name: str,
                       ttl_seconds: int | None = None,
                       scopes: list[str] | None = None) -> dict[str, Any]:
        """Create a new long-lived API token. The secret is returned ONCE —
        the caller must show it to the user immediately; we only keep enough
        to identify + revoke it afterwards."""
        secret = secrets.token_urlsafe(32)
        tok_id = secrets.token_hex(4)
        entry = {
            "token": secret,
            "id": tok_id,
            "name": (name or "unnamed")[:64],
            "scopes": scopes or ["rook"],
            "client_id": "api",
            "created_at": self._now(),
            "last_used_at": None,
            "expires_at": (self._now() + ttl_seconds) if ttl_seconds else None,
        }
        self._api_tokens[secret] = entry
        self._save()
        log.info("api token minted: id=%s name=%s ttl=%s",
                 tok_id, entry["name"], ttl_seconds)
        return entry

    def revoke_api_token(self, token_id: str) -> bool:
        for tok, entry in list(self._api_tokens.items()):
            if entry.get("id") == token_id:
                self._api_tokens.pop(tok, None)
                self._save()
                log.info("api token revoked: id=%s name=%s",
                         token_id, entry.get("name"))
                return True
        return False

    # -- admin UI sessions ---------------------------------------------------

    def admin_login(self, password: str) -> str | None:
        if not self.verify_password(password):
            return None
        sid = secrets.token_urlsafe(24)
        self._admin_sessions[sid] = self._now() + _ADMIN_SESSION_TTL
        return sid

    def admin_session_ok(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        exp = self._admin_sessions.get(session_id)
        if exp is None:
            return False
        if exp < self._now():
            self._admin_sessions.pop(session_id, None)
            return False
        return True

    def admin_logout(self, session_id: str | None) -> None:
        if session_id:
            self._admin_sessions.pop(session_id, None)


# ---------- Starlette route helpers ----------

_LOGIN_HTML = """<!doctype html>
<html><head><title>Rook MCP Authorize</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:24rem;margin:4rem auto;padding:0 1rem;background:#111;color:#eee}}
h1{{font-size:1.2rem;color:#0c9}}
form{{display:flex;flex-direction:column;gap:.8rem}}
input{{padding:.6rem;background:#222;color:#eee;border:1px solid #444;border-radius:4px;font-size:1rem}}
button{{padding:.7rem;background:#0a7;color:#fff;border:0;border-radius:4px;font-size:1rem;cursor:pointer}}
.err{{color:#f55;font-size:.9rem}}
small{{color:#888;font-size:.85rem}}
</style></head>
<body>
<h1>Rook MCP — authorize</h1>
<p><small>A Claude session is asking for access. Enter the admin password to grant a token.</small></p>
{err}
<form method="POST">
<input type="hidden" name="session" value="{session}"/>
<input type="password" name="password" autofocus required placeholder="Admin password"/>
<button type="submit">Authorize</button>
</form>
</body></html>
"""


async def _lenient_token(request: Request, provider: "InMemoryProvider") -> Response:
    """Custom /token endpoint that accepts client credentials via either
    Basic auth or form body, regardless of the client's recorded
    ``token_endpoint_auth_method``. Claude.ai sends them whichever way it
    feels like — we stop arguing about it.

    Implements RFC 6749 §4.1.3 (authorization_code) + §6 (refresh_token)
    plus RFC 7636 (PKCE).
    """
    import base64
    import hashlib
    from starlette.responses import JSONResponse

    form = await request.form()
    grant_type = form.get("grant_type", "")

    # Pull client_id + client_secret from either Basic or form.
    auth_header = request.headers.get("Authorization", "")
    basic_id: str | None = None
    basic_secret: str | None = None
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            if ":" in decoded:
                from urllib.parse import unquote
                a, b = decoded.split(":", 1)
                basic_id, basic_secret = unquote(a), unquote(b)
        except Exception:
            pass

    form_id = form.get("client_id")
    form_secret = form.get("client_secret")
    client_id = basic_id or form_id
    client_secret = basic_secret or form_secret

    if not client_id:
        return JSONResponse({"error": "invalid_client",
                             "error_description": "missing client_id"},
                            status_code=401)
    client = provider._clients.get(str(client_id))
    if client is None:
        return JSONResponse({"error": "invalid_client",
                             "error_description": "unknown client"},
                            status_code=401)
    if client.client_secret:
        if not client_secret or not secrets.compare_digest(
                client.client_secret, str(client_secret)):
            log.warning("/token bad client_secret for %s", client_id)
            return JSONResponse({"error": "invalid_client",
                                 "error_description": "bad client_secret"},
                                status_code=401)

    if grant_type == "authorization_code":
        code = form.get("code", "")
        verifier = form.get("code_verifier", "")
        redirect_uri = form.get("redirect_uri")
        ac = provider._codes.get(str(code))
        if ac is None or ac.expires_at < provider._now() or ac.client_id != client_id:
            log.warning("/token invalid code for client=%s", client_id)
            return JSONResponse({"error": "invalid_grant",
                                 "error_description": "invalid or expired code"},
                                status_code=400)
        # PKCE check (S256 only — that's what we advertised)
        challenge = hashlib.sha256(verifier.encode()).digest()
        b64 = base64.urlsafe_b64encode(challenge).rstrip(b"=").decode()
        if b64 != ac.code_challenge:
            log.warning("/token PKCE mismatch")
            return JSONResponse({"error": "invalid_grant",
                                 "error_description": "PKCE verification failed"},
                                status_code=400)
        # redirect_uri must match if it was specified at /authorize
        if (ac.redirect_uri_provided_explicitly and redirect_uri
                and str(ac.redirect_uri) != str(redirect_uri)):
            log.warning("/token redirect_uri mismatch %r vs %r",
                         ac.redirect_uri, redirect_uri)
            return JSONResponse({"error": "invalid_grant",
                                 "error_description": "redirect_uri mismatch"},
                                status_code=400)
        # consume code, mint tokens
        provider._codes.pop(str(code), None)
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        provider._access[access] = AccessToken(
            token=access, client_id=str(client_id), scopes=ac.scopes,
            expires_at=provider._now() + _ACCESS_TTL, resource=ac.resource,
        )
        provider._refresh[refresh] = RefreshToken(
            token=refresh, client_id=str(client_id), scopes=ac.scopes,
            expires_at=provider._now() + _REFRESH_TTL,
        )
        provider._save()
        log.info("/token issued access+refresh for %s", client_id)
        return JSONResponse({
            "access_token": access, "token_type": "Bearer",
            "expires_in": _ACCESS_TTL, "refresh_token": refresh,
            "scope": " ".join(ac.scopes),
        }, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    if grant_type == "refresh_token":
        rtok = form.get("refresh_token", "")
        rt = provider._refresh.get(str(rtok))
        if rt is None or rt.expires_at < provider._now() or rt.client_id != client_id:
            return JSONResponse({"error": "invalid_grant",
                                 "error_description": "invalid refresh token"},
                                status_code=400)
        access = secrets.token_urlsafe(32)
        provider._access[access] = AccessToken(
            token=access, client_id=str(client_id), scopes=rt.scopes,
            expires_at=provider._now() + _ACCESS_TTL, resource=None,
        )
        provider._save()
        return JSONResponse({
            "access_token": access, "token_type": "Bearer",
            "expires_in": _ACCESS_TTL, "refresh_token": rt.token,
            "scope": " ".join(rt.scopes),
        }, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


def build_oauth_routes(provider: InMemoryProvider) -> list[Route]:
    async def authorize_form(request: Request) -> Response:
        sid = request.query_params.get("session", "")
        if not sid or sid not in provider._pending:
            return HTMLResponse("<h1>Invalid or expired authorize session</h1>",
                                status_code=400)
        return HTMLResponse(_LOGIN_HTML.format(session=sid, err=""))

    async def authorize_submit(request: Request) -> Response:
        form = await request.form()
        sid = form.get("session", "")
        password = form.get("password", "")
        result = await provider.consume_authorize(sid, password)
        if result is None:
            return HTMLResponse(
                _LOGIN_HTML.format(session=sid,
                                   err='<p class="err">Bad password or expired session.</p>'),
                status_code=401)
        redirect_url, _code = result
        return RedirectResponse(redirect_url, status_code=302)

    async def token_handler(request: Request) -> Response:
        return await _lenient_token(request, provider)

    return [
        # Our lenient /token must shadow MCP's strict one. The caller is
        # expected to .insert(0, …) these into the Starlette router so they
        # match before any MCP-installed routes with the same path.
        Route("/token", token_handler, methods=["POST"]),
        Route("/oauth/authorize", authorize_form, methods=["GET"]),
        Route("/oauth/authorize", authorize_submit, methods=["POST"]),
    ]
