"""``huddle.*``: huddles, as a hub plugin (worker ``rook``).

A huddle is a group chat room for a project (or a whole band) where agents
report what they are working on, say when they are blocked, ask the person
for decisions and work with each other in the person's view. It is an
ordinary hub chat room (the ``chat`` plugin's store), so every chat client
reads it; the structure (status strip, pinned questions) lives beside it, see
:mod:`.service`.

Caps (the ``rook_huddle`` MCP tool routes to them):

* ``huddle.read`` (risk ``read``): ``read`` a huddle (statuses, questions,
  messages since a seq), ``list`` every huddle, ``mine`` (your questions).
* ``huddle.write`` (risk ``write``): ``status``, ``ask``, ``say``,
  ``withdraw`` (your own open question).

Answering is for the person, from the dashboard (``/huddle/account-api``,
:mod:`.web`): an answer is posted to the room, unpins the question and wakes
the asking agent the way ``rook_chat_wake`` does.
"""
from __future__ import annotations

import json
import logging

from ....core.plugin import Plugin, capability, place, setting
from ..rooms import caller
from .service import SYSTEM, HuddleService, HuddleStore

log = logging.getLogger("rook.hub.plugins.huddle")

READS = ("read", "list", "mine")
WRITES = ("status", "ask", "say", "withdraw")
ERRORS = (ValueError, KeyError, TypeError, PermissionError)


