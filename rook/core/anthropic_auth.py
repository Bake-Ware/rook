"""Anthropic OAuth token manager — uses Claude Code's subscription auth.

Reads the OAuth credentials from ~/.claude/.credentials.json,
refreshes the access token when expired, and provides a valid token
for API calls.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
TOKEN_REFRESH_URL = "https://console.anthropic.com/v1/oauth/token"
# Refresh 5 minutes before actual expiry
EXPIRY_BUFFER_MS = 5 * 60 * 1000


class AnthropicAuth:
    """Manages OAuth tokens for Anthropic API access via Claude subscription."""

    def __init__(self, credentials_path: Path | None = None):
        self.credentials_path = credentials_path or CREDENTIALS_PATH
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: int = 0
        self._load_credentials()

    def _load_credentials(self) -> None:
        """Load credentials from disk."""
        if not self.credentials_path.exists():
            log.warning("No Claude credentials found at %s", self.credentials_path)
            return

        try:
            data = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            oauth = data.get("claudeAiOauth", {})
            self._access_token = oauth.get("accessToken")
            self._refresh_token = oauth.get("refreshToken")
            self._expires_at = oauth.get("expiresAt", 0)
            log.info("Loaded Anthropic OAuth credentials (expires at %d)", self._expires_at)
        except Exception as e:
            log.error("Failed to load credentials: %s", e)

    def _save_credentials(self) -> None:
        """Save updated credentials to disk."""
        try:
            if self.credentials_path.exists():
                data = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            else:
                data = {}

            data["claudeAiOauth"] = {
                **data.get("claudeAiOauth", {}),
                "accessToken": self._access_token,
                "refreshToken": self._refresh_token,
                "expiresAt": self._expires_at,
            }
            self.credentials_path.write_text(
                json.dumps(data), encoding="utf-8"
            )
            log.info("Saved refreshed credentials")
        except Exception as e:
            log.error("Failed to save credentials: %s", e)

    @property
    def is_expired(self) -> bool:
        now_ms = int(time.time() * 1000)
        return now_ms >= (self._expires_at - EXPIRY_BUFFER_MS)

    async def get_token(self) -> str | None:
        """Get a valid access token, refreshing if needed."""
        if not self._access_token:
            self._load_credentials()

        if self._access_token and not self.is_expired:
            return self._access_token

        # Token expired — try reload from disk first (Claude Code login may have refreshed it)
        self._load_credentials()
        if self._access_token and not self.is_expired:
            log.info("Picked up refreshed token from disk")
            return self._access_token

        # Still expired — try refresh ourselves
        if self._refresh_token:
            await self._refresh()

        return self._access_token

    async def _refresh(self) -> None:
        """Refresh the OAuth access token."""
        log.info("Refreshing Anthropic OAuth token...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    TOKEN_REFRESH_URL,
                    json={
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                        "client_id": "claude-code",
                    },
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.error("Token refresh failed (%d): %s", resp.status, body)
                        return

                    data = await resp.json()
                    self._access_token = data.get("access_token")
                    self._refresh_token = data.get("refresh_token", self._refresh_token)
                    expires_in = data.get("expires_in", 3600)
                    self._expires_at = int(time.time() * 1000) + (expires_in * 1000)
                    self._save_credentials()
                    log.info("Token refreshed, expires in %ds", expires_in)
        except Exception as e:
            log.error("Token refresh error: %s", e)
