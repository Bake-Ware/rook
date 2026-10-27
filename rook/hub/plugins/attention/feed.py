"""The Feed: one stream of what came in for the person, and the sorting they
teach it (docs/design/attention.md, "The Feed" and "Sorting").

The attention plugin has two faces. On the phone it is the one app allowed
to interrupt (cards, alerts, spoken reminders, the quiet feed the phone sorts
itself). On the dashboard it is the **Feed** page: the same things as one
stream, newest first:

* ``phone``: the phone's quiet feed (other apps' notifications as the phone
  summarized them, at most 140 characters, never the full text), pulled from
  ``attention.feed`` on each configured phone;
* ``alert``: alerts agents sent with ``attention.send``;
* ``decision``: cards queued and decided, from the 30-day log
  (:mod:`rook.hub.eventlog`);
* ``work``: tasks finished or blocked, from the knowledge store.

Phone and alert rows are kept here (``feed.db`` in the plugin's data dir,
30 days); decision and work rows are read where they already live.

**Sorting** is the person's say over how the phone sorts an app: ring me
(urgent), tell me (family: a notification of its own, and a summary for the
hub), just list it (normal) or hide it (ignored). They choose it on a Feed
row or on the phone's Attention tab (the phone reports a ``sorted`` event);
the hub keeps the choices and pushes them to every phone as rules in front
of the phone's own defaults (``attention.rules_set``, ``extend_default``), with
the family names from Settings > Attention.
"""
from __future__ import annotations

import contextlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("rook.hub.plugins.attention.feed")

KEEP_DAYS = 30
LINE_MAX, APP_MAX, PKG_MAX, CAT_MAX, ID_MAX = 200, 80, 120, 40, 120
LIMIT_MAX = 300
SOURCES = ("phone", "alert", "decision", "work")
#: The phone's own priorities, plus what an alert or decision counts as.
PRIORITIES = ("urgent", "family", "normal", "ignore")
#: What the person may choose for an app, in the words the pages use.
SORT_CHOICES = {"urgent": "Ring me", "family": "Tell me", "normal": "Just list it",
                "ignore": "Hide it"}
#: The category a sorted app's notifications show on the phone.
SORT_CATEGORY = {"urgent": "urgent", "family": "important", "normal": "other",
                 "ignore": "hidden"}
