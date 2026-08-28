"""Console rooms — named, searchable, band-visible terminal sessions.

A console room is a chat room whose main speaker is a process. The site pumps
``proc.read`` from the worker and appends the output here; every agent on the
band, plus the dashboard, reads it by ``seq`` at its own pace. Writing to the
room feeds the process's stdin. One reliable puller on the request/reply path,
fan-out at the site — nothing streams over the band's broadcast relay.

Two things make this an archive rather than a scrollback:

  * **Rooms are named for the task, not the command.** ``proc.start`` demands a
    label ("set up the model on kaiju"), and that title carries most of the
    retrieval weight — raw console output is terrible search corpus.
  * **A frozen room is permanent.** When the process exits, the room takes a
    closing summary and freezes: immutable, still readable, still indexed. The
    process is gone; the record of what was done is not. Later passes mine
    these to write documentation.

Retention is a ring: frozen rooms are evicted oldest-first past MAX_ROOMS (or
MAX_TOTAL_BYTES, so one runaway build can't outweigh a thousand real sessions).
Live rooms are never evicted.

Text is sanitized on the way in — ANSI escapes and carriage-return redraws
stripped, obvious secrets redacted — because unlike a terminal this corpus is
permanent and full-text searchable by anyone on the band.

sqlite (FTS5) next to the journal and chat stores.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid

log = logging.getLogger("rook.band_mcp.console_rooms")

MAX_ROOMS = 1000                     # frozen rooms kept; oldest evicted past this
MAX_TOTAL_BYTES = 512 * 1024 * 1024  # …or this much text, whichever bites first
MAX_LINE = 4000                      # per-row text cap
CLOSING_GRACE_SECS = 900.0           # unsummarized 'closing' rooms freeze anyway

# Terminal control noise: CSI/OSC escapes, then bare control chars.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Obvious secrets. Deliberately narrow — this redacts values that announce
# themselves, not anything that merely looks random.
_SECRETS = [
    (re.compile(r"(?i)\b([a-z0-9_]*(?:api[_-]?key|token|secret|password|passwd|"
                r"pwd|bearer|authorization))\b(\s*[:=]\s*|\s+)"
                r"(['\"]?)([^\s'\"]{6,})\3"), r"\1\2\3«redacted»\3"),
    (re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), "«redacted-aws-key»"),
    (re.compile(r"\b((?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,})\b"), "«redacted-gh-token»"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "«redacted-gh-pat»"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), "«redacted-api-key»"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "«redacted-slack-token»"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.S), "«redacted-private-key»"),
]


def sanitize(text: str) -> str:
    """Strip terminal control noise and redact obvious secrets.

    Carriage-return redraws (progress bars, spinners) collapse to their final
    state — a 400-line download bar becomes the one line it ended on, which is
    what a reader and the full-text index both actually want.
    """
    if not text:
        return ""
    text = _ANSI.sub("", text)
    out = []
    for line in text.split("\n"):
        # A trailing CR is just the other half of a CRLF — a pty writes every
        # line that way. Drop it BEFORE collapsing redraws, or splitting on it
        # leaves the empty string and erases the whole line.
        if line.endswith("\r"):
            line = line[:-1]
        if "\r" in line:
            # Keep only what survived the last redraw of this line.
            line = line.split("\r")[-1]
        out.append(line)
    text = "\n".join(out)
    text = _CTRL.sub("", text)
    for pat, repl in _SECRETS:
        text = pat.sub(repl, text)
    return text


class ConsoleStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        # Per-room trailing partial line, waiting for its newline.
        self._pending: dict[str, str] = {}
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS rooms (
                    id TEXT PRIMARY KEY,
                    title TEXT, worker TEXT, worker_name TEXT,
                    handle TEXT, cmd TEXT, pty INTEGER,
                    state TEXT,              -- live | closing | frozen
                    opened_by TEXT, participants TEXT,
                    created REAL, last_activity REAL, ended REAL,
                    exit_code INTEGER, bytes INTEGER, cursor INTEGER,
                    summary TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_rooms_state
                    ON rooms(state, last_activity);
                CREATE TABLE IF NOT EXISTS lines (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT, ts REAL, stream TEXT, sender TEXT, text TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_lines_room ON lines(room_id, seq);
                CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
                    text, room_id UNINDEXED, seq UNINDEXED, tokenize='porter'
                );
            """)
            self._db.commit()
        except Exception:
            log.exception("console store init failed; console rooms disabled")
            self._db = None

    @property
    def enabled(self) -> bool:
        return self._db is not None

    # -- lifecycle ---------------------------------------------------------

    def open(self, *, title: str, worker: str, worker_name: str, handle: str,
             cmd: str, pty: bool, opened_by: str) -> dict:
        """Register a live console room for a just-started process."""
        if self._db is None:
            return {"ok": False, "error": "console store not available"}
        rid = uuid.uuid4().hex[:16]
        now = time.time()
        title = str(title or "console")[:200]
        with self._lock:
            self._db.execute(
                "INSERT INTO rooms (id,title,worker,worker_name,handle,cmd,pty,"
                "state,opened_by,participants,created,last_activity,ended,"
                "exit_code,bytes,cursor,summary) "
                "VALUES (?,?,?,?,?,?,?,'live',?,?,?,?,NULL,NULL,0,0,NULL)",
                (rid, title, worker, worker_name, handle, cmd[:2000],
                 1 if pty else 0, opened_by, json.dumps([opened_by]), now, now))
            # The title and command are the highest-signal thing in the room —
            # index them as its first searchable row.
            self._db.execute(
                "INSERT INTO search (text,room_id,seq) VALUES (?,?,0)",
                (f"{title}\n{worker_name}\n{cmd}", rid))
            self._db.commit()
        log.info("console room opened: %s %r on %s", rid, title, worker_name)
        return {"ok": True, "room": rid, "title": title, "worker": worker_name,
                "handle": handle, "state": "live"}

    def append(self, rid: str, text: str, *, stream: str = "out",
               sender: str | None = None, flush: bool = False) -> int:
        """Append sanitized output as whole lines. Returns the last seq written
        (0 if nothing landed).

        Reads arrive on arbitrary byte boundaries, so a chunk routinely ends
        mid-line. The trailing partial line is held until the rest of it shows
        up (or ``flush``/room close forces it out) — otherwise one line would
        split across two rows and neither half would match a search.

        Only the process's own ``out`` stream is buffered that way. Lines we
        write ourselves (``in`` echoes, ``sys`` notices) are already complete,
        so they go straight through — held behind a half-finished prompt like
        ``Password: `` they would be swallowed into it and lose their stream.
        """
        if self._db is None:
            return 0
        text = sanitize(text)
        if stream == "out":
            text = self._pending.pop(rid, "") + text
            parts = text.split("\n")
            if flush:
                lines = parts
            else:
                # The last element has no terminating newline yet.
                self._pending[rid] = parts.pop()
                lines = parts
        else:
            lines = text.split("\n")
        lines = [ln for ln in lines if ln.strip()]
        if not lines:
            return 0
        now = time.time()
        written = 0
        with self._lock:
            seq = 0
            for ln in lines:
                for part in ([ln[i:i + MAX_LINE]
                              for i in range(0, len(ln), MAX_LINE)] or [ln]):
                    cur = self._db.execute(
                        "INSERT INTO lines (room_id,ts,stream,sender,text) "
                        "VALUES (?,?,?,?,?)", (rid, now, stream, sender, part))
                    seq = cur.lastrowid
                    self._db.execute(
                        "INSERT INTO search (text,room_id,seq) VALUES (?,?,?)",
                        (part, rid, seq))
                    written += len(part)
            self._db.execute(
                "UPDATE rooms SET last_activity=?, bytes=bytes+? WHERE id=?",
                (now, written, rid))
            self._db.commit()
        return seq

    def set_cursor(self, rid: str, cursor: int) -> None:
        """Remember how far the pump has drained this session's output."""
        if self._db is None:
            return
        with self._lock:
            self._db.execute("UPDATE rooms SET cursor=? WHERE id=?", (int(cursor), rid))
            self._db.commit()

    def mark_closing(self, rid: str, exit_code: int | None) -> None:
        """Process exited: stop accepting output, await a closing summary."""
        if self._db is None:
            return
        now = time.time()
        self.append(rid, "", stream="out", flush=True)   # flush partial line
        self.append(rid, f"— process exited (code {exit_code}) —", stream="sys")
        with self._lock:
            self._db.execute(
                "UPDATE rooms SET state='closing', ended=?, exit_code=? "
                "WHERE id=? AND state='live'", (now, exit_code, rid))
            self._db.commit()
        log.info("console room closing: %s (exit %s)", rid, exit_code)

    def freeze(self, rid: str, summary: str | None = None,
               by: str | None = None) -> dict:
        """Freeze a room permanently, with an optional closing summary.

        The summary is what makes this findable a month later — raw output
        rarely contains the words anyone will search for. It is indexed with
        the title, not buried in the transcript.
        """
        if self._db is None:
            return {"ok": False, "error": "console store not available"}
        room = self.get(rid)
        if room is None:
            return {"ok": False, "error": f"no such console room: {rid}"}
        if room["state"] == "frozen" and not summary:
            return {"ok": True, "room": rid, "state": "frozen",
                    "note": "already frozen"}
        summary = (summary or "").strip()[:4000] or None
        with self._lock:
            self._db.execute(
                "UPDATE rooms SET state='frozen', summary=COALESCE(?,summary), "
                "ended=COALESCE(ended,?) WHERE id=?", (summary, time.time(), rid))
            if summary:
                self._db.execute(
                    "INSERT INTO search (text,room_id,seq) VALUES (?,?,0)",
                    (f"summary: {summary}", rid))
            self._db.commit()
        if summary:
            self.append(rid, f"— summary ({by or 'agent'}): {summary}", stream="sys")
        self.evict()
        return {"ok": True, "room": rid, "state": "frozen",
                "summary": summary, "exit_code": room.get("exit_code")}

    def sweep_closing(self) -> int:
        """Freeze 'closing' rooms nobody came back to summarize. An agent that
        wandered off shouldn't leave a room live forever."""
        if self._db is None:
            return 0
        cut = time.time() - CLOSING_GRACE_SECS
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM rooms WHERE state='closing' AND ended<?",
                (cut,)).fetchall()
        for (rid,) in rows:
            self.freeze(rid)
        return len(rows)

    # -- reading -----------------------------------------------------------

    def get(self, rid: str) -> dict | None:
        if self._db is None:
            return None
        with self._lock:
            r = self._db.execute(
                "SELECT id,title,worker,worker_name,handle,cmd,pty,state,"
                "opened_by,participants,created,last_activity,ended,exit_code,"
                "bytes,cursor,summary FROM rooms WHERE id=?", (rid,)).fetchone()
        if not r:
            return None
        return {"room": r[0], "title": r[1], "worker": r[2], "worker_name": r[3],
                "handle": r[4], "cmd": r[5], "pty": bool(r[6]), "state": r[7],
                "opened_by": r[8], "participants": json.loads(r[9] or "[]"),
                "created": r[10], "last_activity": r[11], "ended": r[12],
                "exit_code": r[13], "bytes": r[14], "cursor": r[15],
                "summary": r[16]}

    def read(self, rid: str, since_seq: int = 0, limit: int = 300,
             tail: bool = False) -> dict:
        """Lines newer than ``since_seq``. ``tail=True`` returns the LAST
        ``limit`` lines instead — what you want when attaching to a room that
        already has thousands."""
        if self._db is None:
            return {"ok": False, "error": "console store not available"}
        room = self.get(rid)
        if room is None:
            return {"ok": False, "error": f"no such console room: {rid}"}
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            if tail:
                rows = self._db.execute(
                    "SELECT seq,ts,stream,sender,text FROM lines WHERE room_id=? "
                    "ORDER BY seq DESC LIMIT ?", (rid, limit)).fetchall()[::-1]
            else:
                rows = self._db.execute(
                    "SELECT seq,ts,stream,sender,text FROM lines WHERE room_id=? "
                    "AND seq>? ORDER BY seq LIMIT ?",
                    (rid, int(since_seq), limit)).fetchall()
            total = self._db.execute(
                "SELECT COUNT(*) FROM lines WHERE room_id=?", (rid,)).fetchone()[0]
        lines = [{"seq": s, "ts": t, "stream": st, "sender": sn, "text": tx}
                 for (s, t, st, sn, tx) in rows]
        room.update({"ok": True, "lines": lines, "line_count": total,
                     "last_seq": lines[-1]["seq"] if lines else int(since_seq)})
        return room

    def list(self, *, worker: str | None = None, state: str | None = None,
             limit: int = 50) -> dict:
        if self._db is None:
            return {"ok": False, "error": "console store not available"}
        sql = ("SELECT id,title,worker_name,state,created,last_activity,"
               "exit_code,bytes,summary FROM rooms WHERE 1=1")
        params: list = []
        if worker:
            sql += " AND (worker=? OR worker_name=?)"
            params += [worker, worker]
        if state:
            sql += " AND state=?"
            params.append(state)
        sql += " ORDER BY last_activity DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        now = time.time()
        return {"ok": True, "count": len(rows), "rooms": [
            {"room": r[0], "title": r[1], "worker": r[2], "state": r[3],
             "age_secs": round(now - r[4], 1),
             "last_activity_age_secs": round(now - r[5], 1),
             "exit_code": r[6], "bytes": r[7],
             "summary": (r[8][:200] if r[8] else None)} for r in rows]}

    def live_rooms(self) -> list[dict]:
        """Rooms the pump should still be draining."""
        if self._db is None:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT id,worker,handle,cursor FROM rooms WHERE state='live'").fetchall()
        return [{"room": r[0], "worker": r[1], "handle": r[2], "cursor": r[3]}
                for r in rows]

    # -- search ------------------------------------------------------------

    def search(self, query: str, *, worker: str | None = None,
               limit: int = 20, per_room: int = 3) -> dict:
        """Full-text search across every console room, live and frozen.

        Ranked hits are grouped by room and returned with the room's title,
        summary and the matching ``seq`` — so a caller can jump straight to
        that offset with ``read(since_seq=seq-1)`` instead of pulling the whole
        transcript.
        """
        if self._db is None:
            return {"ok": False, "error": "console store not available"}
        q = str(query or "").strip()
        if not q:
            return {"ok": False, "error": "query required"}
        # Bare words are ANDed as a phrase-ish query; anything with FTS
        # operators is passed through untouched.
        if not re.search(r'["*:()]|\bOR\b|\bAND\b|\bNOT\b', q):
            terms = [t for t in re.split(r"\s+", q) if t]
            q = " ".join(f'"{t}"' for t in terms)
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT s.room_id, s.seq, snippet(search,0,'«','»','…',16), rank "
                    "FROM search s WHERE search MATCH ? ORDER BY rank LIMIT ?",
                    (q, max(1, min(int(limit), 200)) * 8)).fetchall()
        except sqlite3.OperationalError as e:
            return {"ok": False, "error": f"bad search query: {e}"}

        grouped: dict[str, dict] = {}
        for (rid, seq, snip, rank) in rows:
            room = self.get(rid)
            if room is None:
                continue
            if worker and worker not in (room["worker"], room["worker_name"]):
                continue
            g = grouped.setdefault(rid, {
                "room": rid, "title": room["title"], "worker": room["worker_name"],
                "state": room["state"], "exit_code": room["exit_code"],
                "summary": room["summary"], "cmd": room["cmd"],
                "age_secs": round(time.time() - room["created"], 1),
                "score": rank, "hits": []})
            if len(g["hits"]) < per_room:
                g["hits"].append({"seq": seq, "snippet": snip})
            if len(grouped) >= limit and rid not in grouped:
                break
        out = sorted(grouped.values(), key=lambda g: g["score"])[:limit]
        return {"ok": True, "query": query, "count": len(out), "results": out}

    # -- retention ---------------------------------------------------------

    def evict(self) -> int:
        """Ring-prune frozen rooms past MAX_ROOMS / MAX_TOTAL_BYTES, oldest
        first. Live and closing rooms are never touched."""
        if self._db is None:
            return 0
        removed = 0
        try:
            with self._lock:
                frozen = self._db.execute(
                    "SELECT id,bytes FROM rooms WHERE state='frozen' "
                    "ORDER BY last_activity DESC").fetchall()
                total = sum(b or 0 for (_i, b) in frozen)
                doomed: list[str] = []
                for idx, (rid, b) in enumerate(frozen):
                    if idx >= MAX_ROOMS or total > MAX_TOTAL_BYTES:
                        doomed.append(rid)
                        total -= (b or 0)
                for rid in doomed:
                    self._pending.pop(rid, None)
                    self._db.execute("DELETE FROM lines WHERE room_id=?", (rid,))
                    self._db.execute("DELETE FROM search WHERE room_id=?", (rid,))
                    self._db.execute("DELETE FROM rooms WHERE id=?", (rid,))
                    removed += 1
                if removed:
                    self._db.commit()
        except Exception:
            log.debug("console evict failed", exc_info=True)
        if removed:
            log.info("console retention: evicted %d frozen room(s)", removed)
        return removed

    def delete(self, rid: str) -> dict:
        if self._db is None:
            return {"ok": False, "error": "console store not available"}
        room = self.get(rid)
        if room is None:
            return {"ok": False, "error": f"no such console room: {rid}"}
        self._pending.pop(rid, None)
        with self._lock:
            self._db.execute("DELETE FROM lines WHERE room_id=?", (rid,))
            self._db.execute("DELETE FROM search WHERE room_id=?", (rid,))
            self._db.execute("DELETE FROM rooms WHERE id=?", (rid,))
            self._db.commit()
        return {"ok": True, "room": rid, "title": room["title"]}

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
