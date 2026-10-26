"""Shared machinery for chat integrations (Telegram, Discord) on the hub.

An integration is a hub-placed plugin (``rook/hub/plugins/telegram.py``,
``discord.py``) built on :class:`ChatIntegration`. It does three things:

* **Bridge**: messages in the configured Rook chat rooms (the same
  ``chat.db`` the MCP chat tools and the dashboard use) are relayed to one
  Telegram chat / Discord channel, and messages there are posted back into
  the room, with sender attribution and mention mapping.
* **Notify**: ``<platform>.send`` posts a message to the configured chat;
  ``notify.send`` (``rook/hub/plugins/notify.py``) fans out to whichever
  integrations are running.
* **Commands**: a small, explicit command set typed in the chat
  (``help``, ``workers``, ``rooms``, ``call``). Every command is checked with
  the permission policy first. The integration fails closed: a command runs
  only when the policy *allows* it (a ``would_deny`` in audit mode is refused
  too).
* **People** (access v2 item 5, when the hub has an access store): each
  person links their own platform account with a one-time code
  (``users.link_code``, then ``/link CODE`` to the bot). A linked account
  acts as its person everywhere (commands, bridged messages, the house
  agent), so their grants decide what it may do; anyone else is ignored. A
  direct message, or the hub chat with no bridged room, goes to the house
  agent (``home.ask`` as that person) and its answer comes back. The hub's
  bot is the default; a person may run their own (``users.chat_bot``: a vault
  secret name), which then hears only them and carries their notifications
  (``notify.send user=``). Shared spaces (a Discord server, a Telegram group)
  are hub resources: there the bot hears linked people holding a ``space``
  grant (owners always), and only commands, replies to it or mentions of it.
  Without an access store (an older hub) the integration keeps its legacy
  behaviour: anyone in the configured chat, commands under the principal
  ``integration:<platform>`` (the default ``integration:*`` table: read +
  write, no exec, no admin).

Loop prevention: messages the bridge writes into a room carry a
``<platform>:<user>`` sender and are never relayed back to that platform;
messages from bots (including this one) are ignored on the way in; notices
and command replies are never written into rooms.

Secrets: the bot token is a ``secret`` setting (vault key
``plugin.<platform>.token``); a person's own bot token is a vault secret they
name. Neither is ever logged or returned; every error string passes through
:meth:`ChatIntegration.mask` first.

See ``docs/integrations.md``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from ..core.plugin import Plugin, place, setting
from ..core.db import atomic_write_json

log = logging.getLogger("rook.hub.integrations")

#: The permission rule that would let an integration exec on named workers.
#: Documented (docs/integrations.md), not enabled: add it to ``rules`` in the
#: policy document to use it.
EXAMPLE_EXEC_RULE = {"id": "telegram-exec-lab", "who": "integration:telegram",
                     "allow": "tier:exec", "on": ["worker-a", "worker-b"]}

COMMANDS = ("help", "workers", "rooms", "call")
DEFAULT_COMMANDS = ["help", "workers", "rooms"]
_MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_.:\-]+[A-Za-z0-9_])")
#: Failed ``link`` attempts one platform account may make per LINK_WINDOW seconds.
LINK_TRIES = 5
LINK_WINDOW = 600.0
#: How often a stranger who writes to the bot directly is told how to link.
HINT_EVERY = 3600.0
#: How long the house agent may take to answer a chat message.
ASK_TIMEOUT = 180.0


def aiohttp_available() -> bool:
    try:
        import aiohttp  # noqa: F401
    except Exception:
        return False
    return True


def split_message(text: str, limit: int) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` characters, preferring
    paragraph, line and sentence boundaries."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(". ", 0, limit)
            cut = cut + 1 if cut > 0 else -1
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return [c for c in chunks if c]


class RateLimiter:
    """Token bucket: ``rate`` events per ``per`` seconds, bursting to ``rate``."""

    def __init__(self, rate: int, per: float = 60.0) -> None:
        self.rate = max(1, int(rate))
        self.per = float(per)
        self.tokens = float(self.rate)
        self.stamp = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.rate, self.tokens + (now - self.stamp) * self.rate / self.per)
        self.stamp = now

    def try_acquire(self) -> bool:
        self._refill()
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False

    def wait_time(self) -> float:
        self._refill()
        return 0.0 if self.tokens >= 1 else (1 - self.tokens) * self.per / self.rate

    async def acquire(self) -> None:
        while not self.try_acquire():
            await asyncio.sleep(max(0.01, self.wait_time()))


class RateLimited(Exception):
    """The platform asked us to slow down."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited; retry after {retry_after:.1f}s")
        self.retry_after = float(retry_after)


class FatalAuth(Exception):
    """The platform rejected the token; retry slowly."""


@dataclass
class Inbound:
    """One message from the platform, normalized."""

    chat_id: str
    user_id: str
    username: str
    text: str
    is_bot: bool = False
    message_id: str = ""
    reply_to: str = ""                        # platform id of the message replied to
    mentions: dict = field(default_factory=dict)  # platform user id -> handle (native mentions)
    private: bool = False                     # a direct message to the bot
    space: str = ""                           # Discord server id / Telegram group chat id
    to_bot: bool = False                      # mentions the bot or replies to it


@dataclass
class Bot:
    """One bot the integration runs: the hub's (``owner`` None, token from
    the plugin's settings) or a person's own (token in vault ``secret``)."""

    owner: str | None = None
    secret: str = ""
    token: str = ""
    bot_id: str = ""
    bot_name: str = ""
    connected: bool = False
    task: Any = None

    @property
    def key(self) -> str:
        return self.owner or "hub"


