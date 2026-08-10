"""Thin OAuth 2.0 front-door so claude.ai's *web* connector can attach.

claude.ai web speaks OAuth (authorization-code + PKCE) — it won't take a bare
bearer header the way Claude Code / the desktop app / the API do. But we don't
want a real OAuth identity system: the band's only secret is its bearer tokens
(see :mod:`tokens`). So this shim makes the bearer token double as the OAuth
credential:

    * The **client secret** you paste into claude.ai's connector IS one of your
      existing rook tokens (the static token, or one minted at ``/tokens``).
    * ``/authorize`` auto-approves and hands back a code — no login page. The
      code is useless on its own; all trust lives in the client secret.
    * ``/token`` verifies that client secret against the same
      :class:`~rook.band_mcp.tokens.TokenStore` used for ``/mcp``, and returns
      **that same token** as the ``access_token``.

Net effect: to complete the flow you must already hold a valid rook token, and
what claude.ai ends up sending on ``/mcp`` is that token — exactly what a
header-only client would send. No separate OAuth token lifecycle, tokens are
unchanged, and revoking a token (at ``/tokens``) still cuts off access.

The shim is an ASGI wrapper mounted in front of the FastMCP app; it handles
three paths and passes everything else straight through, so it can never
interfere with ``/mcp`` or ``/tokens``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

if TYPE_CHECKING:
    from .tokens import TokenStore

log = logging.getLogger("rook.band_mcp.oauth_shim")

_CODE_TTL = 300          # authorization codes live 5 min
# Advertised access-token lifetime. The real expiry is whatever the underlying
# bearer enforces at /mcp (static token: never; API token: its ttl) — this is
# just what we tell claude.ai so it doesn't refresh needlessly.
_ACCESS_TTL = 31536000   # 1 year


class OAuthShim:
    def __init__(self, app, store: "TokenStore", base_url: str) -> None:
        self.app = app
        self.store = store
        self.base = base_url.rstrip("/")
        # code -> {challenge, redirect_uri, client_id, exp}
        self._codes: dict[str, dict] = {}

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path in ("/.well-known/oauth-authorization-server",
                    "/.well-known/openid-configuration"):
            resp: Response = self._metadata()
        elif path == "/authorize":
            resp = await self._authorize(Request(scope, receive))
        elif path == "/token":
            resp = await self._token(Request(scope, receive))
        else:
            return await self.app(scope, receive, send)
        await resp(scope, receive, send)

    # -- discovery -----------------------------------------------------------

    def _metadata(self) -> Response:
        # issuer carries the trailing slash to match the value FastMCP already
        # advertises in the protected-resource metadata's authorization_servers.
        # No registration_endpoint: claude.ai then uses the client_id/secret you
        # supply manually (that's the whole point — the secret is your token).
        return JSONResponse({
            "issuer": self.base + "/",
            "authorization_endpoint": self.base + "/authorize",
            "token_endpoint": self.base + "/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post", "client_secret_basic"],
            "scopes_supported": ["rook"],
        })

    # -- authorize (auto-approve) --------------------------------------------

    async def _authorize(self, request: Request) -> Response:
        q = request.query_params
        if q.get("response_type") != "code":
            return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
        redirect_uri = q.get("redirect_uri", "")
        challenge = q.get("code_challenge", "")
        if not redirect_uri.startswith("https://"):
            return JSONResponse(
                {"error": "invalid_request",
                 "error_description": "redirect_uri must be https"}, status_code=400)
        if not challenge or q.get("code_challenge_method", "S256") != "S256":
            return JSONResponse(
                {"error": "invalid_request",
                 "error_description": "PKCE with S256 is required"}, status_code=400)
        code = secrets.token_urlsafe(32)
        self._codes[code] = {
            "challenge": challenge,
            "redirect_uri": redirect_uri,
            "client_id": q.get("client_id", ""),
            "exp": time.time() + _CODE_TTL,
        }
        self._gc()
        sep = "&" if "?" in redirect_uri else "?"
        loc = redirect_uri + sep + urlencode({"code": code, "state": q.get("state", "")})
        log.info("authorize: auto-approved, redirecting to %s", redirect_uri)
        return RedirectResponse(loc, status_code=302)

    # -- token ---------------------------------------------------------------

    async def _token(self, request: Request) -> Response:
        form = await request.form()
        grant = form.get("grant_type", "")
        _cid, csecret = self._client_creds(request, form)

        if grant == "authorization_code":
            code = str(form.get("code", ""))
            verifier = str(form.get("code_verifier", ""))
            redirect_uri = form.get("redirect_uri")
            rec = self._codes.get(code)
            if not rec or rec["exp"] < time.time():
                return self._err("invalid_grant", "invalid or expired code")
            if redirect_uri and str(redirect_uri) != rec["redirect_uri"]:
                return self._err("invalid_grant", "redirect_uri mismatch")
            # PKCE S256 verification
            digest = hashlib.sha256(verifier.encode()).digest()
            calc = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if calc != rec["challenge"]:
                return self._err("invalid_grant", "PKCE verification failed")
            # The client secret must be a real rook bearer token.
            if not csecret or self.store.verify_bearer(csecret) is None:
                return self._err("invalid_client",
                                 "client_secret is not a valid rook token", 401)
            self._codes.pop(code, None)
            log.info("token: issued (client_secret is a valid rook token)")
            return self._issue(csecret)

        if grant == "refresh_token":
            rtok = str(form.get("refresh_token", ""))
            if not rtok or self.store.verify_bearer(rtok) is None:
                return self._err("invalid_grant", "invalid refresh token")
            return self._issue(rtok)

        return self._err("unsupported_grant_type", f"unsupported grant_type {grant!r}", 400)

    # -- helpers -------------------------------------------------------------

    def _issue(self, token: str) -> Response:
        # access_token IS the bearer the caller proved it holds; /mcp validates
        # it through the same TokenStore. refresh_token = same token, so a
        # refresh just re-confirms the token is still valid (still in the store).
        return JSONResponse({
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": _ACCESS_TTL,
            "refresh_token": token,
            "scope": "rook",
        }, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    def _client_creds(self, request: Request, form) -> tuple[str | None, str | None]:
        """Client id + secret from HTTP Basic or the form body (claude.ai uses
        whichever)."""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                dec = base64.b64decode(auth[6:]).decode("utf-8")
                if ":" in dec:
                    a, b = dec.split(":", 1)
                    return unquote(a), unquote(b)
            except Exception:
                pass
        cid = form.get("client_id")
        csecret = form.get("client_secret")
        return (str(cid) if cid is not None else None,
                str(csecret) if csecret is not None else None)

    def _err(self, code: str, desc: str, status: int = 400) -> Response:
        return JSONResponse({"error": code, "error_description": desc},
                            status_code=status,
                            headers={"Cache-Control": "no-store"})

    def _gc(self) -> None:
        now = time.time()
        for c in [c for c, r in self._codes.items() if r["exp"] < now]:
            self._codes.pop(c, None)
