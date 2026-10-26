"""Session brief and receipt (design item 12): the hub tells an agent what it
needs to start work, once, and knows when to tell it again.

The first reply of an MCP session carries ``_brief``: who the caller acts as,
the tasks it has claimed with each one's latest handoff, unread chat, and a few
working rules. It replaces the start-of-session round of ``rook_whoami``, the
deck, ``rook_handoff_list`` and ``rook_chat_rooms`` calls.

The brief carries a short ``receipt`` and every Rook tool accepts
``receipt=`` (the most used ones advertise it). An agent that repeats the receipt still holds the brief, so
nothing is resent. The brief comes again, built from the current state, when

* a session that has repeated the receipt before calls without it: the agent
  most likely lost its context (compaction);
* the receipt is not the current one: the standing text changed, or the agent
  kept an old one.

A client that never repeats the receipt gets the brief once, not on every
reply. The receipt is a hash of the brief's standing text, not of the MCP
session, so an agent that reconnects (hub restart, new session) and repeats
its receipt is not briefed again.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass

log = logging.getLogger("rook.band_mcp.brief")

PARAM = "receipt"
#: Tools whose schema shows ``receipt``; every tool accepts it. A call to one
#: of these without it is what tells the hub the agent lost the brief.
ADVERTISED = frozenset({
    "rook_call", "rook_task", "rook_knowledge", "rook_huddle", "rook_console_read",
    "rook_console_write", "rook_chat_send", "rook_handoff_save"})
FIRST = "first call of this session"
MISSING = "no receipt: you may have lost the brief, so here it is with your current state"
STALE = "receipt out of date: use this one"

MAX_CLAIMS = 3
MAX_HANDOFFS = 3
MAX_ROOMS = 5
MAX_STEPS = 5
MAX_TEXT = 300


@dataclass
class _Seen:
    briefed: bool = False
    echoed: bool = False
    pending: str | None = None  # due, but the last reply could not carry it
    stale: str | None = None    # an old receipt already answered with the new one


class Briefs:
    """Per MCP session: whether the hub briefed it and whether it repeats the receipt."""

    def __init__(self, cap: int = 5000) -> None:
        self._seen: OrderedDict[str, _Seen] = OrderedDict()
        self._cap = cap

    def _get(self, session: str) -> _Seen:
        s = self._seen.get(session)
        if s is None:
            s = self._seen[session] = _Seen()
            while len(self._seen) > self._cap:
                self._seen.popitem(last=False)
        self._seen.move_to_end(session)
        return s

    def due(self, session: str, current: str, receipt: str | None = None,
            carries: bool = True) -> str | None:
        """Why this call's reply should carry the brief, or None.

        ``carries`` is False for a tool that cannot take a receipt: a call to
        it says nothing about whether the agent still holds the brief."""
        s = self._get(session)
        if receipt == current:
            s.briefed = s.echoed = True
            s.pending = s.stale = None
            return None
        if s.pending:
            return s.pending
        if receipt:
            return None if receipt == s.stale else STALE
        if not s.briefed:
            return FIRST
        if carries and s.echoed:
            return MISSING
        return None

    def sent(self, session: str, reason: str, receipt: str | None = None) -> None:
        s = self._get(session)
        s.briefed, s.pending = True, None
        if reason == STALE:
            s.stale = receipt  # answered: the same old receipt again gets nothing
        if reason in (MISSING, STALE):
            s.echoed = False  # resend once per lapse, not on every later call

    def held(self, session: str, reason: str) -> None:
        self._get(session).pending = reason

    def forget(self, session: str) -> None:
        self._seen.pop(session, None)


def standing(actor: str, kind: str) -> list[str]:
    """The brief's fixed working rules for this caller."""
    lines = [f"You act as {actor}: every call is journaled under that name."]
    if kind == "shared":
        lines.append('This is the shared static token, so your work is attributed to "static". '
                     "Ask the user for your own token for anything long-lived.")
    lines += [
        "This brief replaces the start-of-session round (whoami, deck, handoff list, chat rooms): "
        "it lists your claimed work with its latest handoff, and unread chat, when there are any.",
        "Pass this brief's receipt as receipt= on every Rook tool call. A reply that carries _brief "
        "again means the hub thinks you lost it: read it again.",
        "Continuing a claim: check its handoff's freshness (STALE or SUPERSEDED: verify before acting). "
        'New work: rook_task(action="deck", data={"states": ["in_progress", "blocked"]}), narrowed '
        "by project, then claim it.",
        "Before starting, search rook_knowledge on the job's words.",
        "_task, _unread_chat, _hygiene and _tips notices ride later replies, each once: answer them.",
    ]
    return lines


def receipt_for(lines: list[str]) -> str:
    return hashlib.sha256(json.dumps(lines).encode()).hexdigest()[:8]


def _clip(text, limit: int = MAX_TEXT):
    if not isinstance(text, str) or len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


def claimed(knowledge, sessions, actor: str) -> list[dict]:
    """The caller's open claims, newest first, each with its latest handoff."""
    store = knowledge.store
    out = []
    with store.db(False) as db:
        rows = db.execute(
            "SELECT r.id, r.slug, r.title, r.state FROM claims c JOIN records r ON r.id = c.task "
            "WHERE c.actor = ? AND c.released IS NULL ORDER BY c.started DESC LIMIT ?",
            (actor, MAX_CLAIMS)).fetchall()
        named = store._names(db, [r["id"] for r in rows])
        refs = {}
        for r in rows:
            handoffs = [l for l in store._live_links(db, r["id"]) if l["kind"] == "handoff"]
            refs[r["id"]] = handoffs[-1]["ref"] if handoffs else None
    for r in rows:
        item = {"id": r["id"], **({"name": named[r["id"]]} if r["id"] in named else {}),
                "slug": r["slug"], "title": r["title"], "state": r["state"]}
        if refs[r["id"]]:
            item["handoff"] = _handoff(sessions.get(refs[r["id"]], history=False))
        out.append(item)
    return out


def _handoff(got: dict) -> dict | None:
    cur = got.get("current") if isinstance(got, dict) and got.get("ok") else None
    if not cur:
        return None
    return {"thread_id": cur["thread_id"], "as_of": cur["as_of"], "freshness": cur["freshness"],
            "goal": _clip(cur["goal"]), "state": _clip(cur.get("state")),
            "next_steps": [_clip(s) for s in cur.get("next_steps", [])[:MAX_STEPS]]}


def own_handoffs(sessions, actor: str) -> list[dict]:
    """The caller's most recent open handoff threads (for an agent with no claim)."""
    got = sessions.list_recent(limit=50)
    threads = got.get("threads", []) if isinstance(got, dict) else []
    return [{"thread_id": t["thread_id"], **({"name": t["name"]} if t.get("name") else {}),
             "goal": _clip(t["goal"]), "as_of": t["as_of"], "freshness": t["freshness"]}
            for t in threads if t.get("author") == actor][:MAX_HANDOFFS]


def build(reason: str, actor: str, kind: str, *, claims=(), handoffs=(), unread=()) -> dict:
    lines = standing(actor, kind)
    out = {"why": reason, "receipt": receipt_for(lines), "you": {"actor": actor, "kind": kind},
           "how": lines}
    if claims:
        out["claimed"] = list(claims)
    elif handoffs:
        out["your_handoffs"] = list(handoffs)
    if unread:
        out["unread_chat"] = list(unread)[:MAX_ROOMS]
    return out
