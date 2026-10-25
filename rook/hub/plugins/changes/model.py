"""Change requests: the record, its mechanical checklist and the store
(knowledge change-management-design-20261025; docs/manual/trust.md).

A change request (CR) says what changes, why, on which machines, the plan,
the scripts, the tests (runnable commands with the result they expect) and
how to roll it back (the command, a dry run of it, and a check that the
rollback point exists). Its **kind**:

* ``standard``: routine deploys, reversible config or database changes. The
  hub checks it (:func:`checklist`), runs it, tests it and keeps it or rolls
  it back by itself. No person, no vote.
* ``emergency``: a service is down. A standard fix (roll back the last
  change, restart, fail over) runs at once and the person gets one FYI;
  anything else is one urgent ask with the proposed fix. The rest of the
  record is written afterwards.
* ``normal``: risky or permanent (decommission a server, delete a database).
  A huddle gets one round to reduce it to standard changes; what can't be
  reduced is one card to the person.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from ....core import names
from ....core.db import open_db
from ... import hardstops

KINDS = ("standard", "emergency", "normal")
#: Categories that go as standard changes until the person says otherwise
#: ("that kind is fine" adds one; nothing an agent says removes or adds one).
CATALOG = ("deploy", "config", "database", "restart", "rollback", "failover", "update")
#: Emergency fixes that are standard changes themselves.
STANDARD_FIXES = ("rollback", "restart", "failover")
STATES = ("checking", "running", "kept", "rolled_back", "failed", "refused", "reducing",
          "reduced", "asked", "declined", "reverted")
OPEN = ("checking", "running", "reducing", "asked")
TEXT_MIN, TEXT_MAX, RUN_MAX = 8, 2000, 4000
STEPS_MAX = 20
STEP_TIMEOUT, STEP_TIMEOUT_MAX = 300.0, 3 * 3600.0
_WORKER = re.compile(r"^[^\s]{1,80}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS changes(
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, category TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL, author TEXT NOT NULL, record TEXT NOT NULL, log TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT '', step INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL, updated REAL NOT NULL, ended REAL);
CREATE INDEX IF NOT EXISTS changes_state ON changes(state);
CREATE TABLE IF NOT EXISTS catalog(
    category TEXT PRIMARY KEY, by TEXT NOT NULL, words TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS digests(kind TEXT PRIMARY KEY, at REAL NOT NULL);
"""


class ChangeError(ValueError):
    """Refused; the text says what to fix."""


def _text(raw: Any, field: str, limit: int = TEXT_MAX, required: bool = True) -> str:
    t = " ".join(str(raw or "").split())[:limit]
    if required and len(t) < TEXT_MIN:
        raise ChangeError(f"{field}: at least {TEXT_MIN} characters")
    return t


def _step(raw: Any, field: str, test: bool = False) -> dict:
    if not isinstance(raw, dict):
        raise ChangeError(f"{field}: {{worker, run{', expect_rc?, expect?' if test else ''}, "
                          "timeout?, detach?}")
    worker = str(raw.get("worker") or "").strip()
    run = str(raw.get("run") or "").strip()
    if not _WORKER.match(worker):
        raise ChangeError(f"{field}.worker: the machine it runs on")
    if not run or len(run) > RUN_MAX:
        raise ChangeError(f"{field}.run: the shell command (at most {RUN_MAX} characters)")
    try:
        timeout = float(raw.get("timeout") or STEP_TIMEOUT)
    except (TypeError, ValueError):
        raise ChangeError(f"{field}.timeout: seconds") from None
    out = {"worker": worker, "run": run,
           "timeout": max(5.0, min(timeout, STEP_TIMEOUT_MAX)), "detach": bool(raw.get("detach"))}
    if test:
        out["expect_rc"] = int(raw.get("expect_rc") or 0)
        if raw.get("expect"):
            out["expect"] = str(raw["expect"])[:500]
    return out


def clean(data: Any) -> dict:
    """A CR as submitted, cleaned. What a kind needs is :func:`missing`'s."""
    if not isinstance(data, dict):
        raise ChangeError("data: {kind, category, what, why, machines, plan, scripts, tests, "
                          "rollback {run, dry_run, point}}")
    kind = str(data.get("kind") or "standard").strip().lower()
    if kind not in KINDS:
        raise ChangeError("kind: standard, emergency or normal")
    machines = data.get("machines") or []
    if isinstance(machines, str):
        machines = [m.strip() for m in machines.split(",")]
    machines = list(dict.fromkeys(str(m).strip() for m in machines if str(m).strip()))[:STEPS_MAX]
    out = {"kind": kind, "category": str(data.get("category") or "").strip().lower()[:40],
           "what": _text(data.get("what"), "what"),
           "why": _text(data.get("why"), "why", required=kind != "emergency"),
           "machines": machines, "plan": _text(data.get("plan"), "plan", 4000, required=False),
           "huddle": str(data.get("huddle") or "").strip()[:80],
           "task": str(data.get("task") or "").strip()[:80],
           "fix": str(data.get("fix") or "").strip().lower()[:40]}
    for key, test in (("scripts", False), ("tests", True)):
        steps = data.get(key) or []
        if not isinstance(steps, list) or len(steps) > STEPS_MAX:
            raise ChangeError(f"{key}: a list of at most {STEPS_MAX} steps")
        out[key] = [_step(s, f"{key}[{i}]", test) for i, s in enumerate(steps)]
    rb = data.get("rollback")
    if rb:
        if not isinstance(rb, dict):
            raise ChangeError("rollback: {worker, run, dry_run, point, timeout?, detach?}")
        r = _step(rb, "rollback")
        for key in ("dry_run", "point"):
            if rb.get(key):
                r[key] = str(rb[key]).strip()[:RUN_MAX]
        out["rollback"] = r
    else:
        out["rollback"] = {}
    for s in out["scripts"] + out["tests"] + ([out["rollback"]] if out["rollback"] else []):
        if s["worker"] not in machines:
            raise ChangeError(f"machines: list every machine a step runs on ({s['worker']} is "
                              "missing)")
    return out


