"""``home.*``: the hub's home agent, an LLM that lives at the hub.

The operator assigns one OpenAI-compatible endpoint, a model, a persona and an
API key (a vault reference, ``{{secret:name}}``; never stored in plain text)
on the dashboard's Manage > Home agent page. The agent is then:

* **a chat participant.** It keeps itself present in the hub's chat rooms as
  ``agent:<name>`` (default ``agent:home``). A message that mentions it, that
  says ``@<name>`` in a room it is in, or that is sent in a two-person room
  with it gets a reply built from the room's recent messages. Rooms are
  watched in the hub's own loop; the model call runs as a background task with
  a timeout, so nothing blocks the hub.
* **askable by other agents.** ``home.ask`` on worker ``rook`` (and the MCP
  tool ``rook_home_ask`` on hubs where the home agent is enabled when the MCP
  server starts).
* **journaled as itself.** Its replies and any tool it uses are recorded in the
  call journal under its own identity (auth kind ``home``), never under the
  person or agent who asked.

Tools are off by default. With ``home.tools`` on it may search the shared
knowledge wiki (read-only). See docs/design/home-agent.md.
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
from .llm import PROVIDERS, ChatClient, LLMError, aiohttp_request

log = logging.getLogger("rook.hub.plugins.home")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
KEY_REF = r"(\{\{secret:[a-z0-9][a-z0-9._-]{0,63}\}\})?"
#: Messages older than this are history, not something to answer now.
MAX_AGE_S = 600.0
#: Replies per room per minute (a loop breaker between chatty participants).
ROOM_RATE = 6
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
        self._cursor: dict[str, int] = {}
        self._baseline = False
        self._rate: dict[str, collections.deque] = {}
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

    def _api_key(self, ref: str | None) -> str | None:
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
            return vault.get(m.group(1), self.identity, via="home agent")
        except KeyError:
            raise HomeError(f"no vault secret named {m.group(1)!r}") from None

    def client(self, cfg: dict | None = None, *, need_model: bool = True) -> ChatClient:
        cfg = cfg or self.config()
        if cfg.get("provider", "openai") not in PROVIDERS:
            raise HomeError(f"provider must be one of {list(PROVIDERS)}")
        if not str(cfg.get("base_url") or "").strip():
            raise HomeError("set the home agent's base URL first")
        if need_model and not str(cfg.get("model") or "").strip():
            raise HomeError("pick the home agent's model first")
        try:
            return ChatClient(str(cfg["base_url"]), str(cfg.get("model") or ""),
                              self._api_key(cfg.get("api_key")),
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
                       via: str = "") -> dict:
        """Run one turn (and up to MAX_TOOL_ROUNDS tool rounds). Returns
        ``{content, model, latency_ms, tools_used}``."""
        cli = self.client(cfg)
        tools = [KNOWLEDGE_TOOL] if cfg.get("tools") and self._has_knowledge() else None
        used: list[str] = []
        total = 0
        msgs = list(messages)
        for _round in range(MAX_TOOL_ROUNDS + 1):
            res = await cli.chat(msgs, max_tokens=cfg.get("max_tokens"),
                                 temperature=cfg.get("temperature"),
                                 tools=tools if _round < MAX_TOOL_ROUNDS else None)
            total += res["latency_ms"]
            if not res["tool_calls"] or not tools:
                return {"content": res["content"], "model": res["model"],
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
        row = {"ts": time.time(), "via": via, "ok": ok, **extra}
        if not ok:
            self.last_error = str(extra.get("error") or "")
        self.activity.appendleft(row)

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
        for r in store.rooms_for(self.identity, limit=10000, include_all=True).get("rooms", []):
            self._cursor[r["room"]] = store.last_seq(r["room"])
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
        if msg.get("sender") == self.identity:
            return False
        if self.identity in (msg.get("mentions") or []):
            return True
        if self.identity not in participants:
            return False
        if len(participants) == 2:
            return True
        return re.search(rf"(?<![\w@])@{re.escape(self.agent_name)}\b", msg.get("text") or "",
                         re.I) is not None

    async def tick(self) -> list[asyncio.Task]:
        """One pass over the rooms: spawn a reply task per room where someone
        addressed the agent since the last pass. Returns the tasks started."""
        store = self._store()
        if store is None or not self.enabled() or self.missing():
            return []
        store.touch(self.identity)
        if not self._baseline:
            self._take_baseline(store)
        started: list[asyncio.Task] = []
        dirty = False
        rooms = store.rooms_for(self.identity, limit=200).get("rooms", [])
        for r in rooms:
            rid = r["room"]
            if rid not in self._cursor:
                self._cursor[rid] = 0
                dirty = True
            if not r.get("unread") and self._cursor[rid] >= store.last_seq(rid):
                continue
            got = store.read(rid, self.identity, since_seq=self._cursor[rid], mark=True, limit=200)
            msgs = got.get("messages") or [] if got.get("ok") else []
            if not msgs:
                continue
            self._cursor[rid] = got["last_seq"]
            dirty = True
            now = time.time()
            trigger = None
            for m in msgs:
                if now - float(m.get("ts") or 0) <= MAX_AGE_S and \
                        self.addressed(m, got.get("participants") or []):
                    trigger = m
            if trigger is None or rid in self._busy or not self._room_allows(rid):
                continue
            self._busy.add(rid)
            task = asyncio.get_running_loop().create_task(
                self._reply(rid, got.get("title") or rid, got.get("participants") or [], trigger))
            self._replies.add(task)
            task.add_done_callback(self._replies.discard)
            started.append(task)
        if dirty:
            self._save_cursor()
        return started

    def _room_allows(self, rid: str) -> bool:
        q = self._rate.setdefault(rid, collections.deque())
        now = time.monotonic()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= ROOM_RATE:
            return False
        q.append(now)
        return True

    def room_messages(self, store, rid: str, cfg: dict) -> list[dict]:
        out = []
        for m in store.tail(rid, int(cfg.get("context_messages") or 20)):
            if m["sender"] == self.identity:
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
                res = await self.complete(msgs, cfg, thread=rid, via="chat")
            text = res["content"] or "(no answer)"
            ments = [trigger["sender"]] if len(participants) > 2 else []
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
            if store is not None:
                store.send(rid, self.identity, f"(I could not answer: {err[:200]})", [], False)
            self._note("chat", False, room=title, sender=trigger.get("sender"), error=err[:300],
                       latency_ms=round((time.monotonic() - t0) * 1000))
            self.journal("home.reply", {"room": rid, "to": trigger.get("sender")},
                         {"ok": False, "error": err[:300]}, rid)
        finally:
            self._busy.discard(rid)

    # -- caps ----------------------------------------------------------------
    @capability("ask", risk="write")
    async def ask(self, question: str, context: str = "") -> dict:
        """Ask the hub's home agent (its own LLM) a question; returns its answer.

        ``context`` is extra text it should consider (it cannot see your
        conversation). Errors when the home agent is off or not configured."""
        if not self.enabled():
            raise HomeError("the home agent is off (Manage > Home agent)")
        question = str(question or "").strip()
        if not question:
            raise HomeError("ask needs a question")
        cfg = self.config()
        who = _actor()
        user = f"[{who}] {question}" + (f"\n\nContext:\n{context.strip()}" if context else "")
        msgs = [{"role": "system", "content": self.system_prompt(
                    cfg, "Another agent or a person is asking you directly, not in a chat room.")},
                {"role": "user", "content": user[:20000]}]
        try:
            res = await self.complete(msgs, cfg, via="ask")
        except (LLMError, HomeError) as e:
            self._note("ask", False, sender=who, error=str(e)[:300])
            raise HomeError(str(e)) from None
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
        cfg = self.config(self._form(data))
        if action == "home_models":
            try:
                models = await self.client(cfg, need_model=False).models(
                    timeout=min(float(cfg.get("timeout_s") or 20), 20.0))
            except (LLMError, HomeError) as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "models": models}
        if action == "home_test":
            cfg["tools"] = False
            cfg["timeout_s"] = min(float(cfg.get("timeout_s") or 25), 25.0)
            msgs = [{"role": "system", "content": self.system_prompt(
                        cfg, "This is a connection test from the hub's settings page.")},
                    {"role": "user", "content": "Say hello in one short sentence."}]
            try:
                res = await self.complete(msgs, cfg, via="test")
            except (LLMError, HomeError) as e:
                self._note("test", False, sender=actor, error=str(e)[:300])
                return {"ok": False, "error": str(e)}
            self._note("test", True, sender=actor, model=res["model"],
                       latency_ms=res["latency_ms"])
            return {"ok": True, "reply": res["content"], "model": res["model"],
                    "latency_ms": res["latency_ms"]}
        raise HomeError("use home_save, home_models or home_test")


PLUGIN = HomeAgent
