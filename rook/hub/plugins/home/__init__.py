"""``home.*``: the hub's home agent, an LLM that lives at the hub.

The operator assigns one OpenAI-compatible endpoint, a model, a persona and an
API key (a vault reference, ``{{secret:name}}``; never stored in plain text)
on the dashboard's Manage > Home agent page. The agent is then:

* **a chat participant.** It keeps itself present in the hub's chat rooms as
  ``agent:<name>`` (default ``agent:home``). A message that mentions it, that
  says ``@<name>`` in a room it is in, or that a person sends in a two-person
  room with it gets a reply built from the room's recent messages (agents
  always have to mention it; see ``tick`` for the loop and backlog limits). Rooms are
  watched in the hub's own loop; the model call runs as a background task with
  a timeout, so nothing blocks the hub.
* **askable by other agents.** ``home.ask`` on worker ``rook`` (and the MCP
  tool ``rook_home_ask`` on hubs where the home agent is enabled when the MCP
  server starts).
* **journaled as itself.** Its replies and any tool it uses are recorded in the
  call journal under its own identity (auth kind ``home``), never under the
  person or agent who asked.

Tools are off by default. With ``home.tools`` on it may search the shared
knowledge wiki (read-only). Job ``agent`` steps get a separate, opt-in tool
set that acts on behalf of the job (:mod:`.job_agent`,
:meth:`HomeAgent.work_job_step`); chat never sees it. See
docs/design/home-agent.md and docs/design/jobs.md 6.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import re
import time
from typing import Any

from ....core import context
from ....core.plugin import Plugin, capability, place, setting
from ....core.settings import SECRET_REF
from . import job_agent
from .llm import PROVIDERS, ChatClient, LLMError, aiohttp_request, base_url

log = logging.getLogger("rook.hub.plugins.home")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
KEY_REF = r"(\{\{secret:[a-z0-9][a-z0-9._-]{0,63}\}\})?"
#: Messages older than this are history, not something to answer now.
MAX_AGE_S = 600.0
#: Replies per room per minute (a loop breaker between chatty participants).
ROOM_RATE = 6
#: home.ask calls per caller per minute.
ASK_RATE = 10
#: Replies in a row to agents in one room before it waits for a person.
AGENT_STREAK = 3
#: Most messages read from one room per tick (older ones are history anyway).
READ_LIMIT = 200
#: How often the agent refreshes its chat presence.
TOUCH_S = 30.0
#: At most one "unavailable" note per room in this many seconds.
ERROR_NOTE_S = 600.0
#: At most one "busy" note per room in this many seconds.
BUSY_NOTE_S = 60.0
UNAVAILABLE = "The home agent is unavailable right now."
BUSY = "(Busy: I will answer shortly.)"
MAX_TOOL_ROUNDS = 3
TOOL_RESULT_CHARS = 4000

KNOWLEDGE_TOOL = {"type": "function", "function": {
    "name": "knowledge_search",
    "description": "Search the band's shared knowledge wiki. Returns up to 5 excerpts with slugs.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                   "required": ["query"]}}}


class HomeError(ValueError):
    pass


def _actor() -> str:
    ident = context.caller_identity.get()
    return str(ident) if ident else "system:rook-hub"


def _is_agent(identity: Any) -> bool:
    return str(identity or "").startswith("agent:")


def summarise(err: str) -> str:
    """A failure as one generic line (no hosts, URLs or secret names), for
    places readers below the operator see: ``home.status``, ``home.ask``."""
    e = str(err or "")
    m = re.search(r"HTTP (\d{3})", e)
    if m:
        return f"the model endpoint returned HTTP {m.group(1)}"
    if "did not answer" in e:
        return "the model endpoint timed out"
    if "cannot reach" in e:
        return "the model endpoint is unreachable"
    if "vault" in e or "secret" in e or "API key" in e:
        return "the API key is unavailable (check Manage > Home agent)"
    if "base URL" in e or "model" in e or "provider" in e:
        return "the home agent is not fully configured (Manage > Home agent)"
    return "the model call failed"


class HomeAgent(Plugin):
    NAMESPACE = "home"
    NAME = "home"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    SETTINGS = (
        setting("enabled", bool, False, group="Home agent", order=1, label="Enabled",
                help="On: the home agent answers in hub chat and to home.ask."),
        setting("name", str, "home", pattern=NAME_RE.pattern, group="Home agent", order=2,
                label="Name",
                help="Chat identity agent:<name>; people address it as @<name>."),
        setting("provider", str, "openai", choices=PROVIDERS, group="Model", order=1,
                label="Provider", help="openai: any OpenAI-compatible /v1 endpoint."),
        setting("base_url", "url", "", group="Model", order=2, label="Base URL",
                help="The endpoint's /v1 base, e.g. http://llm.example:1234/v1."),
        setting("model", str, "", group="Model", order=3, label="Model",
                help="A model id from the endpoint's /v1/models."),
        setting("api_key", str, "", pattern=KEY_REF, group="Model", order=4,
                label="API key (vault reference)",
                help="{{secret:<vault name>}}; blank when the endpoint needs no key. The key "
                     "itself stays in the vault."),
        setting("timeout_s", float, 90.0, min=5.0, max=600.0, group="Model", order=5,
                label="Request timeout (s)"),
        setting("max_tokens", int, 1024, min=16, max=32768, group="Model", order=6,
                label="Longest reply (tokens)"),
        setting("temperature", float, None, min=0.0, max=2.0, group="Model", order=7,
                advanced=True, label="Temperature", help="Blank: the endpoint's default."),
        setting("persona", str, "", group="Behaviour", order=1, label="Persona profile",
                help="A persona profile id. Blank: the persona assigned to family 'home' "
                     "(or the default one)."),
        setting("system_prompt", str, "", group="Behaviour", order=2,
                label="System prompt addition",
                help="Appended to the persona and the built-in instructions."),
        setting("context_messages", int, 20, min=1, max=100, group="Behaviour", order=3,
                label="Room messages sent as context"),
        setting("tools", bool, False, group="Behaviour", order=4,
                label="Read-only tools (knowledge search)",
                help="Lets the model search the knowledge wiki. Each search is journaled "
                     "as the home agent."),
        setting("job_steps", bool, True, group="Jobs", order=1, label="Work job agent steps",
                help="Job steps with agent \"home\" may run the home agent with a tool set that "
                     "acts on behalf of the job (never more than the job may do). Chat never "
                     "gets these tools."),
        setting("job_max_tool_calls", int, job_agent.DEFAULT_MAX_CALLS, min=1,
                max=job_agent.MAX_CALLS, group="Jobs", order=2, label="Tool calls per job step",
                help="A job step's max_tool_calls overrides it (up to the same limit)."),
    )
    GUIDANCE = {"home.ask": "The home agent is a local model: give it the context it needs "
                            "in the question; it cannot see your conversation."}
    SKILL = ("### home agent\n"
             "The hub's own LLM (when the operator has set one up). Ask it with "
             "`rook_call(worker=\"rook\", cap=\"home.ask\", args={\"question\": \"...\"})` "
             "(or `rook_home_ask`); `home.status` says whether it is on. People reach it in "
             "chat as `@home` (or its configured name).\n")

    POLL_S = 1.5

    def __init__(self) -> None:
        super().__init__()
        self._node: Any = None
        self._chat: Any = None
        self._task: asyncio.Task | None = None
        self._replies: set[asyncio.Task] = set()
        self._busy: set[str] = set()
        self._sem = asyncio.Semaphore(2)
        self._ask_sem = asyncio.Semaphore(2)
        self._job_sem = asyncio.Semaphore(2)
        self._cursor: dict[str, int] = {}
        self._baseline = False
        self._rate: dict[str, collections.deque] = {}
        self._ask_rate: dict[str, collections.deque] = {}
        self._streak: dict[str, int] = {}               # replies in a row to agents, per room
        self._noted: dict[tuple[str, str], float] = {}  # (room, kind) -> last note posted
        self._touched = 0.0
        self.activity: collections.deque = collections.deque(maxlen=50)
        self.last_error = ""
        self._http = aiohttp_request          # tests swap in a fake endpoint

    # -- wiring ------------------------------------------------------------
    def bind_host(self, node) -> None:
        self._node = node

    def _store(self):
        if self._chat is None and self._node is not None:
            store = getattr(self._node, "chat", None)
            if store is None:
                rooms = self._node.plugin("chat")
                store = getattr(rooms, "_store", None)
            self._chat = store
        return self._chat if getattr(self._chat, "enabled", False) else None

    async def start(self) -> None:
        self._load_cursor()
        self._task = asyncio.get_running_loop().create_task(self._watch())

    async def stop(self) -> None:
        tasks = [t for t in [self._task, *self._replies] if t is not None and not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None

    # -- configuration -----------------------------------------------------
    @property
    def agent_name(self) -> str:
        n = str(self.settings.get("name") or "home").strip().lower()
        return n if NAME_RE.match(n) else "home"

    @property
    def identity(self) -> str:
        return f"agent:{self.agent_name}"

    def enabled(self) -> bool:
        return bool(self.settings.get("enabled"))

    def config(self, overrides: dict | None = None) -> dict:
        cfg = {s.name: self.settings.get(s.name) for s in self.SETTINGS}
        for k, v in (overrides or {}).items():
            if k in cfg:
                cfg[k] = v
        return cfg

    def missing(self, cfg: dict | None = None) -> list[str]:
        cfg = cfg or self.config()
        return [k for k in ("base_url", "model") if not str(cfg.get(k) or "").strip()]

    def _api_key(self, ref: str | None, actor: str | None = None,
                 via: str = "home agent") -> str | None:
        ref = (ref or "").strip()
        if not ref:
            return None
        m = SECRET_REF.match(ref)
        if not m:
            raise HomeError("the API key must be a vault reference: {{secret:<name>}}")
        vault = getattr(self._node, "_vault", None)
        if vault is None:
            raise HomeError("the vault is unavailable on this hub")
        try:
            return vault.get(m.group(1), actor or self.identity, via=via)
        except KeyError:
            raise HomeError(f"no vault secret named {m.group(1)!r}") from None

    def client(self, cfg: dict | None = None, *, need_model: bool = True,
               key_actor: str | None = None, key_via: str = "home agent") -> ChatClient:
        cfg = cfg or self.config()
        if cfg.get("provider", "openai") not in PROVIDERS:
            raise HomeError(f"provider must be one of {list(PROVIDERS)}")
        if not str(cfg.get("base_url") or "").strip():
            raise HomeError("set the home agent's base URL first")
        if need_model and not str(cfg.get("model") or "").strip():
            raise HomeError("pick the home agent's model first")
        try:
            return ChatClient(str(cfg["base_url"]), str(cfg.get("model") or ""),
                              self._api_key(cfg.get("api_key"), key_actor, key_via),
                              float(cfg.get("timeout_s") or 90.0), self._http)
        except LLMError as e:
            raise HomeError(str(e)) from None

    def persona_text(self, cfg: dict | None = None) -> str:
        cfg = cfg or self.config()
        plugin = self._node.plugin("persona") if self._node is not None else None
        if plugin is None:
            return ""
        from ..persona import model as pmodel
        try:
            pid = str(cfg.get("persona") or "").strip()
            doc = plugin.store.profile(pid) if pid else plugin.resolve((), "home")[0]
            return pmodel.render(doc, "home") if doc else ""
        except Exception:
            log.exception("home agent: persona unavailable")
            return ""

    def system_prompt(self, cfg: dict, where: str) -> str:
        parts = [self.persona_text(cfg),
                 f"You are {self.agent_name}, the home agent on this Rook hub (chat identity "
                 f"{self.identity}). {where} Answer plainly and briefly; say when you do not "
                 "know. You have no tools unless they are listed to you."]
        extra = str(cfg.get("system_prompt") or "").strip()
        if extra:
            parts.append(extra)
        return "\n\n".join(p for p in parts if p)

    # -- the model, with optional read-only tools ---------------------------
    async def complete(self, messages: list[dict], cfg: dict, *, thread: str | None = None,
                       via: str = "", may_search: bool = True,
                       cli: ChatClient | None = None) -> dict:
        """Run one turn (and up to MAX_TOOL_ROUNDS tool rounds). Returns
        ``{content, model, latency_ms, tools_used}`` with the API key scrubbed.
        ``may_search`` is False when whoever asked may not read the knowledge
        wiki themselves; the tool is then not offered."""
        cli = cli or self.client(cfg)
        tools = ([KNOWLEDGE_TOOL] if cfg.get("tools") and may_search and self._has_knowledge()
                 else None)
        used: list[str] = []
        total = 0
        msgs = list(messages)
        for _round in range(MAX_TOOL_ROUNDS + 1):
            res = await cli.chat(msgs, max_tokens=cfg.get("max_tokens"),
                                 temperature=cfg.get("temperature"),
                                 tools=tools if _round < MAX_TOOL_ROUNDS else None)
            total += res["latency_ms"]
            if not res["tool_calls"] or not tools:
                return {"content": cli._scrub(res["content"] or ""),
                        "model": cli._scrub(res["model"] or ""),
                        "latency_ms": total, "tools_used": used}
            msgs.append({"role": "assistant", "content": res["content"] or None,
                         "tool_calls": res["tool_calls"]})
            for call in res["tool_calls"]:
                fn = (call.get("function") or {}) if isinstance(call, dict) else {}
                out = await self._run_tool(str(fn.get("name") or ""), fn.get("arguments"), thread)
                used.append(str(fn.get("name") or "?"))
                msgs.append({"role": "tool", "tool_call_id": str(call.get("id") or ""),
                             "content": out})
        raise LLMError("the model kept calling tools")  # pragma: no cover - loop bound

    def _has_knowledge(self) -> bool:
        return self._node is not None and self._node.plugin("knowledge") is not None

    def may_read_knowledge(self, principal: Any) -> bool:
        """May ``principal`` call ``knowledge.read`` on the hub under the
        current policy? The tool runs as the home agent, so this keeps it from
        reading for someone who may not read themselves. ``deny`` and
        ``would_deny`` both count as no. With no permission layer (plain hubs,
        tests) nothing is enforced anywhere, so yes."""
        authz = getattr(getattr(self._node, "client", None), "authz", None)
        if authz is None or principal is None:
            return True
        try:
            from ...authz import target_from_entry
            try:
                entry = self._node.entry()
            except Exception:  # noqa: BLE001
                entry = {"name": "rook", "roles": ["is_hub"]}
            d = authz.store.current().evaluate(
                [principal], "knowledge.read",
                target_from_entry(getattr(self._node, "worker_id", None), entry, local=True))
        except Exception:  # noqa: BLE001 - when unsure, do not offer the tool
            log.exception("home agent: knowledge.read policy check failed")
            return False
        return d.decision in ("allow", "off")

    @staticmethod
    def chat_principal(sender: str) -> Any:
        """A policy principal for a chat sender. Chat identities are display
        strings, not credentials, so this applies the policy's defaults for the
        sender's kind: ``agent:*`` as an agent token, ``user:*`` / ``human:*``
        as a band member, anything else as unverified."""
        from ...policy import Principal
        s = str(sender or "")
        if s.startswith("agent:"):
            return Principal(f"token:{s[6:]}", "token", "agent", label=s)
        if s.startswith(("user:", "human:")):
            return Principal("human:" + s.split(":", 1)[1], "human", "member",
                             ("human:member",), label=s)
        return Principal("unverified", "unverified", verified=False, label=s)

    @staticmethod
    def ask_principal() -> Any:
        """The verified principal of the current call, or None (in-process
        hub code without one)."""
        try:
            from ...authz import current_principal
        except Exception:  # noqa: BLE001
            return None
        return current_principal.get()

    @contextlib.contextmanager
    def _as_self(self):
        """Run in-process caps as the home agent: the knowledge plugin and the
        journal attribute to this identity, never to whoever asked."""
        tokens = [(context.caller_identity, context.caller_identity.set(self.identity))]
        try:
            from ....band_mcp import attribution
            att = attribution.Attribution(identity=self.identity, kind="home", label=self.agent_name,
                                          agent_id=f"home:{self.agent_name}",
                                          actor=f"home.{self.agent_name}.hub")
            tokens.append((attribution.current, attribution.current.set(att)))
        except Exception:  # noqa: BLE001 - the bridge is optional (tests, plain hubs)
            pass
        try:
            yield
        finally:
            for var, tok in reversed(tokens):
                var.reset(tok)

    def audit(self) -> dict:
        return {"kind": "home", "agent_id": f"home:{self.agent_name}", "actor": f"home.{self.agent_name}.hub"}

    def journal(self, cap: str, args: dict, reply: dict, thread: str | None = None) -> None:
        j = getattr(self._node, "journal", None)
        if j is None:
            return
        try:
            j.record(cap=cap, worker="rook", identity=self.identity, args=args, reply=reply,
                     thread_id=thread, audit=self.audit())
        except Exception:
            log.exception("home agent: journaling %s failed", cap)

    async def _run_tool(self, name: str, raw_args: Any, thread: str | None) -> str:
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
        except (ValueError, TypeError):
            args = {}
        if name != "knowledge_search":
            return json.dumps({"error": f"unknown tool {name!r}"})
        cap_args = {"action": "search", "query": str(args.get("query") or "")[:300]}
        with self._as_self():
            try:
                result = await self._node.invoke("knowledge.read", cap_args, self.identity)
                reply = {"ok": True, "result": result}
            except Exception as e:  # noqa: BLE001 - a tool error goes back to the model
                reply = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        self.journal("knowledge.read", cap_args, reply, thread)
        return json.dumps(reply.get("result") if reply["ok"] else {"error": reply["error"]},
                          default=str)[:TOOL_RESULT_CHARS]

    def _note(self, via: str, ok: bool, **extra) -> None:
        """Activity for the operator's page keeps the detail; ``last_error``
        (read-tier ``home.status``) keeps only a summary."""
        row = {"ts": time.time(), "via": via, "ok": ok, **extra}
        if not ok:
            self.last_error = summarise(str(extra.get("error") or ""))
        self.activity.appendleft(row)

    def _once(self, rid: str, kind: str, every: float) -> bool:
        """True at most once per ``every`` seconds per (room, kind)."""
        now = time.monotonic()
        last = self._noted.get((rid, kind))
        if last is not None and now - last < every:
            return False
        self._noted[(rid, kind)] = now
        return True

    # -- chat ----------------------------------------------------------------
    def _cursor_path(self):
        return self.data_dir / "cursor.json"

    def _load_cursor(self) -> None:
        try:
            data = json.loads(self._cursor_path().read_text(encoding="utf-8"))
            self._cursor = {str(k): int(v) for k, v in (data.get("rooms") or {}).items()}
            self._baseline = bool(data.get("baseline"))
        except (OSError, ValueError, AttributeError):
            self._cursor, self._baseline = {}, False

    def _save_cursor(self) -> None:
        try:
            self._cursor_path().write_text(json.dumps({"baseline": self._baseline,
                                                       "rooms": self._cursor}), encoding="utf-8")
        except OSError:
            log.debug("home agent: cursor not saved", exc_info=True)

    def _take_baseline(self, store) -> None:
        """First time on: everything already said is history."""
        for r in store.room_heads(None):
            self._cursor[r["room"]] = r["last_seq"]
        self._baseline = True
        self._save_cursor()

    async def _watch(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("home agent: chat watch failed")
            await asyncio.sleep(self.POLL_S)

    def addressed(self, msg: dict, participants: list[str]) -> bool:
        """Mentioned (metadata, or ``@name`` in a room it is in), or a person
        writing in a 1:1 room with it. Agents always have to mention it, so two
        bots in a 1:1 room do not answer each other forever."""
        if msg.get("sender") == self.identity:
            return False
        if self.identity in (msg.get("mentions") or []):
            return True
        if self.identity not in participants:
            return False
        if len(participants) == 2 and not _is_agent(msg.get("sender")):
            return True
        return re.search(rf"(?<![\w@])@{re.escape(self.agent_name)}\b", msg.get("text") or "",
                         re.I) is not None

    async def tick(self) -> list[asyncio.Task]:
        """One pass over the rooms: spawn a reply task per room where someone
        addressed the agent since the last pass. Returns the tasks started.

        A trigger that cannot be answered yet (a reply already running in the
        room, or the room's rate limit) stays pending: the cursor stops just
        before it and the next pass tries again, until it is older than
        MAX_AGE_S. Rooms are found with one query (``room_heads``); only rooms
        with something past the cursor are read."""
        store = self._store()
        if store is None or not self.enabled() or self.missing():
            return []
        mono = time.monotonic()
        if mono - self._touched >= TOUCH_S:
            store.touch(self.identity)
            self._touched = mono
        if not self._baseline:
            self._take_baseline(store)
        started: list[asyncio.Task] = []
        dirty = False
        for head in store.room_heads(self.identity):
            rid, last = head["room"], head["last_seq"]
            if rid not in self._cursor:
                self._cursor[rid] = 0
                dirty = True
            cur = self._cursor[rid]
            if last <= cur:
                continue
            if last - cur > READ_LIMIT:           # only the newest can be answered anyway
                cur = last - READ_LIMIT
            got = store.read(rid, self.identity, since_seq=cur, mark=True, limit=READ_LIMIT)
            if not got.get("ok"):
                continue
            msgs = got.get("messages") or []
            participants = got.get("participants") or []
            new_cursor = int(got["last_seq"]) if msgs else last
            now = time.time()
            trigger = None
            for m in msgs:
                if m.get("sender") != self.identity and not _is_agent(m.get("sender")):
                    self._streak[rid] = 0          # a person spoke: agents may go again
                if now - float(m.get("ts") or 0) <= MAX_AGE_S and \
                        self.addressed(m, participants):
                    trigger = m
            if trigger is not None and _is_agent(trigger.get("sender")) and \
                    self._streak.get(rid, 0) >= AGENT_STREAK:
                log.info("home agent: %s: %d replies in a row to agents; waiting for a person",
                         rid, self._streak[rid])
                trigger = None
            pending = False
            if trigger is not None:
                busy = rid in self._busy
                if busy or not self._room_allows(rid):
                    pending = True
                    new_cursor = max(cur, int(trigger["seq"]) - 1)   # retried next pass
                    if not busy and self._once(rid, "busy", BUSY_NOTE_S):
                        store.send(rid, self.identity, BUSY, [], False)
            if new_cursor != self._cursor[rid]:
                self._cursor[rid] = new_cursor
                dirty = True
            if trigger is None or pending:
                continue
            if _is_agent(trigger.get("sender")):
                self._streak[rid] = self._streak.get(rid, 0) + 1
            self._busy.add(rid)
            task = asyncio.get_running_loop().create_task(
                self._reply(rid, got.get("title") or rid, participants, trigger))
            self._replies.add(task)
            task.add_done_callback(self._replies.discard)
            started.append(task)
        if dirty:
            self._save_cursor()
        return started

    @staticmethod
    def _allow(buckets: dict, key: str, limit: int, window: float = 60.0) -> bool:
        q = buckets.setdefault(key, collections.deque())
        now = time.monotonic()
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def _room_allows(self, rid: str) -> bool:
        return self._allow(self._rate, rid, ROOM_RATE)

    def room_messages(self, store, rid: str, cfg: dict) -> list[dict]:
        out = []
        for m in store.tail(rid, int(cfg.get("context_messages") or 20)):
            if m["sender"] == self.identity:
                if m["text"] in (BUSY, UNAVAILABLE):      # status notes, not answers
                    continue
                out.append({"role": "assistant", "content": m["text"]})
            else:
                out.append({"role": "user", "content": f"[{m['sender']}] {m['text']}"})
        return out

    async def _reply(self, rid: str, title: str, participants: list[str], trigger: dict) -> None:
        store = self._store()
        cfg = self.config()
        t0 = time.monotonic()
        try:
            async with self._sem:
                others = ", ".join(p for p in participants if p != self.identity) or "nobody"
                where = (f"You are in the chat room \"{title}\" with {others}. Each user message "
                         f"is prefixed with its sender. Reply to the latest message from "
                         f"{trigger['sender']}.")
                msgs = [{"role": "system", "content": self.system_prompt(cfg, where)}]
                msgs += self.room_messages(store, rid, cfg)
                res = await self.complete(
                    msgs, cfg, thread=rid, via="chat",
                    may_search=self.may_read_knowledge(self.chat_principal(trigger["sender"])))
            text = res["content"] or "(no answer)"
            # Address the person who asked in a group room; never @mention an
            # agent (a mention is what makes the other bot answer back).
            ments = ([trigger["sender"]] if len(participants) > 2
                     and not _is_agent(trigger["sender"]) else [])
            store.send(rid, self.identity, text, ments, False)
            self._note("chat", True, room=title, sender=trigger["sender"], model=res["model"],
                       latency_ms=res["latency_ms"], tools=res["tools_used"])
            self.journal("home.reply", {"room": rid, "to": trigger["sender"],
                                        "seq": trigger["seq"]},
                         {"ok": True, "result": {"model": res["model"], "chars": len(text),
                                                 "latency_ms": res["latency_ms"],
                                                 "tools": res["tools_used"]}}, rid)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - say so in the room instead of going silent
            err = str(e) if isinstance(e, (LLMError, HomeError)) else f"{type(e).__name__}: {e}"
            log.warning("home agent: reply in %s failed: %s", rid, err)
            # The room gets a generic line (at most one per room per
            # ERROR_NOTE_S); the detail stays in the log, activity and journal.
            if store is not None and self._once(rid, "error", ERROR_NOTE_S):
                store.send(rid, self.identity, UNAVAILABLE, [], False)
            self._note("chat", False, room=title, sender=trigger.get("sender"), error=err[:300],
                       latency_ms=round((time.monotonic() - t0) * 1000))
            self.journal("home.reply", {"room": rid, "to": trigger.get("sender")},
                         {"ok": False, "error": err[:300]}, rid)
        finally:
            self._busy.discard(rid)

    # -- job steps (docs/design/jobs.md 6) ------------------------------------
    def job_unready(self) -> str:
        """Why a job agent step cannot use the home agent now ("" when it can)."""
        if not self.enabled():
            return "the home agent is off (Manage > Home agent)"
        if not self.settings.get("job_steps", True):
            return "the home agent does not work job steps (setting home.job_steps)"
        missing = self.missing()
        if missing:
            return f"the home agent is not configured ({', '.join(missing)})"
        return ""

    async def work_job_step(self, prompt: str, context: Any, backend: "job_agent.JobBackend", *,
                            model: str | None = None, max_calls: int | None = None,
                            job: str = "", run: str = "", step: str = "") -> dict:
        """Work one job step with the job tool set. ``backend`` (from the jobs
        plugin) carries out every tool call on behalf of the job. Returns
        ``{verdict, text, calls, tools_used, model, rounds}``; raises
        HomeError/LLMError when the model cannot be used. The caller bounds the
        time; tool calls are bounded by ``max_calls`` (default the setting)."""
        why = self.job_unready()
        if why:
            raise HomeError(why)
        cfg = self.config({"model": model} if model else None)
        cfg["tools"] = False                      # chat's knowledge tool stays out of it
        limit = int(max_calls or self.settings.get("job_max_tool_calls")
                    or job_agent.DEFAULT_MAX_CALLS)
        msgs = [{"role": "system", "content": self.system_prompt(cfg, job_agent.WHERE)},
                {"role": "user", "content": job_agent.first_message(prompt, context)[:40000]}]
        args = {"job": job, "run": run, "step": step}
        try:
            async with self._job_sem:
                cli = self.client(cfg)
                out = await job_agent.run_loop(cli, msgs, backend, max_calls=limit,
                                               max_tokens=cfg.get("max_tokens"),
                                               temperature=cfg.get("temperature"))
        except (LLMError, HomeError) as e:
            self._note("job", False, sender=f"job:{job}", error=str(e)[:300])
            self.journal("home.job_step", args, {"ok": False, "error": str(e)[:300]}, run or None)
            raise
        self._note("job", True, sender=f"job:{job}", model=out["model"], verdict=out["verdict"],
                   tools=out["tools_used"])
        self.journal("home.job_step", args,
                     {"ok": True, "result": {"verdict": out["verdict"], "calls": out["calls"],
                                             "tools": out["tools_used"], "model": out["model"]}},
                     run or None)
        return out

    # -- caps ----------------------------------------------------------------
    @capability("ask", risk="write")
    async def ask(self, question: str, context: str = "") -> dict:
        """Ask the hub's home agent (its own LLM) a question; returns its answer.

        ``context`` is extra text it should consider (it cannot see your
        conversation). Errors when the home agent is off or not configured, and
        when one caller asks more than ASK_RATE times a minute."""
        if not self.enabled():
            raise HomeError("the home agent is off (Manage > Home agent)")
        question = str(question or "").strip()
        if not question:
            raise HomeError("ask needs a question")
        cfg = self.config()
        who = _actor()
        principal = self.ask_principal()
        if not self._allow(self._ask_rate, principal.id if principal is not None else who,
                           ASK_RATE):
            raise HomeError(f"rate limited: at most {ASK_RATE} home.ask calls a minute; "
                            "try again shortly")
        user = f"[{who}] {question}" + (f"\n\nContext:\n{context.strip()}" if context else "")
        msgs = [{"role": "system", "content": self.system_prompt(
                    cfg, "Another agent or a person is asking you directly, not in a chat room.")},
                {"role": "user", "content": user[:20000]}]
        try:
            async with self._ask_sem:
                res = await self.complete(msgs, cfg, via="ask",
                                          may_search=self.may_read_knowledge(principal))
        except (LLMError, HomeError) as e:
            self._note("ask", False, sender=who, error=str(e)[:300])
            raise HomeError(summarise(str(e))) from None
        self._note("ask", True, sender=who, model=res["model"], latency_ms=res["latency_ms"],
                   tools=res["tools_used"])
        return {"agent": self.identity, "answer": res["content"], "model": res["model"],
                "latency_ms": res["latency_ms"], "tools_used": res["tools_used"]}

    @capability("status", risk="read")
    def status(self) -> dict:
        """Whether the home agent is on, its identity, endpoint host and model,
        and the last error (never the key)."""
        cfg = self.config()
        host = ""
        with contextlib.suppress(Exception):
            from urllib.parse import urlparse
            host = urlparse(str(cfg.get("base_url") or "")).netloc
        return {"enabled": self.enabled(), "identity": self.identity, "name": self.agent_name,
                "provider": cfg.get("provider"), "endpoint_host": host,
                "model": cfg.get("model") or "", "missing": self.missing(cfg),
                "tools": bool(cfg.get("tools")), "key_set": bool(cfg.get("api_key")),
                "chat": self._store() is not None, "last_error": self.last_error}

    # -- MCP -----------------------------------------------------------------
    def mcp_tools(self, invoke):
        """``rook_home_ask``, only on hubs where the home agent is enabled when
        the MCP server starts (every connect pays for tools/list)."""
        if not self.enabled():
            return []

        async def rook_home_ask(question: str, context: str = "") -> str:
            """Ask the hub's home agent (a local LLM) a question. Give it the context it needs."""
            try:
                return json.dumps(await invoke("home.ask", {"question": question,
                                                            "context": context}))
            except Exception as e:  # noqa: BLE001
                return json.dumps({"ok": False, "error": str(e)})
        return [rook_home_ask]

    # -- the Manage > Home agent page (operator account; via settings_web) ----
    def page(self, svc) -> dict:
        rows = {}
        for s in self.SETTINGS:
            r = svc.resolve(f"home.{s.name}")
            rows[s.name] = {"value": r.get("value"), "source": r.get("source"),
                            "locked": bool(r.get("locked")), "env": r.get("env"),
                            "label": s.label, "help": s.help}
        personas = []
        p = self._node.plugin("persona") if self._node is not None else None
        if p is not None:
            with contextlib.suppress(Exception):
                personas = [{"id": x["id"], "name": x.get("name") or x["id"]}
                            for x in p.store.profiles()]
        secrets = []
        vault = getattr(self._node, "_vault", None)
        if vault is not None:
            with contextlib.suppress(Exception):
                secrets = [s["name"] for s in vault.list()]
        return {"view": "home", "settings": rows, "status": self.status(),
                "personas": personas, "secrets": secrets, "activity": list(self.activity),
                "providers": list(PROVIDERS),
                "history": svc.store.history(key="home.", limit=30)}

    def _form(self, data: dict) -> dict:
        values = data.get("values") or {}
        if not isinstance(values, dict):
            raise HomeError("values must be an object")
        names = {s.name for s in self.SETTINGS}
        return {k: v for k, v in values.items() if k in names}

    async def page_action(self, svc, data: dict, actor: str) -> dict:
        action = data.get("action")
        if action == "home_save":
            values = self._form(data)
            plan = []
            for name, value in values.items():
                key = f"home.{name}"
                row = svc.store.get(key, "hub")
                if value is None or (value == "" and name == "temperature"):
                    if row is not None:
                        plan.append(("reset", key, None))
                    continue
                svc.set(key, value, actor=actor, source="ui", dry_run=True)  # validate all first
                if row is None or row.get("value") != svc.schema.get(key).setting.coerce(value):
                    plan.append(("set", key, value))
            saved = []
            for op, key, value in plan:
                if op == "reset":
                    svc.reset(key, actor=actor, source="ui", note="Home agent page")
                else:
                    svc.set(key, value, actor=actor, source="ui", note="Home agent page")
                saved.append(key)
            return {"ok": True, "saved": saved, **self.page(svc)}
        if action not in ("home_models", "home_test"):
            raise HomeError("use home_save, home_models or home_test")
        cfg, note = self._probe_config(self._form(data))
        extra = {"note": note} if note else {}
        try:
            cli = self.client(cfg, need_model=action == "home_test", key_actor=actor,
                              key_via="home agent page")
        except (LLMError, HomeError) as e:
            return {"ok": False, "error": str(e), **extra}
        if action == "home_models":
            try:
                models = await cli.models(timeout=min(float(cfg.get("timeout_s") or 20), 20.0))
            except (LLMError, HomeError) as e:
                return {"ok": False, "error": str(e), **extra}
            return {"ok": True, "models": [cli._scrub(m) for m in models], **extra}
        cfg["tools"] = False
        cfg["timeout_s"] = min(float(cfg.get("timeout_s") or 25), 25.0)
        cli.timeout = cfg["timeout_s"]
        msgs = [{"role": "system", "content": self.system_prompt(
                    cfg, "This is a connection test from the hub's settings page.")},
                {"role": "user", "content": "Say hello in one short sentence."}]
        try:
            res = await self.complete(msgs, cfg, via="test", cli=cli)
        except (LLMError, HomeError) as e:
            self._note("test", False, sender=actor, error=str(e)[:300])
            return {"ok": False, "error": str(e), **extra}
        self._note("test", True, sender=actor, model=res["model"],
                   latency_ms=res["latency_ms"])
        return {"ok": True, "reply": res["content"], "model": res["model"],
                "latency_ms": res["latency_ms"], **extra}

    def _probe_config(self, form: dict) -> tuple[dict, str]:
        """The settings for a models/test probe from unsaved form values, with
        the API key decided from the SAVED settings only: a vault reference
        typed into the form is never resolved (that would let the page send
        any vault secret anywhere), and the saved key goes only to the saved
        base URL. Returns ``(cfg, note)``; the note says why no key or the
        saved key was used."""
        saved = self.config()
        cfg = self.config(form)
        saved_ref = str(saved.get("api_key") or "").strip()
        form_ref = str(form.get("api_key", saved_ref) or "").strip()
        same_url = (base_url(str(cfg.get("base_url") or ""))
                    == base_url(str(saved.get("base_url") or "")))
        note, key = "", ""
        if not form_ref:
            pass                                       # testing without a key
        elif not saved_ref:
            note = "Save the API key first; probes only use a saved key."
        elif not same_url:
            note = ("The saved API key is only sent to the saved base URL; save the new URL "
                    "first to use the key with it.")
        else:
            key = saved_ref
            if form_ref != saved_ref:
                note = "Used the saved API key; save to try the new one."
        cfg["api_key"] = key
        return cfg, note


PLUGIN = HomeAgent
