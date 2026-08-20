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

log = logging.getLogger("rook.band_mcp.chat_rooms")

_PRESENCE_ONLINE_SECS = 90.0     # seen within this window ⇒ "online"


class ChatStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        d = os.path.dirname(path) or "."
        try:
            os.makedirs(d, exist_ok=True)
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
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
            """)
            self._db.commit()
        except Exception:
            log.exception("chat store init failed; chat disabled")
            self._db = None

    @property
    def enabled(self) -> bool:
        return self._db is not None

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
                self._db.commit()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "room": rid, "title": title, "participants": parts}

    def _room(self, rid: str) -> dict | None:
        with self._lock:
            r = self._db.execute(
                "SELECT id,title,created,last_activity,participants FROM rooms "
                "WHERE id=?", (rid,)).fetchone()
        if not r:
            return None
        return {"id": r[0], "title": r[1], "created": r[2],
                "last_activity": r[3], "participants": json.loads(r[4] or "[]")}

    def _set_participants(self, rid: str, parts: list[str]) -> None:
        self._db.execute("UPDATE rooms SET participants=? WHERE id=?",
                         (json.dumps(parts), rid))

    def send(self, rid: str, sender: str, text: str, mentions: list[str],
             expects_reply: bool) -> dict:
        """Post a message. Mentioning a non-participant auto-invites them.
        Returns the new participant list, the mentioned set, and which of them
        are offline (⇒ will get voicemail; a mention of an offline agent whose
        home worker exposes agent.wake is what the caller may then wake)."""
        if self._db is None:
            return {"ok": False, "error": "chat store not available"}
        text = str(text or "").strip()
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
                self._db.execute(
                    "INSERT INTO messages (room_id,ts,sender,text,mentions,expects_reply) "
                    "VALUES (?,?,?,?,?,?)",
                    (rid, now, sender, text[:8000], json.dumps(ment),
                     1 if expects_reply else 0))
                self._set_participants(rid, parts)
                self._db.execute("UPDATE rooms SET last_activity=? WHERE id=?", (now, rid))
                self._db.commit()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        # In a 2-party room the "other" party is implicitly addressed.
        implicit = [p for p in parts if p != sender] if len(parts) == 2 else []
        addressed = ment or implicit
        offline = [a for a in addressed if not self.is_online(a)]
        return {"ok": True, "room": rid, "participants": parts,
                "mentioned": ment, "addressed": addressed, "offline": offline}

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
            if mark and reader and msgs:
                self._db.execute(
                    "INSERT INTO reads (identity,room_id,last_read_seq) VALUES (?,?,?) "
                    "ON CONFLICT(identity,room_id) DO UPDATE SET last_read_seq=excluded.last_read_seq",
                    (reader, rid, msgs[-1]["seq"]))
                self._db.commit()
        return {"ok": True, "room": rid, "title": room["title"],
                "participants": room["participants"], "messages": msgs,
                "last_seq": msgs[-1]["seq"] if msgs else int(since_seq)}

    def rooms_for(self, identity: str, limit: int = 50) -> dict:
        """Rooms this identity participates in, newest-active first, with unread
        counts (messages past its read watermark, not counting its own)."""
        if self._db is None:
            return {"ok": False, "error": "chat store not available"}
        with self._lock:
            rows = self._db.execute(
                "SELECT id,title,last_activity,participants FROM rooms "
                "ORDER BY last_activity DESC").fetchall()
            out = []
            for (rid, title, la, parts) in rows:
                plist = json.loads(parts or "[]")
                if identity not in plist:
                    continue
                wr = self._db.execute(
                    "SELECT last_read_seq FROM reads WHERE identity=? AND room_id=?",
                    (identity, rid)).fetchone()
                watermark = wr[0] if wr else 0
                cnt = self._db.execute(
                    "SELECT COUNT(*) FROM messages WHERE room_id=? AND seq>? AND sender!=?",
                    (rid, watermark, identity)).fetchone()[0]
                last = self._db.execute(
                    "SELECT sender,text FROM messages WHERE room_id=? ORDER BY seq DESC LIMIT 1",
                    (rid,)).fetchone()
                out.append({"room": rid, "title": title,
                            "last_activity_age_secs": round(time.time() - la, 1),
                            "participants": plist, "unread": cnt,
                            "last_sender": last[0] if last else None,
                            "last_text": (last[1][:120] if last else None)})
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
        return [{"room": x["room"], "title": x["title"], "unread": x["unread"],
                 "from": x["last_sender"]}
                for x in r.get("rooms", []) if x["unread"] > 0]

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
