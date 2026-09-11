"""Private, bounded conversation and job persistence, independent of sockets."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


class Store:
    def __init__(self, path):
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY, session TEXT, kind TEXT, body TEXT, created REAL);
            CREATE INDEX IF NOT EXISTS events_session ON events(session, id);
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, session TEXT, name TEXT, args TEXT,
              status TEXT, result TEXT, updated REAL);
        """)
        self.db.execute("UPDATE jobs SET status='unknown', result=? WHERE status='running'",
                        ("Voice service restarted; do not repeat changes without checking their outcome.",))
        cutoff = time.time() - 7 * 86400
        self.db.execute("DELETE FROM events WHERE created < ?", (cutoff,))
        self.db.execute("DELETE FROM jobs WHERE updated < ? AND status != 'running'", (cutoff,))
        self.db.commit()

    @staticmethod
    def key(principal, conversation):
        # UUID validation prevents unbounded arbitrary identifiers and ambiguous keys.
        cid = str(uuid.UUID(conversation))
        return hashlib.sha256((principal + "\0" + cid).encode()).hexdigest()

    def append(self, session, kind, body):
        self.db.execute("INSERT INTO events(session,kind,body,created) VALUES(?,?,?,?)",
                        (session, kind, json.dumps(body), time.time()))
        self.db.execute("DELETE FROM events WHERE session=? AND id NOT IN "
                        "(SELECT id FROM events WHERE session=? ORDER BY id DESC LIMIT 160)", (session, session))
        self.db.commit()

    def messages(self, session):
        rows = self.db.execute("SELECT kind,body FROM events WHERE session=? ORDER BY id DESC LIMIT 80", (session,)).fetchall()
        groups, size = [], 0
        for row in rows:
            body = json.loads(row["body"])
            if row["kind"] == "tool":
                jid = body["id"]
                group = [{"role": "assistant", "content": None, "tool_calls": [{
                    "id": jid, "type": "function", "function": {"name": body["name"],
                    "arguments": json.dumps(body.get("args", {}))}}]},
                    {"role": "tool", "tool_call_id": jid, "content": body["result"]}]
            else:
                group = [{"role": row["kind"], "content": body["text"]}]
            n = len(json.dumps(group))
            if size + n > 40000:
                break
            size += n
            groups.append(group)
        return [message for group in reversed(groups) for message in group]

    def job(self, jid, session):
        row = self.db.execute("SELECT * FROM jobs WHERE id=? AND session=?", (jid, session)).fetchone()
        return dict(row) if row else None

    def jobs(self, session):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM jobs WHERE session=? ORDER BY updated DESC LIMIT 20", (session,))]

    def create_job(self, session, name, args):
        jid = uuid.uuid4().hex
        self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                        (jid, session, name, json.dumps(args), "running", "", time.time()))
        self.db.commit()
        return jid

    def finish_job(self, jid, status, result):
        self.db.execute("UPDATE jobs SET status=?,result=?,updated=? WHERE id=? AND status='running'",
                        (status, result[:16000], time.time(), jid))
        self.db.commit()
