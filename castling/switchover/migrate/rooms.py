"""0.4 chat rooms and huddles -> the 0.5 hub's streams (castling-streams).

castling-streams (Bake, Oct 28): "Chat room: a stream of ``text`` events
from many participants." "Huddle: a chat stream plus ``status`` and question
events; huddle logic is a plugin on top." "Durable: chat rooms, huddles and
agent sessions keep their full history."

So each 0.4 room becomes a durable stream in the hub's one store
(``<data>/stream-events.db``, which the hub indexes when it starts):

- a plain room -> a ``room`` stream; every message a ``text`` event by its
  sender, at its 0.4 time, with its mentions;
- a huddle room -> a ``huddle`` stream plus a ``huddle`` record (title,
  roles, its project's 0.5 id, state), as the huddle plugin makes them. Its
  messages are ``text`` events; its posts (status lines, questions, answers)
  are ``status`` events that keep the 0.4 post as ``kind04``/``data04``, with
  the question's text from the 0.4 question table. An archived huddle is
  closed, at its 0.4 time.

Not carried: the merge-vote machinery (motions, rounds, ballots, artifacts,
cards; castling-workbench: "Drop the merge-votes room (chair, pins,
unanimous huddle.vote)"), read cursors, presence, and ``huddle_status``,
which only caches each member's latest status post. All of it stays in the
archive.

Each stream carries ``attrs.from04`` (the 0.4 room id): a re-run skips rooms
already there.
"""
from __future__ import annotations

import json
import sqlite3

from rook.kit.ctx import Caller
from rook.streams import SqliteStore, Streams

from . import names
from .source import _tables

TABLES = {"rooms", "messages"}


def _json(v, default):
    try:
        out = json.loads(v) if v else default
    except ValueError:
        return default
    return out if isinstance(out, type(default)) else default


def counts(db: sqlite3.Connection | None) -> dict:
    if db is None or TABLES - _tables(db):
        return {}
    huddles = "huddles" in _tables(db)
    return {"rooms": db.execute("SELECT count(*) FROM rooms").fetchone()[0],
            "messages": db.execute("SELECT count(*) FROM messages").fetchone()[0],
            "huddles": db.execute("SELECT count(*) FROM huddles").fetchone()[0] if huddles else 0}


def events(db: sqlite3.Connection, room: str) -> list[tuple]:
    """A room's 0.4 history as ``(ts, type, by, body)``, oldest first."""
    out = []
    for m in db.execute("SELECT seq, ts, sender, text, mentions, expects_reply FROM messages WHERE room_id=? "
                        "ORDER BY ts, seq", (room,)):
        body = {"text": m["text"] or ""}
        mentions = [str(x) for x in _json(m["mentions"], [])]
        if mentions:
            body["mentions"] = mentions
        if m["expects_reply"]:
            body["expects_reply"] = True
        out.append((m["ts"] or 0.0, m["seq"], "text", m["sender"] or "", body))
    tables = _tables(db)
    if "huddle_posts" not in tables:
        return [e[:1] + e[2:] for e in out]
    questions = {q["seq"]: dict(q) for q in db.execute("SELECT * FROM huddle_questions WHERE room=?", (room,))} \
        if "huddle_questions" in tables else {}
    posted = set()
    for p in db.execute("SELECT seq, sender, kind, data, ts FROM huddle_posts WHERE room=? ORDER BY ts, seq",
                        (room,)):
        data = _json(p["data"], {})
        q = questions.get(p["seq"]) if p["kind"] == "question" else None
        if q is not None:
            posted.add(p["seq"])
        out.append((p["ts"] or 0.0, p["seq"], "status", p["sender"] or "", _post(p["kind"], data, q)))
    for seq, q in questions.items():
        if seq not in posted:                    # asked with no post of its own
            out.append((q["asked"] or 0.0, seq, "status", q["asker"] or "", _post("question", {}, q)))
    out.sort(key=lambda e: (e[0], e[1]))
    return [e[:1] + e[2:] for e in out]


def _post(kind: str, data: dict, q: dict | None) -> dict:
    if q is not None:
        text = q["text"]
        data = {**data, **{k: q[k] for k in ("state", "answer", "answered_by", "answered_label", "answered")
                           if q.get(k) is not None}, "options": _json(q["data"], {}).get("options")}
    elif kind == "status":
        text = data.get("working_on") or data.get("blocked_on") or data.get("state") or "status"
    elif kind == "answer":
        text = "Answer" + (f": {data['relayed']}" if data.get("relayed") else "")
    else:
        text = data.get("text") or kind
    return {"text": str(text), "kind04": kind, "data04": data}


def put(db: sqlite3.Connection | None, streams_path: str, store, band: str, projects: dict) -> dict:
    """Write every room (``streams_path`` ``:memory:`` on a dry run).
    ``store``: a record store with the huddle plugin's types; ``projects``:
    0.4 project id -> 0.5 id."""
    report = {"rooms": 0, "huddles": 0, "messages": 0, "posts": 0, "skipped": 0}
    if db is None or TABLES - _tables(db):
        return report
    sql = SqliteStore(streams_path)
    streams = Streams(sql, host="hub")
    try:
        have = {m.attrs.get("from04") for m in streams.list()}
        huddles = {h["room"]: dict(h) for h in db.execute("SELECT * FROM huddles")} \
            if "huddles" in _tables(db) else {}
        for room in db.execute("SELECT * FROM rooms ORDER BY created, id"):
            if room["id"] in have:
                report["skipped"] += 1
                continue
            _room(db, streams, store, band, projects, dict(room), huddles.get(room["id"]), report)
    finally:
        sql.close()
    return report


def _room(db, streams: Streams, store, band: str, projects: dict, room: dict, huddle: dict | None,
          report: dict) -> None:
    attrs = {"from04": room["id"], "participants04": _json(room["participants"], [])}
    title = (huddle or room)["title"] or f"0.4 room {room['id']}"
    project = projects.get(huddle["ref"]) if huddle and huddle["scope"] == "project" else None
    if huddle:
        roles = [str(r) for r in _json(huddle["roles"], [])]
        attrs.update(plugin="huddle", roles=roles, **({"project": project} if project else {}))
    streams.clock = lambda: (huddle or room)["created"] or 0.0
    meta = streams.open("huddle" if huddle else "room", title=title, producer="huddle" if huddle else "",
                        attrs=attrs)
    for ts, type_, by, body in events(db, room["id"]):
        streams.clock = lambda ts=ts: ts
        streams.append(meta.id, type_, body, by=by)
        report["messages" if type_ == "text" else "posts"] += 1
    if huddle:
        if huddle["archived"]:
            streams.clock = lambda: huddle["archived"]
            streams.close(meta.id, by="import:0.4")
        store.clock = lambda: huddle["created"]
        store.type(names.record("huddle")).create(
            Caller(agent=huddle["creator"] or "import:0.4", band=band, why="import from 0.4"),
            title=title, roles=attrs["roles"], stream=meta.id, state="closed" if huddle["archived"] else "open",
            **({"project": project} if project else {}))
        report["huddles"] += 1
    else:
        report["rooms"] += 1
