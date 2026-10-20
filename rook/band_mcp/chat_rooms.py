"""Site-hosted chat rooms — the agent backroom (design §3).

This is the site-side rooms store, distinct from the worker ``chat.py`` plugin
(which is a per-worker JSONL transcript for the local human). Here rooms live on
the hub so many agents — Claude over MCP, hermes over the band, you from the UI —
share the same conversation.

Model:
  * A **room** is a thread (its id IS the thread_id, shared with journal/handoff
    records). It has participants and a last_activity.
  * **Mention is routing metadata on the send**, not text: in a 2-party room
    every message implicitly addresses the other; in 3+ only mentioned
    participants are expected to respond. Mentioning a non-participant
    auto-invites them.
  * **Presence** is passive: an identity is "online" if it made any MCP call
    recently. Delivery is voicemail by default — an offline recipient sees an
    unread notice piggybacked on its next tool response. Waking an agent (making
    it respond now) is a separate, explicit act handled by the ``agent.wake``
    worker cap, not here.
  * No housekeeping — rooms sort by last_activity and stale ones sink.

sqlite next to the journal.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid

from ..core import names
from ..core.db import add_columns, open_db
from ..core.text import clip, marker
from .secret_mask import scrub

log = logging.getLogger("rook.band_mcp.chat_rooms")

#: A message is stored up to this many characters; a longer one is cut with
#: a ``[truncated: N of M chars]`` marker (inside the limit), never silently.
TEXT_MAX = 8000


def _fit(text: str) -> str:
    text = str(text or "")
    if len(text) <= TEXT_MAX:
        return text
    return clip(text, TEXT_MAX - len(marker(TEXT_MAX, len(text))) - 1)

_PRESENCE_ONLINE_SECS = 90.0     # seen within this window ⇒ "online"


class ChatStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        d = os.path.dirname(path) or "."
        try:
            os.makedirs(d, exist_ok=True)
            self._db = open_db(path, row_factory=False, mkdir=False)
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS rooms (
                    id TEXT PRIMARY KEY, title TEXT, created REAL,
                    last_activity REAL, participants TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT, ts REAL, sender TEXT, text TEXT,
                    mentions TEXT, expects_reply INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_msg_room ON messages(room_id, seq);
                CREATE TABLE IF NOT EXISTS reads (
                    identity TEXT, room_id TEXT, last_read_seq INTEGER,
                    PRIMARY KEY (identity, room_id)
                );
                CREATE TABLE IF NOT EXISTS presence (
                    identity TEXT PRIMARY KEY, last_seen REAL
                );
                CREATE TABLE IF NOT EXISTS avatars (
                    identity TEXT PRIMARY KEY, mime TEXT, data BLOB, updated REAL
                );
            """)
            # quiet: a message that does not count as unread (a huddle's
            # status lines). Additive, so an older release still reads and
            # writes the table (its inserts name their columns).
            add_columns(self._db, "messages", {"quiet": "INTEGER"})
            # Speakable names (rook.core.names; a huddle uses its room's):
            # rooms started by an older release get theirs here.
            names.ensure_table(self._db)
            names.backfill(self._db, "room", [r[0] for r in self._db.execute(
                "SELECT id FROM rooms WHERE id NOT IN "
                "(SELECT id FROM record_names WHERE kind='room')")])
            self._db.commit()
        except Exception:
            log.exception("chat store init failed; chat disabled")
            self._db = None

    @property
    def enabled(self) -> bool:
        return self._db is not None

    # -- names ---------------------------------------------------------------

    def name_of(self, room_id: str) -> str | None:
        if self._db is None or not room_id:
            return None
        with self._lock:
            return names.name_of(self._db, "room", room_id)

    def names_of(self, ids) -> dict:
        if self._db is None:
            return {}
        with self._lock:
            return names.names_of(self._db, "room", ids)

    def resolve_room(self, ref, *, write: bool) -> names.Resolved:
        """A room id, or its speakable name (any case and separators), as
        the room id. A read takes one clear near miss; a write needs the
        exact name. Raises names.NameError_ with candidates."""
        if self._db is None:
            return names.Resolved(id=str(ref or "").strip())
        def exists(rid):
            return self._db.execute("SELECT 1 FROM rooms WHERE id=?", (rid,)).fetchone() is not None
        with self._lock:
            return names.resolve(self._db, "room", ref, exists=exists, write=write, noun="room")

    resolve_ref = resolve_room

    # -- presence ------------------------------------------------------------

    def touch(self, identity: str | None) -> None:
        """Mark an identity as seen now (called on any identified MCP call)."""
        if self._db is None or not identity or identity == "anonymous":
            return
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO presence (identity,last_seen) VALUES (?,?) "
                    "ON CONFLICT(identity) DO UPDATE SET last_seen=excluded.last_seen",
                    (identity, time.time()))
                self._db.commit()
        except Exception:
            log.debug("presence touch failed", exc_info=True)

    def online(self) -> list[dict]:
        if self._db is None:
            return []
        cut = time.time() - _PRESENCE_ONLINE_SECS
        with self._lock:
            rows = self._db.execute(
                "SELECT identity,last_seen FROM presence ORDER BY last_seen DESC").fetchall()
        return [{"identity": i, "last_seen_age_secs": round(time.time() - t, 1),
                 "online": t >= cut} for (i, t) in rows]

    def is_online(self, identity: str) -> bool:
        if self._db is None:
            return False
        with self._lock:
            r = self._db.execute("SELECT last_seen FROM presence WHERE identity=?",
                                 (identity,)).fetchone()
        return bool(r) and r[0] >= time.time() - _PRESENCE_ONLINE_SECS

    # -- rooms ---------------------------------------------------------------

    def start(self, title: str, creator: str, invite: list[str]) -> dict:
        if self._db is None:
            return {"ok": False, "error": "chat store not available"}
        rid = uuid.uuid4().hex[:16]
        title = scrub(title)
        parts = []
        for p in [creator] + list(invite or []):
            p = str(p).strip()
            if p and p not in parts:
                parts.append(p)
        now = time.time()
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO rooms (id,title,created,last_activity,participants) "
                    "VALUES (?,?,?,?,?)",
                    (rid, str(title or "chat")[:200], now, now, json.dumps(parts)))
                name = names.assign(self._db, "room", rid)
                self._db.commit()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "room": rid, "name": name, "title": title, "participants": parts}

    def delete(self, rid: str, identity: str | None) -> dict:
        """Delete a room and everything in it. ``identity`` must be a
        participant (``None`` = trusted caller such as the dashboard admin).
        Rooms are threads with no backups — this is final."""
        if self._db is None:
            return {"ok": False, "error": "chat store not available"}
        room = self._room(rid)
        if room is None:
            return {"ok": False, "error": f"no such room: {rid}"}
        if identity is not None and identity not in room["participants"]:
            return {"ok": False, "error": "only a participant can delete a room"}
        try:
            with self._lock:
                n = self._db.execute("SELECT COUNT(*) FROM messages WHERE room_id=?",
                                     (rid,)).fetchone()[0]
                self._db.execute("DELETE FROM messages WHERE room_id=?", (rid,))
                self._db.execute("DELETE FROM reads WHERE room_id=?", (rid,))
                self._db.execute("DELETE FROM rooms WHERE id=?", (rid,))
                self._db.commit()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return names.with_name({"ok": True, "room": rid, "title": room["title"],
                                "messages_deleted": n}, room.get("name"))

    # -- avatars -------------------------------------------------------------
    # Small images keyed by identity ("agent:<token name>", "user:operator").
    # Set from the /tokens admin page, shown by the dashboard. Kept in this
    # shared db so both services see the same pictures.

    def set_avatar(self, identity: str, mime: str, data: bytes) -> dict:
        if self._db is None:
            return {"ok": False, "error": "chat store not available"}
        identity = str(identity or "").strip()
        if not identity:
            return {"ok": False, "error": "identity required"}
        if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            return {"ok": False, "error": f"unsupported image type {mime!r}"}
        if not data or len(data) > 256 * 1024:
            return {"ok": False, "error": "image must be 1 byte .. 256 KB"}
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO avatars (identity,mime,data,updated) VALUES (?,?,?,?) "
                    "ON CONFLICT(identity) DO UPDATE SET mime=excluded.mime, "
                    "data=excluded.data, updated=excluded.updated",
                    (identity, mime, sqlite3.Binary(data), time.time()))
                self._db.commit()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "identity": identity, "bytes": len(data)}

    def clear_avatar(self, identity: str) -> dict:
        if self._db is None:
            return {"ok": False, "error": "chat store not available"}
        with self._lock:
            cur = self._db.execute("DELETE FROM avatars WHERE identity=?", (identity,))
            self._db.commit()
        return {"ok": True, "identity": identity, "removed": cur.rowcount > 0}

    def get_avatar(self, identity: str) -> tuple[str, bytes, float] | None:
        if self._db is None:
            return None
        with self._lock:
            r = self._db.execute("SELECT mime,data,updated FROM avatars WHERE identity=?",
                                 (identity,)).fetchone()
        return (r[0], bytes(r[1]), r[2]) if r else None

    def avatar_index(self) -> dict[str, int]:
        """``{identity: updated_epoch}`` for every identity with a picture —
        what a UI needs to decide whether to render an image (and to bust its
        cache when the picture changes)."""
        if self._db is None:
            return {}
        with self._lock:
            rows = self._db.execute("SELECT identity,updated FROM avatars").fetchall()
        return {i: int(u or 0) for (i, u) in rows}

    def _room(self, rid: str) -> dict | None:
        with self._lock:
            r = self._db.execute(
                "SELECT id,title,created,last_activity,participants FROM rooms "
                "WHERE id=?", (rid,)).fetchone()
            name = names.name_of(self._db, "room", r[0]) if r else None
        if not r:
            return None
        return names.with_name({"id": r[0], "title": r[1], "created": r[2],
                                "last_activity": r[3], "participants": json.loads(r[4] or "[]")},
                               name)

    def exists(self, rid: str) -> bool:
        return self._db is not None and self._room(rid) is not None

    def room(self, rid: str) -> dict | None:
        """Public read of a room (id, title, participants), or None. Used by
        the approval holds to tell whether a caller owns a room it is deleting."""
        return self._room(rid) if self._db is not None else None

    def join(self, rid: str, identities: list[str]) -> dict:
        """Add participants to a room without posting (a huddle adds whoever
        opens it, so their unread notices start then)."""
        if self._db is None:
            return {"ok": False, "error": "chat store not available"}
        room = self._room(rid)
        if room is None:
            return {"ok": False, "error": f"no such room: {rid}"}
        parts = room["participants"]
        added = [i for i in dict.fromkeys(str(x).strip() for x in identities) if i and i not in parts]
        if added:
            with self._lock:
                self._set_participants(rid, parts + added)
                self._db.commit()
        return names.with_name({"ok": True, "room": rid, "participants": parts + added,
                                "added": added}, room.get("name"))

    def _set_participants(self, rid: str, parts: list[str]) -> None:
        self._db.execute("UPDATE rooms SET participants=? WHERE id=?",
                         (json.dumps(parts), rid))

    def send(self, rid: str, sender: str, text: str, mentions: list[str],
             expects_reply: bool, quiet: bool = False) -> dict:
        """Post a message. Mentioning a non-participant auto-invites them.
        Returns the new participant list, the mentioned set, and which of them
        are offline (⇒ will get voicemail; a mention of an offline agent whose
        home worker exposes agent.wake is what the caller may then wake)."""
        if self._db is None:
            return {"ok": False, "error": "chat store not available"}
        # Known vault values become {{secret:name}} before anything is stored.
        text = scrub(str(text or "").strip())
        if not text:
            return {"ok": False, "error": "empty message"}
        room = self._room(rid)
        if room is None:
            return {"ok": False, "error": f"no such room: {rid}"}
        parts = room["participants"]
        if sender and sender not in parts:
            parts.append(sender)
        ment = []
        for m in (mentions or []):
            m = str(m).strip()
            if not m:
                continue
            ment.append(m)
            if m not in parts:      # mention auto-invites
                parts.append(m)
        now = time.time()
        try:
            with self._lock:
                cur = self._db.execute(
                    "INSERT INTO messages (room_id,ts,sender,text,mentions,expects_reply,quiet) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (rid, now, sender, _fit(text), json.dumps(ment),
                     1 if expects_reply else 0, 1 if quiet else None))
                seq = cur.lastrowid
                self._set_participants(rid, parts)
                self._db.execute("UPDATE rooms SET last_activity=? WHERE id=?", (now, rid))
                self._db.commit()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        # In a 2-party room the "other" party is implicitly addressed.
        implicit = [p for p in parts if p != sender] if len(parts) == 2 else []
        addressed = ment or implicit
        offline = [a for a in addressed if not self.is_online(a)]
        return names.with_name({"ok": True, "room": rid, "seq": seq, "participants": parts,
                                "mentioned": ment, "addressed": addressed, "offline": offline},
                               room.get("name"))

    def read(self, rid: str, reader: str | None, since_seq: int = 0,
             mark: bool = True, limit: int = 200) -> dict:
        if self._db is None:
            return {"ok": False, "error": "chat store not available"}
        room = self._room(rid)
        if room is None:
            return {"ok": False, "error": f"no such room: {rid}"}
        with self._lock:
            rows = self._db.execute(
                "SELECT seq,ts,sender,text,mentions,expects_reply FROM messages "
                "WHERE room_id=? AND seq>? ORDER BY seq LIMIT ?",
                (rid, int(since_seq), max(1, min(limit, 1000)))).fetchall()
            msgs = [{"seq": s, "ts": t, "sender": snd, "text": txt,
                     "mentions": json.loads(mn or "[]"),
                     "expects_reply": bool(er)} for (s, t, snd, txt, mn, er) in rows]
            if mark and reader and msgs and reader in room["participants"]:
                self._db.execute(
                    "INSERT INTO reads (identity,room_id,last_read_seq) VALUES (?,?,?) "
                    "ON CONFLICT(identity,room_id) DO UPDATE SET last_read_seq=excluded.last_read_seq",
                    (reader, rid, msgs[-1]["seq"]))
                self._db.commit()
        # Masked on read too: messages stored before a secret entered the
        # vault (and the dashboard, which reads this store directly).
        msgs = scrub(msgs)
        return names.with_name({"ok": True, "room": rid, "title": scrub(room["title"]),
                                "participants": room["participants"], "messages": msgs,
                                "last_seq": msgs[-1]["seq"] if msgs else int(since_seq)},
                               room.get("name"))

    def last_seq(self, rid: str) -> int:
        """Highest message seq in a room (0 if empty or unknown) - where a
        new reader such as a chat bridge starts without replaying history."""
        if self._db is None:
            return 0
        with self._lock:
            r = self._db.execute("SELECT MAX(seq) FROM messages WHERE room_id=?",
                                 (rid,)).fetchone()
        return int(r[0] or 0) if r else 0

    def tail(self, rid: str, limit: int = 20) -> list[dict]:
        """The last ``limit`` messages of a room, oldest first (read-only: no
        read watermark moves). For an agent that needs recent context."""
        if self._db is None:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT seq,ts,sender,text,mentions FROM messages WHERE room_id=? "
                "ORDER BY seq DESC LIMIT ?", (rid, max(1, min(int(limit), 500)))).fetchall()
        return [{"seq": s, "ts": t, "sender": snd, "text": txt,
                 "mentions": json.loads(mn or "[]")} for (s, t, snd, txt, mn) in reversed(rows)]

    def room_heads(self, identity: str | None = None) -> list[dict]:
        """``[{room, last_seq, participants}]`` for every room (or only those
        ``identity`` is in), in one query and without unread counts or read
        marks. For a watcher that polls many rooms each tick (the home agent)."""
        if self._db is None:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT r.id, r.participants, "
                "(SELECT MAX(m.seq) FROM messages m WHERE m.room_id=r.id) FROM rooms r"
            ).fetchall()
        out = []
        for (rid, parts, seq) in rows:
            plist = json.loads(parts or "[]")
            if identity is not None and identity not in plist:
                continue
            out.append({"room": rid, "last_seq": int(seq or 0), "participants": plist})
        return out

    def rooms_for(self, identity: str, limit: int = 50,
                  include_all: bool = False) -> dict:
        """Rooms this identity participates in, newest-active first, with unread
        counts (messages past its read watermark, not counting its own).
        ``include_all`` also lists rooms it isn't in (``member: False``, no
        unread count) -- for the dashboard, which oversees every room."""
        if self._db is None:
            return {"ok": False, "error": "chat store not available"}
        with self._lock:
            rows = self._db.execute(
                "SELECT id,title,last_activity,participants FROM rooms "
                "ORDER BY last_activity DESC").fetchall()
            named = names.names_of(self._db, "room", [r[0] for r in rows])
            out = []
            for (rid, title, la, parts) in rows:
                plist = json.loads(parts or "[]")
                member = identity in plist
                if not member and not include_all:
                    continue
                cnt = 0
                if member:
                    wr = self._db.execute(
                        "SELECT last_read_seq FROM reads WHERE identity=? AND room_id=?",
                        (identity, rid)).fetchone()
                    watermark = wr[0] if wr else 0
                    cnt = self._db.execute(
                        "SELECT COUNT(*) FROM messages WHERE room_id=? AND seq>? AND sender!=? "
                        "AND quiet IS NULL",
                        (rid, watermark, identity)).fetchone()[0]
                last = self._db.execute(
                    "SELECT sender,text FROM messages WHERE room_id=? ORDER BY seq DESC LIMIT 1",
                    (rid,)).fetchone()
                out.append({"room": rid, **({"name": named[rid]} if rid in named else {}),
                            "title": scrub(title),
                            "last_activity_age_secs": round(time.time() - la, 1),
                            "participants": plist, "member": member,
                            "unread": cnt,
                            "last_sender": last[0] if last else None,
                            "last_text": (scrub(last[1])[:120] if last else None)})
                if len(out) >= limit:
                    break
        return {"ok": True, "count": len(out), "rooms": out}

    def unread_summary(self, identity: str) -> list[dict]:
        """Per-room unread notices for the voicemail piggyback — the compact
        thing stapled onto tool responses so an agent learns it has messages on
        its next call, without polling."""
        if self._db is None or not identity or identity == "anonymous":
            return []
        r = self.rooms_for(identity)
        return [names.with_name({"room": x["room"], "title": x["title"], "unread": x["unread"],
                                 "from": x["last_sender"]}, x.get("name"))
                for x in r.get("rooms", []) if x["unread"] > 0]

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
