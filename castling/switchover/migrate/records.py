"""0.4 jobs -> the migrate report; 0.4 change records -> 0.5 change records.

**Jobs**: 0.4 jobs were step graphs; a 0.5 job is a trigger and one call
(castling design: "Jobs: a time or an event fires ONE call. Nothing more").
The move makes no job records: each 0.4 job's definition and state go in
the report, so a person or an agent rebuilds the ones still wanted as
trigger-plus-call jobs. Run history stays in the archive.

**Change records** (castling-changes: "later agents read past changes to
learn why things are the way they are"; records only, castling-caps C2):
each 0.4 change becomes a ``change`` record: what, why, category, machines
-> workers, plan/scripts/fix -> steps, tests/checks -> testing, rollback,
and the 0.4 outcome (kept, rolled back, failed). The run log stays in the
archive.

A re-run skips changes that are there, by (what, time).
"""
from __future__ import annotations

import json
import sqlite3

from rook.kit.ctx import Caller

from . import names
from .source import _tables

def _json(v, default):
    try:
        out = json.loads(v) if v else default
    except ValueError:
        return default
    return out if isinstance(out, type(default)) else default


def _text(v) -> str:
    if v is None or v == "" or v == [] or v == {}:
        return ""
    if isinstance(v, str):
        return v
    return "```json\n" + json.dumps(v, indent=1, ensure_ascii=False) + "\n```"


def _all(rtype, caller, **where) -> list:
    return [r for archived in (False, True) for r in rtype.list(caller, where=where, archived=archived, limit=500)]


def jobs(db: sqlite3.Connection | None) -> dict:
    """Every 0.4 job's definition and state, for the report."""
    out = []
    if db is None or "jobs" not in _tables(db):
        return {"jobs": out}
    triggers = {}
    if "triggers" in _tables(db):
        for t in db.execute("SELECT job_id, idx, kind, next_at, done FROM triggers ORDER BY job_id, idx"):
            triggers.setdefault(t["job_id"], []).append(dict(t))
    for j in db.execute("SELECT * FROM jobs ORDER BY created, id"):
        out.append({"id04": j["id"], "name": j["name"], "definition04": _json(j["definition"], {}),
                    "enabled04": bool(j["enabled"]), "paused04": j["paused_reason"], "owner04": j["owner"],
                    "triggers04": triggers.get(j["id"], []), "runs04": j["run_count"]})
    return {"jobs": out}


def changes(db: sqlite3.Connection | None, store, band: str, tasks: dict) -> dict:
    """``tasks``: 0.4 task id or slug -> 0.5 task id."""
    report = {"changes": 0, "skipped": 0}
    if db is None or "changes" not in _tables(db):
        return report
    rtype = store.type(names.record("changes"))
    for c in db.execute("SELECT * FROM changes ORDER BY created, id"):
        rec = _json(c["record"], {})
        caller = Caller(agent=c["author"] or "import:0.4", band=band, owner=True, why="import from 0.4")
        what = str(rec.get("what") or f"0.4 change {c['id']}")[:500]
        if any(r["created"] == c["created"] for r in _all(rtype, caller, what=what)):
            report["skipped"] += 1
            continue
        steps = "\n\n".join(x for x in (_text(rec.get("plan")), _text(rec.get("scripts")), _text(rec.get("fix"))) if x)
        testing = "\n\n".join(x for x in (_text(rec.get("tests")), _text(rec.get("checks"))) if x)
        task = rec.get("task")
        fields = {"what": what, "why": str(rec.get("why") or "(none given in 0.4)"),
                  "category": c["category"] or rec.get("category") or None,
                  "workers": [str(m) for m in rec.get("machines") or [] if m] or None,
                  "steps": steps or None, "testing": testing or None,
                  "rollback": _text(rec.get("rollback")) or None,
                  "task": tasks.get(task, task) if isinstance(task, str) and task else None,
                  "outcome": f"0.4 change {c['id']} ({c['kind']}): {c['state']}"
                             + (f", phase {c['phase']}" if c["phase"] else "")
                             + ". Its run log is in the 0.4 archive (plugins/change/changes.db)."}
        store.clock = lambda c=c: c["created"]
        rtype.create(caller, **{k: v for k, v in fields.items() if v is not None})
        report["changes"] += 1
    return report
