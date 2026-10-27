"""``discord.*``: Discord bot integration (hub plugin, worker ``rook``).

Bridges the configured Rook chat rooms to one Discord channel, sends
notifications (``discord.send``, or ``notify.send`` for every channel) and
answers a small command set (``!help``, ``!workers``, ``!rooms``, ``!call``).
People link their accounts with ``!link CODE`` and then act as themselves; a
direct message goes to the house agent; a shared server (``users.space``)
hears people granted on it; a person may run their own bot
(``users.chat_bot``). Talks to the REST API and the
Gateway websocket directly over aiohttp (identify, heartbeat, reconnect with
backoff); no discord.py needed. The bot needs the Message Content intent.
Off by default: set ``enabled`` (env ``ROOK_DISCORD=1``), the token (vault
``plugin.discord.token`` or ``ROOK_DISCORD_TOKEN``) and ``chat_id`` (the
channel id). See docs/integrations.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
from typing import Any

from ...core.plugin import capability
from ..integrations import Bot, ChatIntegration, FatalAuth, Inbound, RateLimited, \
    integration_settings

log = logging.getLogger("rook.hub.plugins.discord")

API = "https://discord.com/api/v10"
#: GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT
INTENTS = (1 << 9) | (1 << 12) | (1 << 15)
_USER_MENTION = re.compile(r"<@!?(\d+)>")
#: Gateway close codes that will not get better by retrying soon.
_FATAL_CLOSE = {4004, 4010, 4011, 4012, 4013, 4014}


class Discord(ChatIntegration):
    NAMESPACE = "discord"
    NAME = "discord"
    PLATFORM = "discord"
    TEXT_LIMIT = 2000
    COMMAND_PREFIX = "!"
    TOKEN_PATTERNS = (r"\b[\w-]{23,28}\.[\w-]{6,7}\.[\w-]{27,}\b",)
    SETTINGS = integration_settings("discord", chat_label="Channel id", api_default=API)
    SKILL = ("### discord\n"
             "When the Discord integration is on: `discord.send` (text) posts to the "
             "configured channel; `discord.status` shows whether it is connected. Prefer "
             "`notify.send` to reach every configured channel. People link their account "
             "with `users.link_code platform=discord` and `!link CODE` to the bot.\n")

    def __init__(self) -> None:
        super().__init__()
        self._session: Any = None
        self._names: dict[str, str] = {}   # user id -> username, from recent messages
        self._guilds: dict[str, str] = {}  # channel id -> server id ('' for none)

    # -- HTTP --------------------------------------------------------------
    def _base(self) -> str:
        return str(self.settings.get("api_base") or API).rstrip("/")

    def _headers(self, token: str = "") -> dict:
        return {"Authorization": f"Bot {token or self.token()}",
                "User-Agent": "DiscordBot (https://github.com/Bake-Ware/rook, 1)"}

    async def connect(self) -> None:
        import aiohttp
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def disconnect(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _rest(self, method: str, path: str, payload: dict | None = None,
                    token: str = "") -> Any:
        async with self._session.request(method, self._base() + path, json=payload,
                                         headers=self._headers(token)) as r:
            try:
                data = await r.json(content_type=None)
            except ValueError:
                data = None
            status = r.status
        if status == 429:
            retry = (data or {}).get("retry_after", 5) if isinstance(data, dict) else 5
            raise RateLimited(float(retry))
        if status in (401, 403) and path.startswith(("/users/@me", "/gateway")):
            raise FatalAuth(f"{path}: HTTP {status} (bad token?)")
        if status >= 400:
            msg = (data or {}).get("message", "") if isinstance(data, dict) else ""
            raise RuntimeError(f"{method} {path.split('?')[0]}: HTTP {status} {str(msg)[:120]}")
        return data

    # -- gateway -----------------------------------------------------------
    async def receive_forever(self, bot: Bot | None = None) -> None:
        own = bot is not None and bot.owner is not None
        bot = bot if own else self._hub_bot
        token = bot.token if own else self.token()
        if not token:
            self.state["last_error"] = "token not set"
            await asyncio.sleep(30)
            return
        if not bot.bot_id:
            me = await self._rest("GET", "/users/@me", token=token)
            bot.bot_id = str(me.get("id") or "")
            bot.bot_name = str(me.get("username") or "")
        gw = await self._rest("GET", "/gateway/bot", token=token)
        url = str((gw or {}).get("url") or "wss://gateway.discord.gg")
        url += ("&" if "?" in url else "?") + "v=10&encoding=json"
        await self._gateway_session(url, bot if own else None)

    async def _gateway_session(self, url: str, bot: Bot | None = None) -> None:
        import aiohttp
        seq: list[int | None] = [None]
        acked = [True]
        async with self._session.ws_connect(url, heartbeat=None, max_msg_size=0) as ws:
            hello = await ws.receive_json(timeout=30)
            if hello.get("op") != 10:
                raise RuntimeError("gateway: expected HELLO")
            interval = float(hello["d"]["heartbeat_interval"]) / 1000.0

            async def beat() -> None:
                while True:
                    await asyncio.sleep(interval)
                    if not acked[0]:
                        await ws.close(code=4000)   # zombie connection: reconnect
                        return
                    acked[0] = False
                    await ws.send_json({"op": 1, "d": seq[0]})

            await ws.send_json({"op": 2, "d": {
                "token": bot.token if bot is not None else self.token(), "intents": INTENTS,
                "properties": {"os": platform.system().lower() or "linux",
                               "browser": "rook", "device": "rook"}}})
            beater = asyncio.get_running_loop().create_task(beat())
            try:
                async for frame in ws:
                    if frame.type != aiohttp.WSMsgType.TEXT:
                        if frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                            break
                        continue
                    msg = json.loads(frame.data)
                    op = msg.get("op")
                    if msg.get("s") is not None:
                        seq[0] = msg["s"]
                    if op == 11:
                        acked[0] = True
                    elif op == 1:
                        await ws.send_json({"op": 1, "d": seq[0]})
                    elif op in (7, 9):          # reconnect / invalid session
                        break
                    elif op == 0:
                        await self._dispatch(msg.get("t"), msg.get("d") or {}, bot)
            finally:
                beater.cancel()
                if bot is not None:
                    bot.connected = False
                else:
                    self.state["connected"] = False
            code = ws.close_code
        if code in _FATAL_CLOSE:
            raise FatalAuth(f"gateway closed with {code}")

    async def _dispatch(self, event: str | None, d: dict, bot: Bot | None = None) -> None:
        b = bot or self._hub_bot
        if event == "READY":
            user = d.get("user") or {}
            b.bot_id = str(user.get("id") or b.bot_id)
            b.bot_name = str(user.get("username") or b.bot_name)
            if bot is not None:
                bot.connected = True
            else:
                self.state["connected"] = True
        elif event == "MESSAGE_CREATE":
            try:
                m = self.parse(d)
                ref = (d.get("referenced_message") or {}).get("author") or {}
                m.to_bot = bool(b.bot_id) and (b.bot_id in m.mentions
                                               or str(ref.get("id", "")) == b.bot_id)
                await self.handle_inbound(m, bot)
            except Exception as e:
                self._error("inbound", e)

    def parse(self, d: dict) -> Inbound:
        author = d.get("author") or {}
        mentions = {str(u.get("id")): str(u.get("username") or "")
                    for u in d.get("mentions") or [] if isinstance(u, dict)}
        self._names.update(mentions)
        ref = d.get("message_reference") or {}
        guild = str(d.get("guild_id") or "")
        if guild:
            self._guilds[str(d.get("channel_id", ""))] = guild
        return Inbound(chat_id=str(d.get("channel_id", "")), user_id=str(author.get("id", "")),
                       username=str(author.get("username") or author.get("id", "")),
                       text=str(d.get("content") or ""), is_bot=bool(author.get("bot")),
                       message_id=str(d.get("id", "")),
                       reply_to=str(ref.get("message_id", "")) if ref else "",
                       mentions=mentions, private=not guild, space=guild)

    def _inbound_text(self, m: Inbound) -> str:
        """Native ``<@id>`` mentions read as ``@name`` in the room; a mention
        of this bot is dropped."""
        def sub(mt: re.Match) -> str:
            uid = mt.group(1)
            if uid == self.bot_id:
                return ""
            return "@" + (m.mentions.get(uid) or self._names.get(uid) or uid)
        return re.sub(r"[ \t]{2,}", " ", _USER_MENTION.sub(sub, m.text)).strip() or m.text

    # -- send --------------------------------------------------------------
    def format_mention(self, handle: str) -> str:
        """Mention-map keys that are numeric user ids render as real pings."""
        return f"<@{handle}>" if handle.isdigit() else "@" + handle

    async def send_raw(self, chat_id: str, text: str, mention_ids: list[str],
                       reply_to: str = "", token: str = "") -> str:
        payload: dict = {"content": text,
                         # Only mapped users may be pinged: no @everyone, roles or
                         # arbitrary <@id> injected by a room message.
                         "allowed_mentions": {"parse": [],
                                              "users": [u for u in mention_ids if u.isdigit()][:100]}}
        if reply_to:
            payload["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
        res = await self._rest("POST", f"/channels/{chat_id}/messages", payload, token)
        return str((res or {}).get("id", ""))

    async def dm_chat(self, platform_user: str, bot: Bot) -> str:
        """The DM channel with a user (opened once, then remembered)."""
        key = f"{bot.key}:{platform_user}"
        if key not in self._dm_chats:
            res = await self._rest("POST", "/users/@me/channels",
                                   {"recipient_id": str(platform_user)},
                                   bot.token if bot.owner is not None else "")
            self._dm_chats[key] = str((res or {}).get("id") or "")
        if not self._dm_chats[key]:
            raise RuntimeError("could not open a direct message channel")
        return self._dm_chats[key]

    async def chat_space(self, chat_id: str) -> str:
        """The server a channel belongs to ('' for a DM or an unknown one)."""
        chat_id = str(chat_id)
        if chat_id not in self._guilds:
            if not chat_id.isdigit():
                return ""
            res = await self._rest("GET", f"/channels/{chat_id}")
            self._guilds[chat_id] = str((res or {}).get("guild_id") or "")
        return self._guilds[chat_id]

    # -- caps --------------------------------------------------------------
    @capability("send", risk="write", scope="external")
    async def send(self, text: str, chat: str | None = None, user: str | None = None) -> dict:
        """Post a message to the configured Discord channel.

        ``chat``: the configured channel (default) or a channel on a shared
        server (users.space) you may use. ``user``: instead, a direct message
        to that person's linked account (yourself, or anyone for owners),
        from their own bot if they run one."""
        if user:
            return await self.notify_user(user, text)
        return await self.notify(text, chat)

    @capability("status", risk="read", scope="external")
    def status(self) -> dict:
        """Discord integration status: connected, channel and token configured
        (never the token), bridged rooms, counters and the last error."""
        return self.status_dict()


PLUGIN = Discord
