"""Reverse secret masking for the dashboard, done by the MCP bridge.

The dashboard never opens the vault (no key, no plaintext in this process).
What it stores or shows that may carry a vault value (chat messages and
titles it writes, chat it displays) goes through the bridge's
``POST /internal/mask`` (rook/band_mcp/mask_web.py), authenticated with the
token the bridge keeps in ``mask.token`` beside its stores (over HTTP, or in
memory when ``rook hub serve`` runs both in one process: rook/remote/upstream.py).
When the bridge cannot mask, the content passes through unchanged and an error
is logged.
"""
import logging
import os
import time

from . import upstream

log = logging.getLogger("rook.remote.mask_client")

URL = ("ROOK_MASK_URL", "http://127.0.0.1:8765/internal/mask")


class BridgeMask:
    TIMEOUT = 5
    ERROR_EVERY = 60.0   # seconds between repeated error logs

    def __init__(self, token_paths, url: str | None = None) -> None:
        self.token_paths = [p for p in token_paths if p]
        self.url = url or os.environ.get(*URL)
        self._token: str | None = None
        self._last_error = 0.0

    def _read_token(self) -> str | None:
        for path in self.token_paths:
            try:
                with open(path, encoding="ascii") as f:
                    tok = f.read().strip()
                if tok:
                    return tok
            except OSError:
                continue
        return None

    def _error(self, why: str) -> None:
        now = time.monotonic()
        if now - self._last_error >= self.ERROR_EVERY:
            self._last_error = now
            log.error("secret masking unavailable in the dashboard (%s); "
                      "content is stored/shown unmasked", why)

    async def is_secret(self, value: str) -> "bool | None":
        """Whether ``value`` is a stored vault secret (or an encoded form the
        masker knows), so a sign-in with it can't approve held calls
        (permissions 3.11). ``None`` when the vault can't be reached: the
        caller fails closed (refuses approvals). Never sends names.

        Fast path: in one-process ``rook hub serve`` the vault is installed in
        this process, so ask it directly."""
        if not isinstance(value, str) or not value:
            return False
        try:
            from ..band_mcp import secret_mask
            masker = secret_mask.installed()
            if masker is not None:
                return bool(masker.contains(value))
        except Exception:  # noqa: BLE001
            log.debug("direct vault is-secret failed; trying the bridge", exc_info=True)
        url = self.url.replace("/internal/mask", "/internal/is-secret")
        for attempt in (0, 1):
            if self._token is None:
                self._token = self._read_token()
            if not self._token:
                self._error("no mask token from the MCP bridge")
                return None
            try:
                status, data = await upstream.request_json(
                    "POST", url, json={"value": value}, timeout=self.TIMEOUT,
                    headers={"Authorization": f"Bearer {self._token}"})
                if status == 401 and attempt == 0:
                    self._token = None
                    continue
                if status != 200:
                    self._error(f"bridge answered {status}")
                    return None
                return bool(data.get("secret"))
            except upstream.Unavailable as e:
                self._error(str(e) or "unavailable")
                return None
            except (KeyError, TypeError) as e:
                self._error(type(e).__name__)
                return None
        self._error("bridge rejected the mask token")
        return None

    async def mask(self, obj):
        """``obj`` with known vault values as {{secret:name}} stubs; ``obj``
        unchanged (and an error logged) if the bridge cannot do it."""
        if obj is None or obj == "" or obj == [] or obj == {}:
            return obj
        for attempt in (0, 1):
            if self._token is None:
                self._token = self._read_token()
            if not self._token:
                self._error("no mask token from the MCP bridge")
                return obj
            try:
                status, data = await upstream.request_json(
                    "POST", self.url, json={"obj": obj}, timeout=self.TIMEOUT,
                    headers={"Authorization": f"Bearer {self._token}"})
                if status == 401 and attempt == 0:
                    self._token = None   # the bridge made a new one; re-read
                    continue
                if status != 200:
                    self._error(f"bridge answered {status}")
                    return obj
                return data["obj"]
            except upstream.Unavailable as e:
                self._error(str(e) or "unavailable")
                return obj
            except (KeyError, TypeError) as e:
                self._error(type(e).__name__)
                return obj
        self._error("bridge rejected the mask token")
        return obj
