"""Persistent sessions for the house agent, the hub side
(docs/design/persistent-agents.md).

The house agent can run as two always-on Claude Code sessions on a session
host (``python -m rook.agents`` on the machine with the Claude login): the
agent itself on Opus in its role directory (the **main lane**) and its voice on
Haiku in ``voice/`` (the **voice lane**, the conductor). This module is the
hub's half:

* **the link** (``/home/agents-link``): the session host dials in over a
  websocket with a token whose sha256 is ``home.link_token_sha256``; the hub
  sends it the role directories' files and lane specs (``configure``) and
  drives the lanes; the host reports what runs (events);
* **the inbox** (:mod:`.inbox`): with ``home.persistent`` on, every house-agent
  turn whose tools the main lane has (the front door's, knowledge search,
  memory) becomes an item in the role's queue instead of a one-shot run on the
  agent host. One item at a time, by priority; notes batched;
* **the main lane's tools**: the main lane calls the hub's tools at
  ``/home/turn-mcp`` with its own long-lived token, and each call runs with the
  handler of the item being worked (that requester's permissions);
* **the flow tools** for the voice lane (same tool server, another token):
  queue, steer, fork, fold, status, btw, pause, resume, cancel;
* **maintenance**: above ``home.compact_at_tokens`` the main lane is asked to
  save durable facts to memory, then compacted;
* **role config**: the persistent roles' model, effort and MCP grants (grants
  are trust-map entries: :mod:`rook.hub.trust` ``grant_tool``).

Without a connected session host every turn takes the one-shot path, as
before (:meth:`accepts`).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from typing import Any

from . import roledir
from .agent_host import TurnFailed, Unavailable, flatten
from .inbox import (BACKGROUND, PERSON, VOICE, Inbox, Item, priority_of, requester,
                    summary_line, turn_text)

log = logging.getLogger("rook.hub.plugins.home.sessions")

LINK_PATH = "/home/agents-link"
HOUSE = "house"
#: Tokens on the tool server live as long as the hub process (a restart sends
#: new ones; the session host reloads its lanes with them).
FOREVER_S = 10 * 365 * 86400.0
#: Requests waiting for a reply from the session host.
REQUEST_TIMEOUT = 20.0
BTW_TIMEOUT = 60.0
#: Voice-turn timings kept for status (the latency numbers).
KEEP_METRICS = 200
FOLD_MARK = re.compile(r"FOLD SUMMARY:\s*", re.I)
ROLE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
#: Persistent turns only for these vias (the conversation), never for job steps,
#: chair turns, role turns or the memory summariser.
PERSISTENT_VIAS = ("ask", "chat")

#: Set around a turn started by a flow tool: the priority it was queued with.
PRIORITY_OVERRIDE: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "home_sessions_priority", default=None)

SAVE_PROMPT = ("Maintenance: your context is large and will be compacted right after this "
               "turn. Save what is worth keeping beyond it with memory_save: durable facts "
               "about the person and the band, decisions and their reasons, open threads. One "
               "fact per call, no secrets, nothing you already saved. Then reply SAVED.")
COMPACT_INSTRUCTIONS = ("Keep: who the person is, open requests and side threads, decisions "
                        "made and why, anything waiting on the person. Drop tool output "
                        "detail.")
FORK_PROMPT = ("[side thread {fid} | from {who} | forked from your conversation]\n\n"
               "This is a side thread: the main conversation goes on without you. Work on "
               "this request only, with your tools as usual:\n\n{text}\n\n"
               "When you are done, end your reply with a line starting 'FOLD SUMMARY:' and "
               "two to four sentences: what you learned, did and decided. That summary is "
               "folded back into the main conversation.")


def _fn(name: str, description: str, props: dict | None = None,
        required: list | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {},
                       **({"required": required} if required else {})}}}


FLOW_TOOLS = {
    "queue": _fn("queue", "Hand a request to the back end (the house agent with all its "
                 "tools). Returns at once; its answer comes back to you as a [back end] "
                 "message. priority: now (the person is waiting, default), next, later.",
                 {"request": {"type": "string"},
                  "priority": {"type": "string", "enum": ["now", "next", "later"]}},
                 ["request"]),
    "steer": _fn("steer", "Add the person's words to what the back end is doing right now "
                 "(it sees them at its next step). interrupt=true stops the current step "
                 "first (\"stop\", \"change of plan\"). If it is idle, the words are queued "
                 "first in line.",
                 {"text": {"type": "string"}, "interrupt": {"type": "boolean"}}, ["text"]),
    "fork": _fn("fork", "Start a side thread from the back end's last finished turn for a "
                "separate request, while it stays busy. Its result comes back to you and "
                "is folded into the back end.", {"request": {"type": "string"}},
                ["request"]),
    "fold": _fn("fold", "Bring a finished side thread's result into the back end now "
                "(blank: every finished one).", {"fork": {"type": "string"}}),
    "status": _fn("status", "What the back end and its side threads are doing, and what "
                  "waits in its queue, without bothering it."),
    "btw": _fn("btw", "A side question to the back end (\"where are we at on that?\"), "
               "answered from its current context without interrupting it or adding to "
               "its history. Takes 2-4 seconds.", {"question": {"type": "string"}},
               ["question"]),
    "pause": _fn("pause", "Stop the back end taking new requests from its queue (the "
                 "current one finishes)."),
    "resume": _fn("resume", "Let the back end take requests from its queue again."),
    "cancel": _fn("cancel", "Cancel something: target \"current\" (stop the running turn), "
                  "a queued item id, or a side thread id.", {"target": {"type": "string"}},
                  ["target"]),
}


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode()).hexdigest()


def _hash_ok(token: str, want: str) -> bool:
    want = str(want or "").strip().lower()
    return bool(want) and bool(token) and hmac.compare_digest(token_hash(token), want)


class Link:
    """The connected session host."""

    def __init__(self, ws: Any, hello: dict) -> None:
        self.ws = ws
        self.hello = hello
        self.host = str(hello.get("host") or "?")[:64]
        self.since = time.time()
        self.lock = asyncio.Lock()

    async def send(self, msg: dict) -> None:
        async with self.lock:
            await self.ws.send_text(json.dumps(msg, separators=(",", ":"), default=str))


class Sessions:
    """The hub's supervisor of persistent sessions (one session host)."""

    def __init__(self, home: Any) -> None:
        self.home = home
        self.link: Link | None = None
        self.inboxes: dict[str, Inbox] = {}
        self.current: dict[str, Item | None] = {}
        self.lanes: dict[str, dict] = {}            # "role/lane" -> state
        self.forks: dict[str, dict] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.voice: dict[str, dict] = {}            # role -> last voice turn
        self.metrics: list[dict] = []               # voice replies and main results
        self._tokens: dict[str, str] = {}           # "role/lane" -> tool server token
        self._ctx_hash: dict[str, str] = {}
        self._pumps: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._compact_due: set[str] = set()
        self._steer_voice: dict[str, float] = {}
        self._state: dict | None = None
        self._trust_version = -1

    # -- settings and saved state ------------------------------------------------
    def cfg(self) -> dict:
        return self.home.config()

    def on(self) -> bool:
        return self.home.enabled() and bool(self.home.settings.get("persistent", False))

    def _state_path(self):
        return self.home.data_dir / "sessions.json"

    def state(self) -> dict:
        if self._state is None:
            try:
                self._state = json.loads(self._state_path().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._state = {}
            self._state.setdefault("sessions", {})
            self._state.setdefault("roles", {})
            self._state.setdefault("resets", [])
        return self._state

    def _save(self) -> None:
        from ....core.db import atomic_write_json
        with contextlib.suppress(Exception):
            atomic_write_json(str(self._state_path()), self.state())

    def role_cfg(self, role: str) -> dict:
        """A persistent role's config: the house agent's from the home settings,
        others from the saved role config."""
        s = self.home.settings
        saved = dict(self.state()["roles"].get(role) or {})
        if role == HOUSE:
            base = {"persistent": True, "voice": bool(s.get("voice_lane", True)),
                    "model": str(s.get("persistent_model") or "opus"),
                    "effort": str(s.get("persistent_effort") or ""),
                    "voice_model": str(s.get("voice_model") or "haiku"),
                    "voice_effort": str(s.get("voice_effort") or "low"),
                    "idle_sleep_min": int(s.get("persistent_idle_sleep_min") or 0),
                    "memory": "off"}
            base.update({k: v for k, v in saved.items() if k in ("model", "effort")})
            return base
        return {"persistent": bool(saved.get("persistent")), "voice": bool(saved.get("voice")),
                "model": str(saved.get("model") or "sonnet"),
                "effort": str(saved.get("effort") or ""), "voice_model": "haiku",
                "voice_effort": "low", "idle_sleep_min": int(saved.get("idle_sleep_min", 30)),
                "memory": str(saved.get("memory") or "write")}

    def roles(self) -> list[str]:
        """Roles that run persistently (the house agent first)."""
        out = [HOUSE] if self.on() else []
        for rid, c in sorted(self.state()["roles"].items()):
            if rid != HOUSE and c.get("persistent") and ROLE_ID.match(rid):
                out.append(rid)
        return out

    # -- tool sets -------------------------------------------------------------------
    def main_tools(self) -> list[dict]:
        """Every tool the main lane offers (by name): the front door's, knowledge
        search and memory. Each request may use only its own subset."""
        from . import BAND_TOOL, front
        out = {(t.get("function") or t)["name"]: t for t in front.TOOLS.values()}
        out.setdefault("knowledge_search", front.KNOWLEDGE)
        out.setdefault("band_status", BAND_TOOL)
        mem = self.home._memory()
        if mem is not None:
            for t in mem.tools(save=True):
                out.setdefault((t.get("function") or t)["name"], t)
        return list(out.values())

    def main_tool_names(self) -> set[str]:
        return {(t.get("function") or t)["name"] for t in self.main_tools()}

    def _token(self, role: str, lane: str) -> str:
        key = f"{role}/{lane}"
        reg = self.home.agents.registry
        tok = self._tokens.get(key)
        if tok and reg.get(tok) is not None:
            return tok
        if lane == "voice":
            tools, handler = list(FLOW_TOOLS.values()), self._flow_handler(role)
        else:
            tools, handler = self.main_tools(), self._main_handler(role)
        tok, _ = reg.open(tools, handler, FOREVER_S)
        self._tokens[key] = tok
        return tok

    def _main_handler(self, role: str):
        async def handler(name: str, args: dict) -> str:
            item = self.current.get(role)
            if item is None or item.handler is None:
                return json.dumps({"ok": False, "error": "no request is being worked on; "
                                                         "tools run only inside a request"})
            if name not in item.tools:
                return json.dumps({"ok": False, "error": f"{name} is not available for this "
                                                         "request"})
            item.meta.setdefault("tools", []).append(name)
            return await item.handler(name, args)
        return handler

    # -- the link ------------------------------------------------------------------
    def connected(self) -> bool:
        return self.link is not None

    async def accept(self, websocket: Any) -> None:
        """The ``/home/agents-link`` websocket (starlette)."""
        auth = websocket.headers.get("authorization", "")
        tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not _hash_ok(tok, self.home.settings.get("link_token_sha256") or ""):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), 15.0)
            hello = json.loads(raw)
            if not isinstance(hello, dict) or hello.get("t") != "hello":
                raise ValueError("expected hello")
        except Exception:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await websocket.close(code=1002)
            return
        link = Link(websocket, hello)
        old, self.link = self.link, link
        if old is not None:
            with contextlib.suppress(Exception):
                await old.ws.close(code=1000)
        for key, st in (hello.get("lanes") or {}).items():
            if isinstance(st, dict):
                self.lanes[key] = {**self.lanes.get(key, {}), **st, "updated": time.time()}
        log.info("session host %s connected", link.host)
        self.home._note("sessions", True, worker=link.host, event="connected")
        watcher = asyncio.get_running_loop().create_task(self._watch(link))
        try:
            await self._configure_safely()
            for role in self.roles():
                self._ensure_pump(role)
                self.inbox(role).wake.set()
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    try:
                        await self._on_message(msg)
                    except Exception:  # noqa: BLE001 - one bad event never drops the link
                        log.exception("session host event failed: %s", str(msg)[:200])
        except Exception:  # noqa: BLE001 - disconnect
            pass
        finally:
            watcher.cancel()
            if self.link is link:
                self.link = None
                for key in list(self.lanes):
                    self.lanes[key]["state"] = "offline"
                log.info("session host %s disconnected", link.host)
                self.home._note("sessions", False, worker=link.host,
                                error="the session host disconnected")

    async def send(self, msg: dict) -> None:
        link = self.link
        if link is None:
            raise Unavailable("no session host is connected")
        try:
            await link.send(msg)
        except Exception as e:  # noqa: BLE001
            raise Unavailable(f"the session host link failed: {e}") from None

    async def request(self, msg: dict, timeout: float = REQUEST_TIMEOUT) -> dict:
        req = msg.setdefault("req", uuid.uuid4().hex[:12])
        fut = asyncio.get_running_loop().create_future()
        self.pending[req] = fut
        try:
            await self.send(msg)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise Unavailable(f"the session host did not answer {msg.get('t')} in "
                              f"{timeout:g}s") from None
        finally:
            self.pending.pop(req, None)

    def turn_url(self) -> str:
        return self.home.agents.turn_url(self.cfg())

    def configure_msg(self) -> dict:
        """Files and lane specs for every persistent role (tokens in env only)."""
        roles: dict[str, dict] = {}
        url = self.turn_url()
        trust = self._trust()
        voice_hash = str(self.home.settings.get("voice_token_sha256") or "").strip().lower()
        for role in self.roles():
            rc = self.role_cfg(role)
            granted = [g["server"] for g in (trust.grants(role) if trust is not None else [])]
            files = roledir.main_files(claude_md=self._claude_md(role), model=rc["model"],
                                       effort=rc["effort"], turn_url=url,
                                       host_servers=granted)
            sess = self.state()["sessions"].get(role) or {}
            lanes = {"main": {"model": rc["model"], "effort": rc["effort"],
                              "tools": list(roledir.MAIN_TOOLS),
                              "env": {roledir.TURN_TOKEN_ENV: self._token(role, "main")},
                              "partial": False, "session_id": sess.get("main") or "",
                              "idle_sleep_s": int(rc["idle_sleep_min"]) * 60, "thinking": True}}
            if rc["voice"]:
                files.update(roledir.voice_files(turn_url=url, model=rc["voice_model"],
                                                 effort=rc["voice_effort"]))
                lanes["voice"] = {"model": rc["voice_model"], "effort": rc["voice_effort"],
                                  "tools": [],
                                  "env": {roledir.FLOW_TOKEN_ENV: self._token(role, "voice")},
                                  "partial": True, "session_id": sess.get("voice") or "",
                                  "idle_sleep_s": 0, "thinking": False}
            roles[role] = {"dir_files": files, "lanes": lanes, "host_servers": granted,
                           "memory": rc["memory"], "voice_token_sha256": voice_hash}
        return {"t": "configure", "roles": roles}

    async def configure(self) -> None:
        if self.link is None:
            return
        trust = self._trust()
        if trust is not None:
            with contextlib.suppress(Exception):
                self._trust_version = int(trust.current().get("version") or 0)
        await self.send(self.configure_msg())

    async def _configure_safely(self) -> None:
        try:
            await self.configure()
        except Unavailable as e:
            log.warning("persistent sessions: cannot configure the session host: %s", e)
            self.home._note("sessions", False, error=str(e)[:300])

    async def _watch(self, link: Link) -> None:
        """While a host is connected: follow trust-map changes (grants)."""
        while self.link is link:
            await asyncio.sleep(10.0)
            with contextlib.suppress(Exception):
                await self.watch_trust()

    def _claude_md(self, role: str) -> str:
        cfg = self.cfg()
        if role == HOUSE:
            extra = str(cfg.get("system_prompt") or "").strip()
            return roledir.house_claude_md(self.home.persona_text(cfg), self.home.agent_name,
                                           extra)
        rp = self.home._node.plugin("role") if self.home._node is not None else None
        doc = None
        if rp is not None:
            with contextlib.suppress(Exception):
                doc = rp.store.get(role)
        if doc is None:
            return roledir.role_claude_md(f"You are the {role} role on this Rook hub.", role)
        try:
            prompt = rp.system_prompt(doc, cfg)
        except Exception:  # noqa: BLE001
            prompt = f"You are {doc.get('name') or role} (role:{role})."
        return roledir.role_claude_md(prompt, str(doc.get("name") or role))

    def _trust(self):
        from ..trust import node_store
        with contextlib.suppress(Exception):
            return node_store(self.home._node)
        return None

    async def watch_trust(self) -> None:
        """Re-configure when the trust map changes (a grant accepted on the
        Trust page takes effect without anyone asking)."""
        trust = self._trust()
        if trust is None or self.link is None:
            return
        with contextlib.suppress(Exception):
            v = int(trust.current().get("version") or 0)
            if v != self._trust_version:
                await self.configure()

    # -- events --------------------------------------------------------------------
    async def _on_message(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "reply":
            fut = self.pending.get(str(msg.get("req") or ""))
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        if t == "hello":
            if self.link is not None:
                self.link.hello = msg
            await self._configure_safely()
            return
        if t != "ev":
            return
        role = str(msg.get("role") or "")
        lane = str(msg.get("lane") or "main")
        kind = msg.get("kind")
        key = f"{role}/{lane}"
        st = self.lanes.setdefault(key, {"state": "offline"})
        st["updated"] = time.time()
        if kind == "state":
            st.update({k: msg[k] for k in ("state", "session_id", "context_tokens", "error")
                       if k in msg})
            sid = str(msg.get("session_id") or "")
            saved = self.state()["sessions"].setdefault(role, {})
            if sid and saved.get(lane) != sid:
                saved[lane] = sid
                self._save()
            if msg.get("state") in ("idle", "sleeping", "stopped"):
                if lane == "voice":
                    await self._maybe_compact_voice(role)
                self.inbox(role).wake.set()
        elif kind == "session":
            sid = str(msg.get("session_id") or "")
            st["session_id"] = sid
            if sid:
                self.state()["sessions"].setdefault(role, {})[lane] = sid
                self._save()
            if msg.get("reset_reason"):
                if lane == "main":
                    self._ctx_hash.pop(role, None)
                reason = str(msg["reset_reason"])[:300]
                self.state()["resets"] = (self.state()["resets"] + [
                    {"role": role, "lane": lane, "at": time.time(), "reason": reason,
                     "session_id": sid}])[-20:]
                self._save()
                st["reset"] = reason
                log.warning("persistent session %s started fresh: %s", key, reason)
                self.home._note("session-reset", False, error=f"{key}: {reason}")
                self.home.journal("home.session_reset", {"role": role, "lane": lane},
                                  {"ok": False, "error": reason})
        elif kind == "turn_start":
            item = self.current.get(role)
            if item is not None and lane == "main":
                item.meta["host_started"] = time.time()
        elif kind == "tool":
            item = self.current.get(role)
            if item is not None and lane == "main":
                item.meta["last_tool"] = str(msg.get("name") or "")[:80]
                item.meta["last_tool_at"] = time.time()
        elif kind == "text":
            item = self.current.get(role)
            if item is not None and lane == "main":
                item.meta["last_text"] = str(msg.get("text") or "")[-400:]
        elif kind == "result":
            await self._on_result(role, lane, msg)
        elif kind == "compacted":
            if lane == "main":
                self._ctx_hash.pop(role, None)      # the next request carries its context again
            st["last_compact"] = {"at": time.time(), "pre": msg.get("pre_tokens"),
                                  "post": msg.get("post_tokens")}
            st["context_tokens"] = msg.get("post_tokens") or st.get("context_tokens")
            self.home.journal("home.session_compact", {"role": role, "lane": lane},
                              {"ok": True, "result": st["last_compact"]})
        elif kind == "fork":
            await self._on_fork(msg)
        elif kind == "btw":
            fut = self.pending.get(str(msg.get("req") or ""))
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif kind == "voice_turn":
            self.voice[role] = {"turn": msg.get("turn"), "text": str(msg.get("text") or "")[:300],
                                "identity": msg.get("identity") or {}, "at": time.time()}
        elif kind == "voice_reply":
            row = {"kind": "voice", "role": role, "at": time.time(),
                   **{k: msg.get(k) for k in ("turn", "first_ms", "total_ms", "tools",
                                               "thinking_tokens")}}
            self._metric(row)

    def _metric(self, row: dict) -> None:
        self.metrics = (self.metrics + [row])[-KEEP_METRICS:]

    async def _on_result(self, role: str, lane: str, msg: dict) -> None:
        key = f"{role}/{lane}"
        st = self.lanes.setdefault(key, {})
        if msg.get("context_tokens"):
            st["context_tokens"] = msg["context_tokens"]
        if lane != "main":
            return
        item = self.current.get(role)
        ident = str(msg.get("item") or "")
        if ident == "steer" or item is None or ident != item.id:
            # A steer that became its own turn (it arrived as the last turn
            # ended): the person's words were answered; pass the reply on.
            text = str(msg.get("text") or "").strip()
            if text and time.time() - self._steer_voice.get(role, 0) < 600:
                with contextlib.suppress(Unavailable):
                    await self.voice_say(role, text)
            return
        dur = msg.get("duration_ms")
        self._metric({"kind": "main", "role": role, "at": time.time(), "item": item.id,
                      "via": item.via, "duration_ms": dur,
                      "queued_ms": round(((item.started or item.created) - item.created) * 1000),
                      "num_turns": msg.get("num_turns"), "context_tokens": msg.get("context_tokens"),
                      "ok": bool(msg.get("ok"))})
        if msg.get("ok"):
            item.settle({"text": str(msg.get("text") or ""), "usage": msg.get("usage") or {},
                         "cost_usd": msg.get("cost_usd"), "duration_ms": dur,
                         "num_turns": msg.get("num_turns")})
        else:
            item.settle(error=str(msg.get("error") or "the turn failed")[:300])
        self.current[role] = None
        self.inbox(role).finish(item)
        if item.kind == "maintenance" and item.meta.get("then_compact"):
            with contextlib.suppress(Unavailable):
                await self.send({"t": "compact", "role": role, "lane": "main",
                                 "instructions": COMPACT_INSTRUCTIONS})
            self._compact_due.discard(role)
        elif self._over(role, "main") and role not in self._compact_due:
            self._compact_due.add(role)
            self.inbox(role).put(Item(id=Inbox.new_id(), via="maintenance", priority=BACKGROUND,
                                      text=SAVE_PROMPT, kind="maintenance", who="the hub",
                                      tools=frozenset({"memory_save", "memory_search"}),
                                      handler=self._memory_handler(),
                                      meta={"then_compact": True}))

    def _over(self, role: str, lane: str) -> bool:
        st = self.lanes.get(f"{role}/{lane}") or {}
        limit = int(self.home.settings.get("compact_at_tokens" if lane == "main"
                                           else "voice_compact_at_tokens") or 0)
        return bool(limit) and int(st.get("context_tokens") or 0) > limit

    async def _maybe_compact_voice(self, role: str) -> None:
        key = f"{role}/voice"
        if self._over(role, "voice") and not self.lanes[key].get("compacting"):
            self.lanes[key]["compacting"] = time.time()
            with contextlib.suppress(Unavailable):
                await self.send({"t": "compact", "role": role, "lane": "voice",
                                 "instructions": "Keep who is speaking and anything still "
                                                 "pending with the back end."})
        elif not self._over(role, "voice"):
            self.lanes[key].pop("compacting", None)

    def _memory_handler(self):
        home = self.home

        async def handler(name: str, args: dict) -> str:
            return await home._run_tool(name, args, None,
                                        {"person": None, "room": None, "save": True})
        return handler

    # -- the inbox and the main lane ---------------------------------------------------
    def inbox(self, role: str) -> Inbox:
        ib = self.inboxes.get(role)
        if ib is None:
            ib = self.inboxes[role] = Inbox(role)
            self.current.setdefault(role, None)
        return ib

    def _ensure_pump(self, role: str) -> None:
        task = self._pumps.get(role)
        if task is None or task.done():
            self._pumps[role] = asyncio.get_running_loop().create_task(self._pump(role))

    def _can_dispatch(self, role: str) -> bool:
        if self.link is None or self.current.get(role) is not None:
            return False
        st = (self.lanes.get(f"{role}/main") or {}).get("state")
        return st in ("idle", "sleeping", "stopped")

    async def _pump(self, role: str) -> None:
        inbox = self.inbox(role)
        while True:
            await inbox.wake.wait()
            inbox.wake.clear()
            if not self._can_dispatch(role):
                continue
            item = inbox.take()
            if item is None:
                continue
            await self._dispatch(role, item)

    async def _dispatch(self, role: str, item: Item) -> None:
        ctx = ""
        if item.context:
            h = hashlib.sha256(item.context.encode()).hexdigest()
            if self._ctx_hash.get(role) != h:
                ctx = item.context
                self._ctx_hash[role] = h
        self.current[role] = item
        self.lanes.setdefault(f"{role}/main", {})["state"] = "busy"
        try:
            await self.send({"t": "send", "role": role, "lane": "main", "item": item.id,
                             "text": turn_text(item, ctx)})
        except Unavailable:
            self.current[role] = None
            self._ctx_hash.pop(role, None)
            item.state = "queued"
            item.started = None
            for n in item.notes:
                n.state = "queued"
            item.notes = []

    def accepts(self, cfg: dict, tools: list[dict], via: str, role: dict | None) -> bool:
        """Whether this turn goes to the house agent's persistent main lane."""
        if role is not None or not self.on() or self.link is None:
            return False
        if not (via.startswith("front-") or via in PERSISTENT_VIAS):
            return False
        names = {(t.get("function") or t)["name"] for t in tools or []}
        return names <= self.main_tool_names()

    async def run(self, cfg: dict, messages: list[dict], *, tools: list[dict], handler: Any,
                  via: str) -> dict:
        """One house-agent turn through the inbox and the main lane; the same
        reply shape as the one-shot agent host turn."""
        system, prompt = flatten(messages)
        names = frozenset((t.get("function") or t)["name"] for t in tools or [])
        m = re.match(r"^(?:.*\n\nThe message to answer now:\n)?\[([^\]\n]{1,80})\]", prompt, re.S)
        who = m.group(1) if m else via
        prio = PRIORITY_OVERRIDE.get()
        item = Item(id=Inbox.new_id(), via=via,
                    priority=priority_of(via) if prio is None else prio, text=prompt,
                    context=system, tools=names, handler=handler, who=who,
                    waiter=asyncio.get_running_loop().create_future())
        inbox = self.inbox(HOUSE)
        self._ensure_pump(HOUSE)
        inbox.put(item)
        timeout = float(cfg.get("agent_timeout_s") or 180.0)
        t0 = time.monotonic()
        try:
            while True:
                left = timeout - (time.monotonic() - t0)
                if left <= 0:
                    raise asyncio.TimeoutError
                try:
                    res = await asyncio.wait_for(asyncio.shield(item.waiter), min(left, 2.0))
                    break
                except asyncio.TimeoutError:
                    if self.link is None and item.state == "queued":
                        # The session host went away before this was sent:
                        # the caller takes the one-shot path instead.
                        inbox.cancel(item.id)
                        item.waiter.add_done_callback(
                            lambda f: None if f.cancelled() else f.exception())
                        raise Unavailable("the session host disconnected") from None
                    if time.monotonic() - t0 >= timeout:
                        raise
        except asyncio.TimeoutError:
            item.waiter.add_done_callback(
                lambda f: None if f.cancelled() else f.exception())
            if item.state == "queued":
                inbox.cancel(item.id)
                raise TurnFailed("the house agent is busy; try again shortly") from None
            with contextlib.suppress(Unavailable):
                await self.send({"t": "interrupt", "role": HOUSE, "lane": "main"})
            raise TurnFailed(f"the house agent's turn ran over {timeout:g}s") from None
        except RuntimeError as e:
            raise TurnFailed(str(e)) from None
        host = self.link.host if self.link is not None else ""
        return {"content": str(res.get("text") or "").strip(),
                "model": self.role_cfg(HOUSE)["model"], "latency_ms":
                    round((time.monotonic() - t0) * 1000),
                "tools_used": list(item.meta.get("tools") or []), "worker": host,
                "worker_id": "", "harness": "claude", "usage": res.get("usage") or {},
                "cost_usd": res.get("cost_usd"), "persistent": True, "item": item.id}

    def note(self, text: str, *, via: str = "report", who: str = "", role: str = HOUSE) -> Item:
        """A background input nobody waits on (a session report, a job result):
        batched into the main lane's next turn."""
        item = Item(id=Inbox.new_id(), via=via, priority=BACKGROUND,
                    text=str(text or "")[:4000], kind="note", who=who or via)
        self._ensure_pump(role)
        return self.inbox(role).put(item)

    # -- flow tools (the voice lane's) -----------------------------------------------
    def _flow_handler(self, role: str):
        async def handler(name: str, args: dict) -> str:
            try:
                out = await self.flow(role, name, args)
            except (Unavailable, ValueError, LookupError) as e:
                out = {"ok": False, "error": str(e)[:300]}
            return json.dumps(out, default=str)
        return handler

    def voice_principal(self, role: str) -> tuple[Any, str]:
        """Who the voice is talking to (the last voice turn's identity): the
        owner is a person; anyone else a guest."""
        ident = (self.voice.get(role) or {}).get("identity") or {}
        name = re.sub(r"[^A-Za-z0-9 _.'-]", "", str(ident.get("name") or ""))[:40].strip()
        if ident.get("owner"):
            name = name or "the owner"
            return self.home.chat_principal(f"user:{name}"), name
        from ...policy import Principal
        label = f"guest:{name or 'voice'}"
        return Principal("unverified", "unverified", verified=False, label=label), label

    async def flow(self, role: str, name: str, args: dict) -> dict:
        if name == "status":
            return self.status(role)
        if name == "queue":
            prio = {"now": VOICE, "next": PERSON, "later": BACKGROUND}.get(
                str(args.get("priority") or "now"), VOICE)
            return self.queue_voice(role, str(args.get("request") or ""), prio)
        if name == "steer":
            return await self.steer(role, str(args.get("text") or ""),
                                    bool(args.get("interrupt")))
        if name == "fork":
            principal, who = self.voice_principal(role)
            return await self.fork(role, str(args.get("request") or ""), who=who,
                                   principal=principal, origin="voice")
        if name == "fold":
            return self.fold(str(args.get("fork") or ""))
        if name == "btw":
            return await self.btw(role, str(args.get("question") or ""))
        if name == "pause":
            self.inbox(role).paused = True
            return {"ok": True, "paused": True}
        if name == "resume":
            self.inbox(role).paused = False
            self.inbox(role).wake.set()
            return {"ok": True, "paused": False}
        if name == "cancel":
            return await self.cancel(role, str(args.get("target") or ""))
        raise ValueError(f"unknown flow tool {name!r}")

    def queue_voice(self, role: str, request: str, priority: int = VOICE,
                    turn: str | None = None) -> dict:
        request = request.strip()
        if not request:
            raise ValueError("queue needs the request")
        principal, who = self.voice_principal(role)
        turn = turn or (self.voice.get(role) or {}).get("turn")
        ahead = self.inbox(role).position() + (1 if self.current.get(role) else 0)
        self._spawn(self._voice_request(role, request, priority, who, principal, turn))
        return {"ok": True, "queued": True, "ahead": ahead,
                "note": "The back end has it; its answer comes back as a [back end] message."}

    async def _voice_request(self, role: str, request: str, priority: int, who: str,
                             principal: Any, turn: Any) -> None:
        tok = PRIORITY_OVERRIDE.set(priority)
        try:
            out = await self.home.voice_request(request, asker=who, principal=principal)
            text = str(out.get("answer") or "").strip() or "(no answer)"
        except Exception as e:  # noqa: BLE001 - the person hears that it failed
            text = f"That didn't work: {str(e)[:200]}"
        finally:
            PRIORITY_OVERRIDE.reset(tok)
        with contextlib.suppress(Unavailable):
            await self.voice_say(role, f"(for: {request[:120]}) {text}", turn)

    async def voice_say(self, role: str, text: str, turn: Any = None) -> None:
        msg = {"t": "voice_say", "role": role, "text": str(text)[:4000]}
        if turn:
            msg["turn"] = turn
        await self.send(msg)

    async def steer(self, role: str, text: str, interrupt: bool = False) -> dict:
        text = text.strip()
        if not text:
            raise ValueError("steer needs the person's words")
        _, who = self.voice_principal(role)
        item = self.current.get(role)
        if item is None:
            return {**self.queue_voice(role, text, VOICE), "steered": False,
                    "note": "The back end was idle, so this went to the front of its queue."}
        if interrupt:
            await self.send({"t": "interrupt", "role": role, "lane": "main"})
        await self.send({"t": "steer", "role": role, "lane": "main",
                         "text": f"[{who}, through the voice, while you work] {text}"})
        item.meta.setdefault("steers", []).append(text[:200])
        self._steer_voice[role] = time.time()
        return {"ok": True, "steered": item.id, "interrupted": interrupt}

    async def cancel(self, role: str, target: str) -> dict:
        target = target.strip()
        if target in ("", "current"):
            if self.current.get(role) is None:
                return {"ok": True, "cancelled": False, "note": "nothing is running"}
            await self.send({"t": "interrupt", "role": role, "lane": "main"})
            return {"ok": True, "cancelled": self.current[role].id}
        if target in self.forks:
            await self.send({"t": "fork_cancel", "fork": target})
            return {"ok": True, "cancelled": target}
        item = self.inbox(role).cancel(target)
        if item is None:
            raise LookupError(f"nothing queued or running is called {target!r}")
        return {"ok": True, "cancelled": item.id}

    # -- forks, fold, btw ---------------------------------------------------------------
    def running_forks(self, role: str | None = None) -> list[dict]:
        return [f for f in self.forks.values() if f["state"] in ("starting", "started")
                and (role is None or f["role"] == role)]

    async def fork(self, role: str, request: str, *, who: str, principal: Any,
                   origin: str = "", repo: str = "") -> dict:
        request = request.strip()
        if not request:
            raise ValueError("fork needs the request")
        limit = int(self.home.settings.get("fork_max") or 2)
        if len(self.running_forks()) >= limit:
            return {"ok": False, "error": f"{limit} side threads are running already; queue it "
                                          "or wait"}
        fid = "f" + secrets.token_hex(3)
        tools, handler = self.home.front.toolkit(request, source="fork", asker=who,
                                                 principal=principal)
        allowed = frozenset((t.get("function") or t)["name"] for t in tools)

        async def guarded(name: str, args: dict) -> str:
            if name not in allowed:
                return json.dumps({"ok": False, "error": f"{name} is not available here"})
            return await handler(name, args)
        tok, _ = self.home.agents.registry.open(self.main_tools(), guarded, FOREVER_S)
        rec = {"id": fid, "role": role, "task": request[:300], "who": who, "origin": origin,
               "state": "starting", "created": time.time(), "token": tok, "folded": False,
               "turn": (self.voice.get(role) or {}).get("turn") if origin == "voice" else None}
        self.forks[fid] = rec
        try:
            await self.send({"t": "fork", "fork": fid, "role": role,
                             "text": FORK_PROMPT.format(fid=fid, who=who, text=request),
                             "env": {roledir.TURN_TOKEN_ENV: tok}, "model": "",
                             "at": "last_completed", **({"repo": repo} if repo else {})})
        except Unavailable:
            self._close_fork(rec, "failed", error="no session host")
            raise
        return {"ok": True, "fork": fid, "note": "Side thread started; its result comes back "
                                                 "to you and is folded in."}

    def _close_fork(self, rec: dict, state: str, **extra) -> None:
        rec.update(state=state, finished=time.time(), **extra)
        self.home.agents.registry.close(rec.pop("token", "") or "")
        old = sorted((f for f in self.forks.values() if f.get("finished")),
                     key=lambda f: f["finished"])
        for f in old[:-20]:
            self.forks.pop(f["id"], None)

    async def _on_fork(self, msg: dict) -> None:
        rec = self.forks.get(str(msg.get("fork") or ""))
        if rec is None:
            return
        state = str(msg.get("state") or "")
        if state == "started":
            rec["state"] = "started"
            rec["session_id"] = msg.get("session_id") or rec.get("session_id")
            return
        if state not in ("done", "failed", "cancelled"):
            return
        text = str(msg.get("text") or "")
        parts = FOLD_MARK.split(text, maxsplit=1)
        summary = (parts[1] if len(parts) > 1 else text[-600:]).strip()[:1200]
        self._close_fork(rec, state, summary=summary, error=str(msg.get("error") or "")[:300],
                         duration_ms=msg.get("duration_ms"))
        if state != "done":
            if rec["origin"] == "voice":
                with contextlib.suppress(Unavailable):
                    await self.voice_say(rec["role"], f"The side thread on '{rec['task'][:80]}' "
                                         f"{'was cancelled' if state == 'cancelled' else 'failed'}.",
                                         rec.get("turn"))
            return
        if rec["origin"] == "voice":
            with contextlib.suppress(Unavailable):
                await self.voice_say(rec["role"], f"(side thread {rec['id']} finished: "
                                     f"{rec['task'][:120]}) {summary}", rec.get("turn"))
        if self.home.settings.get("fold_auto", True):
            self.fold(rec["id"])

    def fold(self, fork: str = "") -> dict:
        """Fold finished side threads into the main lane (as background notes
        its next turn takes in)."""
        picked = [f for f in self.forks.values() if f["state"] == "done" and not f["folded"]
                  and (not fork or f["id"] == fork)]
        if fork and not picked:
            rec = self.forks.get(fork)
            if rec is None:
                raise LookupError(f"no side thread {fork!r}")
            if rec["state"] in ("starting", "started"):
                return {"ok": True, "folded": [], "note": "still running; it folds in when done"}
            return {"ok": True, "folded": [], "note": f"already {rec['state']}"
                    + (" and folded" if rec["folded"] else "")}
        for f in picked:
            f["folded"] = True
            self.note(f"[fold from side thread {f['id']}: '{f['task'][:160]}'] {f['summary']}\n"
                      "Fold this into what you know. If it conflicts with something you decided "
                      "or are doing, settle it yourself when you can, otherwise ask the person "
                      "one line (ask_person).", via="fold", who=f"side thread {f['id']}",
                      role=f["role"])
        return {"ok": True, "folded": [f["id"] for f in picked]}

    async def btw(self, role: str, question: str) -> dict:
        question = question.strip()
        if not question:
            raise ValueError("btw needs the question")
        if not (self.state()["sessions"].get(role) or {}).get("main"):
            return {"ok": False, "error": "the back end has no conversation yet"}
        req = uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        self.pending[req] = fut
        t0 = time.monotonic()
        try:
            await self.send({"t": "btw", "req": req, "role": role, "question": question,
                             "model": str(self.home.settings.get("btw_model") or "haiku")})
            ev = await asyncio.wait_for(fut, BTW_TIMEOUT)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "no answer in time"}
        finally:
            self.pending.pop(req, None)
        out = {"ok": bool(ev.get("ok")), "answer": str(ev.get("text") or "")[:1500],
               "ms": ev.get("ms") or round((time.monotonic() - t0) * 1000)}
        if ev.get("error"):
            out["error"] = str(ev["error"])[:300]
        self._metric({"kind": "btw", "role": role, "at": time.time(), "ms": out["ms"],
                      "ok": out["ok"]})
        return out

    # -- status ---------------------------------------------------------------------------
    def status(self, role: str = HOUSE) -> dict:
        inbox = self.inbox(role)
        main = dict(self.lanes.get(f"{role}/main") or {"state": "offline"})
        voice = dict(self.lanes.get(f"{role}/voice") or {})
        item = self.current.get(role)
        name = "house agent" if role == HOUSE else role
        lane_state = main.get("state") or "offline"
        if self.link is None:
            lane_state = "offline"
        out = {"ok": True,
               "line": summary_line(name, lane_state, inbox, len(self.running_forks(role))),
               "main": {k: main.get(k) for k in ("state", "session_id", "context_tokens",
                                                  "last_compact", "reset") if main.get(k)},
               "paused": inbox.paused, "queue": inbox.counts(),
               "waiting": [i.brief() for i in inbox.queued()][:10],
               "forks": [{k: f.get(k) for k in ("id", "task", "state", "who", "folded")}
                         | {"age_s": round(time.time() - f["created"], 1)}
                         for f in self.forks.values() if f["role"] == role][-8:]}
        if item is not None:
            out["working_on"] = {**item.brief(), "last_tool": item.meta.get("last_tool"),
                                 "steers": len(item.meta.get("steers") or [])}
        if voice:
            out["voice"] = {k: voice.get(k) for k in ("state", "session_id", "context_tokens")
                            if voice.get(k)}
        return out

    def overview(self) -> dict:
        """For home.status and the Home agent page."""
        if not self.on() and not self.state()["roles"]:
            return {"on": False}
        link = self.link
        recent_voice = [m for m in self.metrics if m["kind"] == "voice" and m.get("first_ms")]
        recent_main = [m for m in self.metrics if m["kind"] == "main" and m.get("duration_ms")]
        return {"on": self.on(), "host": link.host if link else "",
                "connected": link is not None, "since": link.since if link else None,
                "roles": {r: self.status(r) for r in (self.roles() or [HOUSE])},
                "grantable": list((link.hello.get("servers") if link else None) or []),
                "resets": self.state()["resets"][-5:],
                "latency": {
                    "voice_first_ms": [m["first_ms"] for m in recent_voice[-20:]],
                    "main_turn_ms": [m["duration_ms"] for m in recent_main[-20:]]}}

    # -- role config (the house agent's role_config tool) ---------------------------------
    def role_config(self, action: str, args: dict, *, principal: Any, said: str,
                    asker: str) -> dict:
        """get | grant | revoke | set for a role's persistent sessions. A grant is
        a guarded trust-map change: the person's own request (or a proposal)."""
        role = str(args.get("role") or "").strip().lower()
        if role in ("", "me", "you", "house agent", self.home.agent_name):
            role = HOUSE
        if not ROLE_ID.match(role):
            raise ValueError("role: a role id (or house)")
        who = requester(principal, asker)
        trust = self._trust()
        if action == "get":
            rc = self.role_cfg(role)
            return {"ok": True, "role": role, **rc,
                    "granted": [g["server"] for g in (trust.grants(role) if trust else [])],
                    "grantable": list(((self.link.hello.get("servers") if self.link else None)
                                       or [])),
                    "status": self.status(role) if role in self.roles() else None}
        if action in ("grant", "revoke"):
            server = str(args.get("server") or "").strip()
            words = str(args.get("words") or said or "").strip()
            if trust is None:
                raise ValueError("this hub has no trust map")
            grantable = set(((self.link.hello.get("servers") if self.link else None) or [])) | \
                set((self.home.settings.get("mcp_catalog") or []))
            if action == "grant" and server not in grantable:
                raise ValueError(f"{server!r} is not an MCP server the session host can give "
                                 f"(it has: {', '.join(sorted(grantable)) or 'none'})")
            from ...trust import PERSON_REQUEST, Actor, TrustError
            op = "grant_tool" if action == "grant" else "revoke_tool"
            data = {"role": role, "server": server}
            if action == "grant" and who["kind"] != "person":
                # Not the person's own request: they decide on the Trust page.
                prop = trust.propose({"op": op, "data": data}, by=asker or "house agent",
                                     principal=who["id"],
                                     why=f"{who['label'] or asker} asked: {words[:300]}")
                return {"ok": True, "proposed": prop["id"],
                        "note": "Granting a tool is the person's call; it waits on the Trust "
                                "page."}
            try:
                out = trust.change(op, data, Actor("house", f"house agent for {who['label']}",
                                                   principal=who["id"], words=words,
                                                   via=PERSON_REQUEST))
            except TrustError as e:
                raise ValueError(str(e)) from None
            self._spawn(self.configure())
            return {"ok": True, "op": op, "role": role, "server": server,
                    "trust_version": out["version"],
                    "note": "Recorded on the trust map; the role's session reloads with it."}
        if action == "set":
            if who["kind"] != "person":
                raise ValueError("only the person changes a role's sessions")
            saved = self.state()["roles"].setdefault(role, {})
            for k in ("persistent", "voice"):
                if k in args and role != HOUSE:
                    saved[k] = bool(args[k])
            for k in ("model", "effort"):
                if k in args:
                    v = str(args[k] or "").strip()
                    if v and not re.match(r"^[A-Za-z0-9_.:\[\]-]{1,80}$", v):
                        raise ValueError(f"{k}: a model alias or id")
                    saved[k] = v
            if "idle_sleep_min" in args and role != HOUSE:
                saved["idle_sleep_min"] = max(0, min(int(args["idle_sleep_min"]), 1440))
            saved["set_by"] = {"who": who["label"], "at": time.time(), "words": said[:300]}
            self._save()
            self.home.journal("home.role_config", {"role": role, "set": {
                k: saved.get(k) for k in ("persistent", "voice", "model", "effort")}},
                {"ok": True})
            self._spawn(self.configure())
            return {"ok": True, "role": role, **self.role_cfg(role)}
        raise ValueError("action: get, grant, revoke or set")

    # -- direct asks to a persistent role (tests, the dashboard) --------------------------
    async def ask_role(self, role: str, text: str, timeout: float = 180.0) -> dict:
        """One turn on a persistent role's main lane with no hub tools."""
        if role not in self.roles():
            raise ValueError(f"{role} does not run persistently")
        item = Item(id=Inbox.new_id(), via="ask", priority=PERSON, text=text, who="operator",
                    waiter=asyncio.get_running_loop().create_future())
        self._ensure_pump(role)
        self.inbox(role).put(item)
        try:
            res = await asyncio.wait_for(asyncio.shield(item.waiter), timeout)
        except asyncio.TimeoutError:
            raise TurnFailed("no answer in time") from None
        except RuntimeError as e:
            raise TurnFailed(str(e)) from None
        return {"ok": True, "role": role, "answer": str(res.get("text") or ""),
                "duration_ms": res.get("duration_ms")}

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        for task in [*self._pumps.values(), *self._tasks]:
            task.cancel()
        await asyncio.gather(*self._pumps.values(), *self._tasks, return_exceptions=True)
        for ib in self.inboxes.values():
            ib.fail_all("the hub is stopping")
        if self.link is not None:
            with contextlib.suppress(Exception):
                await self.link.ws.close(code=1001)


def routes(get_sessions):
    """The session host's websocket, mounted by the MCP server."""
    from starlette.routing import WebSocketRoute

    async def endpoint(websocket):
        sess = get_sessions()
        if sess is None:
            await websocket.close(code=1013)
            return
        await sess.accept(websocket)

    return [WebSocketRoute(LINK_PATH, endpoint)]
