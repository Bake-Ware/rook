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
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(tok)
    os.replace(tmp, path)
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
