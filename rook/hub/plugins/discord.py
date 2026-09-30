"""``discord.*``: Discord bot integration (hub plugin, worker ``rook``).

Bridges the configured Rook chat rooms to one Discord channel, sends
notifications (``discord.send``, or ``notify.send`` for every channel) and
answers a small command set (``!help``, ``!workers``, ``!rooms``, ``!call``)
under the principal ``integration:discord``. Talks to the REST API and the
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
from ..integrations import ChatIntegration, FatalAuth, Inbound, RateLimited, integration_settings

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
             "`notify.send` to reach every configured channel.\n")

    def __init__(self) -> None:
        super().__init__()
        self._session: Any = None
        self._names: dict[str, str] = {}   # user id -> username, from recent messages

    # -- HTTP --------------------------------------------------------------
    def _base(self) -> str:
        return str(self.settings.get("api_base") or API).rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": f"Bot {self.token()}",
                "User-Agent": "DiscordBot (https://github.com/Bake-Ware/rook, 1)"}

    async def connect(self) -> None:
        import aiohttp
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def disconnect(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _rest(self, method: str, path: str, payload: dict | None = None) -> Any:
        async with self._session.request(method, self._base() + path, json=payload,
                                         headers=self._headers()) as r:
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
    async def receive_forever(self) -> None:
        if not self.token():
            self.state["last_error"] = "token not set"
            await asyncio.sleep(30)
            return
        if not self.bot_id:
            me = await self._rest("GET", "/users/@me")
            self.bot_id = str(me.get("id") or "")
            self.bot_name = str(me.get("username") or "")
        gw = await self._rest("GET", "/gateway/bot")
        url = str((gw or {}).get("url") or "wss://gateway.discord.gg")
        url += ("&" if "?" in url else "?") + "v=10&encoding=json"
        await self._gateway_session(url)

    async def _gateway_session(self, url: str) -> None:
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
                "token": self.token(), "intents": INTENTS,
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
                        await self._dispatch(msg.get("t"), msg.get("d") or {})
            finally:
                beater.cancel()
                self.state["connected"] = False
            code = ws.close_code
        if code in _FATAL_CLOSE:
            raise FatalAuth(f"gateway closed with {code}")

    async def _dispatch(self, event: str | None, d: dict) -> None:
        if event == "READY":
            user = d.get("user") or {}
            self.bot_id = str(user.get("id") or self.bot_id)
            self.bot_name = str(user.get("username") or self.bot_name)
            self.state["connected"] = True
        elif event == "MESSAGE_CREATE":
            try:
                await self.handle_inbound(self.parse(d))
            except Exception as e:
                self._error("inbound", e)

    def parse(self, d: dict) -> Inbound:
        author = d.get("author") or {}
        mentions = {str(u.get("id")): str(u.get("username") or "")
                    for u in d.get("mentions") or [] if isinstance(u, dict)}
        self._names.update(mentions)
        ref = d.get("message_reference") or {}
        return Inbound(chat_id=str(d.get("channel_id", "")), user_id=str(author.get("id", "")),
                       username=str(author.get("username") or author.get("id", "")),
                       text=str(d.get("content") or ""), is_bot=bool(author.get("bot")),
                       message_id=str(d.get("id", "")),
                       reply_to=str(ref.get("message_id", "")) if ref else "",
                       mentions=mentions)

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
                       reply_to: str = "") -> str:
        payload: dict = {"content": text,
                         # Only mapped users may be pinged: no @everyone, roles or
                         # arbitrary <@id> injected by a room message.
                         "allowed_mentions": {"parse": [],
                                              "users": [u for u in mention_ids if u.isdigit()][:100]}}
        if reply_to:
            payload["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
        res = await self._rest("POST", f"/channels/{chat_id}/messages", payload)
        return str((res or {}).get("id", ""))

    # -- caps --------------------------------------------------------------
    @capability("send", risk="write")
    async def send(self, text: str, chat: str | None = None) -> dict:
        """Post a message to the configured Discord channel.

        ``chat`` may only name the configured channel (default)."""
        return await self.notify(text, chat)

    @capability("status", risk="read")
    def status(self) -> dict:
        """Discord integration status: connected, channel and token configured
        (never the token), bridged rooms, counters and the last error."""
        return self.status_dict()


PLUGIN = Discord
