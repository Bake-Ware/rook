"""A role's inbox: what its persistent main session works on next
(docs/design/persistent-agents.md 3.5).

Every input for the role becomes an :class:`Item`. One item runs at a time;
the next is the highest priority, oldest first:

    0 voice       the person talking (front door, source voice)
    1 person      a person anywhere else (web, chat, Telegram, MCP asks)
    2 huddle      a huddle the house agent chairs
    3 background  session reports, job results, folded side threads, maintenance

Items nobody waits on (``note``: reports, job results, folds) are batched: all
queued notes ride along with the next turn as "While you were busy: ...", or
become one background turn of their own when nothing else is waiting.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

VOICE, PERSON, HUDDLE, BACKGROUND = 0, 1, 2, 3
PRIORITY_NAMES = {VOICE: "live voice", PERSON: "live", HUDDLE: "huddle",
                  BACKGROUND: "background"}
#: Notes folded into one turn at most (the rest wait for the next one).
BATCH_MAX = 12
NOTE_CHARS = 1200

Handler = Callable[[str, dict], Awaitable[str]]


def priority_of(via: str) -> int:
    """The priority of a turn from where it came from (``via`` of agents.run)."""
    v = str(via or "")
    if v in ("front-voice", "voice"):
        return VOICE
    if v.startswith(("front-huddle", "huddle")):
        return HUDDLE
    if v in ("report", "job", "fold", "maintenance", "note"):
        return BACKGROUND
    return PERSON


@dataclass
class Item:
    id: str
    via: str
    priority: int
    text: str
    context: str = ""
    tools: frozenset = frozenset()
    handler: Handler | None = None
    who: str = ""
    kind: str = "turn"                      # turn | note | maintenance
    waiter: asyncio.Future | None = None
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    state: str = "queued"                   # queued | running | done | failed | cancelled
    error: str = ""
    result: dict | None = None
    notes: list = field(default_factory=list)    # notes batched into this turn
    meta: dict = field(default_factory=dict)

    def brief(self) -> dict:
        return {"id": self.id, "via": self.via, "priority": PRIORITY_NAMES.get(self.priority),
                "who": self.who, "kind": self.kind, "state": self.state,
                "text": self.text[:120], "age_s": round(time.time() - self.created, 1),
                **({"running_s": round(time.time() - self.started, 1)}
                   if self.started and not self.finished else {})}

    def settle(self, result: dict | None = None, error: str = "", state: str = "") -> None:
        """Finish the item and wake whoever waits on it (once)."""
        self.finished = time.time()
        self.state = state or ("failed" if error else "done")
        self.error = error
        self.result = result
        if self.waiter is not None and not self.waiter.done():
            if error:
                self.waiter.set_exception(RuntimeError(error))
            else:
                self.waiter.set_result(result or {})
        for n in self.notes:
            if n.state == "running":
                n.settle(result, error, state)


class Inbox:
    """The queue of one role's main lane (in memory: a hub restart drops what
    was queued, and whoever waited gets an error, as a one-shot turn would)."""

    _ids = itertools.count(1)

    def __init__(self, role: str) -> None:
        self.role = role
        self.items: list[Item] = []
        self.paused = False
        self.done: list[Item] = []          # the last few, for status
        self.wake = asyncio.Event()

    @classmethod
    def new_id(cls) -> str:
        return f"q{next(cls._ids)}"

    def put(self, item: Item) -> Item:
        self.items.append(item)
        self.wake.set()
        return item

    def queued(self) -> list[Item]:
        return [i for i in self.items if i.state == "queued"]

    def counts(self) -> dict:
        q = self.queued()
        return {"waiting": len([i for i in q if i.kind != "note"]),
                "notes": len([i for i in q if i.kind == "note"]),
                "by_priority": {PRIORITY_NAMES[p]: n for p in PRIORITY_NAMES
                                if (n := len([i for i in q if i.priority == p]))}}

    def position(self) -> int:
        return len([i for i in self.queued() if i.kind != "note"])

    def take(self) -> Item | None:
        """The next turn to run, with queued notes batched into it, or None."""
        if self.paused:
            return None
        q = self.queued()
        turns = sorted((i for i in q if i.kind != "note"), key=lambda i: (i.priority, i.created))
        notes = sorted((i for i in q if i.kind == "note"), key=lambda i: i.created)[:BATCH_MAX]
        if turns:
            item = turns[0]
        elif notes:
            item = Item(id=self.new_id(), via="note", priority=BACKGROUND, text="", kind="note",
                        who="the hub")
            self.items.append(item)
        else:
            return None
        if notes and item.kind != "maintenance":
            item.notes = [n for n in notes if n is not item]
            for n in item.notes:
                n.state = "running"
                n.started = time.time()
        item.state = "running"
        item.started = time.time()
        return item

    def finish(self, item: Item) -> None:
        drop = {id(item), *(id(n) for n in item.notes)}
        self.items = [i for i in self.items if id(i) not in drop]
        self.done = ([item] + self.done)[:10]
        self.wake.set()

    def cancel(self, item_id: str) -> Item | None:
        for i in self.queued():
            if i.id == item_id:
                i.settle(error="cancelled", state="cancelled")
                self.items.remove(i)
                return i
        return None

    def fail_all(self, error: str) -> int:
        n = 0
        for i in list(self.items):
            if i.state in ("queued", "running"):
                i.settle(error=error)
                n += 1
        self.items = []
        return n


def batch_text(notes: list[Item]) -> str:
    if not notes:
        return ""
    lines = [f"- ({n.via}, {n.who or 'the hub'}) {n.text[:NOTE_CHARS]}" for n in notes]
    return "While you were busy:\n" + "\n".join(lines)


def turn_text(item: Item, context: str = "") -> str:
    """What the session is sent for an item: its header, the request's context
    when it changed, the batched notes, then the request itself."""
    head = (f"[item {item.id} | from {item.who or 'someone'} | via {item.via} | "
            f"{PRIORITY_NAMES.get(item.priority, '')}]")
    parts = [head]
    if context:
        parts.append("Context for this request (replaces the earlier context):\n" + context)
    notes = batch_text(item.notes)
    if notes:
        parts.append(notes)
    if item.text:
        parts.append(("Now: " if notes else "") + item.text)
    elif not notes:
        parts.append("(no text)")
    return "\n\n".join(parts)


def summary_line(name: str, lane_state: str, inbox: Inbox, forks: int) -> str:
    """``house agent: busy, 2 waiting, 1 fork`` for the dashboard and status()."""
    c = inbox.counts()
    bits = [lane_state or "offline"]
    if inbox.paused:
        bits.append("paused")
    if c["waiting"]:
        bits.append(f"{c['waiting']} waiting")
    if c["notes"]:
        bits.append(f"{c['notes']} note{'s' if c['notes'] != 1 else ''}")
    if forks:
        bits.append(f"{forks} fork{'s' if forks != 1 else ''}")
    return f"{name}: " + ", ".join(bits)


def requester(principal: Any, label: str = "") -> dict:
    """Who an item is for: kind person | agent | guest | system, for the trust
    rules on grants (a person's request) and for the header."""
    kind = str(getattr(principal, "kind", "") or "")
    if principal is None:
        k = "system"
    elif kind == "human" and getattr(principal, "verified", True):
        k = "person"
    elif kind in ("token", "agent"):
        k = "agent"
    else:
        k = "guest"
    return {"kind": k, "id": str(getattr(principal, "id", "") or ""),
            "label": label or str(getattr(principal, "label", "") or "")}