def integration_settings(platform: str, *, chat_label: str, api_default: str) -> tuple:
    """The settings schema every chat integration shares. ``apply``: the
    enable flag, bridged rooms, rate limits and API base are read at start;
    the rest is read on use (the hub refreshes ``plugin.settings`` on save)."""
    up = platform.upper()
    return (
        setting("enabled", bool, default=False, env=f"ROOK_{up}", apply="restart",
                group="General", label=f"{platform.title()} integration",
                help="Bridge rooms, notifications and chat commands. Read at start: "
                     "restart the hub to apply."),
        setting("token", str, secret=True, env=f"ROOK_{up}_TOKEN", group="General",
                label="Bot token",
                help=f"Stored in the vault as plugin.{platform}.token; never shown or logged."),
        setting("chat_id", str, default="", env=f"ROOK_{up}_CHAT", group="General",
                label=chat_label,
                help="The hub's chat: bridged rooms, notifications and commands go here. "
                     "Elsewhere the bot hears only direct messages from linked people and "
                     "shared spaces granted to them (users.space)."),
        setting("rooms", list, default=[], apply="restart", group="Bridge",
                label="Bridged rooms",
                help="Rook chat room ids relayed to and from the chat. Empty: no bridge."),
        setting("mentions", dict, default={}, group="Bridge", label="Mention map",
                help="Platform handle or user id -> Rook identity, e.g. "
                     "{\"alice\": \"user:operator\"}. Used both ways."),
        setting("commands", list, default=list(DEFAULT_COMMANDS), group="Commands",
                label="Allowed commands",
                help=f"Subset of {list(COMMANDS)}. Commands run as the linked person (as "
                     f"integration:{platform} on a hub without people) and are checked against "
                     "the permission policy."),
        setting("allowed_caps", list, default=[], group="Commands",
                label="Caps the call command may run",
                help="Glob patterns (e.g. 'hub.*'). The policy must also allow them."),
        setting("command_users", list, default=[], group="Commands", label="Command users",
                help="Platform user ids allowed to run commands. Empty: anyone in the chat."),
        setting("rate_out", int, default=20, apply="restart", group="Limits", min=1,
                label="Outbound messages per minute"),
        setting("rate_in", int, default=10, apply="restart", group="Limits", min=1,
                label="Inbound messages per user per minute"),
        setting("api_base", "url", default=api_default, env=f"ROOK_{up}_API",
                apply="restart", group="Limits", advanced=True, label="API base URL",
                help="Change only for a proxy or a test server: the Telegram API puts the "
                     "token in the URL."),
    )

def person_refusal(uid: str) -> str:
    """Why the current caller may not message person ``uid`` directly
    (empty: they may). Yourself, always; anyone: owners and in-process code."""
    from . import access as _access
    from .authz import current_principal
    p = current_principal.get()
    if p is None or (p.verified and p.fail_open()):
        return ""
    if _access.user_of(p) == uid:
        return ""
    return f"denied: only {uid} or an owner may message {uid} directly"


