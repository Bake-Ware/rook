"""Record storage on the hub: one SQLite file for every plugin's record types.

A manifest ``records:`` block (castling-manifest-2, "Records") declares a type;
:meth:`RecordStore.register` turns it into tables:

* ``"r:<plugin>.<record>"``: one row per record. Kit columns (id, band,
  revision, created, updated, archived, creator, person) plus one column per
  field. ``creator`` is the agent (or person) that made it; ``person`` is who
  it was made for.
* ``"h:<plugin>.<record>"``: a snapshot per write when ``history: true``.
* ``"f:<plugin>.<record>"``: an FTS5 index over the ``search`` fields.
* ``kit_refs``: every ``ref`` field value, so backlinks work across plugins.
* ``kit_schema``: the fields each type was last registered with.

Migrations are additive: new fields become new columns, dropped fields keep
their column (rolling a plugin back never loses data), index and unique
changes rebuild their indexes, search changes rebuild the FTS table. A type
change is not additive and needs the plugin's migration (SQLite can't retype
a column, so the migration moves it aside and adds the new one).

Every operation takes the :class:`~rook.kit.ctx.Caller`: rows are filtered to
the caller's band, private types to their creator (and owners), and records
name their creator. A private record made for a person is that person's,
whichever agent they use; one made by an agent alone is that agent's. Writes emit ``<plugin>.<record>.created|updated|archived``
through the injected ``emit`` after they commit.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .._types import check, parse_duration
from ..ctx import Caller, KitError, load_run

__all__ = ["RecordDef", "RecordStore", "RecordType", "VERBS"]

VERBS = ("list", "get", "search", "create", "update", "archive", "delete")
KIT_COLUMNS = ("id", "band", "revision", "created", "updated", "archived", "creator", "person")
_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_PLUGIN = re.compile(r"[a-z][a-z0-9-]*\Z")
_AFFINITY = {"int": "INTEGER", "bool": "INTEGER", "float": "REAL"}
_JSON_TYPES = {"json", "list", "map"}
_SAME_STORAGE = {"str", "markdown"}   # a type change between these is still additive
_TYPES = {"str", "int", "float", "bool", "time", "duration", "markdown", "json", "file",
          "secret", "ref", "list", "map"}


@dataclass
class RecordDef:
    """One parsed ``records.<name>`` block."""

    plugin: str
    name: str
    fields: dict[str, dict]
    index: list[tuple[str, ...]] = field(default_factory=list)
    search: list[str] = field(default_factory=list)
    semantic: list[str] = field(default_factory=list)
    history: bool = False
    private: bool = False
    retain: float | None = None
    verbs: list[str] = field(default_factory=list)
    risk: dict[str, str] = field(default_factory=dict)
    migrations: str | None = None

    @property
    def full(self) -> str:
        return f"{self.plugin}.{self.name}"

    @classmethod
    def parse(cls, plugin: str, name: str, block: Mapping) -> "RecordDef":
        if not _PLUGIN.match(plugin):
            raise KitError(f"bad plugin name {plugin!r}")
        if not _NAME.match(name):
            raise KitError(f"bad record name {name!r}: use [a-z][a-z0-9_]*")
        fields = {k: dict(v or {}) for k, v in (block.get("fields") or {}).items()}
        if not fields:
            raise KitError(f"{plugin}.{name}: a record needs fields")
        for f, spec in fields.items():
            if not _NAME.match(f) or f in KIT_COLUMNS:
                raise KitError(f"{plugin}.{name}: bad or reserved field name {f!r}")
            spec.setdefault("type", "str")
            if spec["type"] not in _TYPES:
                raise KitError(f"{plugin}.{name}.{f}: unknown type {spec['type']!r}")
            if spec["type"] == "ref" and not spec.get("to"):
                raise KitError(f"{plugin}.{name}.{f}: a ref needs to:")
            if spec["type"] == "ref" and "." not in spec["to"]:
                spec["to"] = f"{plugin}.{spec['to']}"
        index = [tuple([i] if isinstance(i, str) else i) for i in block.get("index") or []]
        search = list(block.get("search") or [])
        semantic = list(block.get("semantic") or [])
        for f in [c for cols in index for c in cols] + search + semantic:
            if f not in fields:
                raise KitError(f"{plugin}.{name}: index/search names unknown field {f!r}")
        verbs = list(block.get("caps") or [v for v in VERBS if v != "search" or search])
        bad = [v for v in verbs if v not in VERBS]
        if bad:
            raise KitError(f"{plugin}.{name}: unknown record caps {bad}")
        if "search" in verbs and not search:
            raise KitError(f"{plugin}.{name}: caps lists search but no search fields are declared")
        retain = block.get("retain")
        return cls(plugin, name, fields, index, search, semantic,
                   history=bool(block.get("history")), private=bool(block.get("private")),
                   retain=parse_duration(retain) if retain not in (None, "forever") else None,
                   verbs=verbs, risk=dict(block.get("risk") or {}),
                   migrations=block.get("migrations"))


def _script_migrate(plugin_dir: str | Path, ref: str) -> Callable:
    """The plugin's migration script, loaded only when a change needs it."""
    return lambda db, rdef, old: load_run(plugin_dir, ref)(db, rdef, old)


