"""0.4 jobs and change records -> 0.5 records of the jobs and change plugins.

**Jobs** (castling-jobs: a job is "one or more steps" with triggers, runs as
its creator). 0.4 jobs were step graphs; 0.5 jobs are a cap call, an agent
prompt or a script. The move doesn't guess a translation (castling-design:
"an agent imports and reshapes them into 0.5"): each 0.4 job becomes a 0.5
job record of the same name, **disabled**, triggered by hand only, owned by
the hub's owner, with the whole 0.4 definition in ``vars.definition04`` and
its 0.4 state (enabled, triggers, owner) beside it. Nothing runs until
someone reshapes and enables it. Run history stays in the archive (0.5 keeps
runs 30 days).

**Change records** (castling-changes: "later agents read past changes to
learn why things are the way they are"; records only, castling-caps C2):
each 0.4 change becomes a ``change`` record: what, why, category, machines
-> workers, plan/scripts/fix -> steps, tests/checks -> testing, rollback,
and the 0.4 outcome (kept, rolled back, failed). The run log stays in the
archive.

A re-run skips what is there: jobs by name, changes by (what, time).
"""
from __future__ import annotations

import json
import re
import sqlite3

from rook.kit.ctx import Caller

from . import names
from .source import _tables

JOB_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


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


def job_name(name: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9._-]+", "-", str(name or "").lower()).strip("-._")[:60] or "job"
    if not JOB_NAME.match(base):
        base = "j-" + base
    out, n = base, 1
    while out in taken:
        n += 1
        out = f"{base[:60]}-{n}"
    return out


def jobs(db: sqlite3.Connection | None, store, band: str, owner: str) -> dict:
    report = {"jobs": 0, "skipped": 0, "renamed": []}
    if db is None or "jobs" not in _tables(db):
        return report
    rtype = store.type(names.record("jobs"))
    caller = Caller(person=owner, band=band, owner=True, why="import from 0.4")
    taken = {r["name"] for r in _all(rtype, caller)}
    triggers = {}
    if "triggers" in _tables(db):
        for t in db.execute("SELECT job_id, idx, kind, next_at, done FROM triggers ORDER BY job_id, idx"):
            triggers.setdefault(t["job_id"], []).append(dict(t))
    for j in db.execute("SELECT * FROM jobs ORDER BY created, id"):
        if any(r["vars"] and (r["vars"] or {}).get("id04") == j["id"] for r in _all(rtype, caller)):
            report["skipped"] += 1
            continue
        definition = _json(j["definition"], {})
        name = job_name(j["name"], taken)
        taken.add(name)
        if name != j["name"]:
            report["renamed"].append({"from": j["name"], "to": name})
        doc = (definition.get("description") or "").strip()
        doc = (doc + "\n\n" if doc else "") + ("Imported from 0.4, disabled: its 0.4 definition is in "
                                               "vars.definition04. Reshape it into a cap call, agent prompt "
                                               "or script, then enable it.")
        store.clock = lambda j=j: j["created"]
        rtype.create(caller, name=name, doc=doc, trigger={"manual": True}, enabled=False, notify="never",
                     vars={"id04": j["id"], "definition04": definition, "enabled04": bool(j["enabled"]),
                           "paused04": j["paused_reason"], "owner04": j["owner"],
                           "triggers04": triggers.get(j["id"], []), "runs04": j["run_count"]})
        report["jobs"] += 1
    return report


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
