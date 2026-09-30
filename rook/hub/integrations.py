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
  (``help``, ``workers``, ``rooms``, ``call``). Every command runs under the
  integration's own principal ``integration:<platform>`` and is checked with
  the permission policy first. The integration fails closed: a command runs
  only when the policy *allows* it (a ``would_deny`` in audit mode is refused
  too), so the default ``integration:*`` table (read + write, no exec, no
  admin) holds even before the operator turns enforcement on.

Loop prevention: messages the bridge writes into a room carry a
``<platform>:<user>`` sender and are never relayed back to that platform;
messages from bots (including this one) are ignored on the way in; notices
and command replies are never written into rooms.

Secrets: the bot token is a ``secret`` setting (vault key
``plugin.<platform>.token``). It is never logged or returned; every error
string passes through :meth:`ChatIntegration.mask` first.

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

log = logging.getLogger("rook.hub.integrations")

#: The permission rule that would let an integration exec on named workers.
#: Documented (docs/integrations.md), not enabled: add it to ``rules`` in the
#: policy document to use it.
EXAMPLE_EXEC_RULE = {"id": "telegram-exec-lab", "who": "integration:telegram",
                     "allow": "tier:exec", "on": ["worker-a", "worker-b"]}

COMMANDS = ("help", "workers", "rooms", "call")
DEFAULT_COMMANDS = ["help", "workers", "rooms"]
_MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_.:\-]+[A-Za-z0-9_])")


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


