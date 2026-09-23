"""Transactional, band-scoped knowledge and work records. No network or MCP dependency.

Bookkeeping only: records who created and changed each concept, project, task
or knowledge item. Nothing here authorizes, gates or tracks band execution.
``band`` is the existing enrollment band ID (stable across PSK rotation).
Databases written by the 349e3eb prototype keep their approvals/attempts/
operations tables; this module never reads or writes them.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

KINDS = {'concept', 'project', 'task', 'knowledge'}
STATES = {'proposed', 'ready', 'running', 'blocked', 'interrupted', 'completed', 'cancelled', 'archived', 'superseded'}
SCOPE_FIELDS = {'title', 'body', 'parent', 'criteria', 'workers', 'dependencies'}


class Conflict(ValueError):
    pass


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def text(value, limit=20000):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'Expected text of at most {limit} characters')
    return value.strip()


def strings(value, limit=100):
    if not isinstance(value, list) or len(value) > limit or any(not isinstance(s, str) or len(s) > 1000 for s in value):
        raise ValueError('Expected a bounded list of strings')
    return list(dict.fromkeys(value))


def actor_id(actor):
    if not actor or not actor.get('id'):
        raise PermissionError('A named agent or human identity is required')
    return actor['id']


class KnowledgeStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise RuntimeError('Knowledge database is newer than this release')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS actors(id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS records(
                id TEXT PRIMARY KEY, band TEXT NOT NULL, kind TEXT NOT NULL,
                parent TEXT REFERENCES records(id), title TEXT NOT NULL, body TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'proposed', revision INTEGER NOT NULL DEFAULT 1,
                scope_revision INTEGER NOT NULL DEFAULT 1, attrs TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, creator TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS records_band_kind ON records(band,kind,state,updated);
            CREATE INDEX IF NOT EXISTS records_parent ON records(parent);
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY, band TEXT NOT NULL, record TEXT NOT NULL,
                actor TEXT NOT NULL, action TEXT NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_record ON events(band,record,seq);
            CREATE TABLE IF NOT EXISTS receipts(
                band TEXT NOT NULL, actor TEXT NOT NULL, request TEXT NOT NULL,
                digest TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(band,actor,request));
            CREATE TABLE IF NOT EXISTS embeddings(
                record TEXT PRIMARY KEY REFERENCES records(id), revision INTEGER NOT NULL,
                model TEXT NOT NULL, vector TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cursors(name TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(id UNINDEXED,title,body);
            CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
                INSERT INTO records_fts(id,title,body) VALUES(new.id,new.title,new.body); END;
            CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN
                DELETE FROM records_fts WHERE id=old.id;
                INSERT INTO records_fts(id,title,body) VALUES(new.id,new.title,new.body); END;
            PRAGMA user_version=1;
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self, write=True):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        try:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def record(row):
        result = dict(row)
        result['attrs'] = json.loads(result['attrs'])
        return result

    def _get(self, db, band, rid):
        row = db.execute('SELECT * FROM records WHERE id=? AND band=?', (rid, band)).fetchone()
        if row is None:
            raise ValueError('Record not found in this band')
        return self.record(row)

    def _event(self, db, band, rid, actor, action, data):
        aid = actor_id(actor)
        db.execute('INSERT INTO actors VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,updated=excluded.updated',
                   (aid, actor.get('kind', 'agent'), actor.get('label', aid), time.time()))
        db.execute('INSERT INTO events(band,record,actor,action,ts,data) VALUES(?,?,?,?,?,?)',
                   (band, rid, aid, action, time.time(), packed(data)))

    def mutate(self, band, actor, request, operation, data):
        """One idempotent write; receipts cannot be reused with different payloads."""
        aid = actor_id(actor)
        request = text(request, 160)
        if not request:
            raise ValueError('request_id is required for mutations')
        signature = hashlib.sha256(packed([operation, data]).encode()).hexdigest()
        with self.db() as db:
            old = db.execute('SELECT * FROM receipts WHERE band=? AND actor=? AND request=?', (band, aid, request)).fetchone()
            if old:
                if old['digest'] != signature:
                    raise Conflict('request_id was already used for different input')
                return json.loads(old['response'])
            handler = getattr(self, '_op_' + operation, None)
            if handler is None:
                raise ValueError('Unknown operation')
            result = handler(db, band, actor, data)
            db.execute('INSERT INTO receipts VALUES(?,?,?,?,?)', (band, aid, request, signature, packed(result)))
            return result

    def _validate_links(self, db, band, kind, parent, attrs, rid=None):
        if kind in ('project', 'task'):
            p = self._get(db, band, parent)
            expected = ('concept',) if kind == 'project' else ('project', 'task')
            if p['kind'] not in expected or parent == rid:
                raise ValueError('Project needs a concept; task needs a project or parent task')
            seen = {rid}
            while p:
                if p['id'] in seen:
                    raise Conflict('Parent cycle')
                seen.add(p['id'])
                p = self._get(db, band, p['parent']) if p['parent'] else None
        elif parent:
            if kind == 'concept':
                raise ValueError('Concepts cannot have a parent')
            self._get(db, band, parent)
        for key in ('workers', 'dependencies', 'related', 'supersedes', 'evidence', 'sources', 'criteria'):
            attrs[key] = strings(attrs.get(key, []))
        for target in attrs['related'] + attrs['supersedes']:
            if target == rid:
                raise ValueError('A record cannot link to itself')
            self._get(db, band, target)
        visited = set()
        def visit(dep):
            if dep == rid:
                raise Conflict('Dependency cycle')
            if dep in visited:
                return
            visited.add(dep)
            if len(visited) > 1000:
                raise ValueError('Dependency graph too large')
            r = self._get(db, band, dep)
            if r['kind'] != 'task':
                raise ValueError('Dependencies must be tasks')
            for child in r['attrs'].get('dependencies', []):
                visit(child)
        for dep in attrs['dependencies']:
            visit(dep)
        attrs['verification'] = attrs.get('verification', 'unverified')
        if attrs['verification'] not in ('unverified', 'observed', 'verified', 'disputed'):
            raise ValueError('Invalid verification state')
        attrs['knowledge_kind'] = text(attrs.get('knowledge_kind', 'observation'), 40)
        if attrs['knowledge_kind'] not in ('fact', 'decision', 'procedure', 'observation', 'question', 'summary'):
            raise ValueError('Invalid knowledge kind')
        if len(packed(attrs)) > 30000:
            raise ValueError('Metadata is too large')

    def _op_create(self, db, band, actor, data):
        kind = data.get('kind')
        if kind not in KINDS:
            raise ValueError('kind must be concept, project, task, or knowledge')
        title = text(data.get('title', ''), 240)
        if not title:
            raise ValueError('Title is required')
        body = text(data.get('body', ''))
        attrs = dict(data.get('attrs', {}))
        parent = data.get('parent') or None
        self._validate_links(db, band, kind, parent, attrs)
        rid = kind[:1] + '_' + uuid.uuid4().hex
        now = time.time()
        db.execute('INSERT INTO records(id,band,kind,parent,title,body,attrs,created,updated,creator) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (rid, band, kind, parent, title, body, packed(attrs), now, now, actor_id(actor)))
        for superseded in attrs['supersedes']:
            old = self._get(db, band, superseded)
            if kind != 'knowledge' or old['kind'] != 'knowledge':
                raise ValueError('Supersession is for knowledge records')
            db.execute("UPDATE records SET state='superseded',revision=revision+1,updated=? WHERE id=?", (now, superseded))
            self._event(db, band, superseded, actor, 'superseded', {'by': rid})
        r = self._get(db, band, rid)
        self._event(db, band, rid, actor, 'created', r)
        return r

    def _op_update(self, db, band, actor, data):
        r = self._get(db, band, data['id'])
        if data.get('revision') != r['revision']:
            raise Conflict('Record changed; read its current revision before updating')
        patch = data.get('patch', {})
        if not isinstance(patch, dict) or set(patch) - {'title', 'body', 'state', 'attrs'}:
            raise ValueError('Unsupported update fields')
        title = text(patch.get('title', r['title']), 240)
        body = text(patch.get('body', r['body']))
        if not title:
            raise ValueError('Title is required')
        attrs = {**r['attrs'], **patch.get('attrs', {})}
        # Supersession is append-only through a new knowledge record.
        if attrs.get('supersedes') != r['attrs'].get('supersedes'):
            raise ValueError('Create a new knowledge record to supersede another')
        self._validate_links(db, band, r['kind'], r['parent'], attrs, r['id'])
        state = patch.get('state', r['state'])
        if state not in STATES:
            raise ValueError('Invalid state')
        if state == 'completed' and r['kind'] == 'project':
            if db.execute("SELECT 1 FROM records WHERE parent=? AND kind='task' AND state NOT IN ('completed','cancelled','archived')", (r['id'],)).fetchone():
                raise Conflict('Project still has unfinished tasks')
            if not attrs.get('evidence'):
                raise ValueError('Completion requires evidence')
        scope_changed = title != r['title'] or body != r['body'] or any(attrs.get(k) != r['attrs'].get(k) for k in ('criteria', 'workers', 'dependencies'))
        db.execute('UPDATE records SET title=?,body=?,attrs=?,state=?,revision=revision+1,scope_revision=scope_revision+?,updated=? WHERE id=?',
                   (title, body, packed(attrs), state, int(scope_changed), time.time(), r['id']))
        updated = self._get(db, band, r['id'])
        self._event(db, band, r['id'], actor, 'updated', updated)
        return updated

    def get(self, band, rid):
        with self.db(False) as db:
            r = self._get(db, band, rid)
            r['events'] = [{**dict(e), 'data': json.loads(e['data'])} for e in db.execute('SELECT * FROM events WHERE band=? AND record=? ORDER BY seq DESC LIMIT 100', (band, rid))]
            r['children'] = [self.record(e) for e in db.execute('SELECT * FROM records WHERE band=? AND parent=? ORDER BY created LIMIT 200', (band, rid))]
            return r

    def list(self, band, kind=None, worker=None, parent=None, state=None, limit=50, offset=0, attention=False):
        clauses, params = ['band=?'], [band]
        for field, value in [('kind', kind), ('parent', parent), ('state', state)]:
            if value:
                clauses.append(field + '=?'); params.append(value)
        if attention:
            clauses.append("state IN ('proposed','blocked','interrupted')")
        if worker:
            clauses.append("EXISTS(SELECT 1 FROM json_each(records.attrs,'$.workers') WHERE value=?)"); params.append(worker)
        with self.db(False) as db:
            return [self.record(r) for r in db.execute('SELECT * FROM records WHERE ' + ' AND '.join(clauses) + ' ORDER BY updated DESC LIMIT ? OFFSET ?', params + [max(1, min(int(limit), 200)), max(0, int(offset))])]

    def lexical(self, band, query, limit=30):
        import re
        terms = re.findall(r'\w+', text(query, 1000))[:20]
        if not terms:
            return self.list(band, limit=limit)
        match = ' OR '.join('"' + t + '"' for t in terms)
        with self.db(False) as db:
            return [self.record(r) for r in db.execute("SELECT r.* FROM records_fts f JOIN records r ON r.id=f.id WHERE records_fts MATCH ? AND r.band=? AND r.state NOT IN ('archived','superseded') ORDER BY bm25(records_fts) LIMIT ?", (match, band, min(limit, 100)))]

    def context(self, band, worker=None):
        with self.db(False) as db:
            where = 'band=?'
            params = [band]
            if worker:
                where += " AND EXISTS(SELECT 1 FROM json_each(records.attrs,'$.workers') WHERE value=?)"
                params.append(worker)
            tasks = [self.record(r) for r in db.execute("SELECT * FROM records WHERE " + where + " AND kind='task' AND state NOT IN ('completed','cancelled','archived','superseded') ORDER BY CASE state WHEN 'interrupted' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END,updated DESC LIMIT 5", params)]
            recent = [self.record(r) for r in db.execute("SELECT * FROM records WHERE " + where + " AND (kind<>'task' OR state='completed') AND state NOT IN ('archived','superseded') ORDER BY updated DESC LIMIT 5", params)]
        def brief(r):
            return {k: r[k] for k in ('id', 'kind', 'title', 'state', 'updated')} | {'excerpt': r['body'][:240], 'verification': r['attrs'].get('verification', 'unverified')}
        return {'band': band, 'worker': worker, 'open_tasks': [brief(r) for r in tasks[:5]], 'recent': [brief(r) for r in recent[:5]], 'generated_at': time.time()}

    def cursor(self, name, value=None):
        with self.db(value is not None) as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO cursors VALUES(?,?)', (name, str(value)))
            row = db.execute('SELECT value FROM cursors WHERE name=?', (name,)).fetchone()
            return row['value'] if row else None
