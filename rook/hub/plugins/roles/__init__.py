"""``role.*``: huddle roles, their homes and their memory (worker ``rook``).

A role is a charter (what an agent is responsible for), a tool scope and its
own memory (docs/design/huddles.md 4). Each role has a **home directory** on
its agent host (worker plugin ``agenthost``, :mod:`rook.worker.rolehome`):
its memory (scope ``role:<id>``) and the harness config that declares its
tools live there, and every turn of the role runs with that directory as its
working directory. The hub keeps the role documents (:mod:`.model`) and is the
authority on what a role may call: a role turn's tools are served by the home
agent's per-turn tool server, and :meth:`RolesPlugin.role_call` refuses any
cap outside the role's ``tools`` patterns (and, for a role that only
proposes, anything above ``read``).

Caps on worker ``rook`` (none has a dedicated MCP tool yet; H3's
``rook_huddle`` will use them):

* read: ``role.list``, ``role.get``, ``role.history``, ``role.presets``,
  ``role.memory`` (a role's memory, from its host);
* write: ``role.ask`` (one turn of a role, in its home; spends model time);
* admin: ``role.set``, ``role.delete``, ``role.memory_edit``, ``role.move``
  (moves the role's home to another agent host).
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import logging
import uuid
from typing import Any

from ....core import context
from ....core.plugin import Plugin, capability, place, setting
from ....core.text import clip
from . import model
from .model import RoleError

log = logging.getLogger("rook.hub.plugins.roles")

#: A role's tool result is cut to this many characters (``role.tool_result_chars``),
#: with a ``[truncated: N of M chars]`` marker at the cut.
TOOL_RESULT_CHARS = 12000
#: How many tool calls before a role's limit its results start saying how
#: many are left, and the slack the agent host allows past the limit (the
#: role's own memory tools count there, and a role told its budget is spent
#: still needs a call or two to stop).
BUDGET_WARN = 3
BUDGET_SLACK = 4
#: Caps no role may call, whatever its tools say: another role's turn (no
#: loops) and the agent host's own caps (role homes, turns).
NEVER = ("role.ask", "role.move", "role.memory_edit", "agenthost.*")
CALL_TIMEOUT = 60.0
ASK_CHARS = 20000
#: Pre-reading a role's sources (docs/design/huddles.md 4.1): per result, in
#: total, workers per source, and the time for all of them.
SOURCE_CHARS = 1500
SOURCES_CHARS = 8000
SOURCE_WORKERS = 8
SOURCES_TIMEOUT = 30.0


def _actor() -> str:
    ident = context.caller_identity.get()
    return str(ident) if ident else "system:rook-hub"


def _admin_gate(what: str) -> None:
    from ...authz import require_hub_admin
    refusal = require_hub_admin(what)
    if refusal:
        raise PermissionError(refusal)


def _fn(name: str, description: str, props: dict | None = None,
        required: list | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {},
                       **({"required": required} if required else {})}}}


#: The hub tools a role turn can be given (served by the per-turn tool server).
TOOLS = {
    "knowledge_search": _fn("knowledge_search", "Search the band's shared knowledge wiki.",
                            {"query": {"type": "string"}}, ["query"]),
    "rook_workers": _fn("rook_workers", "The band's workers and the caps on each that are in "
                                        "your scope."),
    "rook_call": _fn("rook_call", "Call one capability on a worker (worker \"rook\" is the hub), "
                                  "within your scope. Returns its result.",
                     {"worker": {"type": "string"}, "cap": {"type": "string"},
                      "args": {"type": "object"}}, ["worker", "cap"]),
}


class RolesPlugin(Plugin):
    NAMESPACE = "role"
    NAME = "roles"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    MIGRATIONS = "migrations"
    SETTINGS = (
        setting("max_tool_calls", int, 12, min=1, max=100, label="Tool calls per role turn",
                help="How many tool calls a role turn may make when neither the role nor its "
                     "preset category sets its own (engineering roles default to 30). Near the "
                     "limit the role is told how many are left; past it, its tools answer "
                     "\"answer now with what you have\"."),
        setting("tool_result_chars", int, TOOL_RESULT_CHARS, min=2000, max=60000,
                label="Role tool result length",
                help="A role's tool result is cut to this many characters, with a "
                     "[truncated: N of M chars] marker at the cut."),
        setting("code_locations", list, [], label="Where code lives",
                help="One line per repository: its name, the worker and the folder of its main "
                     "checkout (and where new worktrees go). Every role turn is told these, so "
                     "roles never guess folders; a session.start card's folder must be under "
                     "one of them when any are set."),
    )
    SKILL = ("### roles\n"
             "Huddle roles (project manager, QA, lead developer, ...) each have a charter, a "
             "tool scope and their own memory, kept in the role's home directory on its agent "
             "host. `rook_call(worker=\"rook\", cap=\"role.list\")` lists them; `role.ask` with "
             "`{\"id\": \"qa\", \"question\": ...}` runs one turn of a role. `role.set`, "
             "`role.move` and `role.memory_edit` are admin: ask the user first.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node: Any = None
        self._store: model.RoleStore | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    # -- wiring ----------------------------------------------------------------
    def bind_host(self, node) -> None:
        self._node = node

    @property
    def store(self) -> model.RoleStore:
        if self._store is None:
            self._store = model.RoleStore(self.data_dir / "roles.db", self.migrate)
        return self._store

    async def start(self) -> None:
        try:
            self.store
        except Exception:
            log.exception("role store unavailable")

    def _home(self):
        home = self._node.plugin("home") if self._node is not None else None
        if home is None:
            raise RoleError("roles need the home agent plugin (Manage > Home agent)")
        return home

    def _lock(self, rid: str) -> asyncio.Lock:
        return self._locks.setdefault(rid, asyncio.Lock())

    def role(self, rid: str) -> dict:
        doc = self.store.get(str(rid or ""))
        if doc is None:
            raise RoleError(f"no role {rid!r}")
        return doc

    # -- identity ----------------------------------------------------------------
    def identity(self, role: dict) -> str:
        return f"agent:{self._home().agent_name}/role:{role['id']}"

    def principal(self, role: dict):
        """The policy principal of a role's calls: an agent, named for the role."""
        from ...policy import Principal
        name = self._home().agent_name
        return Principal(f"home:{name}/role:{role['id']}", "token", "agent",
                         label=self.identity(role))

    def audit(self, role: dict) -> dict:
        name = self._home().agent_name
        return {"kind": "home", "agent_id": f"home:{name}/role:{role['id']}",
                "actor": f"home.{name}.role.{role['id']}"}

    @contextlib.contextmanager
    def _as_role(self, role: dict):
        ident = self.identity(role)
        tokens = [(context.caller_identity, context.caller_identity.set(ident))]
        try:
            from ...authz import current_principal
            tokens.append((current_principal, current_principal.set(self.principal(role))))
        except Exception:  # noqa: BLE001
            pass
        try:
            from ....band_mcp import attribution
            a = self.audit(role)
            att = attribution.Attribution(identity=ident, kind="home", label=role["id"],
                                          agent_id=a["agent_id"], actor=a["actor"])
            tokens.append((attribution.current, attribution.current.set(att)))
        except Exception:  # noqa: BLE001 - the bridge is optional (tests, plain hubs)
            pass
        try:
            yield
        finally:
            for var, tok in reversed(tokens):
                var.reset(tok)

    def journal(self, role: dict, cap: str, worker: str, args: dict, reply: dict) -> None:
        j = getattr(self._node, "journal", None)
        if j is None:
            return
        try:
            j.record(cap=cap, worker=worker, identity=self.identity(role), args=args,
                     reply=reply, audit=self.audit(role))
        except Exception:
            log.exception("roles: journaling %s failed", cap)

    # -- the tools of a role turn ---------------------------------------------------
    def tools_for(self, role: dict) -> list[dict]:
        names = ["rook_workers", "rook_call"]
        if model.in_scope(role, "knowledge.read") and self._node.plugin("knowledge") is not None:
            names.insert(0, "knowledge_search")
        return [TOOLS[n] for n in names]

    def _roster(self) -> dict:
        out: dict = {}
        client = getattr(self._node, "client", None)
        with contextlib.suppress(Exception):
            out.update(dict(getattr(client, "workers", {}) or {}))
        with contextlib.suppress(Exception):
            out[self._node.worker_id] = self._node.entry()
        return out

    def _tier(self, cap: str, entry: dict | None) -> str:
        from ....core.authz import effective_tier
        return effective_tier(cap, ((entry or {}).get("tiers") or {}).get(cap))

    def scope_refusal(self, role: dict, cap: str, entry: dict | None) -> str:
        if any(fnmatch.fnmatchcase(cap, p) for p in NEVER):
            return f"roles never call {cap}"
        if not model.in_scope(role, cap):
            return f"{cap} is outside role {role['id']}'s tools"
        if role.get("autonomy") != "act":
            tier = self._tier(cap, entry)
            if tier != "read":
                return (f"{cap} is a {tier} cap and role {role['id']} only proposes; suggest it "
                        "to the person instead")
        return ""

    async def role_workers(self, role: dict) -> dict:
        out = []
        for wid, e in sorted(self._roster().items(),
                             key=lambda kv: str(kv[1].get("name") or kv[0])):
            caps = [c for c in e.get("caps") or [] if not self.scope_refusal(role, c, e)]
            if caps:
                name = "rook" if wid == getattr(self._node, "worker_id", None) else \
                    str(e.get("name") or wid[:8])
                out.append({"name": name, "caps": caps[:200]})
        return {"workers": out}

    async def role_call(self, role: dict, worker: str, cap: str, args: Any) -> dict:
        """One ``rook_call`` from a role turn: the role's scope, then the
        band's policy for the role's principal, then the call, journaled as
        the role."""
        worker, cap = str(worker or "").strip(), str(cap or "").strip()
        args = args if isinstance(args, dict) else {}
        if not worker or not cap:
            return {"ok": False, "error": "rook_call needs worker and cap"}
        from ..jobs.kinds import resolve_worker
        roster = self._roster()
        hub_id = getattr(self._node, "worker_id", None)
        try:
            found = resolve_worker(roster, worker, cap, hub_id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not found:
            return {"ok": False, "error": f"no live worker {worker!r}"}
        wid, name = found
        entry = roster.get(wid)
        why = self.scope_refusal(role, cap, entry)
        if not why:
            why = self._policy(role, cap, wid, entry)
        if why:
            reply = {"ok": False, "denied": why, "error": why}
            self.journal(role, cap, name, args, reply)
            return {"ok": False, "error": why}
        ident, principal = self.identity(role), self.principal(role)
        try:
            with self._as_role(role):
                if wid == hub_id:
                    reply = await asyncio.wait_for(self._node.dispatch(cap, args, ident),
                                                   CALL_TIMEOUT)
                else:
                    reply = await self._node.client.call(cap=cap, args=args, target=wid,
                                                         timeout=CALL_TIMEOUT, identity=ident,
                                                         principal=principal)
        except asyncio.TimeoutError:
            reply = {"ok": False, "error": f"no reply within {CALL_TIMEOUT:.0f}s"}
        except Exception as e:  # noqa: BLE001 - goes back to the model
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        reply = _mask(reply if isinstance(reply, dict) else {"ok": True, "result": reply})
        self.journal(role, cap, name, args, reply)
        return reply

    def _policy(self, role: dict, cap: str, wid: str, entry: dict | None) -> str:
        authz = getattr(getattr(self._node, "client", None), "authz", None)
        if authz is None:
            return ""
        try:
            d = authz.check(cap, wid, entry, identity=self.identity(role),
                            principal=self.principal(role),
                            local=wid == getattr(self._node, "worker_id", None))
        except Exception:  # noqa: BLE001 - when unsure, refuse
            log.exception("roles: policy check failed for %s", cap)
            return f"policy check failed for {cap}"
        if getattr(d, "held", False):
            return f"{cap} needs a person's approval; a role cannot wait for one, suggest it instead"
        return f"denied by policy ({d.rule})" if d.denied else ""

    async def _knowledge(self, role: dict, args: dict) -> dict:
        cap_args = {"action": "search", "query": str(args.get("query") or "")[:300]}
        if not model.in_scope(role, "knowledge.read"):
            return {"ok": False, "error": "knowledge.read is outside this role's tools"}
        with self._as_role(role):
            try:
                result = await self._node.invoke("knowledge.read", cap_args, self.identity(role))
                reply = {"ok": True, "result": result}
            except Exception as e:  # noqa: BLE001
                reply = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        self.journal(role, "knowledge.read", "rook", cap_args, reply)
        return reply

    async def run_tool(self, role: dict, name: str, args: dict) -> str:
        if name == "knowledge_search":
            out: Any = await self._knowledge(role, args)
        elif name == "rook_workers":
            out = await self.role_workers(role)
        elif name == "rook_call":
            out = await self.role_call(role, args.get("worker"), args.get("cap"),
                                       args.get("args"))
        else:
            out = {"ok": False, "error": f"unknown tool {name!r}"}
        limit = int(self.settings.get("tool_result_chars") or TOOL_RESULT_CHARS)
        return clip(json.dumps(out, default=str), limit)

    # -- sources ------------------------------------------------------------------
    async def preread(self, role: dict) -> str:
        """What the role's ``sources`` say now, read at the hub before its
        turn (so the answer does not hang on the model calling tools). Each
        source runs through :meth:`role_call` (the role's scope, read tier
        unless it acts, policy, journal); one without a worker runs on every
        live worker that has the cap (``sessions.list`` across hosts)."""
        sources = list(role.get("sources") or [])
        if not sources:
            return ""
        roster = self._roster()
        hub_id = getattr(self._node, "worker_id", None)
        jobs = []
        for src in sources:
            cap, args = src["cap"], dict(src.get("args") or {})
            if src.get("worker"):
                jobs.append((src["worker"], cap, args))
                continue
            names = []
            for wid, e in roster.items():
                if cap in (e.get("caps") or []):
                    names.append("rook" if wid == hub_id else str(e.get("name") or wid))
            jobs += [(n, cap, args) for n in sorted(names)[:SOURCE_WORKERS]]
        if not jobs:
            return ""

        async def one(worker: str, cap: str, args: dict) -> str:
            reply = await self.role_call(role, worker, cap, args)
            if not reply.get("ok", True) or reply.get("error"):
                return f"{cap} on {worker}: unavailable ({str(reply.get('error'))[:200]})"
            body = json.dumps(reply.get("result", reply), default=str, separators=(",", ":"))
            # Cut visibly: the role reads the rest itself with rook_call.
            return f"{cap} on {worker}: {clip(body, SOURCE_CHARS)}"

        try:
            got = await asyncio.wait_for(asyncio.gather(*(one(*j) for j in jobs)), SOURCES_TIMEOUT)
        except asyncio.TimeoutError:
            return "(your sources did not answer in time; read them with rook_call if you need them)"
        return clip("\n".join(got), SOURCES_CHARS)

    # -- a role turn ---------------------------------------------------------------
    def config_for(self, role: dict) -> dict:
        """The house agent's settings with the role's backend/model/effort."""
        from ..home import agent_host
        home = self._home()
        cfg = home.config()
        if role.get("backend"):
            cfg["backend"] = role["backend"]
        harness = agent_host.harness_of(cfg)
        if harness is None:
            raise RoleError("roles run on an agent harness (claude-code, codex or opencode): "
                            "set the role's backend, or the house agent's (home.backend)")
        prefix = {"claude": "claude", "codex": "codex", "opencode": "opencode"}[harness]
        if role.get("model"):
            cfg[f"{prefix}_model"] = role["model"]
        if role.get("effort") and harness in ("claude", "codex"):
            cfg[f"{prefix}_effort"] = role["effort"]
        return cfg

    def system_prompt(self, role: dict, cfg: dict) -> str:
        home = self._home()
        persona = ""
        if role.get("persona"):
            persona = home.persona_text({**cfg, "persona": role["persona"]})
        else:
            persona = home.persona_text(cfg)
        lines = [f"You are {role['name']} (role:{role['id']}), one of the roles on this Rook "
                 f"hub's team; the house agent ({home.agent_name}) chairs.",
                 f"Your charter: {role.get('charter') or '(none written yet)'}"]
        if role.get("concerns"):
            lines.append("What you watch and raise without being asked:\n"
                         + "\n".join(f"- {c}" for c in role["concerns"]))
        if role.get("checklist"):
            lines.append("Your checklist (work through what applies to the question):\n"
                         + "\n".join(f"- {c}" for c in role["checklist"]))
        if role.get("seed_memory") and not role.get("host") and role.get("memory") != "off":
            # The first turn, before its home (and so its memory) exists:
            # what it starts with, saved into its memory after this turn.
            lines.append("What you know to start with:\n"
                         + "\n".join(f"- {c}" for c in role["seed_memory"]))
        if role.get("sources"):
            lines.append("Where you look before answering (with rook_call):\n" + "\n".join(
                f"- {s['cap']} on {s.get('worker') or 'the worker that has it'}"
                + (f" {json.dumps(s['args'])}" if s.get("args") else "")
                for s in role["sources"]))
        if role.get("autonomy") == "act":
            lines.append("You may act within your tools. Say what you did.")
        else:
            lines.append("You read and you propose; you do not act. When something should be "
                         "done, say exactly what (which cap on which worker, with what) so the "
                         "person can approve it.")
        deny = ((role.get("tools") or {}).get("deny")) or []
        if deny:
            lines.append("Never call these yourself, whatever your autonomy; when one is needed, "
                         "propose it for a person to approve: " + ", ".join(deny))
        if role.get("memory") != "off":
            lines.append("Your memory is yours alone (role:" + role["id"] + "). Keep durable "
                         "facts and procedures that worked with memory_remember; never secrets.")
        lines.append(self.code_locations_text())
        limit = self.tool_limit(role)
        lines.append(f"You have at most {limit} tool calls this turn (your memory tools count "
                     "too). When a tool result says only a few are left, stop calling tools and "
                     "answer with what you have, saying what you could not check.")
        lines.append("Answer plainly and briefly; say when you do not know.")
        return "\n\n".join(p for p in [persona, "\n\n".join(lines)] if p)

    def code_locations(self) -> list[str]:
        """``role.code_locations``: one line per repository (band data, set on
        the hub; never shipped in presets)."""
        raw = self.settings.get("code_locations") or []
        if isinstance(raw, str):
            raw = raw.splitlines()
        return [str(x).strip() for x in raw if str(x).strip()][:40]

    def code_locations_text(self) -> str:
        where = self.code_locations()
        if not where:
            return ("Where code lives: no code locations are set on this hub (Settings, "
                    "role.code_locations). When you need a repository's folder, say it is not "
                    "configured; never guess or search other folders for it.")
        return ("Where code lives on this band (Settings, role.code_locations). Use these "
                "folders, and never guess or search others; a coding session starts in one of "
                "them:\n" + "\n".join(f"- {w}" for w in where))

    def tool_limit(self, role: dict) -> int:
        """A role turn's tool-call budget: the role's own ``max_tool_calls``,
        else its preset category's (by the role's category, or the preset of
        the same id for roles saved before categories), else
        ``role.max_tool_calls``."""
        own = int(role.get("max_tool_calls") or 0)
        if own > 0:
            return own
        cat = role.get("category") or ""
        if not cat:
            with contextlib.suppress(Exception):
                cat = (model.preset(role["id"]) or {}).get("category") or ""
        if cat:
            with contextlib.suppress(Exception):
                for c in model.categories():
                    if c["id"] == cat and c.get("max_tool_calls"):
                        return int(c["max_tool_calls"])
        return int(self.settings.get("max_tool_calls") or 12)

    async def turn(self, role: dict, question: str, context_text: str = "",
                   asker: str = "", *, extra_tools: list[dict] | None = None,
                   extra_handler: Any = None, guide: str = "", via: str = "",
                   preread: bool = False) -> dict:
        """One turn of ``role``. ``extra_tools`` (with ``extra_handler(role,
        name, args) -> str``) are offered next to the role's own (a huddle's
        ``propose``); ``guide`` is appended to the system prompt; ``preread``
        reads the role's sources first and adds them to the context."""
        home = self._home()
        user = (f"[{asker}] " if asker else "") + question
        if context_text:
            user += f"\n\nContext:\n{context_text}"
        from ..home.agent_host import TurnFailed, Unavailable
        async with self._lock(role["id"]):
            role = self.role(role["id"])           # the newest revision, after the queue
            if not role.get("enabled", True):
                raise RoleError(f"role {role['id']} is off")
            cfg = self.config_for(role)
            if preread:
                seen = await self.preread(role)
                if seen:
                    user += "\n\nWhat your sources say now (read for you just before this turn):\n" + seen
            system = self.system_prompt(role, cfg)
            if guide:
                system += "\n\n" + guide
            msgs = [{"role": "system", "content": system},
                    {"role": "user", "content": clip(user, ASK_CHARS)}]
            tools = self.tools_for(role) + list(extra_tools or [])
            extra = {(t.get("function") or t)["name"] for t in extra_tools or []}
            limit = self.tool_limit(role)
            used = 0

            async def handler(name: str, args: dict, role: dict = role) -> str:
                # The budget is kept here, at the hub's tools, so the role is
                # told before the agent host would stop it: a few calls before
                # the end each result says how many are left, and past it the
                # tools refuse and ask for the answer.
                nonlocal used
                used += 1
                if used > limit:
                    return json.dumps({"ok": False, "error": (
                        f"tool budget spent ({limit} calls): call no more tools; answer now with "
                        "what you have and say what you could not check")})
                if name in extra and extra_handler is not None:
                    out = await extra_handler(role, name, args)
                else:
                    out = await self.run_tool(role, name, args)
                left = limit - used
                if left <= BUDGET_WARN:
                    out += (f"\n[budget: {left} of {limit} tool calls left; "
                            + ("answer now with what you have]" if left == 0 else
                               "answer with what you have soon]"))
                return out

            try:
                res = await home.agents.run(cfg, msgs, tools=tools, handler=handler,
                                            via=via or f"role-{role['id']}",
                                            max_tool_calls=limit + BUDGET_SLACK, role=role)
            except (Unavailable, TurnFailed) as e:
                self.journal(role, "role.turn", "rook", {"role": role["id"]},
                             {"ok": False, "error": str(e)[:300]})
                raise RoleError(str(e)) from None
            if not role.get("host"):
                # The home now exists on that host: the role stays there until
                # someone moves it (role.move), whatever home.agent_host says.
                self.store.update(role["id"], "system:roles", f"home created on {res['worker']}",
                                  host=res["worker"])
                await self.seed(self.role(role["id"]))
        self.journal(role, "role.turn", "rook", {"role": role["id"]},
                     {"ok": True, "result": {"worker": res["worker"], "model": res["model"],
                                             "tools": res["tools_used"]}})
        return {"role": role["id"], "identity": self.identity(role), "answer": res["content"],
                "model": res["model"], "worker": res["worker"], "harness": res["harness"],
                "tools_used": res["tools_used"], "harness_tools": res.get("harness_tools", []),
                "latency_ms": res["latency_ms"], "usage": res.get("usage") or {},
                "cost_usd": res.get("cost_usd")}

    async def seed(self, role: dict) -> int:
        """Write the role's ``seed_memory`` into its new home's memory (once,
        right after its first turn made the home). Failures are logged: the
        role still answers, and the seed stays in its document."""
        if role.get("memory") == "off" or not role.get("seed_memory"):
            return 0
        done = 0
        for text in role["seed_memory"]:
            try:
                await self.host_call(role, "agenthost.role_memory_edit",
                                     {"role": role["id"], "action": "remember", "text": text,
                                      "kind": "fact", "id": "", "by": "system:roles"})
                done += 1
            except Exception:  # noqa: BLE001 - bookkeeping, never fails the turn
                log.exception("roles: seeding %s's memory failed", role["id"])
        return done

    # -- the role's host -------------------------------------------------------------
    async def host_call(self, role: dict, cap: str, args: dict, timeout: float = 60.0) -> dict:
        """A cap on the agent host that keeps the role's home, as hub system code."""
        host = role.get("host")
        if not host:
            raise RoleError(f"role {role['id']} has no home yet (it gets one on its first turn)")
        return await self._host_call(host, cap, args, timeout)

    async def _host_call(self, host: str, cap: str, args: dict, timeout: float = 60.0) -> dict:
        home = self._home()
        found = next((h for h in home.agents.hosts() if host in (h["worker_id"], h["name"])), None)
        if found is None:
            raise RoleError(f"{host} is not an agent host on the band")
        if not found["online"]:
            raise RoleError(f"{host} is offline")
        reply = await self._node.client.call(cap=cap, args=args, target=found["worker_id"],
                                             timeout=timeout, **home.agents._who())
        res = reply.get("result") if isinstance(reply.get("result"), dict) else None
        if res is None or not reply.get("ok", True) or not res.get("ok", True):
            err = (res or {}).get("error") or reply.get("error") or "the agent host refused"
            raise RoleError(f"{found['name']}: {err}")
        return res

    # -- read caps -------------------------------------------------------------------
    @capability("list", risk="read")
    def list_(self) -> dict:
        """Every role (id, name, rev, enabled, host, backend, autonomy)."""
        return {"roles": self.store.list()}

    @capability("get", risk="read")
    def get(self, id: str) -> dict:
        """A role document: name, description, category, charter, concerns,
        checklist, examples, seed_memory, sources, tools, builtin, autonomy,
        memory, backend, model, effort, host, persona, enabled, rev."""
        return {"role": self.role(id), "identity": f"role:{id}"}

    @capability("history", risk="read", limit=20)
    def history(self, id: str = "") -> list[dict]:
        """Attributed changes to one role (or all), newest first."""
        return self.store.history(id, limit=200)

    @capability("presets", risk="read")
    def presets(self, id: str = "", category: str = "", full: bool = False) -> dict:
        """The starting roles, grouped by category (product and planning,
        software engineering, operations, ...): ``categories`` in their sort
        order, each {id, name, description, presets}. A preset is brief (id,
        name, description, autonomy, saved) unless ``full``; ``category``
        narrows to one category; ``id`` returns that one preset as a whole
        role document to save with ``role.set``. Nothing is created by
        reading them."""
        saved = {r["id"] for r in self.store.list()}
        if id:
            doc = model.preset(str(id))
            if doc is None:
                raise RoleError(f"no preset {id!r} (role.presets lists them)")
            return {"preset": doc, "saved": doc["id"] in saved}
        cats = model.grouped(full=bool(full))
        if category:
            cats = [c for c in cats if c["id"] == category]
            if not cats:
                raise RoleError(f"no preset category {category!r}")
        for c in cats:
            c["presets"] = [{**p, "saved": p["id"] in saved} for p in c["presets"]]
        return {"categories": cats}

    @capability("memory", risk="read")
    async def memory(self, id: str, query: str = "", limit: int = 50) -> dict:
        """A role's memory (scope role:<id>), read from its home on its agent
        host: the memories matching ``query``, or the newest."""
        role = self.role(id)
        res = await self.host_call(role, "agenthost.role_memory",
                                   {"role": role["id"], "query": query, "limit": int(limit)})
        return {"role": role["id"], "host": role["host"], "memories": res.get("memories") or [],
                "total": res.get("total")}

    # -- write caps ------------------------------------------------------------------
    @capability("ask", risk="write", timeout=None)
    async def ask(self, id: str, question: str, context: str = "") -> dict:
        """One turn of a role: it answers ``question`` (with ``context``) on its
        agent host, in its home directory, with its tools and its memory.
        Turns of one role run one at a time. Returns {role, answer, model,
        worker, harness, tools_used, latency_ms, usage}."""
        question = str(question or "").strip()
        if not question:
            raise RoleError("ask needs a question")
        return await self.turn(self.role(id), question, str(context or ""), _actor())

    # -- admin caps ------------------------------------------------------------------
    @capability("set", risk="admin")
    def set(self, role: dict, note: str = "", dry_run: bool = False,
            expect_rev: int | None = None) -> dict:
        """Create or replace a role (band owners and operator tokens). ``role``:
        {id, name, description, category, charter, concerns[], checklist[],
        examples[], seed_memory[], sources[{cap, args, worker}],
        tools{allow[], deny[]}, builtin[], autonomy, memory, backend, model,
        effort, host, persona, enabled}. ``host`` is kept from the saved role
        when left out (use ``role.move`` to change it)."""
        if not isinstance(role, dict):
            raise RoleError("role must be an object")
        if not dry_run:
            _admin_gate(f"role.set {role.get('id')}")
        old = self.store.get(str(role.get("id") or ""))
        if old is not None and "host" not in role:
            role = {**role, "host": old.get("host", "")}
        return self.store.save(role, _actor(), note, expect_rev, dry_run)

    @capability("delete", risk="admin")
    def delete(self, id: str, note: str = "") -> dict:
        """Delete a role (its history stays; its home on the agent host is left
        in place)."""
        _admin_gate(f"role.delete {id}")
        return self.store.delete(id, _actor(), note)

    @capability("memory_edit", risk="admin")
    async def memory_edit(self, id: str, action: str, text: str = "", kind: str = "fact",
                          memory_id: str = "") -> dict:
        """Change a role's memory on its host: ``action`` remember (``text``,
        ``kind`` fact, procedure or episode) or forget (``memory_id``). The
        memory write rules apply (no secrets, no code)."""
        _admin_gate(f"role.memory_edit {id}")
        role = self.role(id)
        if action not in ("remember", "forget"):
            raise RoleError("action: remember or forget")
        return await self.host_call(role, "agenthost.role_memory_edit",
                                    {"role": role["id"], "action": action, "text": text,
                                     "kind": kind, "id": memory_id, "by": _actor()})

    @capability("move", risk="admin", timeout=None)
    async def move(self, id: str, host: str, note: str = "") -> dict:
        """Move a role to another agent host: its home directory (memory and
        config) is packed on the old host, unpacked on ``host``, and the old
        copy is set aside there (renamed, never deleted). A role without a
        home yet just gets the new host."""
        _admin_gate(f"role.move {id}")
        host = str(host or "").strip()
        if not host:
            raise RoleError("move needs a host")
        async with self._lock(str(id)):
            role = self.role(id)
            src = role.get("host") or ""
            hosts = {h["name"]: h for h in self._home().agents.hosts()}
            hosts.update({h["worker_id"]: h for h in list(hosts.values())})
            dst = hosts.get(host)
            if dst is None:
                raise RoleError(f"{host} is not an agent host on the band")
            if src and src in (dst["name"], dst["worker_id"]):
                return {"ok": True, "id": role["id"], "host": dst["name"], "moved": False}
            out: dict = {"ok": True, "id": role["id"], "from": src or None, "host": dst["name"]}
            if src:
                pack = await self.host_call(role, "agenthost.role_export", {"role": role["id"]},
                                            timeout=120.0)
                got = await self._host_call(dst["name"], "agenthost.role_import",
                                            {"role": role["id"], "archive": pack["archive"],
                                             "sha256": pack["sha256"]}, timeout=120.0)
                out.update(bytes=pack.get("bytes"), memories=got.get("memories"))
                try:
                    aside = await self.host_call(role, "agenthost.role_set_aside",
                                                 {"role": role["id"]})
                    out["old_home"] = aside.get("set_aside")
                except RoleError as e:
                    out["warning"] = f"the old home was left in place: {e}"
            self.store.update(role["id"], _actor(), note or f"moved to {dst['name']}",
                              host=dst["name"])
            if not src:
                # No home yet: its first turn will not be the one that makes it,
                # so the starting memory goes there now (remembering makes the home).
                seeded = await self.seed(self.role(role["id"]))
                if seeded:
                    out["seeded"] = seeded
        return out


def _mask(reply: dict) -> dict:
    """Vault values in a reply become ``{{secret:name}}`` before the model sees them."""
    try:
        from ....band_mcp.secret_mask import scrub
        out = scrub(reply)
        return out if isinstance(out, dict) else reply
    except Exception:  # noqa: BLE001 - no masker on this hub
        return reply


PLUGIN = RolesPlugin
