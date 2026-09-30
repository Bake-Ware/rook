"""``telegram.*``: Telegram bot integration (hub plugin, worker ``rook``).

Bridges the configured Rook chat rooms to one Telegram chat, sends
notifications (``telegram.send``, or ``notify.send`` for every channel) and
answers a small command set (``/help``, ``/workers``, ``/rooms``, ``/call``)
under the principal ``integration:telegram``. Long-polls the Bot API with
``getUpdates`` over aiohttp; no Telegram library needed. Off by default:
set ``enabled`` (env ``ROOK_TELEGRAM=1``), the token (vault
``plugin.telegram.token`` or ``ROOK_TELEGRAM_TOKEN``) and ``chat_id``.
See docs/integrations.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ...core.plugin import capability
from ..integrations import ChatIntegration, FatalAuth, Inbound, RateLimited, integration_settings

log = logging.getLogger("rook.hub.plugins.telegram")

API = "https://api.telegram.org"


class Telegram(ChatIntegration):
    NAMESPACE = "telegram"
    NAME = "telegram"
    PLATFORM = "telegram"
    TEXT_LIMIT = 4096
    COMMAND_PREFIX = "/"
    LONG_POLL_SECS = 25
    TOKEN_PATTERNS = (r"\b\d{5,}:[A-Za-z0-9_-]{30,}\b",)
    SETTINGS = integration_settings("telegram", chat_label="Chat id", api_default=API)
    SKILL = ("### telegram\n"
             "When the Telegram integration is on: `telegram.send` (text) posts to the "
             "configured chat; `telegram.status` shows whether it is connected. Prefer "
             "`notify.send` to reach every configured channel.\n")

    def __init__(self) -> None:
        super().__init__()
        self._session: Any = None
        self._offset = 0

    # -- HTTP --------------------------------------------------------------
    def _url(self, method: str) -> str:
        base = str(self.settings.get("api_base") or API).rstrip("/")
        return f"{base}/bot{self.token()}/{method}"

    async def connect(self) -> None:
        import aiohttp
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.LONG_POLL_SECS + 15))
        try:
            self._offset = int(json.loads((self.data_dir / "offset.json")
                                          .read_text(encoding="utf-8")).get("offset", 0))
        except (OSError, ValueError, AttributeError):
            self._offset = 0

    async def disconnect(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _api(self, method: str, payload: dict | None = None) -> Any:
        async with self._session.post(self._url(method), json=payload or {}) as r:
            try:
                data = await r.json(content_type=None)
            except ValueError:
                data = {}
        if not isinstance(data, dict):
            data = {}
        if data.get("ok"):
            return data.get("result")
        code = data.get("error_code") or r.status
        if code == 429:
            raise RateLimited(float((data.get("parameters") or {}).get("retry_after", 5)))
        if code in (401, 404):
            raise FatalAuth(f"{method}: HTTP {code} (bad token?)")
        raise RuntimeError(f"{method}: HTTP {code} {str(data.get('description') or '')[:120]}")

    # -- receive -----------------------------------------------------------
    async def receive_forever(self) -> None:
        if not self.token():
            self.state["last_error"] = "token not set"
            await asyncio.sleep(30)
            return
        if not self.bot_id:
            me = await self._api("getMe")
            self.bot_id = str(me.get("id") or "")
            self.bot_name = str(me.get("username") or "")
        self.state["connected"] = True
        while True:
            updates = await self._api("getUpdates", {
                "offset": self._offset, "timeout": self.LONG_POLL_SECS,
                "allowed_updates": ["message"]})
            for u in updates or []:
                self._offset = max(self._offset, int(u.get("update_id", 0)) + 1)
                msg = u.get("message")
                if isinstance(msg, dict):
                    try:
                        await self.handle_inbound(self.parse(msg))
                    except Exception as e:
                        self._error("inbound", e)
            if updates:
                self._save_offset()

    def _save_offset(self) -> None:
        try:
            (self.data_dir / "offset.json").write_text(json.dumps({"offset": self._offset}),
                                                       encoding="utf-8")
        except OSError:
            log.debug("telegram: could not persist the update offset", exc_info=True)

    @staticmethod
    def parse(msg: dict) -> Inbound:
        frm = msg.get("from") or {}
        text = msg.get("text") or msg.get("caption") or ""
        mentions = {}
        for ent in msg.get("entities") or []:
            if ent.get("type") == "text_mention" and isinstance(ent.get("user"), dict):
                u = ent["user"]
                mentions[str(u.get("id"))] = str(u.get("username") or u.get("first_name") or "")
        reply = msg.get("reply_to_message") or {}
        return Inbound(chat_id=str((msg.get("chat") or {}).get("id", "")),
                       user_id=str(frm.get("id", "")),
                       username=str(frm.get("username") or frm.get("first_name") or frm.get("id", "")),
                       text=text, is_bot=bool(frm.get("is_bot")),
                       message_id=str(msg.get("message_id", "")),
                       reply_to=str(reply.get("message_id", "")) if reply else "",
                       mentions=mentions)

    # -- send --------------------------------------------------------------
    async def send_raw(self, chat_id: str, text: str, mention_ids: list[str],
                       reply_to: str = "") -> str:
        payload: dict = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_to:
            payload["reply_parameters"] = {"message_id": int(reply_to),
                                           "allow_sending_without_reply": True}
        res = await self._api("sendMessage", payload)
        return str((res or {}).get("message_id", ""))

    # -- caps --------------------------------------------------------------
    @capability("send", risk="write")
    async def send(self, text: str, chat: str | None = None) -> dict:
        """Post a message to the configured Telegram chat.

        ``chat`` may only name the configured chat (default)."""
        return await self.notify(text, chat)

    @capability("status", risk="read")
    def status(self) -> dict:
        """Telegram integration status: connected, chat and token configured
        (never the token), bridged rooms, counters and the last error."""
        return self.status_dict()


PLUGIN = Telegram
