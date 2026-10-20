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
import threading
import time
import uuid

from ..core import names
from ..core.db import open_db
from .secret_mask import scrub

log = logging.getLogger("rook.band_mcp.sessions")


class SessionStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        d = os.path.dirname(path) or "."
        try:
            os.makedirs(d, exist_ok=True)
            self._db = open_db(path, row_factory=False, mkdir=False)
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
            # Speakable names (rook.core.names), one per thread: threads
            # saved by an older release get theirs here.
            names.ensure_table(self._db)
            names.backfill(self._db, "handoff", [r[0] for r in self._db.execute(
                "SELECT DISTINCT thread_id FROM handoffs WHERE thread_id NOT IN "
                "(SELECT id FROM record_names WHERE kind='handoff')")])
            self._db.commit()
        except Exception:
            log.exception("session store init failed; handoffs disabled")
            self._db = None

    @property
    def enabled(self) -> bool:
        return self._db is not None

    # -- names ---------------------------------------------------------------

    def name_of(self, thread_id: str) -> str | None:
        if self._db is None or not thread_id:
            return None
        with self._lock:
            return names.name_of(self._db, "handoff", thread_id)

    def names_of(self, ids) -> dict:
        if self._db is None:
            return {}
        with self._lock:
            return names.names_of(self._db, "handoff", ids)

    def _exists(self, tid: str) -> bool:
        return self._db.execute("SELECT 1 FROM handoffs WHERE thread_id=? LIMIT 1",
                                (tid,)).fetchone() is not None

    def resolve_ref(self, ref, *, write: bool) -> names.Resolved:
        """A thread id, or the thread's speakable name (any case and
        separators), as the thread id. A read takes one clear near miss; a
        write needs the exact name. Raises names.NameError_ with candidates."""
        if self._db is None:
            return names.Resolved(id=str(ref or "").strip())
        with self._lock:
            return names.resolve(self._db, "handoff", ref, exists=self._exists,
                                 write=write, noun="handoff thread")

    def resolve_new(self, ref) -> names.Resolved:
        """``resolve_ref`` for a save: callers may start a thread under any
        id of their own, so an unknown ref that merely looks like three
        words is a new thread id. One made of name words, or close to an
        existing name, is refused (it reads as a mistyped name)."""
        try:
            return self.resolve_ref(ref, write=True)
        except names.NameError_ as e:
            if e.candidates or names.is_name(ref):
                raise
            return names.Resolved(id=str(ref).strip())

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
        the current one with the history visible beneath it. ``thread_id``
        may be the thread's name (exact)."""
        if self._db is None:
            return {"ok": False, "error": "session store not available"}
        try:
            tid = self.resolve_new(thread_id).id if (thread_id or "").strip() else ""
        except names.NameError_ as e:
            return {"ok": False, **e.payload()}
        tid = tid or uuid.uuid4().hex[:16]
        hid = uuid.uuid4().hex[:16]
        # Known vault values become {{secret:name}} before anything is stored.
        row = (hid, tid, round(time.time(), 3), author or "anonymous", status,
               scrub(str(goal or "").strip()), scrub(str(state or "").strip()),
               json.dumps(scrub(self._as_list(decisions))),
               json.dumps(scrub(self._as_list(next_steps))),
               json.dumps(scrub(self._as_list(artifacts))),
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
                name = names.assign(self._db, "handoff", tid)
                self._db.commit()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "id": hid, "thread_id": tid, "name": name, "status": status}

    def _row_to_dict(self, r, name: str | None = None) -> dict:
        (hid, tid, ts, author, status, goal, state, dec, nxt, art, sup, tref) = r
        age = max(0, int(time.time() - ts))
        d = {"id": hid, "thread_id": tid, **({"name": name} if name else {}), "ts": ts,
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
        if status == "closed":
            notes.append("CLOSED — this thread was closed; saving a new handoff "
                         "on it reopens it.")
        elif age > 3 * 86400:
            notes.append(f"STALE — last updated {_ago(ts)}; verify before "
                         f"treating as current.")
        d["freshness"] = notes or ["current"]
        return d

    def get(self, thread_id: str, history: bool = True) -> dict:
        """Return the current (active) handoff for a thread, plus prior
        superseded ones as history. The freshness banner is always attached.
        ``thread_id`` may be the thread's name; a read takes one clear near
        miss (marked ``resolved``)."""
        if self._db is None:
            return {"ok": False, "error": "session store not available"}
        try:
            got = self.resolve_ref(thread_id, write=False)
        except names.NameError_ as e:
            return {"ok": False, **e.payload()}
        tid = got.id
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT id,thread_id,ts,author,status,goal,state,decisions,"
                    "next_steps,artifacts,supersedes,transcript_ref FROM handoffs "
                    "WHERE thread_id=? ORDER BY seq DESC", (tid,)).fetchall()
                name = names.name_of(self._db, "handoff", tid)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if not rows:
            return {"ok": False, "error": f"no handoff for thread {thread_id!r}"}
        current = self._row_to_dict(rows[0], name)
        out = {"ok": True, "current": current}
        if got.via == "fuzzy":
            out["resolved"] = {"from": thread_id, "via": "fuzzy"}
        if history and len(rows) > 1:
            out["history"] = [self._row_to_dict(r, name) for r in rows[1:]]
        return scrub(out)

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
                named = names.names_of(self._db, "handoff", [r[1] for r in rows])
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        items = [self._row_to_dict(r, named.get(r[1])) for r in rows]
        if active_only:
            items = [i for i in items if i["status"] == "active"]
        # Trim heavy fields for the listing.
        for i in items:
            i.pop("state", None)
        return {"ok": True, "count": len(items), "threads": scrub(items)}

    def close_thread(self, thread_id: str, author: str | None = None,
                     reason: str = "") -> dict:
        """Close a thread: a new closing entry (who, why) that keeps the goal.
        Closed threads leave the default listing; history stays readable.
        ``thread_id`` may be the thread's exact name."""
        if self._db is None:
            return {"ok": False, "error": "session store not available"}
        try:
            tid = self.resolve_ref(thread_id, write=True).id
        except names.NameError_ as e:
            return {"ok": False, **e.payload()}
        cur = self.get(tid, history=False)
        if not cur.get("ok"):
            return cur
        if cur["current"]["status"] == "closed":
            return names.with_name({"ok": True, "thread_id": tid, "status": "closed",
                                    "already": True}, cur["current"].get("name"))
        return self.save(thread_id=tid, author=author, goal=cur["current"]["goal"],
                         state=reason or "Closed.", status="closed")

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
