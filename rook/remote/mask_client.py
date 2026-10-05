"""Reverse secret masking for the dashboard, done by the MCP bridge.

The dashboard never opens the vault (no key, no plaintext in this process).
What it stores or shows that may carry a vault value (chat messages and
titles it writes, chat it displays) goes through the bridge's
``POST /internal/mask`` (rook/band_mcp/mask_web.py), authenticated with the
token the bridge keeps in ``mask.token`` beside its stores. When the bridge
cannot mask, the content passes through unchanged and an error is logged.
"""
import logging
import os
import time

import aiohttp

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
                async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=self.TIMEOUT)) as http:
                    async with http.post(self.url, json={"obj": obj},
                                         headers={"Authorization": f"Bearer {self._token}"}) as r:
                        if r.status == 401 and attempt == 0:
                            self._token = None   # the bridge made a new one; re-read
                            continue
                        if r.status != 200:
                            self._error(f"bridge answered {r.status}")
                            return obj
                        data = await r.json()
                        return data["obj"]
            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError) as e:
                self._error(type(e).__name__)
                return obj
        self._error("bridge rejected the mask token")
        return obj
