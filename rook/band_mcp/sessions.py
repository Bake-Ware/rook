"""Handoff records — universal sessions any agent can pick up.

Full-transcript resume across tools (Claude Code ↔ hermes ↔ whatever) is a
non-goal — different formats, different tools, different context semantics. A
*handoff* is the structured 90%: goal, current state, decisions made, artifacts
touched, next steps, plus an optional pointer to the raw transcript. Stored on
the site, keyed by ``thread_id`` (shared with the journal and, later, chat
rooms — one threads registry, several record types), so a session started in
one agent can be continued in another.

The read path does the freshness work (design §7): fetching a handoff ALWAYS
surfaces how old it is and whether something has superseded it, so an agent
can't silently treat stale state as current — the antidote to "outdated info
becoming fact". A newer handoff for the same thread supersedes the older; an
explicit ``supersedes`` marks cross-thread replacement.

sqlite next to the journal. This is exposed as MCP tools (rook_handoff_*),
reachable by any agent on the rook MCP.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid

log = logging.getLogger("rook.band_mcp.sessions")


class SessionStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        d = os.path.dirname(path) or "."
        try:
            os.makedirs(d, exist_ok=True)
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS handoffs (
                    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
                    id         TEXT,
                    thread_id  TEXT,
                    ts         REAL,
                    author     TEXT,
                    status     TEXT,      -- active | superseded | closed
                    goal       TEXT,
                    state      TEXT,
                    decisions  TEXT,      -- JSON array
                    next_steps TEXT,      -- JSON array
                    artifacts  TEXT,      -- JSON array
                    supersedes TEXT,      -- JSON array of thread_ids/ids
                    transcript_ref TEXT
                )
            """)
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_ho_thread ON handoffs(thread_id)")
            self._db.commit()
        except Exception:
            log.exception("session store init failed; handoffs disabled")
            self._db = None

    @property
    def enabled(self) -> bool:
        return self._db is not None

    @staticmethod
    def _as_list(v) -> list[str]:
        """Normalize decisions/next_steps/artifacts to a list of strings.
        Accepts a list, or a string with one item per line (agents tend to
        pass a bulleted blob)."""
        if v is None:
            return []
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            return [ln.strip(" -*\t") for ln in v.replace("\r", "").split("\n")
                    if ln.strip(" -*\t")]
        return [str(v).strip()]

    def save(self, *, thread_id: str | None, author: str | None, goal: str,
             state: str = "", decisions=None, next_steps=None, artifacts=None,
             supersedes=None, transcript_ref: str | None = None,
             status: str = "active") -> dict:
        """Write a handoff. A new handoff for an existing thread marks the
        thread's prior handoffs ``superseded`` (latest-wins), so ``get`` returns
        the current one with the history visible beneath it."""
        if self._db is None:
            return {"ok": False, "error": "session store not available"}
        tid = (thread_id or "").strip() or uuid.uuid4().hex[:16]
        hid = uuid.uuid4().hex[:16]
        row = (hid, tid, round(time.time(), 3), author or "anonymous", status,
               str(goal or "").strip(), str(state or "").strip(),
               json.dumps(self._as_list(decisions)),
               json.dumps(self._as_list(next_steps)),
               json.dumps(self._as_list(artifacts)),
               json.dumps(self._as_list(supersedes)),
               transcript_ref)
        try:
            with self._lock:
                # supersede prior active handoffs on the same thread
                self._db.execute(
                    "UPDATE handoffs SET status='superseded' WHERE thread_id=? "
                    "AND status='active'", (tid,))
                self._db.execute(
                    "INSERT INTO handoffs (id, thread_id, ts, author, status, "
                    "goal, state, decisions, next_steps, artifacts, supersedes, "
                    "transcript_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
                self._db.commit()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "id": hid, "thread_id": tid, "status": status}

    def _row_to_dict(self, r) -> dict:
        (hid, tid, ts, author, status, goal, state, dec, nxt, art, sup, tref) = r
        age = max(0, int(time.time() - ts))
        d = {"id": hid, "thread_id": tid, "ts": ts,
             "as_of": _ago(ts), "age_secs": age, "author": author,
             "status": status, "goal": goal, "state": state,
             "decisions": json.loads(dec or "[]"),
             "next_steps": json.loads(nxt or "[]"),
             "artifacts": json.loads(art or "[]")}
        if json.loads(sup or "[]"):
            d["supersedes"] = json.loads(sup)
        if tref:
            d["transcript_ref"] = tref
        # Freshness banner — always present so stale state can't pass unnoticed.
        notes = []
        if status == "superseded":
            notes.append("SUPERSEDED — a newer handoff exists for this thread; "
                         "this is history, not current state.")
        if age > 3 * 86400:
            notes.append(f"STALE — last updated {_ago(ts)}; verify before "
                         f"treating as current.")
        d["freshness"] = notes or ["current"]
        return d

    def get(self, thread_id: str, history: bool = True) -> dict:
        """Return the current (active) handoff for a thread, plus prior
        superseded ones as history. The freshness banner is always attached."""
        if self._db is None:
            return {"ok": False, "error": "session store not available"}
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT id,thread_id,ts,author,status,goal,state,decisions,"
                    "next_steps,artifacts,supersedes,transcript_ref FROM handoffs "
                    "WHERE thread_id=? ORDER BY seq DESC", (thread_id.strip(),)).fetchall()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if not rows:
            return {"ok": False, "error": f"no handoff for thread {thread_id!r}"}
        current = self._row_to_dict(rows[0])
        out = {"ok": True, "current": current}
        if history and len(rows) > 1:
            out["history"] = [self._row_to_dict(r) for r in rows[1:]]
        return out

    def list_recent(self, limit: int = 20, active_only: bool = True) -> dict:
        """List recent threads (one row per thread — its latest handoff)."""
        if self._db is None:
            return {"ok": False, "error": "session store not available"}
        try:
            with self._lock:
                # latest handoff per thread
                rows = self._db.execute(
                    "SELECT id,thread_id,ts,author,status,goal,state,decisions,"
                    "next_steps,artifacts,supersedes,transcript_ref FROM handoffs h "
                    "WHERE seq IN (SELECT MAX(seq) FROM handoffs GROUP BY thread_id) "
                    "ORDER BY ts DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        items = [self._row_to_dict(r) for r in rows]
        if active_only:
            items = [i for i in items if i["status"] == "active"]
        # Trim heavy fields for the listing.
        for i in items:
            i.pop("state", None)
        return {"ok": True, "count": len(items), "threads": items}

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass


def _ago(ts: float) -> str:
    d = max(0, int(time.time() - ts))
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"
