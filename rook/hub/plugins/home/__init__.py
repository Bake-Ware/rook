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
knowledge wiki (read-only). With ``home.memory`` on (the default) and the
memory plugin loaded it remembers: before answering it reads what it knows
about the person and what is relevant to the message (``memory.context``),
after answering it keeps what it learned (``memory.observe``), and it gets
two memory tools (save, search). See docs/design/memory.md. Job ``agent`` steps get a separate, opt-in tool
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
from ....core.db import atomic_write_json
from ....core.plugin import Plugin, capability, place, setting
from ....core.settings import SECRET_REF
from . import agent_host, job_agent
from .agent_host import AgentHostBackend, TurnFailed, Unavailable
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


BACKENDS = ("openai", "claude-code", "codex", "opencode")


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
        setting("backend", str, "openai", choices=BACKENDS, group="Model", order=-1,
                label="Backend",
                help="openai: an OpenAI-compatible endpoint (below). claude-code, codex, "
                     "opencode: that agent harness on an agent host (a worker where it is "
                     "installed and signed in); see the Agent host group."),
        setting("llm_provider", str, "", pattern=r"^([a-z0-9][a-z0-9_-]{0,31})?$", group="Model",
                order=0, label="Model provider",
                help="A provider from Manage > Model providers (llmanifold, or one added there). "
                     "Set: its address and key are used, and the base URL and API key below "
                     "are not. Blank: the endpoint below."),
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
        setting("agent_host", str, "", group="Agent host", order=1, label="Agent host",
                help="The worker that runs the home agent's turns with its Claude Code, Codex or "
                     "OpenCode (name or id). Change it to move the agent; nothing is lost "
                     "(memory stays at the hub)."),
        setting("agent_fallback_hosts", list, [], group="Agent host", order=2,
                label="Fallback hosts",
                help="Tried in order when the agent host is offline, lacks the harness or is "
                     "not logged in."),
        setting("agent_on_unavailable", str, "fallback_backend",
                choices=agent_host.ON_UNAVAILABLE, group="Agent host", order=3,
                label="When no host is available",
                help="fallback_backend: answer with the OpenAI-compatible endpoint when one is "
                     "set. fail: say the agent is unavailable."),
        setting("agent_timeout_s", float, 180.0, min=10.0, max=1800.0, group="Agent host",
                order=4, label="Turn timeout (s)"),
        setting("agent_callback_url", "url", "", group="Agent host", order=5, advanced=True,
                label="Hub URL for tools",
                help="Where the agent host reaches the hub's per-turn tool server. Blank: the "
                     "hub's public MCP URL."),
        setting("claude_model", str, "haiku", pattern=r"^([A-Za-z0-9_.:\[\]-]{1,80})?$",
                group="Agent host", order=10, label="Claude Code model",
                help="haiku, sonnet or opus (the current model of that family), or a full "
                     "model id."),
        setting("claude_effort", str, "", choices=agent_host.EFFORTS, group="Agent host",
                order=11, label="Claude Code effort",
                help="Blank: Claude Code's default for the model."),
        setting("claude_auth", str, "login", choices=agent_host.AUTH, group="Agent host",
                order=12, label="Claude Code sign-in",
                help="login: the host's own claude login (its subscription). token: a long-lived "
                     "token from `claude setup-token`, kept in the vault."),
        setting("claude_token", str, "", pattern=KEY_REF, group="Agent host", order=13,
                label="Claude Code token (vault reference)",
                help="{{secret:<vault name>}} holding a `claude setup-token` token; used when "
                     "sign-in is token."),
        setting("codex_model", str, "", pattern=r"^([A-Za-z0-9_.:-]{1,80})?$",
                group="Agent host", order=20, label="Codex model",
                help="Blank: Codex's default. List models asks the host's Codex."),
        setting("codex_effort", str, "", choices=agent_host.EFFORTS, group="Agent host",
                order=21, label="Codex reasoning effort", help="Blank: the model's default."),
        setting("opencode_model", str, "", pattern=r"^([A-Za-z0-9_.:/-]{1,120})?$",
                group="Agent host", order=30, label="OpenCode model",
                help="provider/model from the host's OpenCode providers. Blank: its default "
                     "model. OpenCode's free tier refuses custom agents; use your own "
                     "provider."),
        setting("persona", str, "", group="Behaviour", order=1, label="Persona profile",
                help="A persona profile id. Blank: the persona assigned to family 'home' "
                     "(or the default one)."),
        setting("system_prompt", str, "", group="Behaviour", order=2,
                label="System prompt addition",
                help="Appended to the persona and the built-in instructions."),
        setting("context_messages", int, 20, min=1, max=100, group="Behaviour", order=3,
                label="Room messages sent as context"),
        setting("tools", bool, False, group="Behaviour", order=4,
                label="Tools (knowledge search; memory save and search)",
                help="Lets the model search the knowledge wiki and, when memory is on, save and "
                     "search its memory. Each call is journaled as the home agent. Leave off "
                     "for endpoints that do not support tool calls."),
        setting("memory", bool, True, group="Behaviour", order=5, label="Use memory",
                help="Remember people, facts, conversations and skills between conversations "
                     "(Manage > Memory). Off: it neither reads nor writes memory."),
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
        self.agents = AgentHostBackend(self)

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
        # A named model provider (Manage > Model providers) supplies the
        # endpoint and key, so everything reading base_url/api_key from here
        # (the chat client, memory's embeddings) follows it.
        pid = str(cfg.get("llm_provider") or "").strip()
        if pid:
            p = self.llm_provider(pid)
            cfg["_provider"] = pid
            cfg["base_url"] = p.base_url if p is not None else ""
            cfg["api_key"] = p.api_key if p is not None else ""
            if p is not None and not str(cfg.get("model") or "").strip():
                cfg["model"] = p.default_model or (p.models[0] if len(p.models) == 1 else "")
        return cfg

    def llm_provider(self, provider_id: str):
        """A provider from the model providers plugin, or None."""
        llm = self._node.plugin("llm") if self._node is not None else None
        return llm.get(provider_id) if llm is not None else None

    def missing(self, cfg: dict | None = None) -> list[str]:
        cfg = cfg or self.config()
        if self.use_agent_host(cfg):
            return [] if str(cfg.get("agent_host") or "").strip() else ["agent_host"]
        return self._openai_missing(cfg)

    def _openai_missing(self, cfg: dict) -> list[str]:
        if cfg.get("_provider") and not cfg.get("base_url"):
            return ["llm_provider"] + (["model"] if not str(cfg.get("model") or "").strip() else [])
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
        if cfg.get("_provider") and not str(cfg.get("base_url") or "").strip():
            raise HomeError(f"the model provider {cfg['_provider']!r} is not available "
                            "(Manage > Model providers)")
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

    def system_prompt(self, cfg: dict, where: str, memory: str = "") -> str:
        parts = [self.persona_text(cfg),
                 f"You are {self.agent_name}, the home agent on this Rook hub (chat identity "
                 f"{self.identity}). {where} Answer plainly and briefly; say when you do not "
                 "know. You have no tools unless they are listed to you."]
        extra = str(cfg.get("system_prompt") or "").strip()
        if extra:
            parts.append(extra)
        if memory:
            parts.append(memory)
        return "\n\n".join(p for p in parts if p)

    # -- the model, with optional read-only tools ---------------------------
    async def complete(self, messages: list[dict], cfg: dict, *, thread: str | None = None,
                       via: str = "", may_search: bool = True,
                       cli: ChatClient | None = None, memory: dict | None = None) -> dict:
        """Run one turn (and up to MAX_TOOL_ROUNDS tool rounds). Returns
        ``{content, model, latency_ms, tools_used}`` with the API key scrubbed.
        ``may_search`` is False when whoever asked may not read the knowledge
        wiki themselves; the tool is then not offered. ``memory`` (``{person,
        room}``) offers the memory tools, acting for that person."""
        tools = ([KNOWLEDGE_TOOL] if cfg.get("tools") and may_search and self._has_knowledge()
                 else [])
        # Memory tools ride the same switch as knowledge search: some local
        # endpoints refuse any request that carries tools.
        mem = self._memory() if memory is not None and cfg.get("tools") else None
        if mem is not None:
            tools += mem.tools(save=bool(memory.get("save")))
        if cli is None and self.use_agent_host(cfg):
            async def handler(name: str, args: dict) -> str:
                return await self._run_tool(name, args, thread,
                                            memory if mem is not None else None)
            try:
                return await self.agents.run(cfg, messages, tools=tools, handler=handler,
                                             via=via or "turn", max_tool_calls=MAX_TOOL_ROUNDS * 2)
            except Unavailable as e:
                if not self._may_fall_back(cfg):
                    raise HomeError(f"the home agent is unavailable: {e}") from None
                log.warning("home agent: %s; answering with the OpenAI-compatible endpoint", e)
            except TurnFailed as e:
                raise LLMError(str(e)) from None
        cli = cli or self.client(cfg)
        tools = tools or None
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
                out = await self._run_tool(str(fn.get("name") or ""), fn.get("arguments"), thread,
                                           memory if mem is not None else None)
                used.append(str(fn.get("name") or "?"))
                msgs.append({"role": "tool", "tool_call_id": str(call.get("id") or ""),
                             "content": out})
        raise LLMError("the model kept calling tools")  # pragma: no cover - loop bound

    async def tool_turn(self, messages: list[dict], cfg: dict, tools: list[dict],
                        handler: Any, *, via: str, max_calls: int = 8) -> dict:
        """One turn with a caller's own tool set (the huddle chair's
        ``call_on``/``answer``/``propose``), on either backend. ``handler(name,
        args) -> str`` runs each call. Returns ``{content, model, latency_ms,
        tools_used}``; raises HomeError/LLMError."""
        if self.use_agent_host(cfg):
            try:
                return await self.agents.run(cfg, messages, tools=tools, handler=handler,
                                             via=via, max_tool_calls=max_calls)
            except Unavailable as e:
                if not self._may_fall_back(cfg):
                    raise HomeError(f"the home agent is unavailable: {e}") from None
                log.warning("home agent: %s; answering with the OpenAI-compatible endpoint", e)
            except TurnFailed as e:
                raise LLMError(str(e)) from None
        cli = self.client(cfg)
        used: list[str] = []
        total = 0
        msgs = list(messages)
        for _round in range(max_calls + 1):
            res = await cli.chat(msgs, max_tokens=cfg.get("max_tokens"),
                                 temperature=cfg.get("temperature"),
                                 tools=tools if _round < max_calls else None)
            total += res["latency_ms"]
            if not res["tool_calls"]:
                return {"content": cli._scrub(res["content"] or ""),
                        "model": cli._scrub(res["model"] or ""),
                        "latency_ms": total, "tools_used": used}
            msgs.append({"role": "assistant", "content": res["content"] or None,
                         "tool_calls": res["tool_calls"]})
            for call in res["tool_calls"]:
                fn = (call.get("function") or {}) if isinstance(call, dict) else {}
                raw = fn.get("arguments")
                try:
                    args = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
                except (ValueError, TypeError):
                    args = {}
                name = str(fn.get("name") or "")
                used.append(name or "?")
                out = await handler(name, args)
                msgs.append({"role": "tool", "tool_call_id": str(call.get("id") or ""),
                             "content": out})
        raise LLMError("the model kept calling tools")  # pragma: no cover - loop bound

    def use_agent_host(self, cfg: dict) -> bool:
        return agent_host.harness_of(cfg) is not None

    def _may_fall_back(self, cfg: dict) -> bool:
        return (cfg.get("agent_on_unavailable") == "fallback_backend"
                and not self._openai_missing(cfg))

    def _has_knowledge(self) -> bool:
        return self._node is not None and self._node.plugin("knowledge") is not None

    def may_read_knowledge(self, principal: Any) -> bool:
        return self.may_call(principal, "knowledge.read")

    def may_use_memory(self, principal: Any) -> bool:
        return self.may_call(principal, "memory.recall")

    def may_call(self, principal: Any, cap: str) -> bool:
        """May ``principal`` call ``cap`` on the hub under the current
        policy? Tools run as the home agent, so this keeps it from reading
        for someone who may not read themselves. ``deny`` and ``would_deny``
        both count as no. With no permission layer (plain hubs, tests)
        nothing is enforced anywhere, so yes."""
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
                [principal], cap,
                target_from_entry(getattr(self._node, "worker_id", None), entry, local=True))
        except Exception:  # noqa: BLE001 - when unsure, do not offer the tool
            log.exception("home agent: %s policy check failed", cap)
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

    async def _run_tool(self, name: str, raw_args: Any, thread: str | None,
                        memory: dict | None = None) -> str:
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
        except (ValueError, TypeError):
            args = {}
        if name in ("memory_save", "memory_search") and memory is not None:
            return await self._memory_tool(name, args, thread, memory)
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

    # -- memory (rook/hub/plugins/memory; docs/design/memory.md) ----------------
    def _memory(self):
        """The memory plugin when it is loaded and on, and ``home.memory`` is on."""
        if self._node is None or not self.settings.get("memory", True):
            return None
        mem = self._node.plugin("memory")
        return mem if mem is not None and getattr(mem, "ready", lambda: False)() else None

    @staticmethod
    def person_of(sender: Any) -> str | None:
        """A chat sender whose profile memory may be read and written: people
        only. Agents get shared memory, never a profile."""
        s = str(sender or "")
        return s if s.startswith(("user:", "human:")) else None

    async def recall_for(self, query: str, person: str | None, room: str | None = None,
                         people: bool = True) -> dict:
        mem = self._memory()
        if mem is None:
            return {"text": "", "ids": []}
        return await mem.context(query, person, room=room, include_people=people)

    async def _memory_tool(self, name: str, args: dict, thread: str | None,
                           memory: dict) -> str:
        """A memory tool call, run as the home agent for ``memory['person']``
        and journaled under the home agent like knowledge_search."""
        mem = self._memory()
        cap = "memory.remember" if name == "memory_save" else "memory.recall"
        person = memory.get("person")          # None: shared memory only
        with self._as_self():
            try:
                if mem is None:
                    raise RuntimeError("memory is off")
                if name == "memory_save" and not memory.get("save"):
                    raise PermissionError("whoever asked may not write memory")
                result = await mem.run_tool(name, args, person=person, actor=self.identity,
                                            room=memory.get("room"))
                reply = {"ok": "error" not in result, "result": result}
            except Exception as e:  # noqa: BLE001 - a tool error goes back to the model
                reply = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        # The journal gets the shape of the call, not the remembered text or
        # the recalled rows (they are personal; the Memory page has them).
        if name == "memory_save":
            logged = {"kind": str(args.get("kind") or "fact"),
                      "chars": len(str(args.get("text") or ""))}
            jreply = reply
        else:
            logged = {"query": str(args.get("query") or "")[:200]}
            jreply = {"ok": reply["ok"], "result": {"results": len(
                (reply.get("result") or {}).get("results") or [])}}
        self.journal(cap, {"tool": name, **logged}, jreply, thread)
        return json.dumps(reply.get("result") if reply["ok"] else {"error": reply.get("error")},
                          default=str)[:TOOL_RESULT_CHARS]

    async def summarise_for_memory(self, what: str, lines: list[str]) -> str | None:
        """A short summary for the memory plugin's consolidation (no tools),
        journaled as ``home.summarise``. ``what`` is ``conversation`` or ``week``."""
        cfg = self.config()
        cfg["tools"] = False
        task = ("Summarise this conversation for your own memory in two or three plain "
                "sentences: who wanted what, what was decided or answered, and anything to "
                "follow up. Third person. Never include passwords, codes or keys."
                if what == "conversation" else
                "Combine these conversation summaries from one week into one short paragraph "
                "for your own memory. Keep decisions, preferences and open threads; drop "
                "small talk. Never include passwords, codes or keys.")
        body = "\n".join(str(x) for x in lines)[-12000:]
        msgs = [{"role": "system", "content": self.system_prompt(cfg, task)},
                {"role": "user", "content": body}]
        async with self._ask_sem:
            res = await self.complete(msgs, {**cfg, "max_tokens": min(int(cfg.get("max_tokens")
                                                                          or 300), 300)},
                                      via="memory")
        self.journal("home.summarise", {"what": what, "lines": len(lines)},
                     {"ok": True, "result": {"model": res["model"], "chars": len(res["content"])}})
        return res["content"]

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
            atomic_write_json(self._cursor_path(), {"baseline": self._baseline,
                                                    "rooms": self._cursor}, 0o644)
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
            if self._chaired(rid):
                # A huddle the agent chairs: the huddle plugin runs its rounds.
                self._cursor[rid] = last
                dirty = True
                continue
            if last - cur > READ_LIMIT:          # only the newest can be answered anyway
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

    def _chaired(self, rid: str) -> bool:
        hp = self._node.plugin("huddle") if self._node is not None else None
        try:
            return bool(hp is not None and hp.chaired(rid))
        except Exception:  # noqa: BLE001 - a broken huddle store never stops chat
            return False

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
                person = self.person_of(trigger["sender"])
                principal = self.chat_principal(trigger["sender"])
                use_memory = self.may_use_memory(principal)
                recalled = (await self.recall_for(str(trigger.get("text") or ""), person, rid,
                                                  people=person is not None)
                            if use_memory else {"text": "", "ids": []})
                msgs = [{"role": "system", "content": self.system_prompt(cfg, where,
                                                                         recalled["text"])}]
                msgs += self.room_messages(store, rid, cfg)
                res = await self.complete(
                    msgs, cfg, thread=rid, via="chat",
                    may_search=self.may_read_knowledge(principal),
                    memory={"person": person, "room": rid,
                            "save": self.may_call(principal, "memory.remember")}
                    if person and use_memory else None)
            text = res["content"] or "(no answer)"
            # Address the person who asked in a group room; never @mention an
            # agent (a mention is what makes the other bot answer back).
            ments = ([trigger["sender"]] if len(participants) > 2
                     and not _is_agent(trigger["sender"]) else [])
            store.send(rid, self.identity, text, ments, False)
            learned = {"remembered": [], "forgotten": []}
            mem = self._memory()
            if mem is not None and person and use_memory:
                learned = await mem.observe(rid, person, str(trigger.get("text") or ""), text,
                                            self.identity)
            memo = {"recalled": len(recalled["ids"]), "remembered": len(learned["remembered"]),
                    "forgotten": len(learned["forgotten"])}
            self._note("chat", True, room=title, sender=trigger["sender"], model=res["model"],
                       latency_ms=res["latency_ms"], tools=res["tools_used"], memory=memo)
            self.journal("home.reply", {"room": rid, "to": trigger["sender"],
                                        "seq": trigger["seq"]},
                         {"ok": True, "result": {"model": res["model"], "chars": len(text),
                                                 "latency_ms": res["latency_ms"],
                                                 "tools": res["tools_used"],
                                                 "memory": {**memo, "ids": recalled["ids"]}}}, rid)
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
        # Skills that worked before for similar steps, and shared facts; no
        # person's profile (a job is not a conversation with someone).
        recalled = await self.recall_for(str(prompt or ""), None, people=False)
        msgs = [{"role": "system", "content": self.system_prompt(cfg, job_agent.WHERE,
                                                                 recalled["text"])},
                {"role": "user", "content": job_agent.first_message(prompt, context)[:40000]}]
        args = {"job": job, "run": run, "step": step}
        try:
            async with self._job_sem:
                out = None
                if self.use_agent_host(cfg):
                    try:
                        out = await self._agent_job_step(cfg, msgs, backend, limit)
                    except Unavailable as e:
                        if not self._may_fall_back(cfg):
                            raise HomeError(f"the home agent is unavailable: {e}") from None
                        log.warning("home agent: %s; job step uses the OpenAI-compatible "
                                    "endpoint", e)
                    except TurnFailed as e:
                        raise LLMError(str(e)) from None
                if out is None:
                    cli = self.client(cfg)
                    out = await job_agent.run_loop(cli, msgs, backend, max_calls=limit,
                                                   max_tokens=cfg.get("max_tokens"),
                                                   temperature=cfg.get("temperature"))
        except (LLMError, HomeError) as e:
            self._note("job", False, sender=f"job:{job}", error=str(e)[:300])
            self.journal("home.job_step", args, {"ok": False, "error": str(e)[:300]}, run or None)
            raise
        mem = self._memory()
        if mem is not None and out.get("verdict") == "ok" and out.get("tools_used"):
            # A step that worked with tools is a skill worth keeping.
            # Prompt and tool names only (never the result: that is tool
            # output); it reaches prompts once the operator approves it.
            await mem.learn_procedure(str(prompt or ""), out["tools_used"],
                                      job=job, step=step, run=run, actor=self.identity)
        self._note("job", True, sender=f"job:{job}", model=out["model"], verdict=out["verdict"],
                   tools=out["tools_used"], memory={"recalled": len(recalled["ids"])})
        self.journal("home.job_step", args,
                     {"ok": True, "result": {"verdict": out["verdict"], "calls": out["calls"],
                                             "tools": out["tools_used"], "model": out["model"]}},
                     run or None)
        return out

    async def _agent_job_step(self, cfg: dict, msgs: list[dict], backend: Any,
                               limit: int) -> dict:
        """A job step on Claude Code: the job tool set served from the hub's
        per-turn tool server, every call carried out by the jobs ``backend``
        under the same budget as :func:`job_agent.run_loop`."""
        names = [n for n in backend.tool_names() if n in job_agent.TOOLS and n != "finish"]
        tools = [job_agent.TOOLS[n] for n in names] + [job_agent.TOOLS["finish"]]
        state: dict = {"calls": 0, "used": [], "finish": None}

        async def handler(name: str, args: dict) -> str:
            if name == "finish":
                verdict = str(args.get("verdict") or "").lower()
                state["finish"] = (verdict if verdict in job_agent.VERDICTS else "failed",
                                   str(args.get("text") or "")[:2000])
                return json.dumps({"ok": True, "note": "Finished. Stop here; reply with one "
                                                       "short line."})
            if state["calls"] >= limit:
                return json.dumps({"ok": False, "error": f"tool budget used up ({limit} calls); "
                                                         "call finish"})
            state["calls"] += 1
            state["used"].append(name)
            try:
                out = await job_agent.dispatch(backend, name, args)
            except Exception as e:  # noqa: BLE001 - a tool error goes back to the model
                out = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
            return job_agent._dump(out)

        first = dict(msgs[-1])
        first["content"] = (str(first.get("content") or "") + "\n\nYour tools are the "
                            "mcp__home__* tools. End by calling finish.")
        # The jobs plugin bounds the step's time; the turn may use all of it.
        cfg = {**cfg, "agent_timeout_s": 1800.0}
        res = await self.agents.run(cfg, msgs[:-1] + [first], tools=tools, handler=handler,
                                    via="job", max_tool_calls=limit + 2)
        if state["finish"] is not None:
            verdict, text = state["finish"]
        else:
            verdict, text = job_agent.parse_verdict(res["content"])
        return {"verdict": verdict, "text": text, "calls": state["calls"],
                "tools_used": state["used"], "model": res["model"], "rounds": 1,
                "worker": res.get("worker")}

    # -- caps ----------------------------------------------------------------
    @capability("ask", risk="write", timeout=None)
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
        # Asked by an agent (or a person) directly: shared memory only, never
        # anyone's profile or conversations, and nothing is captured from the
        # question. With tools, the model may save shared facts when the asker
        # may write memory itself.
        use_memory = self.may_use_memory(principal)
        recalled = (await self.recall_for(question, None) if use_memory
                    else {"text": "", "ids": []})
        msgs = [{"role": "system", "content": self.system_prompt(
                    cfg, "Another agent or a person is asking you directly, not in a chat room.",
                    recalled["text"])},
                {"role": "user", "content": user[:20000]}]
        try:
            async with self._ask_sem:
                res = await self.complete(msgs, cfg, via="ask",
                                          may_search=self.may_read_knowledge(principal),
                                          memory={"person": None, "room": None,
                                                  "save": self.may_call(principal,
                                                                        "memory.remember")}
                                          if use_memory else None)
        except (LLMError, HomeError) as e:
            self._note("ask", False, sender=who, error=str(e)[:300])
            raise HomeError(summarise(str(e))) from None
        self._note("ask", True, sender=who, model=res["model"], latency_ms=res["latency_ms"],
                   tools=res["tools_used"], memory={"recalled": len(recalled["ids"])})
        return {"agent": self.identity, "answer": res["content"], "model": res["model"],
                "latency_ms": res["latency_ms"], "tools_used": res["tools_used"],
                "memories_used": len(recalled["ids"])}

    @capability("status", risk="read")
    def status(self) -> dict:
        """Whether the home agent is on, its identity, endpoint host and model,
        and the last error (never the key)."""
        cfg = self.config()
        host = ""
        with contextlib.suppress(Exception):
            from .llm import url_host
            host = url_host(str(cfg.get("base_url") or ""))
        agents = {}
        if self.use_agent_host(cfg):
            agents = {"host": cfg.get("agent_host") or "",
                      "harness": agent_host.harness_of(cfg),
                      "fallbacks": list(cfg.get("agent_fallback_hosts") or []),
                      "hosts": self.agents.hosts()}
        return {"enabled": self.enabled(), "identity": self.identity, "name": self.agent_name,
                "backend": cfg.get("backend") or "openai", "agent_host": agents,
                "provider": cfg.get("provider"), "llm_provider": cfg.get("_provider") or "",
                "endpoint_host": host,
                "model": ((agent_host.model_of(cfg) or "default") if self.use_agent_host(cfg)
                          else cfg.get("model")) or "", "missing": self.missing(cfg),
                "tools": bool(cfg.get("tools")), "key_set": bool(cfg.get("api_key")),
                "chat": self._store() is not None, "memory": self._memory() is not None,
                "last_error": self.last_error}

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
        llm_providers = []
        llm = self._node.plugin("llm") if self._node is not None else None
        if llm is not None:
            with contextlib.suppress(Exception):
                llm_providers = [p.describe() for p in llm.providers()]
        return {"view": "home", "settings": rows, "status": self.status(),
                "personas": personas, "secrets": secrets, "activity": list(self.activity),
                "providers": list(PROVIDERS), "llm_providers": llm_providers,
                "backends": list(BACKENDS), "claude_models": list(agent_host.CLAUDE_MODELS),
                "agent_hosts": self.agents.hosts(),
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
        if self.use_agent_host(self.config(self._form(data))):
            return await self._agent_probe(action, self._form(data), actor)
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

    async def _agent_probe(self, action: str, form: dict, actor: str) -> dict:
        """Models and Test for an agent harness backend: the form's values
        (unsaved), except the Claude sign-in token, which is only ever the
        saved one. Models asks the chosen host's harness."""
        cfg = self.config(form)
        if action == "home_models":
            out = await self.agents.models(cfg)
            return {**out, "hosts": self.agents.hosts()}
        cfg["claude_token"] = self.config().get("claude_token")
        cfg["tools"] = False
        cfg["agent_timeout_s"] = min(float(cfg.get("agent_timeout_s") or 90), 90.0)
        cfg["agent_on_unavailable"] = "fail"
        msgs = [{"role": "system", "content": self.system_prompt(
                    cfg, "This is a connection test from the hub's settings page.")},
                {"role": "user", "content": "Say hello in one short sentence."}]
        try:
            res = await self.complete(msgs, cfg, via="test")
        except (LLMError, HomeError) as e:
            self._note("test", False, sender=actor, error=str(e)[:300])
            return {"ok": False, "error": str(e)}
        self._note("test", True, sender=actor, model=res["model"], latency_ms=res["latency_ms"],
                   worker=res.get("worker"), harness=res.get("harness"))
        return {"ok": True, "reply": res["content"], "model": res["model"],
                "latency_ms": res["latency_ms"], "worker": res.get("worker"),
                "usage": res.get("usage"), "cost_usd": res.get("cost_usd")}

    def _probe_config(self, form: dict) -> tuple[dict, str]:
        """The settings for a models/test probe from unsaved form values, with
        the API key decided from the SAVED settings only: a vault reference
        typed into the form is never resolved (that would let the page send
        any vault secret anywhere), and the saved key goes only to the saved
        base URL. Returns ``(cfg, note)``; the note says why no key or the
        saved key was used."""
        saved = self.config()
        cfg = self.config(form)
        if cfg.get("_provider"):
            # The provider's own key, which only ever goes to the provider's
            # own address (both set on Model providers, not in this form).
            return cfg, ""
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
