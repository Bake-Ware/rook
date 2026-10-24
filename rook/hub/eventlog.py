"""The log: one plain line for every decision and reminder (Bake, 2026-10-23:
"30 days? Summarize justification for action from event not details").

The first piece of the events design (Rook knowledge
events-consolidation-model-20261023, section 4): an append-only table of
``event_log`` rows. Each row says **what** happened (a short plain line),
**why** (the summarized reason: "6 decisions waiting and the room was quiet",
never a message's text, a command's arguments or what someone said), and
**how** it was decided or delivered (by the person on the phone, by voice
with their voiceprint, on the dashboard; queued; spoken). ``ref`` points back
to the hold, card or question it is about (internal, never shown) and
``outcome`` uses the design's words (ask, ask_now, tell, handle, drop) or
``decided`` for an answer.

Rows older than :data:`KEEP_DAYS` are deleted on every write. Writes open
their own short transaction, so both hub processes may log to the same file.
"""
from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("rook.hub.eventlog")

KEEP_DAYS = 30
WHAT_MAX, WHY_MAX, HOW_MAX, REF_MAX, SOURCE_MAX = 140, 200, 120, 64, 40
OUTCOMES = ("ask", "ask_now", "tell", "handle", "drop", "decided", "reminded", "skipped")
LIMIT_MAX = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    source TEXT NOT NULL,
    outcome TEXT NOT NULL,
    what TEXT NOT NULL,
    why TEXT NOT NULL DEFAULT '',
    how TEXT NOT NULL DEFAULT '',
    ref TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS event_log_at ON event_log(at);
"""


def _one_line(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


class EventLog:
    """``add`` a line, ``recent`` to read them (newest first)."""

    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self._ready = False

    def _conn(self):
        from ..core.db import transaction
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with transaction(self.path, begin=None) as conn:
                conn.executescript(SCHEMA)
            with contextlib.suppress(OSError):
                self.path.chmod(0o600)
            self._ready = True
        return transaction(self.path)

    def add(self, source: str, outcome: str, what: str, why: str = "", how: str = "",
            ref: str = "") -> dict | None:
        """Write one line; never raises (the log never stops what it records)."""
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome: one of {', '.join(OUTCOMES)}")
        row = {"at": self.clock(), "source": _one_line(source, SOURCE_MAX) or "hub",
               "outcome": outcome, "what": _one_line(what, WHAT_MAX),
               "why": _one_line(why, WHY_MAX), "how": _one_line(how, HOW_MAX),
               "ref": _one_line(ref, REF_MAX)}
        if not row["what"]:
            raise ValueError("what: one plain line")
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "INSERT INTO event_log (at, source, outcome, what, why, how, ref) "
                    "VALUES (:at, :source, :outcome, :what, :why, :how, :ref)", row)
                row["id"] = cur.lastrowid
                conn.execute("DELETE FROM event_log WHERE at < ?",
                             (row["at"] - KEEP_DAYS * 86400,))
        except Exception:  # noqa: BLE001
            log.exception("event log: writing %s failed", outcome)
            return None
        return row

    def recent(self, limit: int = 50, since: float = 0.0, ref: str = "") -> list[dict]:
        limit = max(1, min(int(limit or 50), LIMIT_MAX))
        if not self.path.exists():
            return []
        q = "SELECT id, at, source, outcome, what, why, how, ref FROM event_log WHERE at >= ?"
        params: list = [max(float(since or 0), self.clock() - KEEP_DAYS * 86400)]
        if ref:
            q += " AND ref = ?"
            params.append(ref)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, params)]

    @staticmethod
    def line(row: dict, local: Callable[[float], Any] | None = None) -> str:
        """``"6:12 pm. Reminded you out loud: ... Because: ... (how)"``: the
        readable form, written by code."""
        import datetime as _dt
        when = local(row["at"]) if local else _dt.datetime.fromtimestamp(row["at"]).astimezone()
        stamp = when.strftime("%b %-d %-I:%M %p")
        out = f"{stamp}. {row['what']}"
        if not out.endswith((".", "?", "!")):
            out += "."
        if row.get("why"):
            out += f" Because: {row['why']}"
            if not out.endswith((".", "?", "!")):
                out += "."
        if row.get("how"):
            out += f" ({row['how']})"
        return out
