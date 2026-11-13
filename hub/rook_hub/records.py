"""Records (K1-K7): the one general store everything above core keeps its data in.

A record is a type, a GUID and a three-word moniker (G1), a title, a
description, an owner (a user), a creator (the user, and the token when an
agent acted), a JSON body and its created and updated times (K1). Core never
looks inside the body: there is no schema registry.

These are plain functions for plugin code only. Records are never a raw cap
and never an MCP tool (K2). Every type is declared by the plugin that owns it
before a record of that type can be made, so types do not creep (K2). Core
itself declares one type, `blob` (K3): its bytes live on the hub's disk under
`blobs/`, named by their SHA-256, so the same bytes are kept once; its body
holds the hash, the size and the MIME type.

The store writes nothing to the journal: the plugin cap that changes a record
is already journaled (L1). Nothing here deletes anything on its own (K4).

Storage (K7): one `records` table with indexed columns and a JSON body, a
separate `record_links` table, and a full-text index `record_text` over the
title, the description and the body's text.
"""

import hashlib
import json
import os
import uuid

from .errors import HubError

BLOB = "blob"          # the record type core declares itself (K3)
CORE = "core"          # the owner of that type, until Bake says otherwise

SCHEMA = """
CREATE TABLE IF NOT EXISTS record_types (
    type        TEXT PRIMARY KEY,
    plugin      TEXT NOT NULL            -- the plugin that owns the type (K2)
);
INSERT OR IGNORE INTO record_types (type, plugin) VALUES ('blob', 'core');
CREATE TABLE IF NOT EXISTS records (
    id            INTEGER PRIMARY KEY,   -- the full-text index's rowid
    guid          TEXT NOT NULL UNIQUE,
    moniker       TEXT NOT NULL UNIQUE,
    type          TEXT NOT NULL REFERENCES record_types(type),
    title         TEXT NOT NULL,
    description   TEXT NOT NULL,
    owner         TEXT NOT NULL,         -- user GUID
    creator       TEXT NOT NULL,         -- user GUID
    creator_token TEXT,                  -- token GUID, when an agent created it
    body          TEXT NOT NULL,         -- JSON
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS records_type ON records (type, updated_at);
CREATE TABLE IF NOT EXISTS record_links (
    source    TEXT NOT NULL REFERENCES records(guid),
    target    TEXT NOT NULL REFERENCES records(guid),
    linked_at REAL NOT NULL,
    PRIMARY KEY (source, target)
);
CREATE INDEX IF NOT EXISTS record_links_target ON record_links (target);
CREATE VIRTUAL TABLE IF NOT EXISTS record_text USING fts5(title, description, body);
"""


def _text(value, what):
    if not isinstance(value, str):
        raise HubError("bad_message", f"{what} must be a string.", f"Give {what} as a string.")
    return value


def _json(body):
    try:
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as e:
        raise HubError("bad_message", f"The body is not JSON: {e}.",
                       "Give a body made of objects, lists, strings, numbers, true, false and null.") from None


def _words(body):
    """The text in a body, for the full-text index: its strings, not its keys."""
    if isinstance(body, str):
        return [body]
    if isinstance(body, dict):
        return [w for value in body.values() for w in _words(value)]
    if isinstance(body, list):
        return [w for value in body for w in _words(value)]
    return []


def _index(hub, row):
    """Write one record's row in the full-text index. The lock is held."""
    hub._db.execute("DELETE FROM record_text WHERE rowid = ?", (row["id"],))
    hub._db.execute("INSERT INTO record_text (rowid, title, description, body) VALUES (?, ?, ?, ?)",
                    (row["id"], row["title"], row["description"], " ".join(_words(json.loads(row["body"])))))


def _row(hub, record):
    """A record row by GUID or moniker (G1). The lock is held."""
    row = hub._db.execute("SELECT * FROM records WHERE guid = ? OR moniker = ?", (record, record)).fetchone()
    if row is None:
        raise HubError("unknown_record", f"No record is called {record!r}.",
                       "Search the records and use a GUID or moniker from there.")
    return row