def _q(name: str) -> str:
    """Quote an identifier. Names are validated, so they never hold a quote."""
    return '"' + name + '"'


def _private_key(rec: Mapping) -> str:
    """Who may see a private record, in kit_refs: its person, else its agent."""
    return f"p:{rec['person']}" if rec["person"] else f"a:{rec['creator']}"


def _fts_query(text: str) -> str:
    """Treat the query as plain words (all must match), never FTS syntax."""
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    if not words:
        raise KitError("search needs a query")
    return " ".join('"' + w.replace('"', '""') + '"' for w in words)


class RecordStore:
    """The hub's record store. ``emit(event, payload, source)`` publishes the
    auto events; ``semantic`` is the embedding hook (below)."""

    def __init__(self, path: str = ":memory:", *, emit: Callable[[str, dict, str], Any] | None = None,
                 semantic: Any = None, clock: Callable[[], float] = time.time):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._types: dict[str, RecordType] = {}
        self._emit = emit
        # TODO(castling): semantic search. No embedding model yet. When one
        # exists, pass an object with index(type, id, text), forget(type, id)
        # and search(type, query, limit) -> [id]; the store feeds it the
        # ``semantic`` fields on every write and search(semantic=True) uses it.
        self.semantic = semantic
        self.clock = clock
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS kit_schema (type TEXT PRIMARY KEY, fields TEXT, search TEXT);
            CREATE TABLE IF NOT EXISTS kit_refs (
                src TEXT, src_id TEXT, field TEXT, dst TEXT, dst_id TEXT, band TEXT,
                private_creator TEXT, PRIMARY KEY (src, src_id, field));
            CREATE INDEX IF NOT EXISTS kit_refs_dst ON kit_refs (dst, dst_id);
        """)

    def close(self) -> None:
        self._db.close()

    def _query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        """Reads share the one connection with writers, so they take the lock too."""
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    # -- types ----------------------------------------------------------------

    def register(self, manifest: Mapping, *, plugin_dir: str | Path | None = None,
                 migrate: Callable | None = None) -> list["RecordType"]:
        """Register every type in a manifest's ``records:`` block (the adapter
        over the plain manifest dict). For a non-additive change the type's
        ``migrations:`` script (``migrate(db, rdef, old_fields)`` in it, found
        under ``plugin_dir``) or the ``migrate`` callback runs inside the
        transaction."""
        plugin = manifest["plugin"]
        out = []
        for name, block in (manifest.get("records") or {}).items():
            rdef = RecordDef.parse(plugin, name, block or {})
            fn = migrate
            if fn is None and rdef.migrations and plugin_dir is not None:
                fn = _script_migrate(plugin_dir, f"{rdef.migrations}:migrate")
            out.append(self.define(rdef, migrate=fn))
        return out

    def define(self, rdef: RecordDef, *, migrate: Callable | None = None) -> "RecordType":
        with self._tx() as db:
            row = db.execute("SELECT fields, search FROM kit_schema WHERE type=?", (rdef.full,)).fetchone()
            old = json.loads(row["fields"]) if row else {}
            changed = [f for f, spec in rdef.fields.items() if f in old
                       and old[f].get("type", "str") != spec["type"]
                       and {old[f].get("type", "str"), spec["type"]} - _SAME_STORAGE]
            if changed:
                if migrate is None:
                    raise KitError(f"{rdef.full}: changing the type of {', '.join(changed)} "
                                   "is not additive; the plugin needs a migration")
                migrate(db, rdef, old)
            self._create_tables(db, rdef)
            if row is None or json.loads(row["search"]) != rdef.search:
                self._rebuild_fts(db, rdef)
            db.execute("INSERT OR REPLACE INTO kit_schema VALUES (?, ?, ?)",
                       (rdef.full, json.dumps({**old, **rdef.fields}), json.dumps(rdef.search)))
        rtype = RecordType(self, rdef)
        self._types[rdef.full] = rtype
        return rtype

    def unregister(self, plugin: str) -> None:
        """Forget a plugin's types (on unload). Its data stays for a reinstall or rollback."""
        for full in [t for t in self._types if t.startswith(plugin + ".")]:
            del self._types[full]

    def type(self, full: str) -> "RecordType":
        try:
            return self._types[full]
        except KeyError:
            raise KitError(f"no record type {full}") from None

    def types(self, plugin: str | None = None) -> list["RecordType"]:
        return [t for n, t in self._types.items() if plugin is None or n.startswith(plugin + ".")]

    def bind(self, plugin: str, caller: Caller) -> "_Bound":
        """``ctx.records``: this plugin's types with the caller filled in."""
        return _Bound(self, plugin, caller)

    def _create_tables(self, db: sqlite3.Connection, rdef: RecordDef) -> None:
        t = _q("r:" + rdef.full)
        cols = {r["name"] for r in db.execute(f"PRAGMA table_info({t})")}
        if not cols:
            db.execute(f"CREATE TABLE {t} (id TEXT PRIMARY KEY, band TEXT NOT NULL, "
                       "revision INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, "
                       "archived REAL, creator TEXT, person TEXT)")
            db.execute(f"CREATE INDEX {_q('k:' + rdef.full)} ON {t} (band, updated)")
        for f, spec in rdef.fields.items():
            if f not in cols:
                db.execute(f"ALTER TABLE {t} ADD COLUMN {_q(f)} {_AFFINITY.get(spec['type'], 'TEXT')}")
        want = {f"u:{rdef.full}.{f}": ("UNIQUE ", ("band", f))
                for f, spec in rdef.fields.items() if spec.get("unique")}
        want.update({f"i:{rdef.full}.{','.join(cols_)}": ("", cols_) for cols_ in rdef.index})
        have = {r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", ("r:" + rdef.full,))}
        for name in have - set(want):
            if name[:2] in ("u:", "i:"):
                db.execute(f"DROP INDEX {_q(name)}")
        for name, (unique, cols_) in want.items():
            if name not in have:
                try:
                    db.execute(f"CREATE {unique}INDEX {_q(name)} ON {t} ({', '.join(map(_q, cols_))})")
                except sqlite3.IntegrityError:
                    raise KitError(f"{rdef.full}: can't make {cols_[-1]} unique, existing records repeat it") from None
        if rdef.history:
            db.execute(f"CREATE TABLE IF NOT EXISTS {_q('h:' + rdef.full)} (id TEXT, revision INTEGER, "
                       "ts REAL, actor TEXT, op TEXT, data TEXT, PRIMARY KEY (id, revision))")

    def _rebuild_fts(self, db: sqlite3.Connection, rdef: RecordDef) -> None:
        f = _q("f:" + rdef.full)
        db.execute(f"DROP TABLE IF EXISTS {f}")
        if not rdef.search:
            return
        cols = ", ".join(map(_q, rdef.search))
        db.execute(f"CREATE VIRTUAL TABLE {f} USING fts5(id UNINDEXED, {cols})")
        db.execute(f"INSERT INTO {f} (id, {cols}) SELECT id, {cols} FROM {_q('r:' + rdef.full)}")

    # -- cross-type -----------------------------------------------------------

    def backlinks(self, caller: Caller, full: str, rid: str) -> list[dict]:
        """Records (any plugin) whose ref fields point at ``full`` record ``rid``."""
        rows = self._query(
            "SELECT src, src_id, field FROM kit_refs WHERE dst=? AND dst_id=? AND band=? "
            "AND (private_creator IS NULL OR ? OR private_creator IN (?, ?)) ORDER BY src, src_id",
            (full, rid, caller.band, caller.owner, f"p:{caller.person}" if caller.person else "",
             f"a:{caller.actor}"))
        return [{"type": r["src"], "id": r["src_id"], "field": r["field"]} for r in rows]

    def prune(self, now: float | None = None) -> int:
        """Drop records older than their type's ``retain``. Core runs this daily."""
        now = self.clock() if now is None else now
        gone = 0
        for rtype in list(self._types.values()):
            rdef = rtype.rdef
            if rdef.retain is None:
                continue
            with self._tx() as db:
                ids = [r["id"] for r in db.execute(
                    f"SELECT id FROM {_q('r:' + rdef.full)} WHERE updated < ?", (now - rdef.retain,))]
                for rid in ids:
                    rtype._erase(db, rid)
            gone += len(ids)
        return gone


class _Bound:
    """``ctx.records``: ``ctx.records.item`` or ``ctx.records["item"]``."""

    def __init__(self, store: RecordStore, plugin: str, caller: Caller):
        self._store, self._plugin, self._caller = store, plugin, caller

    def __getitem__(self, name: str) -> "_BoundType":
        return _BoundType(self._store.type(f"{self._plugin}.{name}"), self._caller)

    def __getattr__(self, name: str) -> "_BoundType":
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]


