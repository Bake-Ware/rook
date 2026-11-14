"""The Rook hub, skeleton: knock and approve, identities and keys, heartbeats,
calls and the journal.

The hub never sees a transport. A worker's link is any object with
`key` (the worker key the link authenticated), `async send(bytes)`,
`async recv() -> bytes` (raises EOFError once closed) and `close()`.
Knocks and the link exchange arrive over HTTP (docs/protocol.md, "Carriage"; link.py).
"""

import asyncio
import fcntl
import json
import os
import pathlib
import secrets
import sqlite3
import stat
import threading
import time
import uuid

from nacl.public import PrivateKey
from nacl.signing import SigningKey

from rook_protocol import MAX_MESSAGE, PROTOCOL, ProtocolError, decode, encode, verify_link_key

from . import bands, caps, devices, history, jobs, link, plugins, records, scrub, site, streams, vault, web
from .auth import Auth
from .events import Events
from .errors import HubError, error

DEFAULT_INTERVAL = 15    # seconds; the hub can change it per worker (H2)
MISSED_HEARTBEATS = 3    # H3
CALL_WAIT = 45           # seconds the door and the site wait for a call before answering "still running" (Bake 13 Nov)

# What a follower of a long call reads, and the journal keeps, when the hub restarted before the call ended.
RESTARTED = ("hub_restarted", "The hub restarted while this call was running, so its reply was lost.",
             "The call may still have done its work on the worker: check what it was doing (the journal, or the "
             "worker itself), then call again if it is still needed.")

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
    revoked_at  REAL,
    link_key    TEXT                -- the worker's X25519 link key, from its knock (link design 2.3)
);
CREATE TABLE IF NOT EXISTS knocks (
    key      TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    macs     TEXT NOT NULL,          -- JSON list
    caps     TEXT NOT NULL,          -- JSON list
    protocol INTEGER NOT NULL,
    first_at REAL NOT NULL,
    last_at  REAL NOT NULL,
    link_key TEXT                    -- checked against link_signature before the knock is kept
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
    kind   TEXT NOT NULL,            -- call, auth, band, event, job (a job's run), stream (a close) or plugin (a worker part that failed to load)
                                     -- a call's or job's entry is written when it starts, result {"type": "running", ...}, and holds its reply once it ends
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


def owner_only(top):
    """Take every permission of group and others off `top` and everything under it (not following
    links). Returns (how many files and folders had any, the paths this user may not change: those
    another user owns), and is never stopped by one of those."""
    paths, changed, refused = [top], 0, []
    for folder, folders, files in os.walk(top):
        paths += [os.path.join(folder, name) for name in folders + files]
    for path in paths:
        try:
            mode = os.lstat(path).st_mode
            if mode & 0o077 and not stat.S_ISLNK(mode):
                os.chmod(path, stat.S_IMODE(mode) & 0o700)
                changed += 1
        except PermissionError:
            refused.append(str(path))
    return changed, refused


class Hub:
    def __init__(self, state_dir, clock=time.time, vault_key=None, vault_key_file=None):
        """`vault_key` (32 bytes) opens the vault (X4); without it the vault is locked.
        Or `vault_key_file`: the file the key is kept in, outside `state_dir`, made on the
        first start of a vault never keyed and read on every start (vault.key_from_file)."""
        self.state_dir = pathlib.Path(state_dir)
        if vault_key_file is not None:
            vault_key_file = pathlib.Path(vault_key_file).expanduser().absolute()
            if vault_key_file.resolve().is_relative_to(self.state_dir.resolve()):
                raise HubError("vault_key_inside_data",
                               f"The vault key file {vault_key_file} is inside the data directory {self.state_dir}.",
                               "Keep the key file outside the data directory, so a copy of the data alone "
                               "opens no secret (X4), and start again.")
        self.vault_key_file = vault_key_file
        # The hub's data is its owner's only (X4, X2, L1; Bake 13 Nov): the directory is 0700 and
        # every file the hub writes in it 0600, whatever the umask. What an older start left open
        # to other users is tightened here, once; the start command says so (`tightened`). What another
        # user owns cannot be changed: it is left as it is, the start says that too, and the hub serves.
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.tightened, self.not_tightened = owner_only(self.state_dir)
        self.clock = clock
        self._lock = threading.Lock()  # every entry point takes it before touching the database
        # hub.db is made 0600 before SQLite opens it. SQLite gives the -wal and -shm files it makes
        # beside a database the database's own mode, so they come out 0600 too, on every start.
        os.close(os.open(self.state_dir / "hub.db", os.O_WRONLY | os.O_CREAT, 0o600))
        self._db = sqlite3.connect(self.state_dir / "hub.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # WAL with synchronous=NORMAL: a write is one append, not an fsync, so recording
        # a busy stream does not stall the hub. A power loss can lose the last moments
        # of writes; it cannot corrupt the database. Closing the hub folds the WAL back
        # into hub.db, so a closed hub is still one directory to move (F3).
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        for table in ("keys", "knocks"):     # a data directory made before the link keys gains the column
            if "link_key" not in {row["name"] for row in self._db.execute(f"PRAGMA table_info({table})")}:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN link_key TEXT")
        self._db.executescript(bands.SCHEMA)
        self._db.executescript(records.SCHEMA)
        self._db.executescript(jobs.SCHEMA)
        self._db.executescript(vault.SCHEMA)
        self._db.executescript(streams.SCHEMA)
        self.streams = streams.Streams(self)
        self._db.executescript(plugins.SCHEMA)
        self._db.executescript(devices.SCHEMA)
        self._db.executescript(site.SCHEMA)
        self._db.executescript(history.SCHEMA)
        history.roll_up_existing(self)   # once, for a hub that kept heartbeats before the roll-up (H5)
        if vault_key_file is not None:
            vault_key = vault.key_from_file(self, vault_key_file)
        self.vault = vault.Vault(self, vault_key)
        self.events = Events(self)
        self.auth = Auth(self)
        self._signing_key = self._load_key("hub.key", SigningKey)
        self.public_key = self._signing_key.verify_key.encode().hex()
        self.link_secret = self._load_key("hub_link.key", PrivateKey)    # X25519, for the link (link design 2.2)
        self.link_key = self.link_secret.public_key.encode().hex()
        self.sessions = link.Sessions()      # in memory only: a restart forgets every session
        self._adjectives, self._nouns = _load_words()
        self._links = {}  # worker guid -> its live link
        self._calls = {}  # call id -> (worker guid, future, actor, cap)
        self._running = {}  # call id -> {actor, worker, entry, reply}, while Hub.call runs it
        self._runner = None  # the HTTP server, while serving
        self._serving_dir = None  # the open hub.lock while this hub holds its data directory (serve)
        self.plugins = plugins.Plugins(self)
        self.plugins.load_all()  # every installed plugin, at start (PL3)

    def _load_key(self, name, kind):
        """A key of `kind` kept in the data directory, made on first start (owner-only)."""
        path = self.state_dir / name
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(kind.generate().encode().hex())
        return kind(bytes.fromhex(path.read_text().strip()))

    def close(self):
        """Close links and the database, and let go of the data directory. Call stop_serving() first if serving."""
        for link in list(self._links.values()):
            link.close()
        self._db.close()
        if self._serving_dir is not None:
            self._serving_dir.close()       # the lock goes with the file
            self._serving_dir = None

    # ---------------------------------------------------------------- knock

    def knock(self, message):
        """Answer a knock (R2, R5): pending until approved, then the identity.
        A knock carrying a good join token is approved at once (R3). A knock whose
        link_signature does not verify is refused, and nothing is kept (link design 2.3)."""
        key = message["key"]
        if not verify_link_key(message):
            return error("bad_signature", "The knock's link_signature does not verify under its key.",
                         "Sign the link key with the identity key, as docs/protocol.md says, and knock again.")
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
                "INSERT INTO knocks (key, name, macs, caps, protocol, first_at, last_at, link_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET name = excluded.name, "
                "macs = excluded.macs, caps = excluded.caps, protocol = excluded.protocol, "
                "last_at = excluded.last_at, link_key = excluded.link_key",
                (key, message["name"], json.dumps([m.lower() for m in message["macs"]]),
                 json.dumps(message["caps"]), message["protocol"], now, now, message["link_key"]))
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
        Returns the (host, port) it listens on. Only one hub serves a data directory: this one
        takes it over first, or raises HubError `data_in_use` and changes nothing. Having taken
        it over, it ends the calls a previous run of the hub left running."""
        self._take_over()
        self._runner, address = await web.serve(self, host, port)
        self._calls_cut_short()
        self.events.start()
        return address

    def _take_over(self):
        """Hold the data directory: an exclusive lock on <data>/hub.lock, kept until close().
        The system lets go of it when the process ends, however it ends."""
        if self._serving_dir is not None:
            return
        held = open(os.open(self.state_dir / "hub.lock", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a")
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held.close()
            raise HubError("data_in_use", f"Another hub is serving {self.state_dir.resolve()}.",
                           "Stop that hub first, or give this one a data directory of its own.") from None
        self._serving_dir = held

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
        self._attach_key(guid, key, now, knock["link_key"])
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
            self._attach_key(guid, key, now, knock["link_key"])
            self._see(guid, "mac", json.loads(knock["macs"]), now)
            self._db.execute("DELETE FROM knocks WHERE key = ?", (key,))
            self._journal("auth", actor, guid, {"attached": key, "revoked": revoked})
        for old_key in revoked:
            self.sessions.remove(old_key)     # a revoked key's sessions go at once (link design 3.3)
        link = self._links.get(guid)
        if link is not None and link.key != key:
            link.close()  # the old key's link ends; the old key is refused from now on
        return self.get_worker(guid)

    def _journal(self, kind, actor, worker, result, cap=None, why=None, job=None, run=None):
        """One journal entry (L1), scrubbed of secrets as it is written (L4).
        `actor` is (user or None, token or None). The caller holds the lock and the transaction."""
        why, result = scrub.scrub(why, self.vault), scrub.scrub(result, self.vault)
        return self._db.execute(
            "INSERT INTO journal (at, kind, user, token, worker, cap, why, result, job, run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.clock(), kind, actor[0], actor[1], worker, cap, why, json.dumps(result), job, run)).lastrowid

    def _kept(self, guid, reply):
        """A call's reply as the journal and a long call's stream keep it: never a usable token a hub
        cap returned, and scrubbed of secrets (L4)."""
        return scrub.scrub(caps.for_journal(reply) if guid == caps.HUB else reply, self.vault)

    def _calls_cut_short(self):
        """At start: a call that was running when the hub stopped gets an error saying so, in the
        journal and on its stream if it had one, so nothing waits on it for ever (C3)."""
        with self._lock, self._db:
            for row in self._db.execute("SELECT id, result FROM journal WHERE kind IN ('call', 'job') "
                                        "AND json_extract(result, '$.type') = 'running'").fetchall():
                self._db.execute("UPDATE journal SET result = ? WHERE id = ?",
                                 (json.dumps(error(*RESTARTED, json.loads(row["result"])["id"])), row["id"]))
        self.streams.cut_short(lambda call_id: error(*RESTARTED, call_id))

    def _attach_key(self, guid, key, now, link_key):
        # R5: approval is the hub signing the worker's key (docs/protocol.md, knock_reply). The
        # knock's link key goes with it, for the link exchange (link design 2.3).
        signature = self._signing_key.sign(b"rook-worker-key:" + bytes.fromhex(key)).signature.hex()
        self._db.execute("INSERT INTO keys (key, worker, signature, attached_at, link_key) VALUES (?, ?, ?, ?, ?)",
                         (key, guid, signature, now, link_key))

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
            "health": state.get("health"), "streams": state.get("streams"), "plugins": state.get("plugins"),
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
                    first, guid = guid is None, accepted
                    old = self._links.get(guid)
                    if old is not None and old is not link:
                        old.close()
                    self._links[guid] = link
                    if "open_streams" in message:   # the streams it still has; the rest it lost (D5)
                        self.streams.still_open(guid, message["open_streams"])
                    if first:       # connecting: the plugin worker parts it should have (PL3)
                        self.plugins.connected(guid, link, message.get("plugins", []))
                    self.events.heartbeat(self.get_worker(guid))
                elif message["type"] == "event" and guid is not None:   # a worker emits (J2)
                    record = self.get_worker(guid)
                    self.events.emit(message["name"], dict(message["payload"], worker=guid,
                                                           worker_moniker=record["moniker"]), worker=guid)
                elif message["type"] in ("result", "error", "stream_open") and "id" in message:
                    pending = self._calls.get(message["id"])
                    if pending and pending[0] == guid and not pending[1].done():
                        if message["type"] == "stream_open":   # record it before any data follows (S1, S2)
                            record = self.streams.opened(guid, message, pending[2], pending[3])
                            message = dict(message, stream=record["moniker"])
                        pending[1].set_result(message)
                elif message["type"] == "stream_data" and guid is not None:
                    self.streams.received(guid, message)
                elif message["type"] == "stream_close" and guid is not None:
                    self.streams.closed_by_worker(guid, message)
        except EOFError:
            pass
        finally:
            if guid is not None and self._links.get(guid) is link:
                del self._links[guid]
                for call_id, (owner, future, *_) in list(self._calls.items()):
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
            state = {name: message[name] for name in ("facts", "caps", "band", "health", "streams", "plugins")
                     if name in message}
            self._db.execute(
                "UPDATE workers SET name = ?, protocol = ?, last_heartbeat = ?, state = ? WHERE guid = ?",
                (message["identity"]["name"], message["protocol"], now, json.dumps(state), guid))
            stored = self._db.execute("INSERT INTO heartbeats (worker, at, body) VALUES (?, ?, ?)",
                                      (guid, now, raw.decode("utf-8")))
            history.roll(self, stored.lastrowid)    # the hourly roll-up, in the same transaction (H5)
            self._see(guid, "hostname", [message["facts"]["hostname"]], now)
            self._see(guid, "mac", message["facts"]["macs"], now)
            interval = before["interval"]
            if before["state"] is not None:     # a first heartbeat is not a change of band
                bands.moved(self, before, (message["band"]["gateway_mac"] or "").lower() or None)
            had = json.loads(before["state"]).get("plugins", []) if before["state"] else []
            for part in message.get("plugins", []):
                if "error" in part and part not in had:     # a worker part that failed to load (PL3)
                    self._journal("plugin", (None, None), guid, {"plugin": part["name"], "version": part["version"],
                                                                 "error": part["error"]})
        return {"type": "heartbeat_reply", "protocol": PROTOCOL, "interval": interval}, guid

    # ---------------------------------------------------------------- calls

    async def call(self, worker, cap, args, why, *, user=None, token=None, job=None, run=None, call_id=None):
        """Call `cap` on `worker` (GUID or moniker, or `hub` for the hub's own caps)
        and return the reply message:
        `result`, `stream_open` or `error` (C3, W5). The caller is a user acting
        directly or a client token (T4). Every call is journaled with both (L1);
        a caller who cannot be identified gets an error and no call is made.
        A job's run passes its `job` and `run`; the journal keeps it as kind `job` (J8).
        The journal entry is written as the call starts, as running, and holds the reply once it ends."""
        call_id = call_id or uuid.uuid4().hex
        guid, reply = None, None
        try:
            with self._lock, self._db:
                actor = self.auth.actor(user, token)
                entry = self._journal("job" if job else "call", actor, None, {"type": "running", "id": call_id},
                                      cap, why, job, run)
        except HubError as e:
            return dict(e.message, id=call_id)
        running = self._running[call_id] = {"actor": actor, "worker": None, "entry": entry, "reply": None}
        try:
            request = encode({"type": "call", "id": call_id, "cap": cap, "args": args})
            if worker == caps.HUB:      # the hub's own caps (caps.py)
                guid = caps.HUB
                used = args if cap in caps.KEEP_REFERENCES else self.vault.substitute(args, caps.HUB)
                result = caps.run(self, cap, used, {"user": user, "token": token})
                reply = {"type": "result", "id": call_id, "result": result}
            elif (device := devices.find(self, worker)) is not None:    # a device is called like a worker (D1)
                guid = running["worker"] = device["guid"]
                reply = {"type": "result", "id": call_id, "result": await devices.call(self, device, cap, args)}
            else:
                record = self.get_worker(worker)
                guid, moniker = record["guid"], record["moniker"]
                running["worker"] = guid
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
                    self.check_size(guid, request, "The call")
                    future = asyncio.get_running_loop().create_future()
                    self._calls[call_id] = (guid, future, actor, cap)
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
        except BaseException as e:   # it raised, or was cancelled, inside the hub: the entry still ends
            what = "run" if job else "call"
            reply = error(f"{what}_failed", f"The {what} stopped inside the hub: {type(e).__name__}: {e}",
                          "Read the hub's log for the details, then call again if it is still needed.", call_id)
            raise
        finally:
            running["reply"] = reply
            self._running.pop(call_id, None)
            with self._lock, self._db:
                self._db.execute("UPDATE journal SET worker = ?, result = ? WHERE id = ? "
                                 "AND json_extract(result, '$.type') = 'running'",
                                 (guid, json.dumps(self._kept(guid, reply)), entry))
        return reply

    def largest_message(self, guid):
        """The largest encoded message the hub may send this worker: 16,000,000 bytes, or the
        `max_message` of its last heartbeat if that is smaller (docs/protocol.md, "Sizes"; D4)."""
        with self._lock:
            row = self._db.execute("SELECT json_extract(body, '$.max_message') FROM heartbeats "
                                   "WHERE worker = ? ORDER BY at DESC, id DESC LIMIT 1", (guid,)).fetchone()
        stated = row[0] if row else None
        return min(MAX_MESSAGE, stated) if isinstance(stated, int) else MAX_MESSAGE

    def check_size(self, guid, message, what):
        """Refuse, before sending, an encoded message larger than the worker takes: HubError
        `message_too_large`. `what` names it in the refusal ("The call", "The input")."""
        limit = self.largest_message(guid)
        if len(message) > limit:
            raise HubError("message_too_large", f"{what} is {len(message)} bytes encoded; the worker takes "
                                                f"at most {limit}.",
                           "Send less at once: split it into smaller pieces, or move the data another way.")

    async def call_or_follow(self, worker, cap, args, why, *, user=None, token=None):
        """A call as `call` makes it, for a client that cannot wait long: the MCP door and the site
        (C1, C3; Bake 13 Nov). A reply within CALL_WAIT seconds is returned as it is. Otherwise the
        answer is {"type": "running", id, worker, stream} and the call goes on, once: when it ends,
        its reply, as the journal keeps it, is the stream's last entry and the hub closes the stream."""
        call_id = uuid.uuid4().hex
        task = asyncio.ensure_future(self.call(worker, cap, args, why, user=user, token=token, call_id=call_id))
        done, _ = await asyncio.wait({task}, timeout=CALL_WAIT)
        if done:
            return task.result()
        running = self._running[call_id]      # there until the call ends, and it has not
        stream = self.streams.for_call(call_id, running["worker"], cap, why, running["actor"])
        with self._lock, self._db:
            self._db.execute("UPDATE journal SET worker = ?, result = ? WHERE id = ? "
                             "AND json_extract(result, '$.type') = 'running'",
                             (running["worker"], json.dumps({"type": "running", "id": call_id, "stream": stream["moniker"]}),
                              running["entry"]))
        task.add_done_callback(
            lambda _: self.streams.call_ended(stream["guid"], self._kept(running["worker"], running["reply"])))
        return {"type": "running", "id": call_id, "worker": stream["worker"], "stream": stream["moniker"]}