class Huddle(Plugin):
    NAMESPACE = "huddle"
    NAME = "huddle"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    DEPENDS = ("chat",)
    SETTINGS = (
        setting("enabled", bool, default=True, env="ROOK_HUDDLE", apply="restart",
                label="Huddles",
                help="Group rooms where agents post status and ask you questions, shown on Work > "
                     "Huddle. Read at start: restart the hub to apply."),
        setting("wake_on_answer", bool, default=False, label="Wake the agent when you answer",
                help="After you answer a question, start the asking agent again on the machine it "
                     "asked from (hermes or agent.wake), with the agent's own permissions. Off: it "
                     "sees the answer on its next call."),
    )
    GUIDANCE = {
        "tool:rook_huddle": "",
        "cap:huddle.": "huddle.read/huddle.write take rook_huddle's action, room/project/band, text and data.",
    }
    SKILL = ("### huddles\n"
             "A huddle is a project's (or band's) group room in the person's view. `rook_huddle(action="
             "\"status\", project=..., data={working_on, blocked_on?, needs_decision?, task?})` when you "
             "start, get stuck or finish; `action=\"ask\"` with `text` (and `data.options`) for a "
             "decision only the person can make: it stays pinned until answered, and the answer comes "
             "back as a chat message (and wakes you if your worker can be woken). `read` shows the "
             "status strip, questions and recent messages.\n")

    def __init__(self) -> None:
        super().__init__()
        self.service: HuddleService | None = None
        self._node = None
        # Set by the MCP bridge: callable() -> the attributed caller (audit
        # dict, with ``host``) of the current MCP tool call, or None.
        self.attribution = None

    def available(self) -> bool:
        return bool(self.settings.get("enabled", True))

    def bind_host(self, node) -> None:
        self._node = node
        chat = getattr(node, "chat", None)
        if chat is None:
            rooms = node.plugin("chat")
            chat = getattr(rooms, "_store", None)
        if chat is None or not getattr(chat, "enabled", False):
            log.warning("huddle: the chat store is not open; huddles are off")
            return
        try:
            store = HuddleStore(chat.path)
        except Exception:
            log.exception("huddle: could not open its tables in %s", chat.path)
            return
        self.service = HuddleService(chat, store, project=self._project, bands=self._bands,
                                     link=self._link, client=lambda: getattr(node, "client", None),
                                     origin=self._origin,
                                     wake_on_answer=lambda: bool(self.settings.get("wake_on_answer", False)))

    async def stop(self) -> None:
        if self.service is not None:
            for task in list(self.service.wakes):
                task.cancel()
            self.service.store.close()
            self.service = None

    # -- what the service needs from the rest of the hub ---------------------
    def _knowledge(self):
        kb = self._node.plugin("knowledge") if self._node is not None else None
        return getattr(kb, "service", None)

    def _project(self, ref: str):
        svc = self._knowledge()
        if svc is None:
            return None
        store = svc.store
        band = store.locate(ref, [b["id"] for b in svc.bands()])
        with store.db(False) as db:
            r = store._get(db, band, ref)
        if r["kind"] != "project":
            raise ValueError(f"{ref!r} is a {r['kind']}, not a project")
        return {"id": r["id"], "slug": r["slug"], "title": r["title"], "band": r["band"]}

    def _bands(self):
        svc = self._knowledge()
        if svc is not None:
            return svc.bands()
        enrollment = getattr(self._node, "enrollment", None)
        if enrollment is not None:
            try:
                return [{"id": b["id"], "name": b["name"], "label": b["label"], "primary": bool(b["is_primary"])}
                        for b in enrollment.bands(active_only=True)]
            except Exception:
                log.exception("huddle: listing bands failed")
        return []

    async def _link(self, project: dict, band, room: str) -> None:
        """Record the huddle on its project (a ``chat`` link), so the project
        page lists it with the project's other rooms."""
        svc = self._knowledge()
        if svc is None:
            return
        await svc.dispatch("link", band, None, project["id"], "",
                           {"kind": "chat", "ref": room, "relation": "discussed_in", "note": "Huddle"},
                           f"huddle-link:{room}", actor=SYSTEM)

    def _origin(self):
        """The current caller's authenticated principal (the MCP wrapper's)
        and the host its attribution names. Band callers have none."""
        from ...authz import current_principal
        p = current_principal.get()
        if p is None or getattr(p, "kind", "") == "band":
            return None
        host = None
        if self.attribution is not None:
            try:
                host = (self.attribution() or {}).get("host")
            except Exception:
                log.exception("huddle: attribution lookup failed")
        return {"principal": {"id": p.id, "kind": p.kind, "role": p.role, "groups": list(p.groups),
                              "verified": bool(p.verified), "label": p.label}, "host": host}

    def _svc(self) -> HuddleService:
        if self.service is None:
            raise ValueError("huddles are off on this hub (the chat store is not open)")
        return self.service

    # -- caps ----------------------------------------------------------------
    @capability("read", risk="read")
    async def read(self, action: str = "read", room: str | None = None, project: str | None = None,
                   band: str | None = None, since_seq: int = 0, limit: int = 30) -> dict:
        """Read huddles: read (room|project|band: statuses, questions, messages after since_seq), list, mine.

        read marks the messages read for you; with since_seq 0 it returns the
        last ``limit`` messages. mine: your questions, open and recently answered."""
        svc, me = self._svc(), caller()
        if action == "list":
            return {"huddles": svc.huddles()}
        if action == "mine":
            return {"questions": svc.mine(me)}
        if action == "read":
            # A read changes nothing: no huddle is made, nobody joins.
            return await svc.view(me, since_seq=int(since_seq or 0), limit=max(1, min(int(limit), 200)),
                                  create=False, room=room, project=project, band=band)
        raise ValueError("action must be one of " + ", ".join(READS))

    @capability("write", risk="write")
    async def write(self, action: str, room: str | None = None, project: str | None = None,
                    band: str | None = None, text: str | None = None, data: dict | None = None,
                    mention=None) -> dict:
        """Post in a huddle: status (data {working_on, blocked_on?, needs_decision?, task?}), ask (text, data {options?, task?}), say (text, mention?), withdraw (data {question}).

        The huddle is room=, else project='s, else band='s (default: the
        primary band's); it is made on first use and you join it."""
        svc, me = self._svc(), caller()
        where = {"room": room, "project": project, "band": band}
        if action == "status":
            return await svc.status(me, data or {}, **where)
        if action == "ask":
            return await svc.ask(me, text or (data or {}).get("text"), data, **where)
        if action == "say":
            if isinstance(mention, str):
                mention = [m.strip() for m in mention.split(",") if m.strip()]
            return await svc.say(me, text or "", mention, **where)
        if action == "withdraw":
            return svc.withdraw(me, int((data or {}).get("question") or 0))
        raise ValueError("action must be one of " + ", ".join(WRITES))

    # -- MCP -----------------------------------------------------------------
    def mcp_tools(self, invoke):
        async def rook_huddle(action: str = "read", project: str | None = None, text: str | None = None,
                              data: dict | None = None) -> str:
            """Huddle: a project's group room the person watches (project=; none: the band's).
            status data {working_on, blocked_on?, needs_decision?, task?}: when you start, stall or
            finish. ask text, data {options?}: a decision only the person can make; pinned until
            answered; the answer comes back as chat and wakes you. say text. read (data.since_seq),
            mine."""
            d = dict(data or {})
            where = {"room": d.pop("room", None), "project": project, "band": d.pop("band", None)}
            if action in READS:
                cap, args = "huddle.read", {"action": action, **where, "since_seq": d.get("since_seq")}
            else:
                cap = "huddle.write"
                args = {"action": action, **where, "text": text, "data": d, "mention": d.pop("mention", None)}
            try:
                result = await invoke(cap, {k: v for k, v in args.items() if v is not None})
                if action == "read":
                    result = lean_view(result)
                return json.dumps({"ok": True, "result": result}, separators=(",", ":"), ensure_ascii=False)
            except ERRORS as error:
                return json.dumps({"ok": False, "error": str(error), "code": type(error).__name__},
                                  separators=(",", ":"))
        return [rook_huddle]


def lean_view(view: dict) -> dict:
    """What an agent needs from a read, in fewer tokens: no participants,
    no read receipts, compact messages and statuses."""
    msgs = [{k: m[k] for k in ("seq", "sender", "text", "kind") if m.get(k) is not None} for m in view["messages"]]
    return {"room": view["room"], "title": view["title"],
            "statuses": [{k: s[k] for k in ("identity", "state", "working_on", "blocked_on", "needs_decision",
                                            "task", "ts") if s.get(k)} for s in view["statuses"]],
            "questions": [{k: q[k] for k in ("id", "asker", "text", "state", "options", "answer", "answered_by")
                           if q.get(k)} for q in view["questions"]],
            "messages": msgs, "last_seq": view["last_seq"]}


PLUGIN = Huddle
