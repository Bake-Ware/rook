"""Huddles: a project's (or a band's) group room where agents post status,
ask the person questions and work together in the person's view.

Built on the hub's chat rooms (rook/band_mcp/chat_rooms.py): a huddle IS a
chat room, and every post is an ordinary chat message with readable text, so
the Chat page, ``rook_chat_read`` and older clients see the whole
conversation. :class:`HuddleStore` keeps the structure beside it, in the
same ``chat.db`` (migrations/001_huddle.sql): which room is which project's
huddle, each structured post by message seq, each agent's latest status and
the questions for the person, pinned until answered.

An answer goes back as a chat message that mentions the asker (an unread
notice on its next MCP call) and, when its worker can be woken
(``hermes.chat`` or ``agent.wake``, as ``rook_chat_wake`` does), wakes it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ....band_mcp.chat_wake import find_worker, wake
from ....band_mcp.secret_mask import scrub
from ....core.db import open_db

log = logging.getLogger("rook.hub.plugins.huddle")

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
NAMESPACE = "huddle"
#: The person, as the dashboard's chat panel posts (rook/remote/bootstrap.py).
PERSON = "user:operator"
SYSTEM = {"id": "system:huddle", "kind": "system", "label": "Huddle"}
STATES = ("working", "blocked", "needs_decision", "idle", "done")
TEXT_MAX = 2000
OPTIONS_MAX = 6
# Answered questions stay listed (unpinned) this long, so the asker and the
# person can see what was decided.
ANSWERED_KEEP = 3 * 86400


def _text(value, what, limit=TEXT_MAX, required=False) -> str:
    value = "" if value is None else value
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{what} is longer than {limit} characters")
    if required and not value:
        raise ValueError(f"{what} is required")
    return value


class HuddleStore:
    """The huddle tables in chat.db. One connection, serialised by a lock
    (the chat store does the same with its own)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._db = open_db(path, migrations=MIGRATIONS, ns=NAMESPACE, mkdir=False)

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass

    def _q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args)]

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.rowcount

    # huddles
    def by_scope(self, scope: str, ref: str) -> dict | None:
        rows = self._q("SELECT * FROM huddles WHERE scope=? AND ref=?", (scope, ref))
        return rows[0] if rows else None

    def get(self, room: str) -> dict | None:
        rows = self._q("SELECT * FROM huddles WHERE room=?", (room,))
        return rows[0] if rows else None

    def add(self, room: str, scope: str, ref: str, band: str | None, title: str, creator: str) -> None:
        self._x("INSERT INTO huddles(room,scope,ref,band,title,created,creator) VALUES(?,?,?,?,?,?,?)",
                (room, scope, ref, band, title, time.time(), creator))

    def all(self) -> list[dict]:
        return self._q("SELECT h.*, (SELECT count(*) FROM huddle_questions q WHERE q.room=h.room "
                       "AND q.state='open') open_questions FROM huddles h ORDER BY created")

    # posts, statuses, questions
    def post(self, seq: int, room: str, sender: str, kind: str, data: dict) -> None:
        self._x("INSERT OR REPLACE INTO huddle_posts(seq,room,sender,kind,data,ts) VALUES(?,?,?,?,?,?)",
                (seq, room, sender, kind, json.dumps(data), time.time()))

    def posts(self, room: str, seqs: list[int]) -> dict[int, dict]:
        if not seqs:
            return {}
        marks = ",".join("?" * len(seqs))
        return {r["seq"]: {"kind": r["kind"], "data": json.loads(r["data"])} for r in self._q(
            f"SELECT seq,kind,data FROM huddle_posts WHERE room=? AND seq IN ({marks})", (room, *seqs))}

    def set_status(self, room: str, identity: str, state: str, data: dict, seq: int) -> None:
        self._x("INSERT INTO huddle_status(room,identity,state,data,seq,ts) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(room,identity) DO UPDATE SET state=excluded.state,data=excluded.data,"
                "seq=excluded.seq,ts=excluded.ts", (room, identity, state, json.dumps(data), seq, time.time()))

    def statuses(self, room: str) -> list[dict]:
        return [{"identity": r["identity"], "state": r["state"], **json.loads(r["data"]),
                 "seq": r["seq"], "ts": r["ts"]}
                for r in self._q("SELECT * FROM huddle_status WHERE room=? ORDER BY ts DESC", (room,))]

    def ask(self, seq: int, room: str, asker: str, text: str, data: dict) -> None:
        self._x("INSERT INTO huddle_questions(seq,room,asker,text,data,asked) VALUES(?,?,?,?,?,?)",
                (seq, room, asker, text, json.dumps(data), time.time()))

    @staticmethod
    def _question(r: dict) -> dict:
        out = {"id": r["seq"], "room": r["room"], "asker": r["asker"], "text": r["text"],
               "state": r["state"], "asked": r["asked"], **json.loads(r["data"] or "{}")}
        if r["state"] == "answered":
            out.update(answer=r["answer"], answered_by=r["answered_label"] or r["answered_by"],
                       answered=r["answered"], answer_seq=r["answer_seq"])
        if r["wake"]:
            out["wake"] = json.loads(r["wake"])
        return out

    def question(self, seq: int) -> dict | None:
        rows = self._q("SELECT * FROM huddle_questions WHERE seq=?", (int(seq),))
        return self._question(rows[0]) if rows else None

    def questions(self, room: str | None = None, asker: str | None = None, since: float | None = None) -> list[dict]:
        """Open questions, then those answered (or withdrawn) after ``since``."""
        clauses, args = [], []
        if room:
            clauses.append("room=?"); args.append(room)
        if asker:
            clauses.append("asker=?"); args.append(asker)
        clauses.append("(state='open' OR coalesce(answered,asked)>=?)")
        args.append(since if since is not None else time.time() - ANSWERED_KEEP)
        return [self._question(r) for r in self._q(
            "SELECT * FROM huddle_questions WHERE " + " AND ".join(clauses)
            + " ORDER BY state<>'open', seq", tuple(args))]

    def answer(self, seq: int, answer: str, by: str, label: str, answer_seq: int) -> bool:
        return self._x("UPDATE huddle_questions SET state='answered',answer=?,answered_by=?,answered_label=?,"
                       "answered=?,answer_seq=? WHERE seq=? AND state='open'",
                       (answer, by, label, time.time(), answer_seq, int(seq))) == 1

    def withdraw(self, seq: int, asker: str) -> bool:
        return self._x("UPDATE huddle_questions SET state='withdrawn',answered=? WHERE seq=? AND asker=? "
                       "AND state='open'", (time.time(), int(seq), asker)) == 1

    def set_wake(self, seq: int, info: dict) -> None:
        self._x("UPDATE huddle_questions SET wake=? WHERE seq=?", (json.dumps(info), int(seq)))


