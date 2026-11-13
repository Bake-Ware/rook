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

from nacl.signing import SigningKey

from rook_protocol import PROTOCOL, ProtocolError, decode, encode

from . import bands, caps, jobs, scrub, vault, web
from .auth import Auth
from .events import Events
from .errors import HubError, error

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
    kind   TEXT NOT NULL,            -- call, auth, band, event or job (a job's run)
    user   TEXT,                     -- the acting user (L1); none for events of the hub's own
    token  TEXT,                     -- the token they acted through, if any (L1)
    worker TEXT,
    job    TEXT,                     -- a job's run: the job (J8) ...
    run    TEXT,                     -- ... and the run ID its chain shares (J5)
    cap    TEXT,
    why    TEXT,
    result TEXT NOT NULL             -- call: the reply message; auth: what happened (JSON)
);
CREATE TABLE IF NOT EXISTS monikers (    -- one home, so monikers are unique hub-wide (G1)
    moniker TEXT PRIMARY KEY,
    guid    TEXT NOT NULL UNIQUE
);
"""


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
    def __init__(self, state_dir, clock=time.time, vault_key=None):
        """`vault_key` (32 bytes) opens the vault (X4); without it the vault is locked."""
        self.state_dir = pathlib.Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.Lock()  # every entry point takes it before touching the database
        self._db = sqlite3.connect(self.state_dir / "hub.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.executescript(bands.SCHEMA)
        self._db.executescript(jobs.SCHEMA)
        self._db.executescript(vault.SCHEMA)
        self.vault = vault.Vault(self, vault_key)
        self.events = Events(self)
        self.auth = Auth(self)
        self._signing_key = self._load_signing_key()
        self.public_key = self._signing_key.verify_key.encode().hex()
        self._adjectives, self._nouns = _load_words()
        self._links = {}  # worker guid -> its live link
        self._calls = {}  # call id -> (worker guid, future)
        self._runner = None  # the HTTP server, while serving

    def _load_signing_key(self):
        path = self.state_dir / "hub.key"
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(SigningKey.generate().encode().hex())
        return SigningKey(bytes.fromhex(path.read_text().strip()))

    def close(self):
        """Close links and the database. Call stop_serving() first if serving."""
        for link in list(self._links.values()):
            link.close()
        self._db.close()

    # ---------------------------------------------------------------- knock

    def knock(self, message):
        """Answer a knock (R2, R5): pending until approved, then the identity.
        A knock carrying a good join token is approved at once (R3)."""
        key = message["key"]
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT k.revoked_at, w.moniker FROM keys k "
                "JOIN workers w ON w.guid = k.worker WHERE k.key = ?", (key,)).fetchone()
            if row and row["revoked_at"] is not None:
                return error("key_revoked",
                             f"This key was revoked from {row['moniker']}.",
                             "Generate a new key, knock with it, and ask a person or agent to attach it.")
            if row:
                return self._approved_reply(key)
            actor = None
            if "token" in message:
                try:
                    actor = self.auth.use(message["token"], "join")
                except HubError as e:
                    return e.message
            now = self.clock()
            self._db.execute(
                "INSERT INTO knocks (key, name, macs, caps, protocol, first_at, last_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET name = excluded.name, "
                "macs = excluded.macs, caps = excluded.caps, protocol = excluded.protocol, "
                "last_at = excluded.last_at",
                (key, message["name"], json.dumps([m.lower() for m in message["macs"]]),
                 json.dumps(message["caps"]), message["protocol"], now, now))
            if actor is not None:
                self._approve(key, actor)
                return self._approved_reply(key)
        return {"type": "knock_reply", "protocol": PROTOCOL, "status": "pending"}

    def _approved_reply(self, key):
        row = self._db.execute(
            "SELECT k.signature, w.guid, w.moniker FROM keys k "
            "JOIN workers w ON w.guid = k.worker WHERE k.key = ?", (key,)).fetchone()
        return {"type": "knock_reply", "protocol": PROTOCOL, "status": "approved",
                "guid": row["guid"], "moniker": row["moniker"], "signature": row["signature"],
                "hub_key": self.public_key}

    async def serve(self, host, port):
        """Serve the hub over HTTP: the knock endpoint and the MCP door (web.py).
        Returns the (host, port) it listens on."""
        self._runner, address = await web.serve(self, host, port)
        self.events.start()
        return address

    async def stop_serving(self):
        await self.events.stop()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

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
        return self._db.execute("SELECT moniker FROM monikers WHERE guid = ?", (guid,)).fetchone()[0]

    def _pending(self, key):
        row = self._db.execute("SELECT * FROM knocks WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise HubError("unknown_key", "No worker is knocking with that key.",
                           "List the pending knocks and use a key from there.")
        return row

    def approve(self, key, *, user=None, token=None):
        """Approve a pending knock as a new worker: mint its GUID and moniker,
        sign its key and record the acting user as its owner (R2, R5, W6, W7, G1).
        The actor is a user acting directly, or a client token (T4)."""
        with self._lock, self._db:
            guid = self._approve(key, self.auth.actor(user, token))
        return self.get_worker(guid)

    def _approve(self, key, actor):
        """Approve with the lock and transaction held. `actor` is (user, token)."""
        knock = self._pending(key)
        guid, now = str(uuid.uuid4()), self.clock()
        moniker = self._new_moniker(guid)
        self._db.execute(
            "INSERT INTO workers (guid, moniker, name, owner, approved_at, interval, protocol) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guid, moniker, knock["name"], actor[0], now, DEFAULT_INTERVAL, knock["protocol"]))
        self._attach_key(guid, key, now)
        self._see(guid, "moniker", [moniker], now)
        self._see(guid, "mac", json.loads(knock["macs"]), now)
        self._db.execute("DELETE FROM knocks WHERE key = ?", (key,))
        self._journal("auth", actor, guid, {"approved": key})
        return guid

    def attach(self, key, worker, *, user=None, token=None):
        """Attach a pending knock's key to a known identity and revoke the keys it
        held. Owner, config and journal history stay with the identity (W7, R5)."""
        with self._lock, self._db:
            actor = self.auth.actor(user, token)
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

    def _journal(self, kind, actor, worker, result, cap=None, why=None, job=None, run=None):
        """One journal entry (L1), scrubbed of secrets as it is written (L4).
        `actor` is (user or None, token or None). The caller holds the lock and the transaction."""
        why, result = scrub.scrub(why, self.vault), scrub.scrub(result, self.vault)
        self._db.execute(
            "INSERT INTO journal (at, kind, user, token, worker, cap, why, result, job, run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.clock(), kind, actor[0], actor[1], worker, cap, why, json.dumps(result), job, run))

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

    def _new_moniker(self, guid):
        """A three-word moniker no other hub object holds, recorded for `guid` (G1).
        The caller holds the lock and the transaction."""
        while True:
            first, second = secrets.choice(self._adjectives), secrets.choice(self._adjectives)
            moniker = f"{first}-{second}-{secrets.choice(self._nouns)}"
            if first != second and not self._db.execute(
                    "SELECT 1 FROM monikers WHERE moniker = ?", (moniker,)).fetchone():
                self._db.execute("INSERT INTO monikers (moniker, guid) VALUES (?, ?)", (moniker, guid))
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
                    self.events.heartbeat(self.get_worker(guid))
                elif message["type"] == "event" and guid is not None:   # a worker emits (J2)
                    record = self.get_worker(guid)
                    self.events.emit(message["name"], dict(message["payload"], worker=guid,
                                                           worker_moniker=record["moniker"]), worker=guid)
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
            before = self._db.execute("SELECT * FROM workers WHERE guid = ?", (guid,)).fetchone()
            state = {name: message[name] for name in ("facts", "caps", "band", "health", "streams")
                     if name in message}
            self._db.execute(
                "UPDATE workers SET name = ?, protocol = ?, last_heartbeat = ?, state = ? WHERE guid = ?",
                (message["identity"]["name"], message["protocol"], now, json.dumps(state), guid))
            self._db.execute("INSERT INTO heartbeats (worker, at, body) VALUES (?, ?, ?)",
                             (guid, now, raw.decode("utf-8")))
            self._see(guid, "hostname", [message["facts"]["hostname"]], now)
            self._see(guid, "mac", message["facts"]["macs"], now)
            interval = before["interval"]
            if before["state"] is not None:     # a first heartbeat is not a change of band
                bands.moved(self, before, (message["band"]["gateway_mac"] or "").lower() or None)
        return {"type": "heartbeat_reply", "protocol": PROTOCOL, "interval": interval}, guid

    # ---------------------------------------------------------------- calls

    async def call(self, worker, cap, args, why, *, user=None, token=None, job=None, run=None):
        """Call `cap` on `worker` (GUID or moniker, or `hub` for the hub's own caps)
        and return the reply message:
        `result`, `stream_open` or `error` (C3, W5). The caller is a user acting
        directly or a client token (T4). Every call is journaled with both (L1);
        a caller who cannot be identified gets an error and no call is made.
        A job's run passes its `job` and `run`; the journal keeps it as kind `job` (J8)."""
        call_id = uuid.uuid4().hex
        guid = None
        try:
            with self._lock, self._db:
                actor = self.auth.actor(user, token)
        except HubError as e:
            return dict(e.message, id=call_id)
        try:
            request = encode({"type": "call", "id": call_id, "cap": cap, "args": args})
            if worker == caps.HUB:      # the hub's own caps (caps.py)
                guid = caps.HUB
                used = args if cap in caps.KEEP_REFERENCES else self.vault.substitute(args, caps.HUB)
                result = caps.run(self, cap, used, {"user": user, "token": token})
                reply = {"type": "result", "id": call_id, "result": result}
            else:
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
                    request = encode({"type": "call", "id": call_id, "cap": cap,
                                      "args": self.vault.substitute(args, guid)})   # secret:<name> (X2)
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
            self._journal("job" if job else "call", actor, guid, caps.for_journal(reply) if guid == caps.HUB else reply,
                          cap, why, job, run)
        return reply