def _record(row):
    return {"guid": row["guid"], "moniker": row["moniker"], "type": row["type"], "title": row["title"],
            "description": row["description"], "owner": row["owner"], "creator": row["creator"],
            "creator_token": row["creator_token"], "body": json.loads(row["body"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


# ------------------------------------------------------------------ types

def declare_type(hub, type, plugin):
    """Declare that `plugin` owns record type `type` (K2). Declaring it again for
    the same plugin changes nothing; for another plugin it is refused."""
    if not _text(type, "type") or not _text(plugin, "plugin"):
        raise HubError("bad_message", "type and plugin must not be empty.", "Give the type's name and its plugin's.")
    with hub._lock, hub._db:
        row = hub._db.execute("SELECT plugin FROM record_types WHERE type = ?", (type,)).fetchone()
        if row is not None and row["plugin"] != plugin:
            raise HubError("type_taken", f"Record type {type!r} belongs to plugin {row['plugin']!r}.",
                           f"Use that plugin's caps for {type} records, or declare a type of your own.")
        hub._db.execute("INSERT OR IGNORE INTO record_types (type, plugin) VALUES (?, ?)", (type, plugin))
    return {"type": type, "plugin": plugin}


def list_types(hub):
    with hub._lock:
        return [dict(row) for row in hub._db.execute("SELECT type, plugin FROM record_types ORDER BY type")]


# ------------------------------------------------------------------- CRUD

def _create(hub, type, title, description, body, owner, actor):
    """Insert one record. The lock and the transaction are held."""
    if hub._db.execute("SELECT 1 FROM record_types WHERE type = ?", (type,)).fetchone() is None:
        raise HubError("unknown_type", f"No plugin has declared the record type {type!r}.",
                       f"Declare the type first, with the plugin that owns it: declare_type({type!r}, plugin).")
    owner = actor[0] if owner is None else hub.auth._user(owner)["guid"]
    guid, now = str(uuid.uuid4()), hub.clock()
    cursor = hub._db.execute(
        "INSERT INTO records (guid, moniker, type, title, description, owner, creator, creator_token, body, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (guid, hub._new_moniker(guid), type, title, description, owner, actor[0], actor[1], body, now, now))
    row = hub._db.execute("SELECT * FROM records WHERE id = ?", (cursor.lastrowid,)).fetchone()
    _index(hub, row)
    return _record(row)


def create(hub, type, title, description="", body=None, *, owner=None, user=None, token=None):
    """Make a record of a declared type (K1, K2). The actor is a user acting
    directly or a client token (T4); the owner is the acting user unless another
    user (GUID or moniker) is named. `body` is any JSON value; None means {}.
    A blob is made only by store_blob, so every blob record has bytes behind it (K3)."""
    if type == BLOB:
        raise HubError("blob_needs_bytes", "A blob record is made from its bytes, not from a body.",
                       "Call store_blob with the bytes and their MIME type.")
    title, description = _text(title, "title"), _text(description, "description")
    body = _json({} if body is None else body)
    with hub._lock, hub._db:
        return _create(hub, type, title, description, body, owner, hub.auth.actor(user, token))


def get(hub, record):
    """One record by GUID or moniker (G1)."""
    with hub._lock:
        return _record(_row(hub, record))


def update(hub, record, *, title=None, description=None, body=None):
    """Change a record's title, description or body; what is not given (None) stays.
    A blob's body describes its bytes, so it never changes; its title and description may."""
    changes = {}
    if title is not None:
        changes["title"] = _text(title, "title")
    if description is not None:
        changes["description"] = _text(description, "description")
    if body is not None:
        changes["body"] = _json(body)
    with hub._lock, hub._db:
        row = _row(hub, record)
        if "body" in changes and row["type"] == BLOB:
            raise HubError("blob_body_fixed", f"Record {row['moniker']} is a blob: its body describes its bytes.",
                           "Change its title or description, or store the new bytes with store_blob.")
        if changes:
            hub._db.execute(f"UPDATE records SET {', '.join(f'{name} = ?' for name in changes)}, updated_at = ? "
                            "WHERE id = ?", (*changes.values(), hub.clock(), row["id"]))
            row = hub._db.execute("SELECT * FROM records WHERE id = ?", (row["id"],)).fetchone()
            _index(hub, row)
        return _record(row)


def delete(hub, record):
    """Remove a record and every link to or from it, for good. A blob's bytes stay on disk."""
    with hub._lock, hub._db:
        row = _row(hub, record)
        hub._db.execute("DELETE FROM record_links WHERE source = ? OR target = ?", (row["guid"], row["guid"]))
        hub._db.execute("DELETE FROM record_text WHERE rowid = ?", (row["id"],))
        hub._db.execute("DELETE FROM records WHERE id = ?", (row["id"],))


# ----------------------------------------------------------------- search

def search(hub, type=None, text=None, limit=50):
    """Records of `type`, or whose title, description or body text holds every
    word of `text`, or both; most recently updated first, at most `limit`."""
    where, params, join = [], [], ""
    if type is not None:
        where.append("r.type = ?")
        params.append(type)
    words = text.split() if text else []
    if words:
        join = "JOIN record_text t ON t.rowid = r.id"
        where.append("record_text MATCH ?")
        params.append(" ".join('"' + word.replace('"', '""') + '"' for word in words))
    with hub._lock:
        rows = hub._db.execute(
            f"SELECT r.* FROM records r {join} {'WHERE ' + ' AND '.join(where) if where else ''} "
            "ORDER BY r.updated_at DESC, r.id DESC LIMIT ?", (*params, limit)).fetchall()
    return [_record(row) for row in rows]


# ------------------------------------------------------------------ links

def link(hub, source, target):
    """Link one record to another (K3). Linking the same pair again changes nothing."""
    with hub._lock, hub._db:
        a, b = _row(hub, source)["guid"], _row(hub, target)["guid"]
        hub._db.execute("INSERT OR IGNORE INTO record_links (source, target, linked_at) VALUES (?, ?, ?)",
                        (a, b, hub.clock()))


def unlink(hub, source, target):
    """Remove the link from `source` to `target`, if there is one."""
    with hub._lock, hub._db:
        a, b = _row(hub, source)["guid"], _row(hub, target)["guid"]
        hub._db.execute("DELETE FROM record_links WHERE source = ? AND target = ?", (a, b))


def links(hub, record):
    """A record's links in both directions: `to` the records it links to, `from`
    the records that link to it."""
    with hub._lock:
        guid = _row(hub, record)["guid"]
        out = hub._db.execute("SELECT r.* FROM record_links l JOIN records r ON r.guid = l.target "
                              "WHERE l.source = ? ORDER BY l.linked_at, r.id", (guid,)).fetchall()
        into = hub._db.execute("SELECT r.* FROM record_links l JOIN records r ON r.guid = l.source "
                               "WHERE l.target = ? ORDER BY l.linked_at, r.id", (guid,)).fetchall()
    return {"to": [_record(row) for row in out], "from": [_record(row) for row in into]}


# ------------------------------------------------------------------ blobs

def _blob_path(hub, digest):
    return hub.state_dir / "blobs" / digest


def store_blob(hub, data, mime, title="", description="", *, owner=None, user=None, token=None):
    """Keep `data` (bytes) on the hub's disk and make a `blob` record for it (K3).
    The file is named by its SHA-256, so the same bytes are kept once."""
    if not isinstance(data, (bytes, bytearray)):
        raise HubError("bad_message", "A blob's data must be bytes.", "Give the blob's content as bytes.")
    mime, title, description = _text(mime, "mime"), _text(title, "title"), _text(description, "description")
    digest = hashlib.sha256(data).hexdigest()
    body = _json({"hash": digest, "size": len(data), "mime": mime})
    with hub._lock, hub._db:
        actor = hub.auth.actor(user, token)
        path = _blob_path(hub, digest)
        if not path.exists():
            path.parent.mkdir(exist_ok=True)
            partial = path.with_name(f"{digest}.partial")
            partial.write_bytes(data)
            os.replace(partial, path)
        return _create(hub, BLOB, title, description, body, owner, actor)


def read_blob(hub, record):
    """The bytes of a blob record."""
    with hub._lock:
        row = _row(hub, record)
    if row["type"] != BLOB:
        raise HubError("not_a_blob", f"Record {row['moniker']} is a {row['type']}, not a blob.",
                       "Read its body with get(); only blob records have bytes.")
    return _blob_path(hub, json.loads(row["body"])["hash"]).read_bytes()


def unlinked_blobs(hub):
    """Blob records with no link to or from any record, oldest first, so an agent
    can decide what to do with them (K4). Nothing is removed."""
    with hub._lock:
        rows = hub._db.execute(
            "SELECT r.* FROM records r WHERE r.type = ? AND NOT EXISTS "
            "(SELECT 1 FROM record_links l WHERE l.source = r.guid) AND NOT EXISTS "
            "(SELECT 1 FROM record_links l WHERE l.target = r.guid) ORDER BY r.created_at, r.id", (BLOB,)).fetchall()
    return [_record(row) for row in rows]
