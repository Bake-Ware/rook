"""The drive journal: every run and every frame, in ``drive.db`` in the
plugin's data directory (``<hub state>/plugins/decide``)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any


class DriveJournal:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.db = conn
        self.db.row_factory = sqlite3.Row
        self._lock = threading.Lock()

    def new_run(self, run_id: str, *, identity: str, goal: str, screen_worker: str,
                input_worker: str, dry_run: bool, config: dict) -> None:
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO drive_runs (id, created, identity, goal, screen_worker, input_worker,"
                " dry_run, state, config) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, time.time(), identity or "", goal, screen_worker, input_worker,
                 int(bool(dry_run)), "starting", json.dumps(config, default=str)))

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {"state", "reason", "steps", "backend", "finished"}
        cols = {k: v for k, v in fields.items() if k in allowed}
        if not cols:
            return
        with self._lock, self.db:
            self.db.execute(
                f"UPDATE drive_runs SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                (*cols.values(), run_id))

    def step(self, run_id: str, row: dict) -> None:
        with self._lock, self.db:
            n = self.db.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM drive_steps WHERE run_id = ?",
                                (run_id,)).fetchone()[0]
            self.db.execute("INSERT INTO drive_steps (run_id, n, ts, outcome, data) VALUES (?,?,?,?,?)",
                            (run_id, n, time.time(), str(row.get("outcome") or ""),
                             json.dumps(row, default=str)))

    def runs(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT id, created, identity, goal, screen_worker, input_worker, dry_run, state,"
                " reason, steps, backend, finished FROM drive_runs ORDER BY created DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [{**dict(r), "dry_run": bool(r["dry_run"])} for r in rows]

    def run(self, run_id: str) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM drive_runs WHERE id = ?", (run_id,)).fetchone()
        if r is None:
            return None
        out = dict(r)
        out["dry_run"] = bool(out["dry_run"])
        out["config"] = json.loads(out.get("config") or "{}")
        return out

    def steps(self, run_id: str, last: int = 10) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT n, ts, outcome, data FROM drive_steps WHERE run_id = ? ORDER BY n DESC LIMIT ?",
                (run_id, int(last))).fetchall()
        return [{"n": r["n"], "ts": r["ts"], "outcome": r["outcome"], **json.loads(r["data"])}
                for r in reversed(rows)]

    def mark_interrupted(self) -> int:
        """Runs left open by a previous process: they are not running any more."""
        with self._lock, self.db:
            cur = self.db.execute(
                "UPDATE drive_runs SET state = 'stopped', reason = 'hub restarted', finished = ?"
                " WHERE state IN ('starting', 'running', 'awaiting_confirmation')", (time.time(),))
            return cur.rowcount
