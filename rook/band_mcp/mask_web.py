"""POST /internal/mask: reverse secret masking for the dashboard process.

Only the bridge holds the vault key and the plaintext values. The dashboard
(rook/remote/mask_client.py) sends what it is about to store or show and gets
it back with known values replaced by ``{{secret:name}}`` stubs. Gated by a
random token in ``mask.token`` (0600) beside the bridge's stores, since the
public hostname reaches this app too; the dashboard runs as the same user and
reads it from the shared data directory.
"""
import hmac
import json
import logging
import os
import secrets

from starlette.responses import JSONResponse
from starlette.routing import Route

from . import secret_mask
from ..core.db import atomic_write_text

log = logging.getLogger("rook.band_mcp.mask_web")

TOKEN_FILE = "mask.token"
MAX_BODY = 2_000_000


def ensure_token(store_dir: str) -> str:
    """The token in ``store_dir``/mask.token, created (0600) if missing."""
    path = os.path.join(store_dir or ".", TOKEN_FILE)
    try:
        with open(path, encoding="ascii") as f:
            tok = f.read().strip()
        if len(tok) >= 32:
            return tok
    except FileNotFoundError:
        pass
    tok = secrets.token_hex(32)
    atomic_write_text(path, tok, 0o600)
    return tok


def route(token: str) -> Route:
    async def mask(request):
        got = request.headers.get("authorization", "")
        if not token or not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
            return JSONResponse({"error": "unauthorized"}, 401)
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "too large"}, 413)
        try:
            data = json.loads(body)
            if not isinstance(data, dict) or "obj" not in data:
                raise ValueError
        except ValueError:
            return JSONResponse({"error": "expected {\"obj\": ...}"}, 400)
        if secret_mask.installed() is None:
            return JSONResponse({"error": "no vault on this hub"}, 503)
        try:
            out = secret_mask.installed().mask(data["obj"])
        except Exception:  # noqa: BLE001 — never log the payload
            log.exception("mask endpoint failed")
            return JSONResponse({"error": "masking failed"}, 500)
        return JSONResponse({"ok": True, "obj": out}, headers={"Cache-Control": "no-store"})
    return Route("/internal/mask", mask, methods=["POST"])


def is_secret_route(token: str) -> Route:
    """``POST /internal/is-secret`` -> ``{"secret": bool}``: whether the given
    string is a stored vault value (or an encoded form the masker knows). The
    dashboard asks this at sign-in so a login made with a value agents can
    inject (``{{secret:name}}``) cannot approve held calls (permissions 3.11).
    It returns only a boolean — never a name or a value."""
    async def is_secret(request):
        got = request.headers.get("authorization", "")
        if not token or not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
            return JSONResponse({"error": "unauthorized"}, 401)
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "too large"}, 413)
        try:
            data = json.loads(body)
            value = data["value"] if isinstance(data, dict) else None
            if not isinstance(value, str):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "expected {\"value\": \"...\"}"}, 400)
        masker = secret_mask.installed()
        if masker is None:
            return JSONResponse({"error": "no vault on this hub"}, 503)
        try:
            hit = masker.contains(value)
        except Exception:  # noqa: BLE001 — never log the payload
            log.exception("is-secret check failed")
            return JSONResponse({"error": "check failed"}, 500)
        return JSONResponse({"secret": bool(hit)}, headers={"Cache-Control": "no-store"})
    return Route("/internal/is-secret", is_secret, methods=["POST"])
