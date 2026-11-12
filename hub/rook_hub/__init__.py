"""The Rook hub, skeleton: knock and approve, identities and keys, heartbeats,
calls and the journal.

The hub never sees a transport. A worker's link is any object with
`key` (the worker key the link authenticated), `async send(bytes)`,
`async recv() -> bytes` (raises EOFError once closed) and `close()`.
Knocks arrive over HTTP (docs/protocol.md, "Carriage").
"""

import asyncio
import json
import os
import pathlib
import secrets
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nacl.signing import SigningKey

from rook_protocol import PROTOCOL, ProtocolError, decode, encode

DEFAULT_INTERVAL = 15    # seconds; the hub can change it per worker (H2)
MISSED_HEARTBEATS = 3    # H3

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    guid           TEXT PRIMARY KEY,
    moniker        TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    owner          TEXT NOT NULL,
    approved_at    REAL NOT NULL,
    interval       INTEGER NOT NULL,
    protocol       INTEGER NOT NULL,
    last_heartbeat REAL,
    state          TEXT              -- the latest heartbeat, JSON
);
CREATE TABLE IF NOT EXISTS keys (
    key         TEXT PRIMARY KEY,
    worker      TEXT NOT NULL REFERENCES workers(guid),
    signature   TEXT NOT NULL,
    attached_at REAL NOT NULL,
    revoked_at  REAL
);
CREATE TABLE IF NOT EXISTS knocks (
    key      TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    macs     TEXT NOT NULL,          -- JSON list
    caps     TEXT NOT NULL,          -- JSON list
    protocol INTEGER NOT NULL,
    first_at REAL NOT NULL,
    last_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
    worker   TEXT NOT NULL REFERENCES workers(guid),
    kind     TEXT NOT NULL,          -- moniker, mac or hostname
    value    TEXT NOT NULL,
    first_at REAL NOT NULL,
    last_at  REAL NOT NULL,
    PRIMARY KEY (worker, kind, value)
);
CREATE INDEX IF NOT EXISTS seen_value ON seen (kind, value);
CREATE TABLE IF NOT EXISTS heartbeats (
    id     INTEGER PRIMARY KEY,
    worker TEXT NOT NULL REFERENCES workers(guid),
    at     REAL NOT NULL,
    body   TEXT NOT NULL             -- the heartbeat as received
);
CREATE INDEX IF NOT EXISTS heartbeats_worker_at ON heartbeats (worker, at);
CREATE TABLE IF NOT EXISTS journal (
    id     INTEGER PRIMARY KEY,
    at     REAL NOT NULL,
    kind   TEXT NOT NULL,            -- call or auth
    actor  TEXT NOT NULL,
    worker TEXT,
    cap    TEXT,
    why    TEXT,
    result TEXT NOT NULL             -- call: the reply message; auth: what happened (JSON)
);
"""


def error(code, message, next_step, call_id=None):
    """An `error` message (C3: it always says what to try next)."""
    reply = {"type": "error", "code": code, "message": message, "next": next_step}
    if call_id is not None:
        reply["id"] = call_id
    return reply


class HubError(Exception):
    """A hub function could not do what it was asked. `.message` is an `error` message."""

    def __init__(self, code, message, next_step):
        super().__init__(message)
        self.message = error(code, message, next_step)


def _load_words():
    sections, current = {}, None
    for line in (pathlib.Path(__file__).parent / "words.txt").read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = sections.setdefault(line.strip("[]"), [])
        else:
            current.extend(line.split())
    return sections["adjectives"], sections["nouns"]


class Hub:
    def __init__(self, state_dir, clock=time.time):
        self.state_dir = pathlib.Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.Lock()  # the knock endpoint runs in its own threads
        self._db = sqlite3.connect(self.state_dir / "hub.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._signing_key = self._load_signing_key()
        self.public_key = self._signing_key.verify_key.encode().hex()
        self._adjectives, self._nouns = _load_words()
        self._links = {}  # worker guid -> its live link
        self._calls = {}  # call id -> (worker guid, future)
        self._http = None

    def _load_signing_key(self):
        path = self.state_dir / "hub.key"
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(SigningKey.generate().encode().hex())
        return SigningKey(bytes.fromhex(path.read_text().strip()))

    def close(self):
        if self._http:
            self._http.shutdown()
            self._http.server_close()
        for link in list(self._links.values()):
            link.close()
        self._db.close()

    # ---------------------------------------------------------------- knock

    def knock(self, message):
        """Answer a knock (R2, R5): pending until approved, then the identity."""
        key = message["key"]
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT k.signature, k.revoked_at, w.guid, w.moniker FROM keys k "
                "JOIN workers w ON w.guid = k.worker WHERE k.key = ?", (key,)).fetchone()
            if row and row["revoked_at"] is not None:
                return error("key_revoked",
                             f"This key was revoked from {row['moniker']}.",
                             "Generate a new key, knock with it, and ask a person or agent to attach it.")
            if row:
                return {"type": "knock_reply", "protocol": PROTOCOL, "status": "approved",
                        "guid": row["guid"], "moniker": row["moniker"], "signature": row["signature"],
                        "hub_key": self.public_key}
            now = self.clock()
            self._db.execute(
                "INSERT INTO knocks (key, name, macs, caps, protocol, first_at, last_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET name = excluded.name, "
                "macs = excluded.macs, caps = excluded.caps, protocol = excluded.protocol, "
                "last_at = excluded.last_at",
                (key, message["name"], json.dumps([m.lower() for m in message["macs"]]),
                 json.dumps(message["caps"]), message["protocol"], now, now))
        return {"type": "knock_reply", "protocol": PROTOCOL, "status": "pending"}

    def serve_knocks(self, host, port):
        """Serve `POST /knock` over HTTP in background threads. Returns (host, port)."""
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != "/knock":
                    self.send_error(404)
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    message = decode(body)
                    if message["type"] != "knock":
                        raise ProtocolError(f"expected a knock, got {message['type']}")
                    reply = hub.knock(message)
                except ProtocolError as e:
                    reply = error("bad_message", str(e), "Send a knock as docs/protocol.md defines it.")
                status = {"bad_message": 400, "key_revoked": 403}.get(reply.get("code"), 200)
                data = encode(reply)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self._http = ThreadingHTTPServer((host, port), Handler)
        threading.Thread(target=self._http.serve_forever, daemon=True).start()
        return self._http.server_address

    def list_pending(self):
        """Pending knocks with name and offered caps, read-only (R4), each flagged
        when its name or a MAC matches a known identity (W7)."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM knocks ORDER BY first_at").fetchall()
            pending = []
            for row in rows:
                macs = json.loads(row["macs"])
                pending.append({
                    "key": row["key"], "name": row["name"], "macs": macs,
                    "caps": json.loads(row["caps"]), "protocol": row["protocol"],
                    "first_at": row["first_at"], "last_at": row["last_at"],
                    "likely_rekey_of": self._likely_rekey(row["name"], macs),
                })
        return pending

    def _likely_rekey(self, name, macs):
        matches = {}
        for row in self._db.execute(
                "SELECT guid AS worker, 'name' AS kind, name AS value FROM workers WHERE name = ? "
                "UNION SELECT worker, kind, value FROM seen WHERE kind IN ('moniker', 'hostname') AND value = ? "
                f"UNION SELECT worker, kind, value FROM seen WHERE kind = 'mac' AND value IN ({','.join('?' * len(macs))})",
                (name, name, *macs)):
            matches.setdefault(row["worker"], []).append(f"{row['kind']} {row['value']}")
        return [{"guid": guid, "moniker": self._moniker(guid), "matched": sorted(found)}
                for guid, found in matches.items()]

    def _moniker(self, guid):
        return self._db.execute("SELECT moniker FROM workers WHERE guid = ?", (guid,)).fetchone()[0]

    def _pending(self, key):
        row = self._db.execute("SELECT * FROM knocks WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise HubError("unknown_key", "No worker is knocking with that key.",
                           "List the pending knocks and use a key from there.")
        return row

    def approve(self, actor, key):
        """Approve a pending knock as a new worker: mint its GUID and moniker,
        sign its key and record `actor` as its owner (R2, R5, W6, W7, G1)."""
        with self._lock, self._db:
            knock = self._pending(key)
            guid, now = str(uuid.uuid4()), self.clock()
            moniker = self._new_moniker()
            self._db.execute(
                "INSERT INTO workers (guid, moniker, name, owner, approved_at, interval, protocol) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (guid, moniker, knock["name"], actor, now, DEFAULT_INTERVAL, knock["protocol"]))
            self._attach_key(guid, key, now)
            self._see(guid, "moniker", [moniker], now)
            self._see(guid, "mac", json.loads(knock["macs"]), now)
            self._db.execute("DELETE FROM knocks WHERE key = ?", (key,))
            self._journal("auth", actor, guid, {"approved": key})
        return self.get_worker(guid)

    def attach(self, actor, key, worker):
        """Attach a pending knock's key to a known identity and revoke the keys it
        held. Owner, config and journal history stay with the identity (W7, R5)."""
        with self._lock, self._db:
            knock = self._pending(key)
            guid, now = self._resolve(worker)["guid"], self.clock()
            revoked = [row[0] for row in self._db.execute(
                "SELECT key FROM keys WHERE worker = ? AND revoked_at IS NULL ORDER BY attached_at", (guid,))]
            self._db.execute("UPDATE keys SET revoked_at = ? WHERE worker = ? AND revoked_at IS NULL",
                             (now, guid))
            self._attach_key(guid, key, now)
            self._see(guid, "mac", json.loads(knock["macs"]), now)
            self._db.execute("DELETE FROM knocks WHERE key = ?", (key,))
            self._journal("auth", actor, guid, {"attached": key, "revoked": revoked})
        link = self._links.get(guid)
        if link is not None and link.key != key:
            link.close()  # the old key's link ends; the old key is refused from now on
        return self.get_worker(guid)

    def _journal(self, kind, actor, worker, result, cap=None, why=None):
        """One journal entry (L1). The caller holds the lock and the transaction."""
        self._db.execute(
            "INSERT INTO journal (at, kind, actor, worker, cap, why, result) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.clock(), kind, actor, worker, cap, why, json.dumps(result)))

    def _attach_key(self, guid, key, now):
        # R5: approval is the hub signing the worker's key (docs/protocol.md, knock_reply).
        signature = self._signing_key.sign(b"rook-worker-key:" + bytes.fromhex(key)).signature.hex()
        self._db.execute("INSERT INTO keys (key, worker, signature, attached_at) VALUES (?, ?, ?, ?)",
                         (key, guid, signature, now))

    def _see(self, guid, kind, values, now):
        for value in values:
            self._db.execute(
                "INSERT INTO seen (worker, kind, value, first_at, last_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (worker, kind, value) DO UPDATE SET last_at = excluded.last_at",
                (guid, kind, value.lower() if kind == "mac" else value, now, now))

    def _new_moniker(self):
        while True:
            first, second = secrets.choice(self._adjectives), secrets.choice(self._adjectives)
            moniker = f"{first}-{second}-{secrets.choice(self._nouns)}"
            if first != second and not self._db.execute(
                    "SELECT 1 FROM workers WHERE moniker = ?", (moniker,)).fetchone():
                return moniker

    # -------------------------------------------------------------- workers

    def _resolve(self, worker):
        """A worker row by GUID or moniker (G1)."""
        row = self._db.execute("SELECT * FROM workers WHERE guid = ? OR moniker = ?",
                               (worker, worker)).fetchone()
        if row is None:
            raise HubError("unknown_worker", f"No worker is called {worker!r}.",
                           "List the workers and use a GUID or moniker from there.")
        return row

    def _record(self, row):
        state = json.loads(row["state"]) if row["state"] else {}
        last = row["last_heartbeat"]
        return {
            "guid": row["guid"], "moniker": row["moniker"], "name": row["name"],
            "owner": row["owner"], "approved_at": row["approved_at"],
            "interval": row["interval"], "protocol": row["protocol"], "last_heartbeat": last,
            "online": last is not None and self.clock() - last <= MISSED_HEARTBEATS * row["interval"],
            "caps": state.get("caps", []), "facts": state.get("facts"), "band": state.get("band"),
            "health": state.get("health"), "streams": state.get("streams"),
        }

    def get_worker(self, worker):
        with self._lock:
            return self._record(self._resolve(worker))

    def list_workers(self):
        with self._lock:
            return [self._record(row) for row in
                    self._db.execute("SELECT * FROM workers ORDER BY approved_at")]

    def seen(self, worker):
        """The seen-workers record of one identity (W7)."""
        with self._lock:
            guid = self._resolve(worker)["guid"]
            record = {"monikers": [], "macs": [], "hostnames": []}
            for row in self._db.execute("SELECT kind, value FROM seen WHERE worker = ? ORDER BY first_at",
                                        (guid,)):
                record[row["kind"] + "s"].append(row["value"])
            record["keys"] = [dict(row) for row in self._db.execute(
                "SELECT key, attached_at, revoked_at FROM keys WHERE worker = ? ORDER BY attached_at", (guid,))]
            return record

    def set_interval(self, worker, seconds):
        """Set one worker's heartbeat interval; it gets it in the next heartbeat reply (H2)."""
        with self._lock, self._db:
            self._db.execute("UPDATE workers SET interval = ? WHERE guid = ?",
                             (int(seconds), self._resolve(worker)["guid"]))

    # ----------------------------------------------------------------- link

    async def serve_link(self, link):
        """Serve one worker link until it closes."""
        guid = None
        try:
            while True:
                data = await link.recv()
                try:
                    message = decode(data)
                except ProtocolError as e:
                    await link.send(encode(error("bad_message", str(e),
                                                 "Send messages as docs/protocol.md defines them.")))
                    continue
                if message["type"] == "heartbeat":
                    reply, accepted = self._heartbeat(link.key, message, data)
                    await link.send(encode(reply))
                    if accepted is None:
                        link.close()
                        return
                    guid = accepted
                    old = self._links.get(guid)
                    if old is not None and old is not link:
                        old.close()
                    self._links[guid] = link
                elif message["type"] in ("result", "error", "stream_open") and "id" in message:
                    pending = self._calls.get(message["id"])
                    if pending and pending[0] == guid and not pending[1].done():
                        pending[1].set_result(message)
                # stream_data and stream_close: fan-out to clients is not in this skeleton (S2).
        except EOFError:
            pass
        finally:
            if guid is not None and self._links.get(guid) is link:
                del self._links[guid]
                for call_id, (owner, future) in list(self._calls.items()):
                    if owner == guid and not future.done():
                        future.set_result(error("worker_disconnected", "The worker's link closed before it answered.",
                                                "Check whether the worker is online, then call again.", call_id))

    def _heartbeat(self, key, message, raw):
        """Store a heartbeat (H1, H4) and its raw copy (H5); reply with the interval (H2).
        Returns (reply, worker guid), or (error, None) when the key is not accepted."""
        if message["identity"]["key"] != key:
            return error("bad_message", "The heartbeat names a different key from the link's.",
                         "Send the key this link was opened with."), None
        with self._lock, self._db:
            row = self._db.execute("SELECT worker, revoked_at FROM keys WHERE key = ?", (key,)).fetchone()
            if row is None:
                return error("unknown_key", "This key was never approved.",
                             "Knock with it and wait for a person or agent to approve it."), None
            if row["revoked_at"] is not None:
                return error("key_revoked", "This key was revoked.",
                             "Generate a new key, knock with it, and ask a person or agent to attach it."), None
            guid, now = row["worker"], self.clock()
            state = {name: message[name] for name in ("facts", "caps", "band", "health", "streams")
                     if name in message}
            self._db.execute(
                "UPDATE workers SET name = ?, protocol = ?, last_heartbeat = ?, state = ? WHERE guid = ?",
                (message["identity"]["name"], message["protocol"], now, json.dumps(state), guid))
            self._db.execute("INSERT INTO heartbeats (worker, at, body) VALUES (?, ?, ?)",
                             (guid, now, raw.decode("utf-8")))
            self._see(guid, "hostname", [message["facts"]["hostname"]], now)
            self._see(guid, "mac", message["facts"]["macs"], now)
            interval = self._db.execute("SELECT interval FROM workers WHERE guid = ?", (guid,)).fetchone()[0]
        return {"type": "heartbeat_reply", "protocol": PROTOCOL, "interval": interval}, guid

    # ---------------------------------------------------------------- calls

    async def call(self, actor, worker, cap, args, why):
        """Call `cap` on `worker` (GUID or moniker) and return the reply message:
        `result`, `stream_open` or `error` (C3, W5). Every call is journaled (L1)."""
        call_id = uuid.uuid4().hex
        guid = None
        try:
            request = encode({"type": "call", "id": call_id, "cap": cap, "args": args})
            record = self.get_worker(worker)
            guid, moniker = record["guid"], record["moniker"]
            link = self._links.get(guid)
            if link is None or not record["online"]:
                reply = error("worker_offline", f"{moniker} is not connected.",
                              f"Wait for {moniker} to come back online, or call another worker "
                              f"that announces {cap}.", call_id)
            elif cap not in {c["name"] for c in record["caps"]}:
                reply = error("unknown_cap", f"{moniker} does not announce {cap}.",
                              f"List the caps {moniker} announces and call one of those.", call_id)
            else:
                future = asyncio.get_running_loop().create_future()
                self._calls[call_id] = (guid, future)
                try:
                    await link.send(request)
                    reply = await future
                except EOFError:
                    reply = error("worker_disconnected", f"{moniker}'s link closed before the call was sent.",
                                  "Check whether the worker is online, then call again.", call_id)
                finally:
                    self._calls.pop(call_id, None)
        except ProtocolError as e:
            reply = error("bad_message", str(e), "Fix the cap name or arguments and call again.", call_id)
        except HubError as e:
            reply = dict(e.message, id=call_id)
        with self._lock, self._db:
            self._journal("call", actor, guid, reply, cap, why)
        return reply
