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

from ....band_mcp.chat_wake import wake, worker_for_host
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

    # chaired huddles (migrations/002_chair.sql)
    def set_roles(self, room: str, roles: list[str]) -> None:
        self._x("UPDATE huddles SET roles=? WHERE room=?", (json.dumps(list(roles)), room))

    def set_archived(self, room: str, archived: bool) -> bool:
        return self._x("UPDATE huddles SET archived=? WHERE room=?",
                       (time.time() if archived else None, room)) == 1

    def chaired(self, room: str) -> bool:
        rows = self._q("SELECT roles FROM huddles WHERE room=?", (room,))
        return bool(rows) and rows[0]["roles"] is not None

    def expired(self, before: float) -> list[str]:
        """Archived chaired huddles archived before ``before``."""
        return [r["room"] for r in self._q(
            "SELECT room FROM huddles WHERE roles IS NOT NULL AND archived IS NOT NULL "
            "AND archived<?", (before,))]

    def round_add(self, rid: str, room: str, trigger_seq: int, asker: str, plan: dict) -> None:
        self._x("INSERT INTO huddle_rounds(id,room,trigger_seq,asker,state,plan,started) "
                "VALUES(?,?,?,?,?,?,?)", (rid, room, trigger_seq, asker, "chair",
                                          json.dumps(plan), time.time()))

    def round_set(self, rid: str, **fields) -> None:
        sets, args = [], []
        for k, v in fields.items():
            if k not in ("state", "plan", "replies", "close_seq", "error", "ended"):
                raise KeyError(k)
            sets.append(f"{k}=?")
            args.append(json.dumps(v) if k in ("plan", "replies") else v)
        self._x(f"UPDATE huddle_rounds SET {','.join(sets)} WHERE id=?", (*args, rid))

    @staticmethod
    def _round(r: dict) -> dict:
        return {**r, "plan": json.loads(r["plan"] or "{}"), "replies": json.loads(r["replies"] or "[]")}

    def round(self, rid: str) -> dict | None:
        rows = self._q("SELECT * FROM huddle_rounds WHERE id=?", (rid,))
        return self._round(rows[0]) if rows else None

    def rounds(self, room: str, limit: int = 20) -> list[dict]:
        return [self._round(r) for r in reversed(self._q(
            "SELECT * FROM huddle_rounds WHERE room=? ORDER BY started DESC LIMIT ?",
            (room, int(limit))))]

    def unfinished_rounds(self) -> list[dict]:
        return [self._round(r) for r in self._q(
            "SELECT * FROM huddle_rounds WHERE state NOT IN ('done','failed')")]

    def card_add(self, card: dict) -> None:
        self._x("INSERT INTO huddle_cards(id,room,round,seq,kind,by,summary,call,state,result,created)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (card["id"], card["room"], card.get("round"), card.get("seq"), card["kind"],
                 card["by"], card["summary"], json.dumps(card["call"]) if card.get("call") else None,
                 card["state"], json.dumps(card["result"]) if card.get("result") is not None else None,
                 time.time()))

    def card_set(self, cid: str, *, expect: tuple[str, ...] | None = None, **fields) -> bool:
        sets, args = [], []
        for k, v in fields.items():
            if k not in ("state", "result", "seq", "decided_by", "decided", "summary", "call"):
                raise KeyError(k)
            sets.append(f"{k}=?")
            args.append(json.dumps(v) if k in ("result", "call") and v is not None else v)
        where, wargs = "id=?", [cid]
        if expect:
            where += f" AND state IN ({','.join('?' * len(expect))})"
            wargs += list(expect)
        return self._x(f"UPDATE huddle_cards SET {','.join(sets)} WHERE {where}", (*args, *wargs)) == 1

    @staticmethod
    def _card(r: dict) -> dict:
        out = {k: r[k] for k in ("id", "room", "round", "seq", "kind", "by", "summary", "state",
                                 "created", "decided_by", "decided")}
        out["call"] = json.loads(r["call"]) if r["call"] else None
        out["result"] = json.loads(r["result"]) if r["result"] else None
        if out["state"] == "failed":
            from .chair import card_error
            out["error"] = card_error(out)
        return out

    def card(self, cid: str) -> dict | None:
        rows = self._q("SELECT * FROM huddle_cards WHERE id=?", (cid,))
        return self._card(rows[0]) if rows else None

    def cards(self, room: str, round: str | None = None) -> list[dict]:
        if round:
            rows = self._q("SELECT * FROM huddle_cards WHERE room=? AND round=? ORDER BY created",
                           (room, round))
        else:
            rows = self._q("SELECT * FROM huddle_cards WHERE room=? ORDER BY created", (room,))
        return [self._card(r) for r in rows]

    def drop(self, room: str) -> None:
        """Forget a huddle whose chat room was deleted."""
        with self._lock:
            for table in ("huddles", "huddle_posts", "huddle_status", "huddle_questions",
                          "huddle_rounds", "huddle_cards"):
                self._db.execute(f"DELETE FROM {table} WHERE room=?", (room,))
            self._db.commit()

    @staticmethod
    def _question(r: dict) -> dict:
        data = json.loads(r["data"] or "{}")
        origin = data.pop("origin", None) or {}
        out = {"id": r["seq"], "room": r["room"], "asker": r["asker"], "text": r["text"],
               "state": r["state"], "asked": r["asked"], **data}
        # Where the asker was asking from (recorded at ask time, never
        # chosen by the agent); the principal itself stays internal.
        if origin.get("worker") or origin.get("host"):
            out["from_worker"] = (origin.get("worker") or {}).get("name") or origin.get("host")
        if r["state"] == "answered":
            out.update(answer=r["answer"], answered_by=r["answered_label"] or r["answered_by"],
                       answered=r["answered"], answer_seq=r["answer_seq"])
        if r["wake"]:
            out["wake"] = json.loads(r["wake"])
        return out

    def question(self, seq: int) -> dict | None:
        rows = self._q("SELECT * FROM huddle_questions WHERE seq=?", (int(seq),))
        return self._question(rows[0]) if rows else None

    def origin(self, seq: int) -> dict:
        """The asker's recorded principal and worker (see HuddleService.ask)."""
        rows = self._q("SELECT data FROM huddle_questions WHERE seq=?", (int(seq),))
        return (json.loads(rows[0]["data"] or "{}").get("origin") or {}) if rows else {}

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
    project (best effort); ``client()`` -> the band client or None;
    ``origin()`` -> the current caller's verified principal and its
    self-declared host (X-Rook-Host, not verified)
    (``{principal: {id, kind, role, groups, verified, label}, host}``), or
    None when there is none to trust (a band caller, in-process code)."""

    def __init__(self, chat: Any, store: HuddleStore, *, project: Callable, bands: Callable,
                 link: Callable | None = None, client: Callable | None = None,
                 origin: Callable[[], dict | None] = lambda: None,
                 wake_on_answer: Callable[[], bool] = lambda: False) -> None:
        self.chat = chat
        self.store = store
        self._project = project
        self._bands = bands
        self._link = link
        self._client = client or (lambda: None)
        self._wake_on_answer = wake_on_answer
        self._origin = origin
        self.wakes: set[asyncio.Task] = set()   # running wake-ups (tests await them)
        #: The chair (rounds, cards: :mod:`.chair`), set by the plugin.
        self.chair: Any = None

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

    def _live(self, h: dict | None) -> dict | None:
        """``h``, or None (and its rows dropped) when its chat room was
        deleted: the next post makes a fresh huddle instead of failing."""
        if h is not None and not self.chat.exists(h["room"]):
            log.info("huddle %s: its chat room is gone; dropping it", h["room"])
            self.store.drop(h["room"])
            return None
        return h

    async def resolve(self, identity: str, room: str | None = None, project: str | None = None,
                      band: str | None = None, create: bool = True) -> dict:
        """The huddle a call means: ``room`` (an existing huddle), else the
        ``project``'s, else the ``band``'s (default: the primary band).
        ``create``: made on first use and ``identity`` joins it; reads pass
        False and change nothing."""
        if room:
            h = self._live(self.store.get(room))
            if h is None:
                raise ValueError(f"{room!r} is not a huddle; open one with project= or band=")
        elif project:
            p = self._project(project)
            if p is None:
                raise ValueError("project huddles need the knowledge plugin; use band=")
            h = self._live(self.store.by_scope("project", p["id"])) or (
                await self._create("project", p["id"], p["band"], "Huddle: " + p["title"], identity, p)
                if create else None)
        else:
            b = self._band(band)
            h = self._live(self.store.by_scope("band", b["id"])) or (
                await self._create("band", b["id"], b["id"], "Huddle: " + (b.get("name") or b["id"]), identity)
                if create else None)
        if h is None:
            raise ValueError("no huddle there yet: post a status or ask to start it")
        if create and identity and identity != PERSON:
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
    def _send(self, room: str, sender: str, text: str, mentions=(), expects_reply=False,
              quiet=False) -> dict:
        sent = self.chat.send(room, sender, text, list(mentions), expects_reply, quiet=quiet)
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
        # Quiet: another agent's status line is no reason to notify everyone
        # in the room (_unread_chat); a decision is pinned for the person.
        sent = self._send(h["room"], identity, text, [PERSON] if decision else [], bool(decision), quiet=True)
        body = {k: v for k, v in (("working_on", working), ("blocked_on", blocked),
                                  ("needs_decision", decision), ("task", task)) if v}
        body = scrub(body)
        self.store.post(sent["seq"], h["room"], identity, "status", {"state": state, **body})
        self.store.set_status(h["room"], identity, state, body, sent["seq"])
        out = {"room": h["room"], "seq": sent["seq"], "state": state}
        if decision:
            self.store.ask(sent["seq"], h["room"], identity, scrub(decision),
                           {**self._question_data(data, task), **self._asked_from()})
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
        return out

    def _asked_from(self) -> dict:
        """The asker's verified principal and the worker its self-declared
        host (X-Rook-Host) names, at ask time, so an answer can only wake
        that worker, as that principal.
        Nothing for callers that cannot be trusted (over the band,
        unverified): their questions are answered without a wake-up."""
        try:
            o = self._origin() or {}
        except Exception:
            log.exception("huddle: caller lookup failed")
            return {}
        p = o.get("principal") or {}
        if not p.get("id") or not p.get("verified", True) or p.get("kind") in ("band", "unverified") \
                or p["id"] == "unverified":
            return {}
        origin = {"principal": p}
        host = o.get("host")
        if host:
            origin["host"] = host
            client = self._client()
            wid = worker_for_host(client.workers, host) if client is not None else None
            if wid:
                origin["worker"] = {"id": wid, "name": client.workers[wid].get("name") or wid}
        return {"origin": origin}

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
        self.store.ask(sent["seq"], h["room"], identity, question, {**extra, **self._asked_from()})
        return {"room": h["room"], "question": sent["seq"], "state": "open"}

    async def say(self, identity: str, text: str, mention=None, **where) -> dict:
        h = await self.resolve(identity, **where)
        sent = self._send(h["room"], identity, _text(text, "text", 8000, required=True), mention or [])
        return {"room": h["room"], "seq": sent["seq"], "offline": sent.get("offline", [])}

    def withdraw(self, identity: str, question: int) -> dict:
        if not self.store.withdraw(question, identity):
            raise ValueError("no open question of yours with that id")
        return {"question": int(question), "state": "withdrawn"}

    # -- chaired huddles (docs/design/huddles.md 5) -------------------------
    def _chair(self):
        if self.chair is None:
            raise ValueError("the chair is not available on this hub")
        return self.chair

    async def start(self, identity: str, title: str, roles: list[str] | None = None,
                    band: str | None = None) -> dict:
        """Start a chaired huddle: the person, the house agent in the chair
        and ``roles`` (role ids; None: ``huddle.default_roles`` that exist;
        []: a 1:1 with the chair)."""
        chair = self._chair()
        title = _text(title, "title", 200) or "Huddle"
        known = {r["id"] for r in (chair.roles().store.list() if chair._plugin("role") else [])}
        if roles is None:
            roles = [r for r in chair.setting("default_roles", []) if r in known]
        if isinstance(roles, str):
            roles = [r.strip() for r in roles.split(",") if r.strip()]
        roles = [str(r).lstrip("@").strip().lower() for r in roles or []]
        unknown = [r for r in roles if r not in known]
        if unknown:
            raise ValueError(f"no role {unknown[0]!r} (role.list; save one from role.presets)")
        roles = list(dict.fromkeys(roles))
        b = self._band(band)
        members = [PERSON] + ([identity] if identity and identity != PERSON else [])
        started = self.chat.start(title, identity or PERSON, members)
        if not started.get("ok"):
            raise ValueError(started.get("error") or "could not start the huddle room")
        room = started["room"]
        self.store.add(room, "talk", room, b["id"], title, identity or PERSON)
        self.store.set_roles(room, roles)
        return {"room": room, "title": title, "roles": roles}

    def _chaired(self, room: str) -> dict:
        h = self._live(self.store.get(room)) if room else None
        if h is None:
            raise ValueError(f"{room!r} is not a huddle")
        if h.get("roles") is None:
            raise ValueError("that huddle has no chair (a project's or band's status huddle); "
                             "post with huddle.write say")
        return h

    async def post(self, identity: str, room: str, text: str, mention=None) -> dict:
        """Post in a chaired huddle. A person's (or a job's) post starts a
        round: the chair routes it, roles answer. ``@<role>`` calls that
        role directly."""
        h = self._chaired(room)
        if h.get("archived"):
            raise ValueError("that huddle is archived; unarchive it to post")
        text = _text(text, "text", 8000, required=True)
        sent = self._send(room, identity, text, mention or [])
        rid = self._chair().on_post(h, sent["seq"], identity, text)
        return {"room": room, "seq": sent["seq"], "round": rid}

    def archive(self, room: str, archived: bool = True) -> dict:
        h = self._live(self.store.get(room)) if room else None
        if h is None:
            raise ValueError(f"{room!r} is not a huddle")
        self.store.set_archived(room, bool(archived))
        return {"room": room, "archived": bool(archived)}

    async def card(self, room: str, card: str, action: str, by: str, principal=None,
                   data: dict | None = None) -> dict:
        """``approve`` / ``dismiss`` a card (people only), or ``propose`` one
        (``data`` {kind, summary, worker?, cap?, args?}) as ``by``."""
        chair = self._chair()
        h = self._chaired(room)
        if action == "propose":
            return await chair._propose(None, h["room"], by, data or {})
        c = self.store.card(card)
        if c is None or c["room"] != h["room"]:
            raise ValueError("no such card in that huddle")
        return await chair.decide(card, action, by, principal)

    # -- reading -----------------------------------------------------------
    async def view(self, reader: str, since_seq: int = 0, limit: int = 50, mark: bool = True,
                   create: bool = False, targets: bool = False, **where) -> dict:
        """A huddle's status strip, questions and messages after
        ``since_seq`` (the last ``limit`` when since_seq is 0). ``create``
        (the dashboard): open the huddle if there is none yet. ``targets``:
        say on each open question which worker an answer would wake."""
        h = await self.resolve(reader, create=create, **where)
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
        questions = self.store.questions(room)
        if targets:
            for q in questions:
                if q["state"] == "open":
                    q["wake_target"] = self.wake_target(q["id"])
        out = {"room": room, "title": h["title"], "scope": h["scope"], "ref": h["ref"], "band": h["band"],
               "participants": read["participants"], "statuses": self.store.statuses(room),
               "questions": questions, "messages": msgs, "last_seq": read["last_seq"]}
        if h.get("roles") is not None:
            out.update(roles=json.loads(h["roles"]), archived=h.get("archived"),
                       chair=self.chair.chair_identity() if self.chair is not None else None,
                       rounds=self.store.rounds(room, 10), cards=self.store.cards(room))
        return out

    def huddles(self, archived: bool = False) -> list[dict]:
        """Every huddle (archived ones only with ``archived``), newest
        activity first for chaired ones."""
        out = []
        for h in self.store.all():
            if self._live(h) is None:
                continue
            if h.get("archived") and not archived:
                continue
            last = self.chat.tail(h["room"], 1)
            row = {**h, "last": last[0] if last else None,
                   "agents": len(self.store.statuses(h["room"]))}
            row["roles"] = json.loads(h["roles"]) if h.get("roles") is not None else None
            row["chaired"] = h.get("roles") is not None
            if row["chaired"]:
                row["open_cards"] = sum(1 for c in self.store.cards(h["room"])
                                        if c["state"] == "proposed")
            out.append(row)
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
        if self._live(self.store.get(q["room"])) is None:
            raise ValueError("that huddle's chat room was deleted")
        answer = scrub(_text(text, "answer", TEXT_MAX, required=True))
        sent = self._send(q["room"], PERSON, f"Answer for {q['asker']}: {answer}", [q["asker"]])
        if not self.store.answer(q["id"], answer, by, label, sent["seq"]):
            raise ValueError("that question was answered meanwhile")
        self.store.post(sent["seq"], q["room"], PERSON, "answer", {"question": q["id"]})
        wake_state = self._start_wake(q, answer)
        self.store.set_wake(q["id"], wake_state)
        return {"question": q["id"], "seq": sent["seq"], "wake": wake_state}

    def wake_target(self, question: int) -> dict:
        """Which worker answering ``question`` would wake, and whether it
        can: ``{state: "ready"|"off"|"notice", worker?, reason?}``. Only the
        worker recorded when the question was asked (named by the asker's
        own X-Rook-Host, so not verified), and only as the asker: policy for
        that principal decides whether it may start anything there."""
        if not self._wake_on_answer():
            return {"state": "off", "reason": "waking agents on an answer is off (Plugins > huddle)"}
        client = self._client()
        if client is None:
            return {"state": "notice", "reason": "no band connection here"}
        origin = self.store.origin(question)
        if not origin.get("principal"):
            return {"state": "notice", "reason": "the asker could not be verified"}
        rec = origin.get("worker") or {}
        wid = rec.get("id") if rec.get("id") in client.workers else None
        if wid is None and origin.get("host"):
            wid = worker_for_host(client.workers, origin["host"])
        if wid is None:
            return {"state": "notice", "worker": rec.get("name") or origin.get("host"),
                    "reason": "the agent's machine is not on the band"}
        w = client.workers.get(wid) or {}
        name = w.get("name") or wid
        caps = w.get("caps", []) or []
        if "hermes.chat" not in caps and "agent.wake" not in caps:
            return {"state": "notice", "worker": name, "reason": f"{name} cannot start an agent"}
        return {"state": "ready", "worker": name, "worker_id": wid}

    def _start_wake(self, q: dict, answer: str) -> dict:
        target = self.wake_target(q["id"])
        if target["state"] != "ready":
            return target
        client, wid, name = self._client(), target["worker_id"], target["worker"]
        p = self.store.origin(q["id"])["principal"]
        from ...policy import Principal
        principal = Principal(str(p["id"]), str(p.get("kind") or ""), str(p.get("role") or ""),
                              tuple(p.get("groups") or ()), verified=bool(p.get("verified", True)),
                              label=str(p.get("label") or ""))
        # The person's words are the answer; the question is the agent's own,
        # quoted and labelled as such, never presented as the person's note.
        note = f'The person answered the question you asked in this huddle. Their answer: "{answer}"'
        quoted = q["text"]

        async def run() -> None:
            result = await wake(client, self.chat, q["room"], wid, q["asker"], note,
                                principal=principal, quoted=quoted)
            result["state"] = "woken" if result.get("ok") else "failed"
            result["at"] = time.time()
            self.store.set_wake(q["id"], result)
        task = asyncio.get_running_loop().create_task(run())
        self.wakes.add(task)
        task.add_done_callback(self.wakes.discard)
        return {"state": "waking", "worker": name}