class ChatIntegration(Plugin):
    """Base class: bridge loop, commands, notifications, masking, backoff.

    Subclasses set ``PLATFORM``, ``TEXT_LIMIT``, ``COMMAND_PREFIX`` and
    implement :meth:`connect`, :meth:`disconnect`, :meth:`receive_forever`,
    :meth:`send_raw` and :meth:`format_mention`.
    """

    PLATFORM = ""
    TEXT_LIMIT = 2000
    COMMAND_PREFIX = "/"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    POLL_SECS = 2.0                 # bridge poll interval (tests shorten it)
    MAX_BURST = 20                  # messages relayed per room per poll before summarizing
    TOKEN_PATTERNS: tuple = ()      # regexes for token-shaped strings to mask

    def __init__(self) -> None:
        super().__init__()
        self._node: Any = None
        self._chat: Any = None
        self._own_chat = False
        self._tasks: list[asyncio.Task] = []
        self._out: RateLimiter | None = None
        self._in: dict[str, RateLimiter] = {}
        self._reply_rooms: OrderedDict[str, str] = OrderedDict()
        self._watermarks: dict[str, int] = {}
        self._titles: dict[str, str] = {}
        self._send_lock = asyncio.Lock()
        self._hub_bot = Bot()
        self._user_bots: dict[str, Bot] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._echo: OrderedDict[tuple, None] = OrderedDict()   # (room, seq) we wrote
        self._link_fails: dict[str, list[float]] = {}
        self._hinted: dict[str, float] = {}
        self._dm_chats: dict[str, str] = {}
        self._asks: set[asyncio.Task] = set()
        self.state: dict[str, Any] = {"connected": False, "last_error": None,
                                      "relayed_out": 0, "relayed_in": 0, "dropped": 0,
                                      "commands_run": 0, "denied": 0, "unknown": 0,
                                      "asked": 0, "linked": 0}

    @property
    def bot_id(self) -> str:
        return self._hub_bot.bot_id

    @bot_id.setter
    def bot_id(self, value: str) -> None:
        self._hub_bot.bot_id = value

    @property
    def bot_name(self) -> str:
        return self._hub_bot.bot_name

    @bot_name.setter
    def bot_name(self, value: str) -> None:
        self._hub_bot.bot_name = value

    # -- plugin hooks ------------------------------------------------------
    def available(self) -> bool:
        if not self.settings.get("enabled", False):
            return False
        if not aiohttp_available():
            log.info("%s: aiohttp is not installed; integration disabled", self.PLATFORM)
            return False
        return True

    def bind_host(self, node: Any) -> None:
        self._node = node

    @property
    def principal_id(self) -> str:
        return f"integration:{self.PLATFORM}"

    def principal(self, user: Inbound | None = None):
        from .policy import Principal
        label = f"{self.PLATFORM} user {user.user_id}" if user else self.PLATFORM
        return Principal(self.principal_id, "integration", "integration", label=label)

    def token(self) -> str:
        return str(self.settings.get("token") or "")

    def chat_id(self) -> str:
        return str(self.settings.get("chat_id") or "")

    def rooms(self) -> list[str]:
        return [str(r) for r in (self.settings.get("rooms") or []) if str(r).strip()]

    async def start(self) -> None:
        self._out = RateLimiter(int(self.settings.get("rate_out") or 20))
        self._loop = asyncio.get_running_loop()
        self._open_chat()
        await self.connect()
        self._tasks.append(self._loop.create_task(
            self._supervise("receive", self.receive_forever)))
        if self._chat is not None and self.rooms():
            self._tasks.append(self._loop.create_task(
                self._supervise("bridge", self._bridge_forever)))
        self._reconcile_bots()

    async def stop(self) -> None:
        tasks = [*self._tasks, *self._asks,
                 *(b.task for b in self._user_bots.values() if b.task is not None)]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._asks.clear()
        self._user_bots.clear()
        try:
            await self.disconnect()
        except Exception:
            log.debug("%s: disconnect failed", self.PLATFORM, exc_info=True)
        if self._own_chat and self._chat is not None:
            self._chat.close()
        self._chat = None
        self.state["connected"] = False

    def heartbeat(self) -> dict | None:
        return {"up": 1 if self.state["connected"] else 0}

    # -- platform surface (subclasses) ---------------------------------------
    async def connect(self) -> None:
        """Open HTTP sessions; must not raise for a missing token."""

    async def disconnect(self) -> None:
        """Close what :meth:`connect` opened."""

    async def receive_forever(self, bot: Bot | None = None) -> None:
        """Receive platform messages and hand each to :meth:`handle_inbound`
        (with ``bot`` when it is a person's own bot; None: the hub's). Return
        or raise to be restarted with backoff."""
        raise NotImplementedError

    async def send_raw(self, chat_id: str, text: str, mention_ids: list[str],
                       reply_to: str = "", token: str = "") -> str:
        """Send one chunk (with ``token``: as a person's own bot); return the
        platform message id. Raise :class:`RateLimited` on a 429."""
        raise NotImplementedError

    async def dm_chat(self, platform_user: str, bot: Bot) -> str:
        """The chat id for a direct message to ``platform_user``."""
        return str(platform_user)

    async def chat_space(self, chat_id: str) -> str:
        """The shared space (server / group id) a chat id belongs to, or ''."""
        return str(chat_id)

    def format_mention(self, handle: str) -> str:
        return "@" + handle

    # -- masking -------------------------------------------------------------
    def mask(self, text: Any) -> str:
        """``text`` with the token (and anything token-shaped) replaced."""
        s = str(text)
        tok = ""
        try:
            tok = self.token()
        except Exception:
            pass
        for t in [tok, *(b.token for b in self._user_bots.values())]:
            if t and len(t) >= 4:
                s = s.replace(t, "***")
        for pat in self.TOKEN_PATTERNS:
            s = re.sub(pat, "***", s)
        return s

    def _error(self, where: str, e: BaseException) -> str:
        msg = self.mask(f"{where}: {type(e).__name__}: {e}")[:300]
        self.state["last_error"] = msg
        log.warning("%s %s", self.PLATFORM, msg)
        return msg

    # -- supervision / backoff ------------------------------------------------
    async def _supervise(self, name: str, fn) -> None:
        delay = 1.0
        while True:
            started = time.monotonic()
            try:
                await fn()
                if time.monotonic() - started > 30:
                    delay = 1.0
            except asyncio.CancelledError:
                raise
            except FatalAuth as e:
                self._error(name, e)
                delay = 600.0
            except Exception as e:
                self._error(name, e)
                if time.monotonic() - started > 30:
                    delay = 1.0
            if name == "receive":
                self.state["connected"] = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300.0) if delay < 600 else delay

    # -- chat store / bridge ----------------------------------------------------
    def _chat_path(self) -> str | None:
        path = os.environ.get("ROOK_CHAT_DB")
        if path:
            return path
        state_dir = getattr(self._node, "_state_dir", None)
        return os.path.join(state_dir, "chat.db") if state_dir else None

    def _open_chat(self) -> None:
        if self._chat is not None or not self.rooms():
            return
        path = self._chat_path()
        if not path:
            log.warning("%s: no hub state directory; the room bridge is off", self.PLATFORM)
            return
        from ..band_mcp.chat_rooms import ChatStore
        store = ChatStore(path)
        if not store.enabled:
            return
        self._chat, self._own_chat = store, True

    def use_chat_store(self, store: Any) -> None:
        """Share an already open store (tests, or a host that owns one)."""
        self._chat, self._own_chat = store, False

    def _watermark_path(self):
        return self.data_dir / "bridge.json"

    def _load_watermarks(self) -> None:
        try:
            data = json.loads(self._watermark_path().read_text(encoding="utf-8"))
            self._watermarks = {k: int(v) for k, v in (data.get("rooms") or {}).items()}
        except (OSError, ValueError, AttributeError):
            self._watermarks = {}

    def _save_watermarks(self) -> None:
        try:
            atomic_write_json(self._watermark_path(), {"rooms": self._watermarks}, 0o644)
        except OSError:
            log.debug("%s: could not save bridge watermarks", self.PLATFORM, exc_info=True)

    async def _bridge_forever(self) -> None:
        self._load_watermarks()
        for rid in self.rooms():
            if rid not in self._watermarks:  # new bridge: start from now, no history flood
                self._watermarks[rid] = self._chat.last_seq(rid)
        self._save_watermarks()
        while True:
            await self.bridge_once()
            await asyncio.sleep(self.POLL_SECS)

    def _own_sender(self, sender: str) -> bool:
        s = str(sender or "")
        return s.startswith(self.PLATFORM + ":") or s == self.principal_id

    async def bridge_once(self) -> int:
        """Relay new room messages to the platform. Returns how many were sent."""
        if self._chat is None:
            return 0
        chat_id = self.chat_id()
        if not chat_id or not self.token():
            return 0
        if not self.authorized_hub("chat.read"):
            return 0
        sent = 0
        rooms = self.rooms()
        for rid in rooms:
            since = self._watermarks.get(rid, 0)
            res = await asyncio.to_thread(self._chat.read, rid, None, since, False, 500)
            if not res.get("ok"):
                continue
            self._titles[rid] = res.get("title") or rid
            msgs = [m for m in res.get("messages", []) if not self._own_sender(m.get("sender"))
                    and (rid, m.get("seq")) not in self._echo]
            if res.get("messages"):
                self._watermarks[rid] = res["last_seq"]
            skipped = max(0, len(msgs) - self.MAX_BURST)
            if skipped:
                msgs = msgs[-self.MAX_BURST:]
                self.state["dropped"] += skipped
                await self.send_text(chat_id, f"({skipped} earlier messages in "
                                              f"{self._titles[rid]!r} were not relayed)")
            for m in msgs:
                text, ids = self.render_outbound(m, self._titles[rid] if len(rooms) > 1 else "")
                mid = await self.send_text(chat_id, text, ids)
                if mid:
                    self._remember_reply(mid, rid)
                    sent += 1
                    self.state["relayed_out"] += 1
            if res.get("messages"):
                self._save_watermarks()
        return sent

    def _remember_reply(self, mid: str, rid: str) -> None:
        self._reply_rooms[str(mid)] = rid
        while len(self._reply_rooms) > 2000:
            self._reply_rooms.popitem(last=False)

    def _mention_map(self) -> dict[str, str]:
        m = self.settings.get("mentions") or {}
        return {str(k).lstrip("@"): str(v) for k, v in m.items()} if isinstance(m, dict) else {}

    def render_outbound(self, msg: dict, room_title: str = "") -> tuple[str, list[str]]:
        """``[title] sender: text`` with Rook mentions mapped to platform ones."""
        reverse = {}
        for handle, ident in self._mention_map().items():
            reverse.setdefault(ident, handle)
        text = str(msg.get("text") or "")
        ids: list[str] = []
        for ident in msg.get("mentions") or []:
            handle = reverse.get(ident)
            if handle is None:
                continue
            native = self.format_mention(handle)
            if ("@" + ident) in text:
                text = text.replace("@" + ident, native)
            elif native not in text:
                text = f"{native} {text}"
            ids.append(handle)
        prefix = f"[{room_title}] " if room_title else ""
        return f"{prefix}{msg.get('sender') or '?'}: {text}", ids

    def map_inbound_mentions(self, m: Inbound, participants: list[str]) -> list[str]:
        mapping = self._mention_map()
        out: list[str] = []
        for uid, handle in (m.mentions or {}).items():
            ident = mapping.get(str(uid)) or mapping.get(str(handle))
            if ident and ident not in out:
                out.append(ident)
        for word in _MENTION_RE.findall(m.text or ""):
            ident = mapping.get(word) or (word if word in participants else None)
            if ident and ident not in out:
                out.append(ident)
        return out

    def _inbound_text(self, m: Inbound) -> str:
        return m.text

    # -- inbound ----------------------------------------------------------------
    def _rate_ok(self, user_id: str) -> bool:
        bucket = self._in.get(user_id)
        if bucket is None:
            bucket = self._in[user_id] = RateLimiter(int(self.settings.get("rate_in") or 10))
        return bucket.try_acquire()

    async def handle_inbound(self, m: Inbound, bot: Bot | None = None) -> str:
        """Route one platform message. Returns what happened (for tests/logs):
        ``ignored:*``, ``linked``, ``command``, ``bridged``, ``asked`` or
        ``dropped:*``."""
        bot = bot or self._hub_bot
        if m.is_bot or any(b.bot_id and str(m.user_id) == str(b.bot_id)
                           for b in (bot, self._hub_bot)):
            return "ignored:bot"
        if self._people() is None:
            return await self._legacy_inbound(m)
        text = (m.text or "").strip()
        if not text:
            return "ignored:empty"
        if not self._rate_ok(str(m.user_id)):
            self.state["dropped"] += 1
            return "dropped:rate"
        code = self._link_arg(text)
        if code is not None:
            return await self._link(m, bot, code)
        try:
            uid = self._people().linked_user(self.PLATFORM, str(m.user_id))
        except Exception as e:  # noqa: BLE001 - a broken store hears nobody
            self._error("access", e)
            uid = None
        if uid is None or (bot.owner is not None and uid != bot.owner):
            self.state["unknown"] += 1
            if m.private:
                await self._hint(m, bot)
            return "ignored:unknown"
        where = self._where(m, bot, uid)
        if where.startswith("ignored"):
            return where
        person = self.person(uid)
        if text.startswith(self.COMMAND_PREFIX):
            reply = await self.run_command(text[len(self.COMMAND_PREFIX):], m, person=person)
            if reply:
                await self.send_text(m.chat_id, reply, reply_to=m.message_id, bot=bot)
            return "command"
        if where == "hub" and self._chat is not None and self.rooms():
            return await self._bridge_in(m, person)
        if where == "space" and not m.to_bot:
            return "ignored:chatter"
        return self._ask(m, bot, person)

    async def _legacy_inbound(self, m: Inbound) -> str:
        """A hub without people (no access store): anyone in the configured
        chat, commands as ``integration:<platform>``."""
        if not self.chat_id() or str(m.chat_id) != self.chat_id():
            return "ignored:chat"
        text = (m.text or "").strip()
        if not text:
            return "ignored:empty"
        if not self._rate_ok(str(m.user_id)):
            self.state["dropped"] += 1
            return "dropped:rate"
        if text.startswith(self.COMMAND_PREFIX):
            reply = await self.run_command(text[len(self.COMMAND_PREFIX):], m)
            if reply:
                await self.send_text(m.chat_id, reply, reply_to=m.message_id)
            return "command"
        return await self._bridge_in(m)

    def _where(self, m: Inbound, bot: Bot, uid: str) -> str:
        """``hub`` (the hub's chat), ``dm``, ``space`` (a shared space the
        person may use) or ``ignored:*``. A person's own bot hears only direct
        messages."""
        if m.private:
            return "dm"
        if bot.owner is not None:
            return "ignored:chat"
        if self.chat_id() and str(m.chat_id) == self.chat_id():
            return "hub"
        store = self._people()
        try:
            if m.space and store.space(self.PLATFORM, m.space) is not None:
                return "space" if store.may_use_space(uid, self.PLATFORM, m.space) \
                    else "ignored:space"
        except Exception as e:  # noqa: BLE001
            self._error("access", e)
        return "ignored:chat"

    # -- people ------------------------------------------------------------------
    def _people(self):
        """The hub's access store (people, links, spaces), or None on a hub
        without one (legacy behaviour). A store that failed to open is
        returned too: its errors make every sender a stranger."""
        authz = self._authz()
        return getattr(authz, "access", None) if authz is not None else None

    def person(self, uid: str):
        """The principal a linked account acts as: its person, as an owner or
        a member, reached through this integration."""
        from .policy import Principal
        store = self._people()
        role, label = "member", uid
        try:
            role = "owner" if store.is_owner(uid) else "member"
            label = next((m.get("label") for m in store.members()
                          if m.get("user_id") == uid and m.get("label")), uid)
        except Exception as e:  # noqa: BLE001 - when unsure, the narrower role
            self._error("access", e)
        return Principal(f"human:{uid}", "human", role, (f"human:{role}",),
                         via=(self.principal_id,), label=f"{label} ({self.PLATFORM})")

    @staticmethod
    def chat_identity(person: Any) -> str:
        """How a person signs bridged room messages: ``user:<name>``."""
        name = str(getattr(person, "label", "") or "").rsplit(" (", 1)[0]
        name = re.sub(r"[^a-z0-9_.-]+", "-", name.lower()).strip("-")
        return "user:" + (name or str(person.id).split(":", 1)[-1])

    def _link_arg(self, text: str) -> str | None:
        """The code in ``/link CODE`` (Telegram also ``/start CODE``, from a
        t.me deep link), else None."""
        parts = text.split()
        if len(parts) != 2 or not parts[0].startswith(self.COMMAND_PREFIX):
            return None
        name = parts[0][len(self.COMMAND_PREFIX):].split("@", 1)[0].lower()
        if name == "link" or (name == "start" and self.COMMAND_PREFIX == "/"):
            return parts[1]
        return None

    async def _link(self, m: Inbound, bot: Bot, code: str) -> str:
        key = str(m.user_id)
        now = time.monotonic()
        fails = [t for t in self._link_fails.get(key, []) if now - t < LINK_WINDOW]
        self._link_fails[key] = fails
        if len(fails) >= LINK_TRIES:
            self.state["dropped"] += 1
            return "dropped:link-rate"
        try:
            uid = await asyncio.to_thread(self._people().redeem, self.PLATFORM, code, key,
                                          m.username, bot_owner=bot.owner)
        except Exception as e:  # noqa: BLE001
            self._error("link", e)
            uid = None
        if uid is None:
            fails.append(now)
            if len(self._link_fails) > 5000:
                self._link_fails = {k: v for k, v in self._link_fails.items()
                                    if v and now - v[-1] < LINK_WINDOW}
            await self.send_text(m.chat_id, "That code is unknown, used or expired. Ask your "
                                 "Rook hub for a new one.", reply_to=m.message_id, bot=bot)
            return "ignored:bad-code"
        self._link_fails.pop(key, None)
        self.state["linked"] += 1
        who = self.person(uid).label.rsplit(" (", 1)[0]
        await self.send_text(m.chat_id, f"Linked: messages from this account now act as {who}.",
                             reply_to=m.message_id, bot=bot)
        return "linked"

    async def _hint(self, m: Inbound, bot: Bot) -> None:
        """Tell a stranger writing directly how to link, at most once an hour."""
        key, now = str(m.user_id), time.monotonic()
        if now - self._hinted.get(key, -HINT_EVERY) < HINT_EVERY:
            return
        self._hinted[key] = now
        if len(self._hinted) > 5000:
            self._hinted = {k: v for k, v in self._hinted.items() if now - v < HINT_EVERY}
        await self.send_text(m.chat_id, "I only answer people linked to this Rook hub. Ask it "
                             f"for a link code, then send {self.COMMAND_PREFIX}link CODE here.",
                             bot=bot)

    def _ask(self, m: Inbound, bot: Bot, person: Any) -> str:
        """Hand the message to the house agent as ``person``; its answer is
        sent back to the chat when it comes (the receive loop goes on)."""
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._ask_house(m, bot, person))
        self._asks.add(task)
        task.add_done_callback(self._asks.discard)
        return "asked"

    async def _ask_house(self, m: Inbound, bot: Bot, person: Any) -> None:
        node = self._node
        client = getattr(node, "client", None)
        if client is None:
            return
        self.state["asked"] += 1
        try:
            reply = await client.call(
                cap="home.ask", args={"question": self._inbound_text(m), "source": self.PLATFORM},
                target=node.worker_id, timeout=ASK_TIMEOUT,
                identity=f"{person.id}/{self.PLATFORM}", principal=person)
        except asyncio.TimeoutError:
            reply = {"ok": False, "error": "the house agent took too long"}
        except Exception as e:  # noqa: BLE001
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if reply.get("ok"):
            out = reply.get("result")
            text = str(out.get("answer") or "") if isinstance(out, dict) else str(out or "")
        else:
            text = f"I couldn't pass that on: {reply.get('error')}"
        if text.strip():
            await self.send_text(m.chat_id, text, reply_to=m.message_id, bot=bot)

    # -- a person's own bot --------------------------------------------------------
    def bot_label(self, uid: str | None = None) -> str:
        """The bot that speaks for ``uid`` (their own, else the hub's)."""
        bot = self._user_bots.get(uid or "") or self._hub_bot
        name = bot.bot_name
        return ("@" + name if self.COMMAND_PREFIX == "/" else name) if name else ""

    def reload_bots(self) -> None:
        """Start, restart or stop people's own bots to match the access store
        (``users.chat_bot`` calls it). Safe from any thread."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._reconcile_bots)

    def _vault_token(self, secret: str, uid: str) -> str:
        vault = getattr(self._node, "_vault", None) or getattr(self._node, "vault", None)
        if vault is None or not secret:
            return ""
        try:
            return str(vault.get(secret, self.principal_id, via=f"{self.PLATFORM} bot for {uid}"))
        except KeyError:
            return ""

    def _reconcile_bots(self) -> None:
        store = self._people()
        try:
            rows = store.bots(self.PLATFORM) if store is not None else []
        except Exception as e:  # noqa: BLE001
            self._error("bots", e)
            return
        wanted = {r["user_id"]: r["secret"] for r in rows}
        tokens = {uid: self._vault_token(secret, uid) for uid, secret in wanted.items()}
        for uid, b in list(self._user_bots.items()):
            # gone, renamed, or the token in the vault changed (rotated): restart
            if wanted.get(uid) != b.secret or tokens.get(uid) != b.token:
                if b.task is not None:
                    b.task.cancel()
                del self._user_bots[uid]
        loop = self._loop
        for uid, secret in wanted.items():
            if uid in self._user_bots or loop is None:
                continue
            token = tokens[uid]
            if not token:
                self.state["last_error"] = (f"own bot for {uid}: vault secret {secret!r} is "
                                            "missing; using the hub's bot")
                continue
            b = Bot(owner=uid, secret=secret, token=token)
            b.task = loop.create_task(self._supervise(f"bot:{uid}",
                                                      lambda b=b: self.receive_forever(b)))
            self._user_bots[uid] = b

    def bot_for(self, uid: str | None) -> Bot:
        return self._user_bots.get(uid or "") or self._hub_bot

    async def notify_user(self, uid: str, text: str) -> dict:
        """:meth:`notify_person` for a cap: a person may message themselves;
        owners and in-process hub code anyone."""
        refusal = person_refusal(uid)
        if refusal:
            return {"ok": False, "error": refusal}
        return await self.notify_person(uid, text)

    async def notify_person(self, uid: str, text: str) -> dict:
        """A direct message to a person's linked account, from their own bot
        if they run one, else the hub's."""
        store = self._people()
        if store is None:
            return {"ok": False, "error": f"{self.PLATFORM}: this hub has no people to message"}
        try:
            links = await asyncio.to_thread(store.links, uid, self.PLATFORM)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": self._error("access", e)}
        if not links:
            return {"ok": False, "error": f"{self.PLATFORM}: {uid} has not linked an account"}
        bot = self.bot_for(uid)
        if not (bot.token if bot.owner else self.token()):
            return {"ok": False, "error": f"{self.PLATFORM}: token not set"}
        try:
            chat = await self.dm_chat(links[0]["platform_user"], bot)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": self._error("dm", e)}
        if not str(text or "").strip():
            return {"ok": False, "error": "empty message"}
        mid = await self.send_text(chat, str(text), bot=bot)
        if not mid:
            return {"ok": False, "error": self.state.get("last_error") or "send failed"}
        return {"ok": True, "channel": self.PLATFORM, "message_id": mid,
                "bot": "own" if bot.owner else "hub"}

    def _target_rooms(self, m: Inbound) -> list[str]:
        rooms = self.rooms()
        if m.reply_to and self._reply_rooms.get(str(m.reply_to)) in rooms:
            return [self._reply_rooms[str(m.reply_to)]]
        return rooms[:1]

    async def _bridge_in(self, m: Inbound, person: Any = None) -> str:
        if self._chat is None or not self.rooms():
            return "ignored:no-bridge"
        if not self.authorized_hub("chat.write", person):
            return "dropped:policy"
        sender = (self.chat_identity(person) if person is not None
                  else f"{self.PLATFORM}:{m.username or m.user_id}")
        text = self._inbound_text(m)
        for rid in self._target_rooms(m):
            room = await asyncio.to_thread(self._chat.read, rid, None, 0, False, 1)
            parts = room.get("participants", []) if room.get("ok") else []
            mentions = self.map_inbound_mentions(m, parts)
            res = await asyncio.to_thread(self._chat.send, rid, sender, text, mentions,
                                          bool(mentions))
            if res.get("ok"):
                self.state["relayed_in"] += 1
                if person is not None:   # signed as the person: never relay it back
                    self._echo[(rid, res.get("seq"))] = None
                    while len(self._echo) > 2000:
                        self._echo.popitem(last=False)
                await asyncio.to_thread(self._chat.touch, sender)
        return "bridged"

    # -- sending ------------------------------------------------------------------
    async def send_text(self, chat_id: str, text: str, mention_ids: list[str] | None = None,
                        reply_to: str = "", bot: Bot | None = None) -> str:
        """Rate-limited, chunked send with 429 retry (as ``bot`` when it is a
        person's own). Returns the last message id, or '' when it could not be
        sent (the error is in ``state``)."""
        own = {"token": bot.token} if bot is not None and bot.owner is not None else {}
        if not (own.get("token") if own else self.token()):
            self.state["last_error"] = "token not set"
            return ""
        last = ""
        async with self._send_lock:
            for i, chunk in enumerate(split_message(self.mask(text), self.TEXT_LIMIT)):
                if self._out is not None:
                    await self._out.acquire()
                for attempt in range(3):
                    try:
                        last = await self.send_raw(str(chat_id), chunk, mention_ids or [],
                                                   reply_to if i == 0 else "", **own)
                        break
                    except RateLimited as e:
                        if attempt == 2:
                            self._error("send", e)
                            return ""
                        await asyncio.sleep(min(e.retry_after, 60.0))
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self._error("send", e)
                        return ""
        return str(last or "")

    async def notify(self, text: str, chat: str | None = None) -> dict:
        target = str(chat or "") or self.chat_id()
        if not target:
            return {"ok": False, "error": f"{self.PLATFORM}: no chat configured"}
        if target != self.chat_id():
            refusal = await self._space_refusal(target)
            if refusal:
                return {"ok": False, "error": f"{self.PLATFORM}: {refusal}"}
        if not str(text or "").strip():
            return {"ok": False, "error": "empty message"}
        mid = await self.send_text(target, str(text))
        if not mid:
            return {"ok": False, "error": self.state.get("last_error") or "send failed"}
        return {"ok": True, "channel": self.PLATFORM, "message_id": mid}

    async def _space_refusal(self, chat: str) -> str:
        """Why the current caller may not post to ``chat`` (empty: they may).
        Beyond the hub's chat only a shared space works: owners and in-process
        hub code may post to any, a person to one they hold a grant on."""
        from . import access as _access
        from .authz import current_principal
        store = self._people()
        if store is None:
            return "only the configured chat can be notified"
        try:
            space = await self.chat_space(chat)
            known = space and await asyncio.to_thread(store.space, self.PLATFORM, space)
        except Exception as e:  # noqa: BLE001
            return self._error("space", e)
        if not known:
            return ("only the configured chat or a shared space (users.space) can be "
                    "notified")
        p = current_principal.get()
        if p is None or (p.verified and p.fail_open()):
            return ""
        uid = _access.user_of(p)
        if uid and await asyncio.to_thread(store.may_use_space, uid, self.PLATFORM, space):
            return ""
        return f"no grant on the shared space {self.PLATFORM}:{space}"

    # -- permissions ----------------------------------------------------------------
    def _authz(self):
        return getattr(getattr(self._node, "client", None), "authz", None)

    def _resolve(self, worker: str) -> tuple[str | None, dict | None, bool]:
        node = self._node
        if worker.lower() == "rook" and node is not None:
            return node.worker_id, node.entry(), True
        roster = getattr(getattr(node, "client", None), "workers", {}) or {}
        if worker in roster:
            return worker, roster[worker], False
        named = [wid for wid, w in roster.items() if (w.get("name") or "").lower() == worker.lower()]
        if len(named) == 1:
            return named[0], roster[named[0]], False
        return None, None, False

    def authorize(self, cap: str, worker: str, user: Inbound | None = None, *,
                  wait_ok: bool = False, args: dict | None = None, person: Any = None):
        """``(decision, target_id, error)``. Fails closed: anything but an
        ``allow`` (or ``off`` for read/write) is refused, so a ``would_deny``
        in audit mode is still a no for an integration."""
        authz = self._authz()
        if authz is None:
            return None, None, "permissions are not configured on this hub; commands are off"
        wid, entry, local = self._resolve(worker)
        if wid is None:
            return None, None, f"no live worker named {worker!r}"
        who = person if person is not None else self.principal(user)
        d = authz.check(cap, wid, entry, principal=who, local=local, args=args)
        # A held call goes on to the band client, which waits for a person to
        # approve it (permissions 3.11) before sending anything.
        ok = d.decision == "allow" or (wait_ok and d.held) \
            or (d.decision == "off" and d.tier in ("read", "write"))
        if not ok:
            self.state["denied"] += 1
            return d, wid, (f"denied: {who.label if person is not None else self.principal_id} "
                            f"may not call {cap} ({d.tier}) on "
                            f"{(entry or {}).get('name') or worker}"
                            + (f" [rule {d.rule}]" if d.rule else ""))
        return d, wid, None

    def authorized_hub(self, cap: str, person: Any = None) -> bool:
        """Bridge-side check for hub actions (``chat.read`` / ``chat.write``).
        With no permissions layer the bridge runs (commands still refuse)."""
        if self._authz() is None or self._node is None:
            return True
        d, _wid, err = self.authorize(cap, "rook", person=person)
        return err is None and not (d is not None and d.held)  # nobody waits on the bridge

    # -- commands ----------------------------------------------------------------------
    def _commands(self) -> list[str]:
        allowed = self.settings.get("commands")
        allowed = DEFAULT_COMMANDS if allowed is None else allowed
        return [c for c in COMMANDS if c in allowed]

    async def run_command(self, line: str, m: Inbound, person: Any = None) -> str:
        parts = line.strip().split(None, 3)
        if not parts:
            return ""
        name = parts[0].split("@", 1)[0].lower()     # Telegram: /workers@botname
        if "@" in parts[0] and self.bot_name and \
                parts[0].split("@", 1)[1].lower() != self.bot_name.lower():
            return ""                                 # addressed to another bot
        if name not in COMMANDS:
            return ""                                 # not ours: stay quiet
        if name not in self._commands():
            return f"{self.COMMAND_PREFIX}{name} is not enabled here."
        users = [str(u) for u in (self.settings.get("command_users") or [])]
        if users and str(m.user_id) not in users:
            return "You are not allowed to run commands here."
        self.state["commands_run"] += 1
        try:
            return await getattr(self, f"_cmd_{name}")(parts[1:], m, person)
        except Exception as e:
            return self._error(f"command {name}", e)

    async def _cmd_help(self, args: list[str], m: Inbound, person: Any = None) -> str:
        p = self.COMMAND_PREFIX
        who = person.label.rsplit(" (", 1)[0] if person is not None else self.principal_id
        lines = [f"Rook commands (run as {who}):"]
        help_ = {"help": "this list", "workers": "live workers", "rooms": "bridged rooms",
                 "call": "call <worker> <cap> [json args]: run an allowed cap"}
        for c in self._commands():
            lines.append(f"{p}{c} - {help_[c]}")
        return "\n".join(lines)

    async def _cmd_workers(self, args: list[str], m: Inbound, person: Any = None) -> str:
        _d, _wid, err = self.authorize("band.workers", "rook", m, person=person)
        if err:
            return err
        roster = getattr(getattr(self._node, "client", None), "workers", {}) or {}
        names = sorted({str(w.get("name") or wid[:8]) for wid, w in roster.items()})
        return f"{len(names)} workers: " + ", ".join(names) if names else "No live workers."

    async def _cmd_rooms(self, args: list[str], m: Inbound, person: Any = None) -> str:
        _d, _wid, err = self.authorize("chat.read", "rook", m, person=person)
        if err:
            return err
        rooms = self.rooms()
        if not rooms:
            return "No rooms are bridged."
        return "Bridged rooms: " + ", ".join(f"{self._titles.get(r, r)} ({r})" for r in rooms)

    async def _cmd_call(self, args: list[str], m: Inbound, person: Any = None) -> str:
        p = self.COMMAND_PREFIX
        if len(args) < 2:
            return f"usage: {p}call <worker> <cap> [json args]"
        worker, cap = args[0], args[1]
        patterns = [str(x) for x in (self.settings.get("allowed_caps") or [])]
        if not any(fnmatch.fnmatchcase(cap, pat) for pat in patterns):
            return f"{cap} is not in this integration's allowed caps."
        try:
            cargs = json.loads(args[2]) if len(args) > 2 else {}
        except ValueError:
            return "args must be a JSON object"
        if not isinstance(cargs, dict):
            return "args must be a JSON object"
        d, wid, err = self.authorize(cap, worker, m, wait_ok=True, args=cargs, person=person)
        if err:
            return err
        client = self._node.client
        who = person if person is not None else self.principal(m)
        identity = (f"{person.id}/{self.PLATFORM}" if person is not None
                    else f"{self.principal_id}/user:{m.user_id}")
        try:
            reply = await client.call(cap=cap, args=cargs, target=wid, timeout=30.0,
                                      identity=identity, principal=who, _decision=d)
        except asyncio.TimeoutError:
            return f"{cap} on {worker} timed out"
        if not reply.get("ok"):
            return self.mask(f"{cap} failed: {reply.get('error')}")[:1500]
        out = reply.get("result")
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
        return self.mask(text)[:1500]

    # -- caps (subclasses expose them under their namespace) -------------------------------
    def status_dict(self) -> dict:
        return {"platform": self.PLATFORM, "enabled": True, "token_set": bool(self.token()),
                "chat_set": bool(self.chat_id()), "rooms": len(self.rooms()),
                "bridge": self._chat is not None and bool(self.rooms()),
                "bot": self.bot_name or None, "commands": self._commands(),
                "people": self._people() is not None,
                "own_bots": sum(1 for b in self._user_bots.values() if b.connected),
                **{k: self.state[k] for k in ("connected", "relayed_out", "relayed_in",
                                              "dropped", "commands_run", "denied", "unknown",
                                              "asked", "linked")},
                "last_error": self.state["last_error"]}