SORTING_MAX = 200
FAMILY_MAX = 50
#: Log outcomes that belong in the stream (reminder pacing and skips don't).
DECISION_OUTCOMES = ("ask", "ask_now", "tell", "decided")
#: Task states a work row reports.
WORK_STATES = {"done": "Done", "blocked": "Blocked"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS feed (
    id TEXT PRIMARY KEY,
    at REAL NOT NULL,
    source TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',
    category TEXT NOT NULL DEFAULT '',
    app TEXT NOT NULL DEFAULT '',
    pkg TEXT NOT NULL DEFAULT '',
    line TEXT NOT NULL,
    device TEXT NOT NULL DEFAULT '',
    ref TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS feed_at ON feed(at);
"""


def one_line(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


class FeedStore:
    """Phone and alert rows, kept :data:`KEEP_DAYS`. ``add`` ignores a row
    whose id is already there, so pulling the same phone feed twice adds
    nothing."""

    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self._ready = False

    def _conn(self):
        from ....core.db import transaction
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with transaction(self.path, begin=None) as conn:
                conn.executescript(SCHEMA)
            with contextlib.suppress(OSError):
                self.path.chmod(0o600)
            self._ready = True
        return transaction(self.path)

    def add(self, rows: list[dict]) -> int:
        """Insert new rows; returns how many were new. Never raises."""
        clean = []
        for r in rows:
            line = one_line(r.get("line"), LINE_MAX)
            ident = one_line(r.get("id"), ID_MAX)
            if not line or not ident or r.get("source") not in SOURCES:
                continue
            at = r.get("at")
            clean.append({"id": ident, "at": float(at) if isinstance(at, (int, float)) and at > 0
                          else self.clock(), "source": r["source"],
                          "priority": r.get("priority") if r.get("priority") in PRIORITIES
                          else "normal",
                          "category": one_line(r.get("category"), CAT_MAX),
                          "app": one_line(r.get("app"), APP_MAX),
                          "pkg": one_line(r.get("pkg"), PKG_MAX),
                          "line": line, "device": one_line(r.get("device"), APP_MAX),
                          "ref": one_line(r.get("ref"), ID_MAX)})
        if not clean:
            return 0
        try:
            with self._conn() as conn:
                before = conn.total_changes
                conn.executemany(
                    "INSERT OR IGNORE INTO feed (id, at, source, priority, category, app, pkg, "
                    "line, device, ref) VALUES (:id, :at, :source, :priority, :category, :app, "
                    ":pkg, :line, :device, :ref)", clean)
                added = conn.total_changes - before
                conn.execute("DELETE FROM feed WHERE at < ?", (self.clock() - KEEP_DAYS * 86400,))
            return added
        except Exception:  # noqa: BLE001
            log.exception("feed: writing %d rows failed", len(clean))
            return 0

    def recent(self, limit: int = 50, before: float = 0.0,
               sources: tuple[str, ...] = ("phone", "alert")) -> list[dict]:
        limit = max(1, min(int(limit or 50), LIMIT_MAX))
        if not self.path.exists() or not sources:
            return []
        marks = ",".join("?" * len(sources))
        q = f"SELECT * FROM feed WHERE at >= ? AND source IN ({marks})"
        params: list = [self.clock() - KEEP_DAYS * 86400, *sources]
        if before:
            q += " AND at < ?"
            params.append(float(before))
        q += " ORDER BY at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, params)]


# -- rows from where they already live ---------------------------------------------------

def phone_rows(entries: list, wid: str, device: str) -> list[dict]:
    """``attention.feed`` entries from one phone as feed rows. Only the
    summary fields the phone gives (never full text)."""
    out = []
    for e in entries or []:
        if not isinstance(e, dict) or not e.get("id") or not e.get("summary"):
            continue
        out.append({"id": f"phone:{wid}:{e['id']}", "at": e.get("ts"), "source": "phone",
                    "priority": e.get("priority"), "category": e.get("category"),
                    "app": e.get("app"), "pkg": e.get("pkg") or "", "line": e.get("summary"),
                    "device": device})
    return out


def notice_row(ev: dict, wid: str, device: str, ts: float) -> dict | None:
    """A ``notice`` event (urgent or family) as the same row the phone's feed
    gives for it: an urgent notice's id is the feed entry's with an ``n`` in
    front, so a later pull adds nothing twice."""
    ident = str(ev.get("id") or "")
    if ident.startswith("nf"):
        ident = ident[1:]
    if not ident or not ev.get("summary"):
        return None
    return {"id": f"phone:{wid}:{ident}", "at": ts, "source": "phone",
            "priority": ev.get("priority"), "category": ev.get("category"),
            "app": ev.get("app"), "pkg": ev.get("pkg") or "", "line": ev.get("summary"),
            "device": device}


def decision_rows(eventlog: Any, limit: int, before: float = 0.0) -> list[dict]:
    """Cards queued and decided, from the 30-day log."""
    if eventlog is None:
        return []
    try:
        rows = eventlog.recent(LIMIT_MAX)
    except Exception:  # noqa: BLE001
        log.exception("feed: reading the log failed")
        return []
    out = []
    for r in rows:
        if r["outcome"] not in DECISION_OUTCOMES or (before and r["at"] >= before):
            continue
        out.append({"id": f"log:{r['id']}", "at": r["at"], "source": "decision",
                    "priority": "urgent" if r["outcome"] == "ask_now" else "normal",
                    "category": r["outcome"], "app": "", "line": one_line(r["what"], LINE_MAX),
                    "detail": one_line(r.get("how"), 120)})
        if len(out) >= limit:
            break
    return out


def work_rows(service: Any, limit: int, before: float = 0.0, scan: int = 600) -> list[dict]:
    """Tasks that became done or blocked, newest first, from the knowledge
    store (every band the hub has)."""
    if service is None:
        return []
    try:
        bands = [b["id"] for b in service.bands()]
        if not bands:
            return []
        marks = ",".join("?" * len(bands))
        q = (f"SELECT e.seq, e.record, e.actor, e.ts, e.data, r.title, r.slug, r.attrs "
             f"FROM events e JOIN records r ON r.id=e.record "
             f"WHERE r.kind='task' AND e.action='updated' AND e.band IN ({marks})")
        params: list = list(bands)
        if before:
            q += " AND e.ts < ?"
            params.append(float(before))
        q += " ORDER BY e.seq DESC LIMIT ?"
        params.append(scan)
        with service.store.db(False) as db:
            rows = db.execute(q, params).fetchall()
    except Exception:  # noqa: BLE001
        log.exception("feed: reading work updates failed")
        return []
    out = []
    for r in rows:
        try:
            state = (json.loads(r["data"] or "{}") or {}).get("state")
        except ValueError:
            continue
        if state not in WORK_STATES:
            continue
        try:
            attrs = json.loads(r["attrs"] or "{}") or {}
        except ValueError:
            attrs = {}
        why = attrs.get("outcome") if state == "done" else attrs.get("blocked_reason")
        line = f"{WORK_STATES[state]}: {r['title']}" + (f" ({one_line(why, 120)})" if why else "")
        out.append({"id": f"work:{r['seq']}", "at": r["ts"], "source": "work",
                    "priority": "normal", "category": state, "app": "",
                    "line": one_line(line, LINE_MAX), "detail": one_line(r["actor"], 80),
                    "slug": r["slug"] or r["record"]})
        if len(out) >= limit:
            break
    return out


# -- sorting --------------------------------------------------------------------------------

def sort_key(app: str, pkg: str) -> str:
    """One choice per app: by package when the phone gave one, else by name."""
    pkg = str(pkg or "").strip().lower()
    return f"pkg:{pkg}" if pkg else f"app:{str(app or '').strip().lower()}"


def clean_choice(app: Any, pkg: Any, priority: Any) -> tuple[str, str, str]:
    """``(app, pkg, priority)`` or ValueError: what a sort request may say."""
    app, pkg = one_line(app, APP_MAX), one_line(pkg, PKG_MAX)
    if priority not in SORT_CHOICES:
        raise ValueError("priority: one of " + ", ".join(SORT_CHOICES))
    if not app and not pkg:
        raise ValueError("app: which app's notifications this is about")
    if pkg and (any(c.isspace() for c in pkg) or "." not in pkg):
        raise ValueError("pkg: an Android package name, like com.example.app")
    return app, pkg, priority


def choose(sorting: list[dict], app: str, pkg: str, priority: str, by: str,
           now: float) -> dict:
    """Record the person's choice for one app (replacing an earlier one for
    the same app). Returns the stored choice."""
    key = sort_key(app, pkg)
    old = next((s for s in sorting if sort_key(s.get("app"), s.get("pkg")) == key), None)
    if old is None and pkg:
        # The same app chosen earlier by name only (an older phone row).
        old = next((s for s in sorting if not s.get("pkg")
                    and sort_key(s.get("app"), "") == sort_key(app, "")), None)
    if old is not None:
        sorting.remove(old)
    row = {"id": old["id"] if old else secrets.token_hex(4), "app": app or (old or {}).get("app")
           or pkg, "pkg": pkg, "priority": priority, "by": one_line(by, 120), "at": now}
    sorting.insert(0, row)
    del sorting[SORTING_MAX:]
    return row


def choice_for(sorting: list[dict], app: str, pkg: str) -> str | None:
    """The person's choice for this app, if any."""
    key = sort_key(app, pkg)
    for s in sorting:
        if sort_key(s.get("app"), s.get("pkg")) == key or (
                not s.get("pkg") and sort_key(s.get("app"), "") == sort_key(app, "")):
            return s["priority"]
    return None


def family_names(raw: Any) -> list[str]:
    out: list[str] = []
    for n in raw if isinstance(raw, list) else str(raw or "").split(","):
        n = one_line(n, 60)
        if len(n) >= 2 and n.lower() not in {x.lower() for x in out}:
            out.append(n)
    return out[:FAMILY_MAX]


def rules_doc(sorting: list[dict], family: list[str]) -> dict | None:
    """The rules the hub pushes: the person's choices in front of the phone's
    defaults, and the family names. None when there is nothing to say."""
    if not sorting and not family:
        return None
    rules = []
    for s in sorting:
        match = {"packages": [s["pkg"]]} if s.get("pkg") else {"app_labels": [s["app"]]}
        rules.append({"priority": s["priority"], "category": SORT_CATEGORY[s["priority"]],
                      "note": f"yours: {SORT_CHOICES[s['priority']].lower()} ({s['app']})",
                      "match": match})
    return {"version": 1, "about": "Your sorting from the Rook hub (Feed page or the "
            "Attention tab), in front of the phone's own rules.", "extend_default": True,
            "family_names": family, "rules": rules}


EMPTY_DOC = {"version": 1, "extend_default": True, "family_names": [], "rules": []}