class _BoundType:
    def __init__(self, rtype: "RecordType", caller: Caller):
        self._rtype, self._caller = rtype, caller

    def __getattr__(self, verb: str) -> Callable[..., Any]:
        if verb not in VERBS + ("history",):
            raise AttributeError(verb)
        method = getattr(self._rtype, verb)
        return lambda *a, **kw: method(self._caller, *a, **kw)


class RecordType:
    """Operations on one record type. Every method takes the caller first."""

    def __init__(self, store: RecordStore, rdef: RecordDef):
        self.store, self.rdef = store, rdef
        self._t = _q("r:" + rdef.full)

    # -- reads ----------------------------------------------------------------

    def _visible(self, caller: Caller, p: str = "") -> tuple[str, list]:
        sql, params = f"{p}band = ?", [caller.band]
        if self.rdef.private and not caller.owner:
            sql += f" AND ({p}person = ? OR ({p}person IS NULL AND {p}creator = ?))"
            params += [caller.person or "", caller.actor]
        return sql, params

    def _row(self, caller: Caller, rid: str) -> sqlite3.Row:
        vis, params = self._visible(caller)
        rows = self.store._query(f"SELECT * FROM {self._t} WHERE id = ? AND {vis}", [rid, *params])
        if not rows:
            raise KitError(f"no {self.rdef.full} {rid}")
        return rows[0]

    def _out(self, row: sqlite3.Row) -> dict:
        rec = {k: row[k] for k in KIT_COLUMNS if k != "band"}
        for f, spec in self.rdef.fields.items():
            v = row[f]
            if v is not None and spec["type"] in _JSON_TYPES:
                v = json.loads(v)
            elif v is not None and spec["type"] == "bool":
                v = bool(v)
            rec[f] = v
        return rec

    def get(self, caller: Caller, id: str, history: bool = False, backlinks: bool = False) -> dict:
        rec = self._out(self._row(caller, id))
        if history:
            rec["history"] = self.history(caller, id)
        if backlinks:
            rec["backlinks"] = self.store.backlinks(caller, self.rdef.full, id)
        return rec

    def history(self, caller: Caller, id: str) -> list[dict]:
        if not self.rdef.history:
            raise KitError(f"{self.rdef.full} keeps no history")
        self._row(caller, id)
        rows = self.store._query(
            f"SELECT revision, ts, actor, op, data FROM {_q('h:' + self.rdef.full)} "
            "WHERE id = ? ORDER BY revision", (id,))
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def list(self, caller: Caller, where: Mapping | None = None, archived: bool = False,
             limit: int = 50, offset: int = 0, order: str | None = None) -> list[dict]:
        vis, params = self._visible(caller)
        sql = [vis, "archived IS NOT NULL" if archived else "archived IS NULL"]
        for f, v in (where or {}).items():
            self._field(f)
            if v is None:
                sql.append(f"{_q(f)} IS NULL")
            else:
                sql.append(f"{_q(f)} = ?")
                params.append(self._store_value(f, v))
        order = order or "-updated"
        col = order.lstrip("-")
        if col not in ("created", "updated") and col not in self.rdef.fields:
            raise KitError(f"can't order by {col}")
        rows = self.store._query(
            f"SELECT * FROM {self._t} WHERE {' AND '.join(sql)} ORDER BY {_q(col)} "
            f"{'DESC' if order.startswith('-') else 'ASC'}, id LIMIT ? OFFSET ?",
            [*params, max(1, min(int(limit), 500)), max(0, int(offset))])
        return [self._out(r) for r in rows]

    def search(self, caller: Caller, query: str, limit: int = 20, semantic: bool = False) -> list[dict]:
        if semantic:
            if self.store.semantic is None or not self.rdef.semantic:
                raise KitError("semantic search is not available yet")
            ids = self.store.semantic.search(self.rdef.full, query, int(limit))
            out = []
            for rid in ids:
                try:
                    out.append(self.get(caller, rid))
                except KitError:
                    pass
            return out
        if not self.rdef.search:
            raise KitError(f"{self.rdef.full} declares no search fields")
        vis, params = self._visible(caller, "r.")
        ft = _q("f:" + self.rdef.full)
        rows = self.store._query(
            f"SELECT r.* FROM {ft} JOIN {self._t} r ON r.id = {ft}.id "
            f"WHERE {ft} MATCH ? AND {vis} AND r.archived IS NULL ORDER BY bm25({ft}) LIMIT ?",
            [_fts_query(query), *params, max(1, min(int(limit), 200))])
        return [self._out(r) for r in rows]

    # -- writes ---------------------------------------------------------------

    def create(self, caller: Caller, **values: Any) -> dict:
        data = self._clean(values, creating=True)
        now = self.store.clock()
        rid = "r_" + uuid.uuid4().hex[:16]
        with self.store._tx() as db:
            self._check_refs(db, caller, data)
            cols = ["id", "band", "revision", "created", "updated", "creator", "person", *data]
            vals = [rid, caller.band, 1, now, now, caller.actor, caller.person,
                    *(self._store_value(f, v) for f, v in data.items())]
            self._write(db, f"INSERT INTO {self._t} ({', '.join(map(_q, cols))}) "
                            f"VALUES ({', '.join('?' * len(cols))})", vals)
            rec = self._out(db.execute(f"SELECT * FROM {self._t} WHERE id = ?", (rid,)).fetchone())
            self._after_write(db, caller, rec, "create")
        self._event("created", caller, rec)
        return rec

    def update(self, caller: Caller, id: str, revision: int | None = None, **values: Any) -> dict:
        data = self._clean(values, creating=False)
        with self.store._tx() as db:
            row = self._row(caller, id)
            self._check_revision(row, revision)
            self._check_refs(db, caller, data)
            sets = ["revision = revision + 1", "updated = ?", *(f"{_q(f)} = ?" for f in data)]
            self._write(db, f"UPDATE {self._t} SET {', '.join(sets)} WHERE id = ?",
                        [self.store.clock(), *(self._store_value(f, v) for f, v in data.items()), id])
            rec = self._out(db.execute(f"SELECT * FROM {self._t} WHERE id = ?", (id,)).fetchone())
            self._after_write(db, caller, rec, "update")
        self._event("updated", caller, rec)
        return rec

    def archive(self, caller: Caller, id: str, revision: int | None = None, restore: bool = False) -> dict:
        with self.store._tx() as db:
            row = self._row(caller, id)
            self._check_revision(row, revision)
            now = self.store.clock()
            db.execute(f"UPDATE {self._t} SET archived = ?, revision = revision + 1, updated = ? "
                       "WHERE id = ?", (None if restore else now, now, id))
            rec = self._out(db.execute(f"SELECT * FROM {self._t} WHERE id = ?", (id,)).fetchone())
            self._after_write(db, caller, rec, "restore" if restore else "archive")
        self._event("updated" if restore else "archived", caller, rec)
        return rec

    def delete(self, caller: Caller, id: str) -> dict:
        with self.store._tx() as db:
            self._row(caller, id)
            self._erase(db, id)
        return {"id": id, "deleted": True}

    # -- helpers --------------------------------------------------------------

    def _field(self, name: str) -> dict:
        try:
            return self.rdef.fields[name]
        except KeyError:
            raise KitError(f"{self.rdef.full} has no field {name}") from None

    def _clean(self, values: Mapping, creating: bool) -> dict:
        data = {}
        for f, v in values.items():
            spec = self._field(f)
            try:
                check(f, spec, v)
            except ValueError as e:
                raise KitError(f"bad args: {e}") from None
            data[f] = v
        if creating:
            for f, spec in self.rdef.fields.items():
                if data.get(f) is None and "default" in spec:
                    data[f] = spec["default"]
                if spec.get("required") and data.get(f) is None:
                    raise KitError(f"bad args: {f} is required")
        else:
            for f in data:
                if self.rdef.fields[f].get("required") and data[f] is None:
                    raise KitError(f"bad args: {f} is required")
        return data

    def _store_value(self, f: str, v: Any) -> Any:
        if v is None:
            return None
        kind = self.rdef.fields[f]["type"]
        if kind in _JSON_TYPES:
            return json.dumps(v, sort_keys=True)
        if kind == "bool":
            return int(bool(v))
        return v

    def _write(self, db: sqlite3.Connection, sql: str, params: list) -> None:
        try:
            db.execute(sql, params)
        except sqlite3.IntegrityError as e:
            field_ = str(e).rsplit(".", 1)[-1]
            raise KitError(f"{self.rdef.full}: {field_} is already used") from None

    def _check_revision(self, row: sqlite3.Row, revision: int | None) -> None:
        if revision is None:
            if self.rdef.history:
                raise KitError(f"bad args: {self.rdef.full} keeps history; pass the current revision")
            return
        if int(revision) != row["revision"]:
            raise KitError(f"Record changed: {row['id']} is at revision {row['revision']}, "
                           f"not {revision}; get it and re-apply", retry=True)

    def _check_refs(self, db: sqlite3.Connection, caller: Caller, data: Mapping) -> None:
        for f, v in data.items():
            spec = self.rdef.fields[f]
            if spec["type"] != "ref" or v is None or spec["to"] not in self.store._types:
                continue
            target = self.store._types[spec["to"]]
            if db.execute(f"SELECT 1 FROM {target._t} WHERE id = ? AND band = ?",
                          (v, caller.band)).fetchone() is None:
                raise KitError(f"bad args: {f}: no {spec['to']} {v}")

    def _after_write(self, db: sqlite3.Connection, caller: Caller, rec: dict, op: str) -> None:
        """History snapshot, FTS row, ref rows, semantic feed: all inside the write."""
        rdef, rid = self.rdef, rec["id"]
        fields = {f: rec[f] for f in rdef.fields}
        if rdef.history:
            db.execute(f"INSERT INTO {_q('h:' + rdef.full)} VALUES (?, ?, ?, ?, ?, ?)",
                       (rid, rec["revision"], rec["updated"], caller.actor, op,
                        json.dumps(fields, sort_keys=True)))
        if rdef.search:
            ft = _q("f:" + rdef.full)
            db.execute(f"DELETE FROM {ft} WHERE id = ?", (rid,))
            db.execute(f"INSERT INTO {ft} (id, {', '.join(map(_q, rdef.search))}) "
                       f"VALUES (?{', ?' * len(rdef.search)})",
                       [rid, *(self._store_value(f, rec[f]) for f in rdef.search)])
        for f, spec in rdef.fields.items():
            if spec["type"] != "ref":
                continue
            if rec[f] is None:
                db.execute("DELETE FROM kit_refs WHERE src = ? AND src_id = ? AND field = ?",
                           (rdef.full, rid, f))
            else:
                db.execute("INSERT OR REPLACE INTO kit_refs VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (rdef.full, rid, f, spec["to"], rec[f], caller.band,
                            _private_key(rec) if rdef.private else None))
        if self.store.semantic is not None and rdef.semantic:
            self.store.semantic.index(rdef.full, rid, "\n\n".join(str(rec[f] or "") for f in rdef.semantic))

    def _erase(self, db: sqlite3.Connection, rid: str) -> None:
        rdef = self.rdef
        db.execute(f"DELETE FROM {self._t} WHERE id = ?", (rid,))
        if rdef.search:
            db.execute(f"DELETE FROM {_q('f:' + rdef.full)} WHERE id = ?", (rid,))
        if rdef.history:
            db.execute(f"DELETE FROM {_q('h:' + rdef.full)} WHERE id = ?", (rid,))
        db.execute("DELETE FROM kit_refs WHERE src = ? AND src_id = ?", (rdef.full, rid))
        if self.store.semantic is not None and rdef.semantic:
            self.store.semantic.forget(rdef.full, rid)

    def _event(self, what: str, caller: Caller, rec: dict) -> None:
        if self.store._emit is None:
            return
        payload = {"id": rec["id"], "revision": rec["revision"], "by": caller.actor,
                   "band": caller.band}
        if not self.rdef.private:
            payload["record"] = rec
        self.store._emit(f"{self.rdef.full}.{what}", payload, self.rdef.plugin)