def integration_settings(platform: str, *, chat_label: str, api_default: str,
                         text_limit_help: str = "") -> tuple:
    """The settings schema every chat integration shares."""
    up = platform.upper()
    return (
        setting("enabled", bool, default=False, env=f"ROOK_{up}",
                label=f"{platform.title()} integration",
                help="Read at start: restart the hub to apply."),
        setting("token", str, secret=True, env=f"ROOK_{up}_TOKEN", label="Bot token",
                help=f"Stored in the vault as plugin.{platform}.token; never shown or logged."),
        setting("chat_id", str, default="", env=f"ROOK_{up}_CHAT", label=chat_label,
                help="Where bridged rooms, notifications and commands go. Messages from any "
                     "other chat are ignored."),
        setting("rooms", list, default=[], label="Bridged rooms",
                help="Rook chat room ids relayed to and from the chat. Empty: no bridge."),
        setting("commands", list, default=list(DEFAULT_COMMANDS), label="Allowed commands",
                help=f"Subset of {list(COMMANDS)}. Commands run as integration:{platform} and "
                     "are checked against the permission policy."),
        setting("allowed_caps", list, default=[], label="Caps the call command may run",
                help="Glob patterns (e.g. 'hub.*'). The policy must also allow them."),
        setting("command_users", list, default=[], label="Command users",
                help="Platform user ids allowed to run commands. Empty: anyone in the chat."),
        setting("mentions", dict, default={}, label="Mention map",
                help="Platform handle or user id -> Rook identity, e.g. "
                     "{\"alice\": \"user:operator\"}. Used both ways."),
        setting("rate_out", int, default=20, label="Outbound messages per minute"),
        setting("rate_in", int, default=10, label="Inbound messages per user per minute"),
        setting("api_base", str, default=api_default, env=f"ROOK_{up}_API",
                label="API base URL", help="Change only for a proxy or a test server."),
    )


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
        self.bot_id = ""
        self.bot_name = ""
        self.state: dict[str, Any] = {"connected": False, "last_error": None,
                                      "relayed_out": 0, "relayed_in": 0, "dropped": 0,
                                      "commands_run": 0, "denied": 0}

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
        self._open_chat()
        await self.connect()
        self._tasks.append(asyncio.get_running_loop().create_task(
            self._supervise("receive", self.receive_forever)))
        if self._chat is not None and self.rooms():
            self._tasks.append(asyncio.get_running_loop().create_task(
                self._supervise("bridge", self._bridge_forever)))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
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

    async def receive_forever(self) -> None:
        """Receive platform messages and hand each to :meth:`handle_inbound`.
        Return or raise to be restarted with backoff."""
        raise NotImplementedError

    async def send_raw(self, chat_id: str, text: str, mention_ids: list[str],
                       reply_to: str = "") -> str:
        """Send one chunk; return the platform message id. Raise
        :class:`RateLimited` on a 429."""
        raise NotImplementedError

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
        if tok and len(tok) >= 4:
            s = s.replace(tok, "***")
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
            tmp = self._watermark_path().with_suffix(".tmp")
            tmp.write_text(json.dumps({"rooms": self._watermarks}), encoding="utf-8")
            tmp.replace(self._watermark_path())
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
            msgs = [m for m in res.get("messages", []) if not self._own_sender(m.get("sender"))]
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

    async def handle_inbound(self, m: Inbound) -> str:
        """Route one platform message. Returns what happened (for tests/logs):
        ``ignored:*``, ``command``, ``bridged`` or ``dropped:*``."""
        if m.is_bot or (self.bot_id and str(m.user_id) == str(self.bot_id)):
            return "ignored:bot"
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

    def _target_rooms(self, m: Inbound) -> list[str]:
        rooms = self.rooms()
        if m.reply_to and self._reply_rooms.get(str(m.reply_to)) in rooms:
            return [self._reply_rooms[str(m.reply_to)]]
        return rooms[:1]

    async def _bridge_in(self, m: Inbound) -> str:
        if self._chat is None or not self.rooms():
            return "ignored:no-bridge"
        if not self.authorized_hub("chat.write"):
            return "dropped:policy"
        sender = f"{self.PLATFORM}:{m.username or m.user_id}"
        text = self._inbound_text(m)
        for rid in self._target_rooms(m):
            room = await asyncio.to_thread(self._chat.read, rid, None, 0, False, 1)
            parts = room.get("participants", []) if room.get("ok") else []
            mentions = self.map_inbound_mentions(m, parts)
            res = await asyncio.to_thread(self._chat.send, rid, sender, text, mentions,
                                          bool(mentions))
            if res.get("ok"):
                self.state["relayed_in"] += 1
                await asyncio.to_thread(self._chat.touch, sender)
        return "bridged"

    # -- sending ------------------------------------------------------------------
    async def send_text(self, chat_id: str, text: str, mention_ids: list[str] | None = None,
                        reply_to: str = "") -> str:
        """Rate-limited, chunked send with 429 retry. Returns the last message
        id, or '' when it could not be sent (the error is in ``state``)."""
        if not self.token():
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
                                                   reply_to if i == 0 else "")
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
            return {"ok": False, "error": f"{self.PLATFORM}: only the configured chat can be "
                                          "notified"}
        if not str(text or "").strip():
            return {"ok": False, "error": "empty message"}
        mid = await self.send_text(target, str(text))
        if not mid:
            return {"ok": False, "error": self.state.get("last_error") or "send failed"}
        return {"ok": True, "channel": self.PLATFORM, "message_id": mid}

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

    def authorize(self, cap: str, worker: str, user: Inbound | None = None):
        """``(decision, target_id, error)``. Fails closed: anything but an
        ``allow`` (or ``off`` for read/write) is refused, so a ``would_deny``
        in audit mode is still a no for an integration."""
        authz = self._authz()
        if authz is None:
            return None, None, "permissions are not configured on this hub; commands are off"
        wid, entry, local = self._resolve(worker)
        if wid is None:
            return None, None, f"no live worker named {worker!r}"
        d = authz.check(cap, wid, entry, principal=self.principal(user), local=local)
        ok = d.decision == "allow" or (d.decision == "off" and d.tier in ("read", "write"))
        if not ok:
            self.state["denied"] += 1
            return d, wid, (f"denied: {self.principal_id} may not call {cap} ({d.tier}) on "
                            f"{(entry or {}).get('name') or worker}"
                            + (f" [rule {d.rule}]" if d.rule else ""))
        return d, wid, None

    def authorized_hub(self, cap: str) -> bool:
        """Bridge-side check for hub actions (``chat.read`` / ``chat.write``).
        With no permissions layer the bridge runs (commands still refuse)."""
        if self._authz() is None or self._node is None:
            return True
        _d, _wid, err = self.authorize(cap, "rook")
        return err is None

    # -- commands ----------------------------------------------------------------------
    def _commands(self) -> list[str]:
        allowed = self.settings.get("commands")
        allowed = DEFAULT_COMMANDS if allowed is None else allowed
        return [c for c in COMMANDS if c in allowed]

    async def run_command(self, line: str, m: Inbound) -> str:
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
            return await getattr(self, f"_cmd_{name}")(parts[1:], m)
        except Exception as e:
            return self._error(f"command {name}", e)

    async def _cmd_help(self, args: list[str], m: Inbound) -> str:
        p = self.COMMAND_PREFIX
        lines = [f"Rook commands (run as {self.principal_id}):"]
        help_ = {"help": "this list", "workers": "live workers", "rooms": "bridged rooms",
                 "call": "call <worker> <cap> [json args]: run an allowed cap"}
        for c in self._commands():
            lines.append(f"{p}{c} - {help_[c]}")
        return "\n".join(lines)

    async def _cmd_workers(self, args: list[str], m: Inbound) -> str:
        _d, _wid, err = self.authorize("band.workers", "rook", m)
        if err:
            return err
        roster = getattr(getattr(self._node, "client", None), "workers", {}) or {}
        names = sorted({str(w.get("name") or wid[:8]) for wid, w in roster.items()})
        return f"{len(names)} workers: " + ", ".join(names) if names else "No live workers."

    async def _cmd_rooms(self, args: list[str], m: Inbound) -> str:
        _d, _wid, err = self.authorize("chat.read", "rook", m)
        if err:
            return err
        rooms = self.rooms()
        if not rooms:
            return "No rooms are bridged."
        return "Bridged rooms: " + ", ".join(f"{self._titles.get(r, r)} ({r})" for r in rooms)

    async def _cmd_call(self, args: list[str], m: Inbound) -> str:
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
        d, wid, err = self.authorize(cap, worker, m)
        if err:
            return err
        client = self._node.client
        try:
            reply = await client.call(cap=cap, args=cargs, target=wid, timeout=30.0,
                                      identity=f"{self.principal_id}/user:{m.user_id}",
                                      principal=self.principal(m), _decision=d)
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
                **{k: self.state[k] for k in ("connected", "relayed_out", "relayed_in",
                                              "dropped", "commands_run", "denied")},
                "last_error": self.state["last_error"]}

