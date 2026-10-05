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
            CREATE TABLE IF NOT EXISTS dictation (
              id INTEGER PRIMARY KEY, session TEXT, text TEXT, created REAL);
            CREATE INDEX IF NOT EXISTS dictation_session ON dictation(session, id);
        """)
        # Dictation used to share the trimmed event history; move any such rows out.
        self.db.execute("INSERT INTO dictation(session,text,created) SELECT session,json_extract(body,'$.text'),created "
                        "FROM events WHERE kind='dictation' ORDER BY id")
        self.db.execute("DELETE FROM events WHERE kind='dictation'")
        self.db.execute("UPDATE jobs SET status='unknown', result=? WHERE status='running'",
                        ("Voice service restarted; do not repeat changes without checking their outcome.",))
        cutoff = time.time() - 7 * 86400
        self.db.execute("DELETE FROM events WHERE created < ?", (cutoff,))
        self.db.execute("DELETE FROM jobs WHERE updated < ? AND status != 'running'", (cutoff,))
        self.db.execute("DELETE FROM dictation WHERE created < ?", (cutoff,))
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
        rows = self.db.execute("SELECT kind,body FROM events WHERE session=? ORDER BY id DESC LIMIT 80",
                               (session,)).fetchall()
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

    # Dictation is the user's document, not conversation history: its own table,
    # never trimmed by the event window and never shown to the model. It is bounded
    # by total size; past the cap new segments are refused, never old ones dropped.
    DICTATION_MAX_CHARS = 200_000

    def dictation(self, session):
        """Dictated segments, oldest first, as (id, text)."""
        rows = self.db.execute("SELECT id,text FROM dictation WHERE session=? ORDER BY id", (session,)).fetchall()
        return [(row["id"], row["text"]) for row in rows]

    def add_dictation(self, session, text):
        """Append a segment; False (and nothing stored) when it would exceed the cap."""
        used = self.db.execute("SELECT COALESCE(SUM(LENGTH(text)),0) FROM dictation WHERE session=?",
                               (session,)).fetchone()[0]
        if used + len(text) > self.DICTATION_MAX_CHARS:
            return False
        self.db.execute("INSERT INTO dictation(session,text,created) VALUES(?,?,?)", (session, text, time.time()))
        self.db.commit()
        return True

    def drop_dictation(self, session, last=False, through=None):
        """Remove the last segment, segments up to id ``through``, or all of them."""
        if last:
            self.db.execute("DELETE FROM dictation WHERE id=(SELECT MAX(id) FROM dictation WHERE session=?)",
                            (session,))
        elif through is not None:
            self.db.execute("DELETE FROM dictation WHERE session=? AND id<=?", (session, through))
        else:
            self.db.execute("DELETE FROM dictation WHERE session=?", (session,))
        self.db.commit()

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
