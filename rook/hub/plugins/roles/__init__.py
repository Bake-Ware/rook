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
  ``role.session_start`` (a coding session for a role, on a branch: what an
  approved card runs for a role that only proposes), ``role.session_stop``;
* read: ``role.workspace`` (a role's workspace files, read-only),
  ``role.sessions`` (the coding sessions roles started);
* admin: ``role.set``, ``role.delete``, ``role.memory_edit``, ``role.move``
  (moves the role's home to another agent host).

What a role may do without a person is the hub's call, per cap
(:mod:`.tiers`): reads in its scope and its own workspace always; starting,
following and steering a coding session on a branch when its autonomy is
``routine`` or ``act``; everything else becomes an action card.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import logging
import time
import uuid
from typing import Any

from ....core import context
from ....core.plugin import Plugin, capability, place, setting
from ....core.text import clip
from . import model, sessions as rs, tiers, workspace as ws
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
#: Following a role's coding session: how long one terminal read waits, and
#: how many WORKING lines a session posts at most (the rest only update it).
FOLLOW_WAIT = 20.0
STATUS_POSTS = 4


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
                                  "within your scope. Reads run at once; anything else becomes an "
                                  "action card the person approves (the reply says so: do not "
                                  "retry it). why: one line, the card's summary.",
                     {"worker": {"type": "string"}, "cap": {"type": "string"},
                      "args": {"type": "object"}, "why": {"type": "string"}}, ["worker", "cap"]),
    "start_session": _fn(
        "start_session",
        "Start a Claude Code session to do coding or document work for you, on its own git "
        "branch in a worktree of one of the code locations you were given (never the main "
        "branch). It commits, pushes that branch and opens a pull request; it never merges. "
        "No approval is needed. Its progress and its DONE/BLOCKED report come back to you (and "
        "the huddle) on their own: do not poll it. instructions: everything it needs (it sees "
        "only these, plus your workspace folder, whose files you can name).",
        {"location": {"type": "string", "description": "the code location's name (blank: the "
                                                         "only one)"},
         "branch": {"type": "string", "description": "a new branch name, e.g. design-h6"},
         "instructions": {"type": "string"}, "title": {"type": "string"}},
        ["branch", "instructions"]),
    "session_status": _fn("session_status", "Your coding sessions: state, newest status, pull "
                                            "request, and (with id) its latest messages.",
                          {"id": {"type": "string"}}),
    "session_send": _fn("session_send", "Give one of your stopped sessions more work (it resumes "
                                        "with its history, on the same branch). A running "
                                        "session cannot be interrupted: wait for its report.",
                        {"id": {"type": "string"}, "text": {"type": "string"}},
                        ["id", "text"]),
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
                     "one of them when any are set. A role's own coding sessions start only "
                     "here: name first, then \"worker <name>\", the checkout's folder, "
                     "\"worktrees go under <folder>\" and the base (origin/master)."),
        setting("protected_workers", list, [], label="Protected devices",
                help="Workers (by name) where every call a role makes, reads included, needs a "
                     "person's approval card: family phones and tablets, the TVs. Roles never "
                     "act on these on their own, whatever their autonomy."),
        setting("session_model", str, "", label="Role session model",
                help="The Claude Code model a role's coding session runs on (sonnet, opus or an "
                     "id). Blank: Claude Code's default on that host."),
        setting("session_stall_minutes", int, 20, min=2, max=600, label="Session quiet alarm",
                help="A role's coding session with no output for this many minutes is reported "
                     "to the role and the huddle as stalled (once)."),
        setting("session_max_minutes", int, 180, min=5, max=1440, label="Session time limit",
                help="A role's coding session still running after this many minutes is stopped "
                     "and reported as blocked."),
        setting("sessions_per_role", int, 2, min=1, max=10, label="Live sessions per role",
                help="How many coding sessions one role may have running at once."),
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
        self._sessions: rs.SessionStore | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._homes: dict[str, str] = {}          # agent host -> its roles dir
        self._follows: dict[str, asyncio.Task] = {}
        self._waking: set[asyncio.Task] = set()

    # -- wiring ----------------------------------------------------------------
    def bind_host(self, node) -> None:
        self._node = node

    @property
    def store(self) -> model.RoleStore:
        if self._store is None:
            self._store = model.RoleStore(self.data_dir / "roles.db", self.migrate)
        return self._store

    @property
    def sessions(self) -> rs.SessionStore:
        if self._sessions is None:
            from ....core import names
            with self.store._lock, self.store.db:
                names.ensure_table(self.store.db)
            self._sessions = rs.SessionStore(self.store.db, self.store._lock)
        return self._sessions

    async def start(self) -> None:
        try:
            self.store
        except Exception:
            log.exception("role store unavailable")
            return
        # Sessions that were running when the hub stopped: follow them again
        # from where the hub left off (their terminals live on their hosts).
        with contextlib.suppress(Exception):
            for rec in self.sessions.list(live=True, limit=100):
                self._follow(rec["id"])

    async def stop(self) -> None:
        tasks = list(self._follows.values()) + list(self._waking)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._follows.clear()

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
        out = [TOOLS[n] for n in names]
        if not role.get("workspace_native"):
            out += list(ws.TOOLS.values())
        if tiers.is_routine(role):
            out += [TOOLS["start_session"], TOOLS["session_status"], TOOLS["session_send"]]
        return out

    def protected(self) -> list[str]:
        raw = self.settings.get("protected_workers") or []
        if isinstance(raw, str):
            raw = raw.replace(",", "\n").splitlines()
        return [str(x).strip() for x in raw if str(x).strip()]

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
        """Why ``cap`` is not the role's to call at all ("" when it is in
        scope: its tier then says whether it runs or becomes a card)."""
        if any(fnmatch.fnmatchcase(cap, p) for p in NEVER):
            return f"roles never call {cap}"
        if not model.in_scope(role, cap):
            return f"{cap} is outside role {role['id']}'s tools"
        return ""

    def cap_tier(self, role: dict, cap: str, entry: dict | None, worker: str = "") -> tuple[str, str]:
        """``(tier, why)``: free (runs), or approval (a card); see :mod:`.tiers`."""
        return tiers.cap_tier(role, cap, self._tier(cap, entry), worker, self.protected())

    def runs_now(self, role: dict, cap: str, entry: dict | None, worker: str = "") -> bool:
        """In scope and free: the role may call it without a person."""
        return not self.scope_refusal(role, cap, entry) and \
            self.cap_tier(role, cap, entry, worker)[0] == tiers.FREE

    async def role_workers(self, role: dict) -> dict:
        out = []
        for wid, e in sorted(self._roster().items(),
                             key=lambda kv: str(kv[1].get("name") or kv[0])):
            name = "rook" if wid == getattr(self._node, "worker_id", None) else \
                str(e.get("name") or wid[:8])
            caps = [c for c in e.get("caps") or [] if not self.scope_refusal(role, c, e)]
            now = [c for c in caps if self.cap_tier(role, c, e, name)[0] == tiers.FREE]
            ask = [c for c in caps if c not in now]
            if caps:
                row: dict = {"name": name, "caps": now[:200]}
                if ask:
                    row["needs_approval"] = ask[:100]
                out.append(row)
        return {"workers": out, "note": "caps runs at once; needs_approval becomes a card the "
                                        "person approves when you call it"}

    async def role_call(self, role: dict, worker: str, cap: str, args: Any, *, why: str = "",
                        approval: Any = None) -> dict:
        """One ``rook_call`` from a role turn: the role's scope, its tier
        (:mod:`.tiers`: free calls run, anything else becomes an action card
        through ``approval(card)`` when the turn has one, else is refused),
        then the band's policy for the role's principal, then the call,
        journaled as the role."""
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
        refusal = self.scope_refusal(role, cap, entry)
        if not refusal:
            tier, need = self.cap_tier(role, cap, entry, name)
            if tier != tiers.FREE:
                return await self._needs_approval(role, need, approval, {
                    "kind": "call", "summary": why or f"@{role['id']} asks to run {cap} on {name}",
                    "worker": name, "cap": cap, "args": args})
            refusal = self._policy(role, cap, wid, entry)
        if refusal:
            reply = {"ok": False, "denied": refusal, "error": refusal}
            self.journal(role, cap, name, args, reply)
            return {"ok": False, "error": refusal}
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

    async def _needs_approval(self, role: dict, why: str, approval: Any, card: dict) -> dict:
        """A call above the role's tier: an action card when the turn can
        make one (a huddle), else a refusal that says so."""
        self.journal(role, card.get("cap") or card["kind"], card.get("worker") or "rook",
                     card.get("args") or {}, {"ok": False, "held": True, "error": why})
        if approval is None:
            return {"ok": False, "needs_approval": True,
                    "error": f"{why}. Ask the person for it (in a huddle it becomes a card)."}
        try:
            made = await approval(card)
        except Exception as e:  # noqa: BLE001 - goes back to the model
            made = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        if not made.get("ok"):
            return {"ok": False, "needs_approval": True,
                    "error": f"{why}; the card was not made: {made.get('error')}"}
        return {"ok": False, "needs_approval": True, "card": made.get("card"),
                "card_name": made.get("name"), "state": made.get("state"),
                "note": f"{why}. It is now card {made.get('name') or made.get('card')}, waiting for "
                        "the person. Do not call it again; carry on, and mention the card."}

    async def run_tool(self, role: dict, name: str, args: dict, *, room: str = "",
                       approval: Any = None) -> str:
        args = args if isinstance(args, dict) else {}
        try:
            if name == "knowledge_search":
                out: Any = await self._knowledge(role, args)
            elif name == "rook_workers":
                out = await self.role_workers(role)
            elif name == "rook_call":
                out = await self.role_call(role, args.get("worker"), args.get("cap"),
                                           args.get("args"), why=str(args.get("why") or "")[:300],
                                           approval=approval)
            elif name in ws.TOOLS:
                out = await self.workspace_tool(role, name, args)
            elif name in ("start_session", "session_status", "session_send"):
                out = await self.session_tool(role, name, args, room=room, approval=approval)
            else:
                out = {"ok": False, "error": f"unknown tool {name!r}"}
        except (RoleError, ws.WorkspaceError, rs.SessionError) as e:
            out = {"ok": False, "error": str(e)}
        limit = int(self.settings.get("tool_result_chars") or TOOL_RESULT_CHARS)
        return clip(json.dumps(out, default=str), limit)

    # -- the role's workspace (docs/design/huddles.md 4.5) ----------------------------
    def _host_of(self, role: dict) -> str:
        """The agent host that keeps (or, before its first turn, will keep)
        the role's home."""
        host = role.get("host") or ""
        if not host:
            with contextlib.suppress(Exception):
                host = str(self._home().config().get("agent_host") or "")
        if not host:
            raise RoleError(f"role {role['id']} has no agent host yet (home.agent_host)")
        return host

    async def _workspace(self, role: dict) -> ws.Workspace:
        host = self._host_of(role)
        base = self._homes.get(host)
        if not base:
            got = await self._host_call(host, "agenthost.roles", {})
            base = str(got.get("base") or "")
            if not base.startswith("/"):
                raise RoleError(f"{host} did not say where role homes live")
            self._homes[host] = base

        async def call(cap: str, args: dict) -> dict:
            return await self._host_call(host, cap, args)
        return ws.Workspace(call, f"{base.rstrip('/')}/{role['id']}")

    async def workspace_tool(self, role: dict, name: str, args: dict) -> dict:
        """One workspace tool call: the hub checks the path, then the host's
        own file caps do it. Journaled as the role (``role.workspace``)."""
        try:
            space = await self._workspace(role)
            out = await space.run(name, args)
        except (ws.WorkspaceError, RoleError) as e:
            out = {"ok": False, "error": str(e)}
        brief = {k: v for k, v in args.items() if k in ("path", "pattern", "append")}
        self.journal(role, "role.workspace", "rook", {"tool": name, **brief},
                     {"ok": bool(out.get("ok")), **({"error": out["error"]} if not out.get("ok")
                                                    else {})})
        return out

    # -- coding sessions a role starts (docs/design/huddles.md 4.5, 5.5) -----------------
    async def session_tool(self, role: dict, name: str, args: dict, *, room: str = "",
                           approval: Any = None) -> dict:
        tier, why = tiers.tool_tier(role, name)
        if name == "session_status":
            return self._status_of(role, str(args.get("id") or ""))
        if name == "start_session":
            call = {"role": role["id"], "location": str(args.get("location") or ""),
                    "branch": str(args.get("branch") or ""),
                    "instructions": str(args.get("instructions") or ""),
                    "title": str(args.get("title") or ""), "room": room}
            if tier != tiers.ROUTINE:
                return await self._needs_approval(role, why, approval, {
                    "kind": "session.start", "worker": "rook", "cap": "role.session_start",
                    "args": call, "summary": f"@{role['id']}: start a coding session on branch "
                                             f"{call['branch']}: {call['title'] or call['instructions'][:120]}"})
            return await self.start_session(role, **{k: v for k, v in call.items() if k != "role"},
                                            actor=self.identity(role))
        if name == "session_send":
            if tier != tiers.ROUTINE:
                return await self._needs_approval(role, why, approval, {
                    "kind": "session.send", "worker": "rook", "cap": "role.session_send",
                    "args": {"id": str(args.get("id") or ""), "text": str(args.get("text") or "")},
                    "summary": f"@{role['id']}: more work for session {args.get('id')}"})
            return await self.resume_session(role, str(args.get("id") or ""),
                                             str(args.get("text") or ""), room=room)
        return {"ok": False, "error": f"unknown tool {name!r}"}

    def _session(self, ref: str, role: dict | None = None) -> dict:
        from ....core import names
        ref = str(ref or "").strip()
        rec = self.sessions.get(ref)
        if rec is None and ref:
            with contextlib.suppress(Exception):
                hits = names.find(self.store.db, "session", ref)
                rec = self.sessions.get(hits[0]) if hits else None
        if rec is None:
            raise rs.SessionError(f"no session {ref!r}" + (" (session_status lists yours)"
                                                           if role else ""))
        if role is not None and rec["role"] != role["id"]:
            raise rs.SessionError(f"session {ref} is @{rec['role']}'s, not yours")
        return rec

    def _named(self, rec: dict) -> dict:
        from ....core import names
        out = {k: rec.get(k) for k in ("id", "role", "room", "worker", "branch", "worktree",
                                        "title", "state", "status", "pr_url", "native_id",
                                        "started", "ended", "runs")}
        with contextlib.suppress(Exception):
            out["name"] = names.name_of(self.store.db, "session", rec["id"])
        data = rec.get("data") or {}
        for k in ("denied", "cost_usd", "tools"):
            if data.get(k):
                out[k] = data[k]
        return out

    def _status_of(self, role: dict, ref: str) -> dict:
        if not ref:
            return {"ok": True, "sessions": [self._named(r) for r in
                                             self.sessions.list(role=role["id"], limit=10)]}
        rec = self._session(ref, role)
        out = self._named(rec)
        out["latest"] = (rec.get("data") or {}).get("texts", [])[-3:]
        return {"ok": True, "session": out}

    async def start_session(self, role: dict, *, location: str = "", branch: str = "",
                            instructions: str = "", title: str = "", room: str = "",
                            actor: str = "") -> dict:
        """Start a Claude Code session for ``role`` on ``branch`` in a worktree
        of a code location, and follow it. The rules are the hub's
        (:mod:`.sessions`); this is the routine path and what an approved
        ``role.session_start`` card runs."""
        instructions = str(instructions or "").strip()
        if not instructions:
            raise rs.SessionError("instructions: what the session should do (it sees only these)")
        if len(instructions) > rs.INSTRUCTIONS_MAX:
            raise rs.SessionError(f"instructions: at most {rs.INSTRUCTIONS_MAX} characters; put "
                                  "the long part in a workspace file and name it")
        loc = rs.pick_location(rs.locations(self.code_locations()), location)
        branch = rs.check_branch(branch, loc["base"])
        if loc["worker"].lower() in {p.lower() for p in self.protected()}:
            raise rs.SessionError(f"{loc['worker']} is a protected device")
        live = self.sessions.list(role=role["id"], live=True)
        cap = int(self.settings.get("sessions_per_role") or 2)
        if len(live) >= cap:
            raise rs.SessionError(f"you already have {len(live)} sessions running (at most {cap}); "
                                  "wait for one to report")
        wt = rs.worktree_path(loc, branch)
        busy = self.sessions.live_on(loc["worker"], wt)
        if busy:
            raise rs.SessionError(f"session {busy['id']} (@{busy['role']}) is already working in "
                                  f"{wt}; wait for its report or pick another branch")
        space = ""
        with contextlib.suppress(Exception):
            space = (await self._workspace(role)).root
        sid, native = rs.new_id(), rs.new_native_id()
        huddle = ""
        if room:
            hp = self._node.plugin("huddle")
            with contextlib.suppress(Exception):
                huddle = hp.service.store.name("room", room) or room
        prompt = rs.session_prompt(role, loc, branch, wt, instructions, space, huddle)
        settings = rs.session_settings(wt, branch, space)
        title = (title or f"@{role['id']}: {branch}").strip()
        args = rs.launch_args(loc, branch, wt, native, prompt, settings, title,
                              model=str(self.settings.get("session_model") or ""))
        rec = self.sessions.add({"id": sid, "role": role["id"], "room": room or "",
                                 "worker": loc["worker"], "native_id": native, "repo": loc["repo"],
                                 "branch": branch, "worktree": wt, "title": title,
                                 "instructions": instructions, "state": "starting",
                                 "data": {"by": actor or self.identity(role),
                                          "location": loc["name"]}})
        from ....core import names
        with contextlib.suppress(Exception):
            with self.store._lock, self.store.db:
                names.assign(self.store.db, "session", sid)
        try:
            opened = await self._open_terminal(role, loc["worker"], args)
        except Exception as e:  # noqa: BLE001 - the record says why
            self.sessions.set(sid, state="failed", status=f"could not start: {e}"[:500],
                              ended=time.time())
            raise rs.SessionError(f"could not start the session on {loc['worker']}: {e}") from None
        self.sessions.set(sid, terminal=opened, state="running", last_output=time.time())
        self.journal(role, "role.session_start", "rook",
                     {"session": sid, "worker": loc["worker"], "branch": branch, "worktree": wt},
                     {"ok": True, "result": {"terminal": opened}})
        self._follow(sid)
        rec = self.sessions.get(sid) or rec
        if room:
            self._post(rec, f"Started a coding session for @{role['id']} on {loc['worker']}: branch "
                            f"{branch} in {wt}. It reports back here.", quiet=True)
        return {"ok": True, **self._named(rec),
                "note": "Started. It reports progress and DONE/BLOCKED back to you and the huddle "
                        "on its own; do not poll it."}

    async def _open_terminal(self, role: dict, worker: str, args: dict) -> str:
        """``work.stream.open`` on ``worker`` as hub system code for the role
        (the call is fixed by the hub: a launcher, not the role's choice)."""
        from ..jobs.kinds import resolve_worker
        roster = self._roster()
        found = resolve_worker(roster, worker, "work.stream.open",
                               getattr(self._node, "worker_id", None))
        if not found:
            raise RoleError(f"{worker} is not on the band with work.stream.open")
        with self._as_role(role):
            reply = await self._node.client.call(cap="work.stream.open", args=args,
                                                 target=found[0], timeout=40.0,
                                                 identity=self.identity(role),
                                                 principal=self.principal(role))
        res = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        if not reply.get("ok", True) or not res.get("id"):
            raise RoleError(str(reply.get("error") or res.get("error") or "no terminal id")[:300])
        return str(res["id"])

    async def resume_session(self, role: dict, ref: str, text: str, room: str = "") -> dict:
        """More work for one of the role's stopped sessions: Claude Code
        resumes it (same id, same branch and worktree) with ``text``."""
        rec = self._session(ref, role)
        text = str(text or "").strip()
        if not text:
            raise rs.SessionError("text: what the session should do next")
        if rec["state"] in ("starting", "running"):
            raise rs.SessionError("that session is still running; wait for its report")
        loc = {"name": (rec.get("data") or {}).get("location") or "", "worker": rec["worker"],
               "repo": rec["repo"], "worktrees": "", "base": "origin/master"}
        for l in rs.locations(self.code_locations()):
            if l["repo"] == rec["repo"] and l["worker"] == rec["worker"]:
                loc = l
        space = ""
        with contextlib.suppress(Exception):
            space = (await self._workspace(role)).root
        prompt = rs.session_prompt(role, loc, rec["branch"], rec["worktree"], text, space)
        settings = rs.session_settings(rec["worktree"], rec["branch"], space)
        args = rs.launch_args(loc, rec["branch"], rec["worktree"], rec["native_id"], prompt,
                              settings, rec.get("title") or rec["branch"], resume=rec["native_id"],
                              model=str(self.settings.get("session_model") or ""))
        data = dict(rec.get("data") or {})
        data.pop("reported", None)
        data.pop("stalled", None)
        opened = await self._open_terminal(role, rec["worker"], args)
        self.sessions.set(rec["id"], terminal=opened, state="running", cursor=0, ended=None,
                          runs=int(rec.get("runs") or 1) + 1, last_output=time.time(),
                          room=room or rec.get("room") or "", data=data,
                          status="resumed: " + text[:200])
        self.journal(role, "role.session_send", "rook", {"session": rec["id"]},
                     {"ok": True, "result": {"terminal": opened}})
        self._follow(rec["id"])
        return {"ok": True, **self._named(self.sessions.get(rec["id"]) or rec),
                "note": "Resumed. It reports back on its own."}

    # -- following ------------------------------------------------------------------
    def _follow(self, sid: str) -> None:
        old = self._follows.get(sid)
        if old is not None and not old.done():
            old.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._follow_loop(sid))
        self._follows[sid] = task
        task.add_done_callback(lambda t, sid=sid: self._follows.pop(sid, None)
                               if self._follows.get(sid) is t else None)

    async def _read(self, rec: dict, wait: float) -> dict:
        from ..jobs.kinds import resolve_worker
        found = resolve_worker(self._roster(), rec["worker"], "work.stream.read",
                               getattr(self._node, "worker_id", None))
        if not found:
            return {"ok": False, "error": f"{rec['worker']} is not on the band", "gone": False}
        reply = await self._node.client.call(cap="work.stream.read",
                                             args={"id": rec["terminal"], "cursor": rec["cursor"],
                                                   "wait": wait, "max_bytes": 262144},
                                             target=found[0], timeout=wait + 15.0,
                                             **self._home().agents._who())
        res = reply.get("result") if isinstance(reply.get("result"), dict) else None
        if res is None or not reply.get("ok", True):
            err = str(reply.get("error") or (res or {}).get("error") or "read failed")
            return {"ok": False, "error": err,
                    "gone": "no such terminal" in err.lower() or "unknown terminal" in err.lower()}
        return res

    async def _follow_loop(self, sid: str) -> None:
        """Follow one session to its end: post its status lines quietly, and
        report DONE, BLOCKED and anything unexpected to the role and the
        huddle. No job: the hub does it."""
        follow = rs.Follow()
        misses = 0
        try:
            while True:
                rec = self.sessions.get(sid)
                if rec is None or rec["state"] not in ("starting", "running"):
                    return
                if not rec.get("terminal"):
                    await self._finish(rec, follow, "failed", "UNEXPECTED",
                                       "it never got a terminal (the hub stopped while starting it)")
                    return
                now = time.time()
                limit = 60 * int(self.settings.get("session_max_minutes") or 180)
                if rec.get("started") and now - rec["started"] > limit * int(rec.get("runs") or 1):
                    await self._stop_terminal(rec)
                    await self._finish(rec, follow, "blocked", "BLOCKED",
                                       f"stopped: still running after {limit // 60} minutes")
                    return
                try:
                    res = await self._read(rec, FOLLOW_WAIT)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - the host may be busy
                    res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                if not res.get("ok", True) or "next" not in res:
                    misses += 1
                    if res.get("gone") or misses >= 30:
                        await self._finish(rec, follow, "failed", "UNEXPECTED",
                                           "its terminal on " + rec["worker"] + " is gone ("
                                           + str(res.get("error"))[:200] + "); its host may have "
                                           "restarted")
                        return
                    await asyncio.sleep(min(60.0, 5.0 * misses))
                    continue
                misses = 0
                chunk = rs.decode(str(res.get("enc") or "t"), str(res.get("data") or ""))
                fields: dict = {"cursor": int(res.get("next") or rec["cursor"])}
                if chunk:
                    fields["last_output"] = now
                    events = follow.feed(chunk)
                    if follow.native_id and follow.native_id != rec.get("native_id"):
                        fields["native_id"] = follow.native_id
                    if follow.pr_urls:
                        fields["pr_url"] = follow.pr_urls[-1]
                    data = dict(rec.get("data") or {})
                    data.update(texts=follow.texts[-4:], tools=follow.tools)
                    fields["data"] = data
                    self.sessions.set(sid, **fields)
                    rec = self.sessions.get(sid) or rec
                    for word, text in events:
                        await self._on_status(rec, follow, word, text)
                else:
                    self.sessions.set(sid, **fields)
                    stall = 60 * int(self.settings.get("session_stall_minutes") or 20)
                    data = dict(rec.get("data") or {})
                    quiet = now - float(rec.get("last_output") or now)
                    if res.get("running", True) and quiet > stall and not data.get("stalled"):
                        data["stalled"] = now
                        self.sessions.set(sid, data=data)
                        await self._report(rec, "UNEXPECTED",
                                           f"no output for {int(quiet // 60)} minutes; it may be "
                                           "stuck (it is still running)")
                if not res.get("running", True) and res.get("eof", True):
                    rec = self.sessions.get(sid) or rec
                    word, text = follow.report(res.get("exit_code"))
                    data = dict(rec.get("data") or {})
                    data.update(denied=follow.denied[:20], cost_usd=follow.cost,
                                raw=follow.raw[-6:])
                    self.sessions.set(sid, data=data)
                    state = {"DONE": "done", "BLOCKED": "blocked", "QUESTION": "blocked"}.get(
                        word, "failed")
                    await self._finish(self.sessions.get(sid) or rec, follow, state, word, text)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let a follower die silently
            log.exception("roles: following session %s failed", sid)
            with contextlib.suppress(Exception):
                rec = self.sessions.get(sid)
                if rec is not None:
                    await self._finish(rec, follow, "failed", "UNEXPECTED",
                                       "the hub stopped following it after an error; check it "
                                       "with session_status")

    async def _on_status(self, rec: dict, follow: rs.Follow, word: str, text: str) -> None:
        """A status line while it runs: WORKING is posted quietly (a few at
        most); DONE, BLOCKED and QUESTION are kept for the one report the run
        sends when it ends."""
        if word != "WORKING":
            self.sessions.set(rec["id"], status=f"{word.lower()}: {text}"[:500])
            return
        data = dict(rec.get("data") or {})
        self.sessions.set(rec["id"], status=("working: " + text)[:500])
        n = int(data.get("posts") or 0)
        if n < STATUS_POSTS and rec.get("room"):
            data["posts"] = n + 1
            self.sessions.set(rec["id"], data=data)
            self._post(rec, f"[{rec['branch']}] working: {text[:300]}", quiet=True)

    async def _finish(self, rec: dict, follow: rs.Follow, state: str, word: str, text: str) -> None:
        self.sessions.set(rec["id"], state=state, ended=time.time(),
                          status=f"{word.lower()}: {text}"[:500])
        await self._report(self.sessions.get(rec["id"]) or rec, word, text)

    async def _stop_terminal(self, rec: dict) -> None:
        from ..jobs.kinds import resolve_worker
        with contextlib.suppress(Exception):
            found = resolve_worker(self._roster(), rec["worker"], "work.stream.close",
                                   getattr(self._node, "worker_id", None))
            if found:
                await self._node.client.call(cap="work.stream.close", args={"id": rec["terminal"]},
                                             target=found[0], timeout=20.0,
                                             **self._home().agents._who())

    def _sender(self, rec: dict) -> str:
        from ....core import names
        name = ""
        with contextlib.suppress(Exception):
            name = names.name_of(self.store.db, "session", rec["id"]) or ""
        return f"session:{name or rec['id']}"

    def _post(self, rec: dict, text: str, quiet: bool = True) -> None:
        hp = self._node.plugin("huddle") if self._node is not None else None
        svc = getattr(hp, "service", None)
        if svc is None or not rec.get("room"):
            return
        with contextlib.suppress(Exception):
            svc._send(rec["room"], self._sender(rec), text, [f"role:{rec['role']}"], quiet=quiet)

    async def _report(self, rec: dict, word: str, text: str) -> None:
        """DONE, BLOCKED, QUESTION or UNEXPECTED: posted in the huddle the
        session belongs to, and the role is woken (through the chair, which
        may also call on roles that should review the result). Without a
        huddle the role gets one turn with the report. Each report goes out
        once per run."""
        data = dict(rec.get("data") or {})
        key = f"{rec.get('runs') or 1}:{word}:{text[:80]}"
        done = list(data.get("reported") or [])
        if key in done:
            return
        data["reported"] = (done + [key])[-20:]
        self.sessions.set(rec["id"], data=data)
        pr = f" Pull request: {rec['pr_url']}." if rec.get("pr_url") and rec["pr_url"] not in text \
            else ""
        body = (f"@{rec['role']} your session on {rec['branch']} reports {word}: {text}{pr}"
                f" (session {self._sender(rec).split(':', 1)[1]}, {rec['worker']}:{rec['worktree']})")
        hp = self._node.plugin("huddle") if self._node is not None else None
        svc = getattr(hp, "service", None)
        if svc is not None and rec.get("room"):
            try:
                svc.session_report(rec["room"], rec["role"], self._sender(rec), body, word)
                return
            except Exception:  # noqa: BLE001 - fall back to waking the role alone
                log.exception("roles: reporting session %s in its huddle failed", rec["id"])
        task = asyncio.get_running_loop().create_task(self._wake(rec, body))
        self._waking.add(task)
        task.add_done_callback(self._waking.discard)

    async def _wake(self, rec: dict, body: str) -> None:
        with contextlib.suppress(Exception):
            role = self.role(rec["role"])
            res = await self.turn(role, body, asker=self._sender(rec), via=f"session-{role['id']}")
            data = dict((self.sessions.get(rec["id"]) or rec).get("data") or {})
            data["answer"] = str(res.get("answer") or "")[:2000]
            self.sessions.set(rec["id"], data=data)

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
        lines.append(WORKSPACE_TEXT)
        if tiers.is_routine(role):
            lines.append(ROUTINE_TEXT)
        else:
            lines.append("You read and you propose; you do not act. When something should be "
                         "done, say exactly what (which cap on which worker, with what) so the "
                         "person can approve it.")
        lines.append(TIER_TEXT)
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

    def workspace_native(self, role: dict, cfg: dict) -> bool:
        """The role's host runs its workspace natively (Claude Code's own file
        tools, scoped by its home's settings): the host announces it."""
        from ..home import agent_host
        if agent_host.harness_of(cfg) != "claude":
            return False
        with contextlib.suppress(Exception):
            host = self._host_of(role)
            for h in self._home().agents.hosts():
                if host in (h["worker_id"], h["name"]):
                    return h.get("workspace") == "native"
        return False

    async def turn(self, role: dict, question: str, context_text: str = "",
                   asker: str = "", *, extra_tools: list[dict] | None = None,
                   extra_handler: Any = None, guide: str = "", via: str = "",
                   preread: bool = False, room: str = "", approval: Any = None) -> dict:
        """One turn of ``role``. ``extra_tools`` (with ``extra_handler(role,
        name, args) -> str``) are offered next to the role's own (a huddle's
        ``propose``); ``guide`` is appended to the system prompt; ``preread``
        reads the role's sources first and adds them to the context.
        ``room``: the huddle the turn is for (a session it starts reports
        there); ``approval(card) -> {ok, card}``: makes an action card for a
        call above the role's tier (without it such calls are refused)."""
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
            if self.workspace_native(role, cfg):
                role = {**role, "workspace_native": True}
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
                    out = await self.run_tool(role, name, args, room=room, approval=approval)
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

    @capability("workspace", risk="read")
    async def workspace(self, id: str, path: str = "") -> dict:
        """A role's workspace, read-only (its drafts, notes and working
        documents, in its home on its agent host): the file list, or with
        ``path`` one file's content."""
        role = self.role(id)
        space = await self._workspace(role)
        try:
            if path and not str(path).endswith("/"):
                return {"role": role["id"], "host": self._host_of(role), **await space.read(path)}
            return {"role": role["id"], "host": self._host_of(role),
                    **await space.list(str(path or "").rstrip("/"))}
        except ws.WorkspaceError as e:
            raise RoleError(str(e)) from None

    @capability("sessions", risk="read")
    def sessions_(self, role: str = "", id: str = "", live: bool = False, limit: int = 20) -> dict:
        """The coding sessions roles started: state, newest status, branch,
        worktree, pull request (``role`` narrows, ``live`` only running ones;
        ``id`` one session with its latest messages)."""
        if id:
            rec = self._session(id)
            out = self._named(rec)
            out["latest"] = (rec.get("data") or {}).get("texts", [])[-3:]
            out["instructions"] = rec.get("instructions")
            return {"session": out}
        return {"sessions": [self._named(r) for r in
                             self.sessions.list(role=role, live=bool(live), limit=int(limit))]}

    # -- write caps ------------------------------------------------------------------
    @capability("session_start", risk="write", timeout=120)
    async def session_start(self, role: str, branch: str, instructions: str, location: str = "",
                            title: str = "", room: str = "") -> dict:
        """Start a coding session for a role (on a branch, in a worktree of a
        code location; it may open a pull request, never merge). Roles with
        routine autonomy do this themselves; for the others it is what an
        approved card runs."""
        return await self.start_session(self.role(role), location=location, branch=branch,
                                        instructions=instructions, title=title, room=room,
                                        actor=_actor())

    @capability("session_send", risk="write", timeout=120)
    async def session_send(self, id: str, text: str) -> dict:
        """More work for a stopped role session (it resumes on its branch)."""
        rec = self._session(id)
        return await self.resume_session(self.role(rec["role"]), rec["id"], text)

    @capability("session_stop", risk="write")
    async def session_stop(self, id: str) -> dict:
        """Stop a role's running coding session (its terminal is closed; its
        worktree and branch stay)."""
        rec = self._session(id)
        if rec["state"] in ("starting", "running"):
            await self._stop_terminal(rec)
            self.sessions.set(rec["id"], state="stopped", ended=time.time(),
                              status=f"stopped by {_actor()}")
            task = self._follows.get(rec["id"])
            if task is not None:
                task.cancel()
        return {"ok": True, **self._named(self.sessions.get(rec["id"]) or rec)}

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