class HuddleService:
    """Huddle operations for the caps, the MCP tool and the dashboard API.

    ``project(ref)`` -> ``{id, slug, title, band}`` (raises if unknown, None
    when the knowledge plugin is off); ``bands()`` -> ``[{id, name,
    primary}]``; ``link(project, band, room)`` records the room on the
    project (best effort); ``client()`` -> the band client or None."""

    def __init__(self, chat: Any, store: HuddleStore, *, project: Callable, bands: Callable,
                 link: Callable | None = None, client: Callable | None = None,
                 wake_on_answer: Callable[[], bool] = lambda: True) -> None:
        self.chat = chat
        self.store = store
        self._project = project
        self._bands = bands
        self._link = link
        self._client = client or (lambda: None)
        self._wake_on_answer = wake_on_answer
        self.wakes: set[asyncio.Task] = set()   # running wake-ups (tests await them)

    # -- which huddle ----------------------------------------------------
    def _band(self, ref: str | None) -> dict:
        bands = self._bands() or [{"id": "default", "name": "Default", "primary": True}]
        if ref:
            match = [b for b in bands if ref in (b["id"], b.get("name"), b.get("label"))]
        else:
            match = [b for b in bands if b.get("primary")] or bands[:1]
        if len(match) != 1:
            raise ValueError(f"unknown band {ref!r}")
        return match[0]

    async def resolve(self, identity: str, room: str | None = None, project: str | None = None,
                      band: str | None = None, create: bool = True) -> dict:
        """The huddle a call means: ``room`` (an existing huddle), else the
        ``project``'s, else the ``band``'s (default: the primary band). Made
        on first use; ``identity`` joins it."""
        if room:
            h = self.store.get(room)
            if h is None:
                raise ValueError(f"{room!r} is not a huddle; open one with project= or band=")
        elif project:
            p = self._project(project)
            if p is None:
                raise ValueError("project huddles need the knowledge plugin; use band=")
            h = self.store.by_scope("project", p["id"]) or (
                await self._create("project", p["id"], p["band"], "Huddle: " + p["title"], identity, p)
                if create else None)
        else:
            b = self._band(band)
            h = self.store.by_scope("band", b["id"]) or (
                await self._create("band", b["id"], b["id"], "Huddle: " + (b.get("name") or b["id"]), identity)
                if create else None)
        if h is None:
            raise ValueError("no huddle there yet")
        if identity and identity != PERSON:
            self.chat.join(h["room"], [identity])
        return h

    async def _create(self, scope: str, ref: str, band: str | None, title: str, identity: str,
                      project: dict | None = None) -> dict:
        started = self.chat.start(title, identity or PERSON, [PERSON])
        if not started.get("ok"):
            raise ValueError(started.get("error") or "could not start the huddle room")
        try:
            self.store.add(started["room"], scope, ref, band, title, identity or PERSON)
        except Exception:
            # Two callers raced to open the same huddle: keep the first.
            self.chat.delete(started["room"], None)
            h = self.store.by_scope(scope, ref)
            if h is None:
                raise
            return h
        if project is not None and self._link is not None:
            try:
                await self._link(project, band, started["room"])
            except Exception:
                log.exception("linking huddle %s to %s failed", started["room"], ref)
        return self.store.get(started["room"])

    # -- agents ------------------------------------------------------------
    def _send(self, room: str, sender: str, text: str, mentions=(), expects_reply=False) -> dict:
        sent = self.chat.send(room, sender, text, list(mentions), expects_reply)
        if not sent.get("ok"):
            raise ValueError(sent.get("error") or "post failed")
        return sent

    async def status(self, identity: str, data: dict, **where) -> dict:
        """Post a status: data {working_on, blocked_on?, needs_decision?,
        task?, state?, worker?}. A needs_decision also pins a question."""
        data = dict(data or {})
        working = _text(data.get("working_on"), "working_on", 500)
        blocked = _text(data.get("blocked_on"), "blocked_on", 1000)
        decision = _text(data.get("needs_decision"), "needs_decision", TEXT_MAX)
        task = _text(data.get("task"), "task", 200)
        state = data.get("state") or ("needs_decision" if decision else "blocked" if blocked else "working")
        if state not in STATES:
            raise ValueError("state must be one of " + ", ".join(STATES))
        if not (working or blocked or decision or state in ("idle", "done")):
            raise ValueError("status needs data.working_on (and blocked_on / needs_decision when they apply)")
        h = await self.resolve(identity, **where)
        parts = [("Working on: " + working) if working else "Status: " + state.replace("_", " ")]
        if blocked:
            parts.append("Blocked: " + blocked)
        if decision:
            parts.append("Needs a decision: " + decision)
        text = scrub(" | ".join(parts) + (f" (task {task})" if task else ""))
        sent = self._send(h["room"], identity, text, [PERSON] if decision else [], bool(decision))
        body = {k: v for k, v in (("working_on", working), ("blocked_on", blocked),
                                  ("needs_decision", decision), ("task", task)) if v}
        body = scrub(body)
        self.store.post(sent["seq"], h["room"], identity, "status", {"state": state, **body})
        self.store.set_status(h["room"], identity, state, body, sent["seq"])
        out = {"room": h["room"], "seq": sent["seq"], "state": state}
        if decision:
            self.store.ask(sent["seq"], h["room"], identity, scrub(decision),
                           self._question_data(data, task))
            out["question"] = sent["seq"]
        return out

    @staticmethod
    def _question_data(data: dict, task: str) -> dict:
        options = data.get("options") or []
        if not isinstance(options, list) or len(options) > OPTIONS_MAX:
            raise ValueError(f"options must be a list of at most {OPTIONS_MAX} short answers")
        options = [_text(o, "option", 200, required=True) for o in options]
        out = {"options": scrub(options)} if options else {}
        if task:
            out["task"] = task
        worker = _text(data.get("worker"), "worker", 100)
        if worker:
            out["worker"] = worker
        return out

    async def ask(self, identity: str, text: str, data: dict | None = None, **where) -> dict:
        """Ask the person: pinned in the huddle until answered."""
        data = dict(data or {})
        question = scrub(_text(text, "text", TEXT_MAX, required=True))
        task = _text(data.get("task"), "task", 200)
        extra = self._question_data(data, task)
        h = await self.resolve(identity, **where)
        opts = extra.get("options")
        sent = self._send(h["room"], identity, "Question: " + question
                          + (" (options: " + " / ".join(opts) + ")" if opts else ""), [PERSON], True)
        self.store.post(sent["seq"], h["room"], identity, "question", extra)
        self.store.ask(sent["seq"], h["room"], identity, question, extra)
        return {"room": h["room"], "question": sent["seq"], "state": "open"}

    async def say(self, identity: str, text: str, mention=None, **where) -> dict:
        h = await self.resolve(identity, **where)
        sent = self._send(h["room"], identity, _text(text, "text", 8000, required=True), mention or [])
        return {"room": h["room"], "seq": sent["seq"], "offline": sent.get("offline", [])}

    def withdraw(self, identity: str, question: int) -> dict:
        if not self.store.withdraw(question, identity):
            raise ValueError("no open question of yours with that id")
        return {"question": int(question), "state": "withdrawn"}

    # -- reading -----------------------------------------------------------
    async def view(self, reader: str, since_seq: int = 0, limit: int = 50, mark: bool = True,
                   **where) -> dict:
        """A huddle's status strip, questions and messages after
        ``since_seq`` (the last ``limit`` when since_seq is 0)."""
        h = await self.resolve(reader, **where)
        room = h["room"]
        if not since_seq:
            since_seq = max(0, self.chat.last_seq(room) - max(1, int(limit)))
        read = self.chat.read(room, reader, since_seq=int(since_seq), mark=mark, limit=max(1, int(limit)))
        if not read.get("ok"):
            raise ValueError(read.get("error") or "read failed")
        msgs = read["messages"]
        posts = self.store.posts(room, [m["seq"] for m in msgs])
        for m in msgs:
            if m["seq"] in posts:
                m.update(posts[m["seq"]])
        return {"room": room, "title": h["title"], "scope": h["scope"], "ref": h["ref"], "band": h["band"],
                "participants": read["participants"], "statuses": self.store.statuses(room),
                "questions": self.store.questions(room), "messages": msgs, "last_seq": read["last_seq"]}

    def huddles(self) -> list[dict]:
        out = []
        for h in self.store.all():
            last = self.chat.tail(h["room"], 1)
            out.append({**h, "last": last[0] if last else None,
                        "agents": len(self.store.statuses(h["room"]))})
        return out

    def mine(self, identity: str) -> list[dict]:
        """The caller's questions: open, and answered in the last few days."""
        return self.store.questions(asker=identity)

    # -- the person --------------------------------------------------------
    async def answer(self, question: int, text: str, by: str, label: str) -> dict:
        """The person answers a pinned question: an answer message that
        mentions the asker, the question unpinned, and the asker woken when
        its worker can be (in the background; the result lands on the
        question as ``wake``)."""
        q = self.store.question(question)
        if q is None:
            raise ValueError("no such question")
        if q["state"] != "open":
            raise ValueError(f"that question is already {q['state']}")
        answer = scrub(_text(text, "answer", TEXT_MAX, required=True))
        sent = self._send(q["room"], PERSON, f"Answer for {q['asker']}: {answer}", [q["asker"]])
        if not self.store.answer(q["id"], answer, by, label, sent["seq"]):
            raise ValueError("that question was answered meanwhile")
        self.store.post(sent["seq"], q["room"], PERSON, "answer", {"question": q["id"]})
        wake_state = self._start_wake(q, answer)
        self.store.set_wake(q["id"], wake_state)
        return {"question": q["id"], "seq": sent["seq"], "wake": wake_state}

    def _start_wake(self, q: dict, answer: str) -> dict:
        if not self._wake_on_answer():
            return {"state": "off"}
        client = self._client()
        if client is None:
            return {"state": "notice", "reason": "no band connection here"}
        wid = find_worker(client.workers, q["asker"], q.get("worker"))
        if wid is None:
            return {"state": "notice", "reason": "its worker is not on the band"}
        caps = (client.workers.get(wid) or {}).get("caps", []) or []
        name = (client.workers.get(wid) or {}).get("name") or wid
        if "hermes.chat" not in caps and "agent.wake" not in caps:
            return {"state": "notice", "worker": name, "reason": f"{name} cannot be woken"}
        note = f"You asked in this huddle: {q['text']}\nThe answer: {answer}"

        async def run() -> None:
            result = await wake(client, self.chat, q["room"], wid, PERSON, note)
            result["state"] = "woken" if result.get("ok") else "failed"
            result["at"] = time.time()
            self.store.set_wake(q["id"], result)
        task = asyncio.get_running_loop().create_task(run())
        self.wakes.add(task)
        task.add_done_callback(self.wakes.discard)
        return {"state": "waking", "worker": name}
