"""Bearer-token-only auth for the band MCP. No OAuth.

Two ways to authenticate:
  * a single fixed ``static_token`` (env ``ROOK_MCP_STATIC_TOKEN``) — paste it
    once into a client's Authorization header, done.
  * long-lived, named, revocable per-agent tokens minted through the
    password-gated ``/tokens`` admin page (see :mod:`api_tokens_ui`) — for
    headless agents that want their own bearer instead of sharing the fixed
    one.

No client registration, no authorize/consent screen, no code exchange —
FastMCP is handed a plain :class:`mcp.server.auth.provider.TokenVerifier` and
never mounts any OAuth authorization-server routes.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any

from mcp.server.auth.provider import AccessToken, TokenVerifier

log = logging.getLogger("rook.band_mcp.tokens")

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


class TokenStore:
    """Fixed static token + a persisted set of named/revocable API tokens.

    ``admin_password`` gates the ``/tokens`` management page (mint/list/
    revoke). If unset, the page's login can never succeed — no admin
    password means no way to mint new tokens through the UI, but existing
    persisted tokens and the static token still verify fine.
    """

    def __init__(self, admin_password: str | None = None,
                 persist_path: str | None = None,
                 static_token: str | None = None) -> None:
        self._password = admin_password or None
        self._persist_path = persist_path
        self._static_token = static_token if (static_token and len(static_token) >= 16) else None
        # API tokens for headless agents. Keyed by full token. Each value:
        #   {token, id, name, scopes, created_at, last_used_at, expires_at}
        # expires_at = None means no expiry.
        self._api_tokens: dict[str, dict[str, Any]] = {}
        # Admin UI sessions for /tokens page. session_id -> expires_at.
        self._admin_sessions: dict[str, int] = {}
        self._lock = asyncio.Lock()

        if persist_path:
            self._load()

    def _load(self) -> None:
        """Reads ``api_tokens`` from the persist file. Any other keys (e.g.
        leftover ``clients``/``access``/``refresh`` from a pre-bearer-only
        oauth.json) are silently ignored — this is how existing minted
        tokens migrate forward without a dedicated migration step."""
        import os, json as _json
        if not self._persist_path or not os.path.exists(self._persist_path):
            return
        try:
            with open(self._persist_path) as f:
                data = _json.load(f)
        except Exception:
            log.exception("failed to load %s", self._persist_path)
            return
        for t in data.get("api_tokens", []):
            tok = t.get("token")
            if tok:
                self._api_tokens[tok] = t
        log.info("loaded token store: api_tokens=%d", len(self._api_tokens))

    def _save(self) -> None:
        if not self._persist_path:
            return
        import os, json as _json, tempfile
        try:
            data = {"api_tokens": list(self._api_tokens.values())}
            d = os.path.dirname(self._persist_path) or "."
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".tokens-", suffix=".json")
            with os.fdopen(fd, "w") as f:
                _json.dump(data, f, indent=2)
            os.replace(tmp, self._persist_path)
            os.chmod(self._persist_path, 0o600)
        except Exception:
            log.exception("token store persist failed")

    def _now(self) -> int:
        return int(time.time())

    def verify_password(self, candidate: str) -> bool:
        if not self._password:
            return False
        return secrets.compare_digest(self._password, candidate or "")

    # -- bearer verification --------------------------------------------------

    def verify_bearer(self, token: str) -> AccessToken | None:
        if not token:
            return None
        # Fixed static bearer — checked first, constant-time.
        if self._static_token and secrets.compare_digest(self._static_token, token):
            return AccessToken(token=token, client_id="static",
                               scopes=["rook"], expires_at=None, resource=None)
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


class StoreTokenVerifier(TokenVerifier):
    """Adapts a :class:`TokenStore` to FastMCP's TokenVerifier interface."""

    def __init__(self, store: TokenStore) -> None:
        self._store = store

    async def verify_token(self, token: str) -> AccessToken | None:
        return self._store.verify_bearer(token)
