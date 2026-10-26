"""``telegram.*``: Telegram bot integration (hub plugin, worker ``rook``).

Bridges the configured Rook chat rooms to one Telegram chat, sends
notifications (``telegram.send``, or ``notify.send`` for every channel) and
answers a small command set (``/help``, ``/workers``, ``/rooms``, ``/call``).
People link their accounts with ``/link CODE`` (or a ``t.me/<bot>?start=CODE``
link) and then act as themselves; a direct message goes to the house agent;
a person may run their own bot (``users.chat_bot``). Long-polls the Bot API
with ``getUpdates`` over aiohttp, one loop per bot; no Telegram library
needed. Off by default:
set ``enabled`` (env ``ROOK_TELEGRAM=1``), the token (vault
``plugin.telegram.token`` or ``ROOK_TELEGRAM_TOKEN``) and ``chat_id``.
See docs/integrations.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ...core.db import atomic_write_json
from ...core.plugin import capability
from ..integrations import Bot, ChatIntegration, FatalAuth, Inbound, RateLimited, \
    integration_settings

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
             "`notify.send` to reach every configured channel. People link their account "
             "with `users.link_code platform=telegram` and `/link CODE` to the bot.\n")

    def __init__(self) -> None:
        super().__init__()
        self._session: Any = None
        self._offset = 0

    # -- HTTP --------------------------------------------------------------
    def _url(self, method: str, token: str = "") -> str:
        base = str(self.settings.get("api_base") or API).rstrip("/")
        return f"{base}/bot{token or self.token()}/{method}"

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

    async def _api(self, method: str, payload: dict | None = None, token: str = "") -> Any:
        async with self._session.post(self._url(method, token), json=payload or {}) as r:
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
    async def receive_forever(self, bot: Bot | None = None) -> None:
        own = bot is not None and bot.owner is not None
        bot = bot if own else self._hub_bot
        token = bot.token if own else self.token()
        if not token:
            self.state["last_error"] = "token not set"
            await asyncio.sleep(30)
            return
        tok = {"token": bot.token} if own else {}
        if not bot.bot_id:
            me = await self._api("getMe", **tok)
            bot.bot_id = str(me.get("id") or "")
            bot.bot_name = str(me.get("username") or "")
        if own:
            bot.connected = True
            offset = self._load_offset(bot.key)
        else:
            self.state["connected"] = True
        try:
            while True:
                updates = await self._api("getUpdates", {
                    "offset": offset if own else self._offset, "timeout": self.LONG_POLL_SECS,
                    "allowed_updates": ["message"]}, **tok)
                for u in updates or []:
                    nxt = int(u.get("update_id", 0)) + 1
                    if own:
                        offset = max(offset, nxt)
                    else:
                        self._offset = max(self._offset, nxt)
                    msg = u.get("message")
                    if isinstance(msg, dict):
                        try:
                            m = self.parse(msg)
                            m.to_bot = self._to_bot(msg, bot)
                            await self.handle_inbound(m, bot if own else None)
                        except Exception as e:
                            self._error("inbound", e)
                if updates:
                    self._save_offset(bot.key, offset if own else self._offset)
        finally:
            if own:
                bot.connected = False

    def _offset_file(self, key: str = "hub"):
        return self.data_dir / ("offset.json" if key == "hub" else f"offset-{key}.json")

    def _load_offset(self, key: str) -> int:
        try:
            return int(json.loads(self._offset_file(key).read_text(encoding="utf-8"))
                       .get("offset", 0))
        except (OSError, ValueError, AttributeError):
            return 0

    def _save_offset(self, key: str = "hub", offset: int | None = None) -> None:
        try:
            atomic_write_json(self._offset_file(key),
                              {"offset": self._offset if offset is None else offset}, 0o644)
        except OSError:
            log.debug("telegram: could not persist the update offset", exc_info=True)

    @staticmethod
    def _to_bot(msg: dict, bot: Bot) -> bool:
        """A reply to the bot's message, or ``@botname`` in the text."""
        replied = ((msg.get("reply_to_message") or {}).get("from") or {}).get("id")
        if bot.bot_id and replied is not None and str(replied) == bot.bot_id:
            return True
        text = str(msg.get("text") or msg.get("caption") or "").lower()
        return bool(bot.bot_name) and f"@{bot.bot_name.lower()}" in text

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
        chat = msg.get("chat") or {}
        kind = str(chat.get("type") or "")
        return Inbound(chat_id=str(chat.get("id", "")),
                       user_id=str(frm.get("id", "")),
                       username=str(frm.get("username") or frm.get("first_name") or frm.get("id", "")),
                       text=text, is_bot=bool(frm.get("is_bot")),
                       message_id=str(msg.get("message_id", "")),
                       reply_to=str(reply.get("message_id", "")) if reply else "",
                       mentions=mentions, private=kind == "private",
                       space=str(chat.get("id", "")) if kind in ("group", "supergroup") else "")

    # -- send --------------------------------------------------------------
    async def send_raw(self, chat_id: str, text: str, mention_ids: list[str],
                       reply_to: str = "", token: str = "") -> str:
        payload: dict = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_to:
            payload["reply_parameters"] = {"message_id": int(reply_to),
                                           "allow_sending_without_reply": True}
        res = await self._api("sendMessage", payload, token)
        return str((res or {}).get("message_id", ""))

    # -- caps --------------------------------------------------------------
    @capability("send", risk="write", scope="external")
    async def send(self, text: str, chat: str | None = None, user: str | None = None) -> dict:
        """Post a message to the configured Telegram chat.

        ``chat``: the configured chat (default) or a shared group (users.space)
        you may use. ``user``: instead, a direct message to that person's
        linked account (yourself, or anyone for owners), from their own bot
        if they run one."""
        if user:
            return await self.notify_user(user, text)
        return await self.notify(text, chat)

    @capability("status", risk="read", scope="external")
    def status(self) -> dict:
        """Telegram integration status: connected, chat and token configured
        (never the token), bridged rooms, counters and the last error."""
        return self.status_dict()


PLUGIN = Telegram