WORKSPACE_TEXT = (
    "Your workspace is your own folder for drafts, notes, scratch files and working documents "
    "(workspace/ in your home; the workspace_* tools, or your file tools where you have them). "
    "Read, write, list, search and delete there freely: no approval. Keep notes and drafts "
    "there; tasks are for work items only, never a notepad. Nothing outside it is yours to "
    "write.")
ROUTINE_TEXT = (
    "You act on routine work yourself, without asking: reading anything in your scope, your "
    "workspace, and coding or document changes through a Claude Code session you start on its "
    "own branch (start_session) in one of the code locations; it opens a pull request and never "
    "merges. Its progress and its DONE or BLOCKED report come back to you by themselves: do not "
    "poll it or set up a watcher. Say what you did.")
TIER_TEXT = (
    "The hub, not you, decides what needs a person: merges, deploys, deletes outside your "
    "workspace, settings, secrets, jobs and anything on a protected device always do. When you "
    "call one with rook_call it becomes an action card for the person; do not retry it.")


def _mask(reply: dict) -> dict:
    """Vault values in a reply become ``{{secret:name}}`` before the model sees them."""
    try:
        from ....band_mcp.secret_mask import scrub
        out = scrub(reply)
        return out if isinstance(out, dict) else reply
    except Exception:  # noqa: BLE001 - no masker on this hub
        return reply


PLUGIN = RolesPlugin