def missing(cr: dict) -> list[str]:
    """What a CR of its kind still lacks (empty: complete)."""
    out = []
    if not cr["machines"]:
        out.append("machines")
    if not cr["scripts"]:
        out.append("scripts")
    if cr["kind"] == "emergency":
        return out
    if len(cr.get("plan") or "") < TEXT_MIN:
        out.append("plan")
    if not cr["tests"]:
        out.append("tests")
    rb = cr.get("rollback") or {}
    for key in ("run", "dry_run", "point"):
        if not rb.get(key):
            out.append(f"rollback.{key}")
    return out


def commands(cr: dict) -> list[tuple[str, str]]:
    """``(worker, command)`` of everything a CR runs."""
    out = [(s["worker"], s["run"]) for s in cr["scripts"] + cr["tests"]]
    rb = cr.get("rollback") or {}
    out += [(rb["worker"], rb[k]) for k in ("run", "dry_run", "point") if rb.get(k)]
    return out


def checklist(cr: dict, *, catalog: tuple | list, sacred: Any) -> list[str]:
    """Why a CR is not a standard change (empty: it is). ``sacred(worker)``
    says whether the trust map keeps a machine for the person."""
    out = [f"missing {m}" for m in missing(cr)]
    if cr["kind"] == "standard" and cr["category"] not in catalog:
        out.append(f"category {cr['category'] or '(none)'} is not a standard kind "
                   f"({', '.join(catalog)}; the person adds kinds)")
    if cr["kind"] == "emergency" and cr["fix"] not in STANDARD_FIXES:
        out.append(f"fix {cr['fix'] or '(none)'} is not a standard fix "
                   f"({', '.join(STANDARD_FIXES)})")
    for m in cr["machines"]:
        if sacred(m):
            out.append(f"{m} is sacred: changes there ask the person first")
    for worker, cmd in commands(cr):
        why = hardstops.stop("shell.exec", {"cmd": cmd})
        if why:
            out.append(f"on {worker}: {why.removeprefix('hard stop: ')}")
        for host in sorted(hardstops.remote_hosts(cmd)):
            if sacred(host) or sacred(host.split(".", 1)[0]):
                out.append(f"on {worker} it reaches {host}, which is sacred")
    return out


class Store:
    """``changes.db`` in the plugin's data dir."""

    def __init__(self, path: str, clock=time.time) -> None:
        self._db = open_db(path)
        self._db.executescript(_SCHEMA)
        names.ensure_table(self._db)
        self._lock = threading.Lock()
        self._clock = clock

    def _row(self, r: Any) -> dict:
        out = dict(r)
        out["record"] = json.loads(out["record"])
        out["log"] = json.loads(out["log"])
        return out

    def add(self, cr: dict, author: str, state: str) -> dict:
        now = self._clock()
        with self._lock:
            ident = names.generate()
            while self._db.execute("SELECT 1 FROM changes WHERE id=?", (ident,)).fetchone():
                ident = names.generate()
            self._db.execute(
                "INSERT INTO changes(id, kind, category, state, author, record, log, created, "
                "updated) VALUES (?,?,?,?,?,?,?,?,?)",
                (ident, cr["kind"], cr["category"], state, author, json.dumps(cr), "[]", now, now))
            self._db.commit()
        return self.get(ident)

    def get(self, ident: str) -> dict:
        r = self._db.execute("SELECT * FROM changes WHERE id=?",
                             (names.normalize(ident) or str(ident),)).fetchone()
        if r is None:
            raise ChangeError(f"no change request {ident!r}")
        return self._row(r)

    def list(self, states: tuple | list = (), since: float = 0, limit: int = 50) -> list[dict]:
        q, args = "SELECT * FROM changes WHERE updated >= ?", [since]
        if states:
            q += f" AND state IN ({','.join('?' * len(states))})"
            args += list(states)
        q += " ORDER BY updated DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        return [self._row(r) for r in self._db.execute(q, args).fetchall()]

    def set(self, ident: str, **fields: Any) -> dict:
        fields["updated"] = self._clock()
        if fields.get("state") in ("kept", "rolled_back", "failed", "refused", "reduced",
                                   "declined", "reverted"):
            fields["ended"] = fields["updated"]
        for k in ("record", "log"):
            if k in fields:
                fields[k] = json.dumps(fields[k], default=str)
        with self._lock:
            self._db.execute(f"UPDATE changes SET {', '.join(f'{k}=?' for k in fields)} "
                             "WHERE id=?", (*fields.values(), ident))
            self._db.commit()
        return self.get(ident)

    def note(self, ident: str, entry: dict) -> dict:
        row = self.get(ident)
        log = row["log"] + [{"at": self._clock(), **entry}]
        return self.set(ident, log=log[-200:])

    def catalog(self) -> list[str]:
        added = [r[0] for r in self._db.execute("SELECT category FROM catalog").fetchall()]
        return list(dict.fromkeys(list(CATALOG) + added))

    def add_category(self, category: str, by: str, words: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO catalog VALUES (?,?,?,?)",
                             (category, by, words, self._clock()))
            self._db.commit()

    def digest_at(self, kind: str) -> float:
        r = self._db.execute("SELECT at FROM digests WHERE kind=?", (kind,)).fetchone()
        return float(r[0]) if r else 0.0

    def set_digest(self, kind: str, at: float) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO digests VALUES (?,?)", (kind, at))
            self._db.commit()
