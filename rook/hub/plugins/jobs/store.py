"""The jobs store: one SQLite file (``jobs.db`` in the plugin's data dir).

Several hub processes may share the file (HA later), so every decision that
must happen once is a single atomic statement or an ``IMMEDIATE``
transaction:

* firing a trigger moves its ``next_at`` with a compare-and-set and enqueues
  the run in the same transaction (two schedulers never fire one occurrence);
* claiming a run is ``UPDATE runs SET state='running' ... WHERE state='due'
  AND lease_until < now`` (two schedulers never start one run);
* a running run holds a lease it renews; a run whose lease ran out is
  ``interrupted`` by whichever scheduler notices.

All instants are epoch seconds.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ....core import migrations
from .cron import Cron, parse_duration, parse_instant

NAMESPACE = "job"
MIGRATIONS = Path(__file__).resolve().parent / "migrations"
ACTIVE = ("due", "running")
WAITING = ("queued", "due")
FINAL = ("success", "failure", "hang", "interrupted", "dropped", "blocked", "cancelled")
#: Final states an ``after`` trigger counts as a finished run.
AFTER_STATES = {"success": ("success",),
                "failure": ("failure", "hang", "interrupted", "blocked"),
                "finish": ("success", "failure", "hang", "interrupted", "blocked")}
_JSON_COLS = ("owner_info", "definition", "vars", "steps", "alerts", "detail")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _dumps(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False, default=str)


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    out = dict(r)
    for k in _JSON_COLS:
        if isinstance(out.get(k), str):
            try:
                out[k] = json.loads(out[k])
            except ValueError:
                pass
    if "missed" in out:
        out["missed"] = bool(out["missed"])
    if "enabled" in out:
        out["enabled"] = bool(out["enabled"])
    return out


class JobStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None,
                                   timeout=10.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=10000")
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        with self._lock:
            migrations.apply(self._db, MIGRATIONS, NAMESPACE)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """An IMMEDIATE transaction: the write lock is taken up front, so a
        read-decide-write inside it is atomic across processes."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        with self._lock:
            return _row(self._db.execute(sql, args).fetchone())

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [_row(r) for r in self._db.execute(sql, args).fetchall()]

    # -- jobs --------------------------------------------------------------------
    def get_job(self, ref: str) -> dict | None:
        """By id, else by name."""
        return (self._one("SELECT * FROM jobs WHERE id=?", (ref,))
                or self._one("SELECT * FROM jobs WHERE name=?", (ref,)))

    def list_jobs(self) -> list[dict]:
        return self._all("SELECT * FROM jobs ORDER BY name")

    def create_job(self, job: dict, owner_info: dict, now: float, tz: str) -> dict:
        jid = job.get("id") or _new_id("j")
        job = {**job, "id": jid, "owner": owner_info["id"]}
        with self._tx() as db:
            if db.execute("SELECT 1 FROM jobs WHERE name=?", (job["name"],)).fetchone():
                raise ValueError(f"a job named {job['name']!r} already exists")
            if db.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone():
                raise ValueError(f"a job with id {jid!r} already exists")
            db.execute("INSERT INTO jobs(id, name, owner, owner_info, enabled, definition, revision,"
                       " created, updated, updated_by) VALUES (?,?,?,?,?,?,1,?,?,?)",
                       (jid, job["name"], owner_info["id"], _dumps(owner_info), int(bool(job.get("enabled", True))),
                        _dumps(job), now, now, owner_info["id"]))
            self._rebuild_triggers(db, job, now, tz)
            self._history(db, jid, 1, now, owner_info["id"], "create", {})
        return self.get_job(jid)

    def update_job(self, jid: str, job: dict, actor: str, now: float, tz: str,
                   expect_revision: int | None = None, *, owner_info: dict | None = None,
                   action: str = "update", detail: dict | None = None) -> dict:
        """Replace a job's definition. ``owner_info`` hands the job (and the
        identity its runs use) to a new owner. Each call adds a history row."""
        with self._tx() as db:
            cur = db.execute("SELECT revision, owner FROM jobs WHERE id=?", (jid,)).fetchone()
            if cur is None:
                raise KeyError(f"no job {jid!r}")
            if expect_revision is not None and cur["revision"] != expect_revision:
                raise ValueError(f"job changed since revision {expect_revision} (now {cur['revision']})")
            clash = db.execute("SELECT id FROM jobs WHERE name=? AND id<>?", (job["name"], jid)).fetchone()
            if clash:
                raise ValueError(f"a job named {job['name']!r} already exists")
            owner = owner_info["id"] if owner_info else cur["owner"]
            job = {**job, "id": jid, "owner": owner}
            db.execute("UPDATE jobs SET name=?, enabled=?, paused_reason=CASE WHEN ? THEN NULL ELSE paused_reason END,"
                       " definition=?, revision=revision+1, updated=?, updated_by=? WHERE id=?",
                       (job["name"], int(bool(job.get("enabled", True))), int(bool(job.get("enabled", True))),
                        _dumps(job), now, actor, jid))
            if owner_info:
                db.execute("UPDATE jobs SET owner=?, owner_info=? WHERE id=?", (owner, _dumps(owner_info), jid))
            self._rebuild_triggers(db, job, now, tz)
            self._history(db, jid, cur["revision"] + 1, now, actor, action, detail or {})
        return self.get_job(jid)

    def set_enabled(self, jid: str, enabled: bool, actor: str, now: float, tz: str,
                    reason: str | None = None, *, definition: dict | None = None,
                    owner_info: dict | None = None, detail: dict | None = None) -> dict:
        """Enable or pause a job. ``definition`` / ``owner_info`` replace the
        job and its owner in the same write (an identity reset)."""
        row = self.get_job(jid)
        if row is None:
            raise KeyError(f"no job {jid!r}")
        owner = owner_info["id"] if owner_info else row["owner"]
        job = {**(definition or row["definition"]), "id": row["id"], "owner": owner, "enabled": bool(enabled)}
        with self._tx() as db:
            db.execute("UPDATE jobs SET enabled=?, paused_reason=?, definition=?, revision=revision+1,"
                       " updated=?, updated_by=? WHERE id=?",
                       (int(bool(enabled)), None if enabled else reason, _dumps(job), now, actor, row["id"]))
            if owner_info:
                db.execute("UPDATE jobs SET owner=?, owner_info=? WHERE id=?", (owner, _dumps(owner_info), row["id"]))
            if enabled:  # schedules restart from now: nothing "missed" while it was off
                self._rebuild_triggers(db, job, now, tz)
            rev = db.execute("SELECT revision FROM jobs WHERE id=?", (row["id"],)).fetchone()["revision"]
            self._history(db, row["id"], rev, now, actor, "enable" if enabled else "disable",
                          {**({"reason": reason} if reason and not enabled else {}), **(detail or {})})
        return self.get_job(row["id"])

    # -- history -------------------------------------------------------------------
    @staticmethod
    def _history(db: sqlite3.Connection, jid: str, revision: int | None, now: float, actor: str | None,
                 action: str, detail: dict) -> None:
        db.execute("INSERT INTO history(job_id, revision, at, actor, action, detail) VALUES (?,?,?,?,?,?)",
                   (jid, revision, now, actor, action, _dumps(detail or {})))

    def add_history(self, jid: str, now: float, actor: str | None, action: str, detail: dict | None = None) -> None:
        with self._tx() as db:
            cur = db.execute("SELECT revision FROM jobs WHERE id=?", (jid,)).fetchone()
            self._history(db, jid, cur["revision"] if cur else None, now, actor, action, detail or {})

    def history(self, jid: str, limit: int = 20) -> list[dict]:
        """A job's changes, newest first."""
        return self._all("SELECT revision, at, actor, action, detail FROM history WHERE job_id=?"
                         " ORDER BY id DESC LIMIT ?", (jid, max(1, min(int(limit), 500))))

    def delete_job(self, jid: str) -> dict:
        """Removes the job, its triggers and its finished or waiting runs;
        a running run is asked to cancel and is pruned later."""
        with self._tx() as db:
            if not db.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone():
                raise KeyError(f"no job {jid!r}")
            db.execute("DELETE FROM jobs WHERE id=?", (jid,))
            db.execute("DELETE FROM triggers WHERE job_id=?", (jid,))
            db.execute("DELETE FROM history WHERE job_id=?", (jid,))
            running = db.execute("UPDATE runs SET cancel=1 WHERE job_id=? AND state='running'", (jid,)).rowcount
            gone = db.execute("DELETE FROM runs WHERE job_id=? AND state<>'running'", (jid,)).rowcount
        return {"deleted": jid, "runs_deleted": gone, "runs_cancelling": running}

    # -- triggers ---------------------------------------------------------------
    @staticmethod
    def _rebuild_triggers(db: sqlite3.Connection, job: dict, now: float, tz: str) -> None:
        db.execute("DELETE FROM triggers WHERE job_id=?", (job["id"],))
        last = db.execute("SELECT id FROM runs WHERE job_id=? AND finished IS NOT NULL"
                          " ORDER BY finished DESC LIMIT 1", (job["id"],)).fetchone()
        for i, t in enumerate(job.get("triggers") or []):
            kind = t.get("kind")
            next_at, done, anchor = None, 0, None
            if kind == "cron":
                next_at = Cron(t["expr"]).next_fire(now, t.get("tz") or tz)
                done = int(next_at is None)
            elif kind == "at":
                next_at = parse_instant(t["when"])
                done = int(next_at <= now)
            elif kind == "after":
                anchor = last["id"] if last else None
            else:
                continue
            db.execute("INSERT INTO triggers(job_id, idx, kind, next_at, done, anchor) VALUES (?,?,?,?,?,?)",
                       (job["id"], i, kind, next_at, done, anchor))

    def triggers(self, jid: str | None = None) -> list[dict]:
        if jid:
            return self._all("SELECT * FROM triggers WHERE job_id=? ORDER BY idx", (jid,))
        return self._all("SELECT t.* FROM triggers t JOIN jobs j ON j.id=t.job_id WHERE j.enabled=1"
                         " ORDER BY t.next_at")

    def due_triggers(self, now: float) -> list[dict]:
        return self._all("SELECT t.* FROM triggers t JOIN jobs j ON j.id=t.job_id"
                         " WHERE j.enabled=1 AND t.done=0 AND t.next_at IS NOT NULL AND t.next_at<=?"
                         " ORDER BY t.next_at", (now,))

    def after_triggers(self) -> list[dict]:
        return self._all("SELECT t.* FROM triggers t JOIN jobs j ON j.id=t.job_id"
                         " WHERE j.enabled=1 AND t.kind='after'")

    def last_finished(self, jid: str) -> dict | None:
        return self._one("SELECT * FROM runs WHERE job_id=? AND finished IS NOT NULL"
                         " AND state NOT IN ('dropped','cancelled') ORDER BY finished DESC, rowid DESC LIMIT 1",
                         (jid,))

    def arm_after(self, jid: str, idx: int, expect_anchor: str | None, anchor: str,
                  next_at: float | None) -> bool:
        """Point an ``after`` trigger at a newly finished run (compare-and-set
        on the anchor)."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE triggers SET anchor=?, next_at=? WHERE job_id=? AND idx=? AND anchor IS ?",
                (anchor, next_at, jid, idx, expect_anchor))
            return cur.rowcount == 1

    def fire(self, trig: dict, new_next: float | None, done: bool, fires: list[tuple[float, bool]],
             label: str, now: float) -> list[dict] | None:
        """Move a trigger past its due fires and enqueue them, atomically.
        Returns the runs created, or ``None`` when another scheduler already
        moved the trigger (nothing done)."""
        with self._tx() as db:
            cur = db.execute("UPDATE triggers SET next_at=?, done=? WHERE job_id=? AND idx=? AND next_at IS ?"
                             " AND done=0", (new_next, int(done), trig["job_id"], trig["idx"], trig["next_at"]))
            if cur.rowcount != 1:
                return None
            job = _row(db.execute("SELECT * FROM jobs WHERE id=?", (trig["job_id"],)).fetchone())
            if job is None or not job["enabled"]:
                return []
            return [self._enqueue(db, job, label, missed, at, now) for at, missed in fires]

    # -- runs: create ---------------------------------------------------------------
    def enqueue(self, job: dict, trigger: str, now: float, *, missed: bool = False,
                scheduled: float | None = None, vars: dict | None = None,
                state: str | None = None, error: str | None = None) -> dict:
        with self._tx() as db:
            return self._enqueue(db, job, trigger, missed, scheduled if scheduled is not None else now, now,
                                 vars=vars, state=state, error=error)

    @staticmethod
    def _enqueue(db: sqlite3.Connection, job: dict, trigger: str, missed: bool, scheduled: float,
                 now: float, *, vars: dict | None = None, state: str | None = None,
                 error: str | None = None) -> dict:
        """Overlap rules (docs/design/jobs.md 4): ``parallel`` always runs;
        ``skip`` drops a trigger while a run is waiting or active; ``queue``
        waits behind the active run, without limit unless ``max_queue`` is
        set, over which the trigger is recorded as ``dropped``."""
        definition = job["definition"]
        ov = definition.get("overlap") or {}
        mode, cap = ov.get("mode", "queue"), ov.get("max_queue")
        if state is None:
            counts = dict(db.execute("SELECT state, count(*) FROM runs WHERE job_id=? AND state IN"
                                     " ('queued','due','running') GROUP BY state", (job["id"],)).fetchall())
            busy = counts.get("due", 0) + counts.get("running", 0)
            queued = counts.get("queued", 0)
            if mode == "parallel":
                state = "due"
            elif mode == "skip":
                state = "dropped" if busy or queued else "due"
                error = error or ("skipped: a run is already active" if state == "dropped" else None)
            elif not busy and not queued:
                state = "due"
            elif cap is None or queued < cap:
                state = "queued"
            else:
                state, error = "dropped", error or f"queue full (max_queue {cap})"
        rid = _new_id("r")
        finished = now if state in FINAL else None
        db.execute("INSERT INTO runs(id, job_id, job_name, job_revision, trigger, missed, state, scheduled,"
                   " created, finished, vars, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (rid, job["id"], job["name"], job.get("revision"), trigger, int(bool(missed)), state,
                    scheduled, now, finished, _dumps(vars or {}), error))
        return _row(db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone())

    # -- runs: lifecycle ---------------------------------------------------------------
    def promote(self) -> int:
        """Move each job's oldest queued run to due once nothing of that job
        is due or running."""
        with self._lock:
            return self._db.execute(
                "UPDATE runs SET state='due' WHERE id IN ("
                " SELECT q.id FROM runs q WHERE q.state='queued'"
                " AND q.rowid=(SELECT min(rowid) FROM runs q2 WHERE q2.job_id=q.job_id AND q2.state='queued')"
                " AND NOT EXISTS (SELECT 1 FROM runs a WHERE a.job_id=q.job_id AND a.state IN ('due','running')))"
            ).rowcount

    def claim(self, owner: str, now: float, lease: float) -> dict | None:
        """Atomically take the oldest due run (one statement)."""
        with self._lock:
            row = self._db.execute(
                "UPDATE runs SET state='running', lease_owner=?, lease_until=?, started=?"
                " WHERE id=(SELECT id FROM runs WHERE state='due' AND lease_until<? ORDER BY created, rowid LIMIT 1)"
                " AND state='due' RETURNING id", (owner, now + lease, now, now)).fetchone()
        return self.get_run(row["id"]) if row else None

    def renew(self, rid: str, owner: str, until: float) -> tuple[bool, bool]:
        """(still ours, cancel requested)."""
        with self._lock:
            held = self._db.execute("UPDATE runs SET lease_until=? WHERE id=? AND lease_owner=? AND state='running'",
                                    (until, rid, owner)).rowcount == 1
            row = self._db.execute("SELECT cancel FROM runs WHERE id=?", (rid,)).fetchone()
        return held, bool(row and row["cancel"])

    def save_progress(self, rid: str, owner: str, steps: dict, executions: int) -> None:
        with self._lock:
            self._db.execute("UPDATE runs SET steps=?, executions=? WHERE id=? AND lease_owner=? AND state='running'",
                             (_dumps(steps), executions, rid, owner))

    def finish(self, rid: str, owner: str, state: str, now: float, *, steps: dict | None = None,
               executions: int | None = None, error: str | None = None, alerts: Any = None,
               identity_used: str | None = None) -> bool:
        with self._lock:
            return self._db.execute(
                "UPDATE runs SET state=?, finished=?, steps=COALESCE(?, steps), executions=COALESCE(?, executions),"
                " error=?, alerts=?, identity_used=COALESCE(?, identity_used), lease_until=0"
                " WHERE id=? AND lease_owner=? AND state='running'",
                (state, now, _dumps(steps) if steps is not None else None, executions, error,
                 _dumps(alerts) if alerts is not None else None, identity_used, rid, owner)).rowcount == 1

    def set_identity(self, rid: str, identity: str) -> None:
        with self._lock:
            self._db.execute("UPDATE runs SET identity_used=? WHERE id=?", (identity, rid))

    def interrupt(self, now: float, owner: str | None = None) -> list[str]:
        """Mark runs that can no longer finish ``interrupted``: any whose
        lease ran out, plus (on start) any held by ``owner``, this hub's
        previous process. They are not resumed."""
        with self._tx() as db:
            rows = db.execute("SELECT id FROM runs WHERE state='running' AND (lease_until<? OR lease_owner IS ?)",
                              (now, owner)).fetchall()
            ids = [r["id"] for r in rows]
            for rid in ids:
                db.execute("UPDATE runs SET state='interrupted', finished=?, lease_until=0,"
                           " error=COALESCE(error, 'interrupted: the hub stopped while it ran') WHERE id=?",
                           (now, rid))
        return ids

    def cancel(self, rid: str, now: float) -> dict:
        with self._tx() as db:
            run = db.execute("SELECT state FROM runs WHERE id=?", (rid,)).fetchone()
            if run is None:
                raise KeyError(f"no run {rid!r}")
            if run["state"] in WAITING:
                db.execute("UPDATE runs SET state='cancelled', finished=?, error='cancelled' WHERE id=?", (now, rid))
            elif run["state"] == "running":
                db.execute("UPDATE runs SET cancel=1 WHERE id=?", (rid,))
        return self.get_run(rid)

    # -- runs: reads -------------------------------------------------------------------
    def get_run(self, rid: str) -> dict | None:
        return self._one("SELECT * FROM runs WHERE id=?", (rid,))

    def runs(self, jid: str | None = None, states: list[str] | None = None, limit: int = 20,
             since: float | None = None, missed: bool | None = None) -> list[dict]:
        where, args = [], []
        if jid:
            where.append("job_id=?")
            args.append(jid)
        if states:
            where.append("state IN (%s)" % ",".join("?" * len(states)))
            args += list(states)
        if since is not None:
            where.append("created>=?")
            args.append(since)
        if missed is not None:
            where.append("missed=?")
            args.append(int(missed))
        sql = "SELECT * FROM runs" + (" WHERE " + " AND ".join(where) if where else "")
        return self._all(sql + " ORDER BY created DESC, rowid DESC LIMIT ?", (*args, max(1, min(int(limit), 500))))

    def counts(self, jid: str) -> dict:
        with self._lock:
            return dict(self._db.execute("SELECT state, count(*) FROM runs WHERE job_id=? GROUP BY state",
                                         (jid,)).fetchall())

    # -- retention -----------------------------------------------------------------------
    def prune(self, now: float, default_days: int) -> int:
        """Delete finished runs older than their job's ``retention_days`` (or
        the hub default; runs of deleted jobs use the default)."""
        days = {}
        for job in self.list_jobs():
            rd = (job["definition"] or {}).get("retention_days")
            days[job["id"]] = int(rd) if rd else default_days
        gone = 0
        with self._tx() as db:
            for jid, d in days.items():
                gone += db.execute("DELETE FROM runs WHERE job_id=? AND finished IS NOT NULL AND finished<?",
                                   (jid, now - d * 86400)).rowcount
            known = list(days)
            gone += db.execute("DELETE FROM runs WHERE finished IS NOT NULL AND finished<? AND job_id NOT IN (%s)"
                               % (",".join("?" * len(known)) or "''"),
                               (now - default_days * 86400, *known)).rowcount
        return gone


def grace_seconds(job: dict) -> float:
    try:
        return parse_duration(((job.get("missed") or {}).get("grace")) or 0)
    except ValueError:
        return 0.0
