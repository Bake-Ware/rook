"""Transactional, band-scoped knowledge and work records. No network or MCP dependency.

The durable, provider-neutral record of what agents know and what work was
done, is being done or is to be done — with who did what. See
docs/DESIGN-agent-work-system.md.

- **Records** (concept → project → task, plus knowledge) are wiki pages: each
  has a unique per-band ``slug`` and a markdown body that can reference others
  with ``[[slug]]``; backlinks are computed on read.
- **Links** are an append-only audit trail tying a record to artifacts
  (journal calls, consoles, handoffs, files, commits, agents, other records).
- **Claims** record which agent is on a task and when it was last active.
  They never prevent anyone from working.
- **Saving rules** validate records only (never execution): done needs an
  outcome and evidence; stopping unfinished work needs a handoff; verifying a
  fact needs a traceable evidence link.

``band`` is the existing enrollment band ID. Nothing here authorizes or gates
band calls.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

KINDS = ('concept', 'project', 'task', 'knowledge')
STATES = {
    'task': ('todo', 'in_progress', 'blocked', 'paused', 'done', 'cancelled', 'archived'),
    'project': ('active', 'paused', 'done', 'archived'),
    'concept': ('active', 'paused', 'done', 'archived'),
    'knowledge': ('active', 'superseded', 'archived'),
}
INITIAL = {'task': 'todo', 'project': 'active', 'concept': 'active', 'knowledge': 'active'}
OPEN_TASK = ('todo', 'in_progress', 'blocked', 'paused')
VERIFICATION = ('unverified', 'verified', 'disputed')
KNOWLEDGE_KINDS = ('fact', 'decision', 'procedure', 'observation', 'question', 'summary')
LINK_KINDS = ('journal', 'console', 'handoff', 'chat', 'file', 'commit', 'agent', 'record', 'url')
RELATIONS = ('produced', 'evidence', 'touched', 'discussed_in', 'blocked_by', 'duplicates',
             'supersedes', 'mentions', 'source')
# A URL is a pointer, not something Rook can trace; it can't verify a fact.
TRACEABLE = tuple(k for k in LINK_KINDS if k != 'url')
SLUG = re.compile(r'^[a-z0-9][a-z0-9-]{0,79}$')
WIKI = re.compile(r'\[\[([a-z0-9][a-z0-9-]{0,79})\]\]')
SCHEMA = 2


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


def slugify(title):
    s = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:80].strip('-')
    return s or 'page'


def actor_id(actor):
    if not actor or not actor.get('id'):
        raise PermissionError('An attributed identity is required')
    return actor['id']


class KnowledgeStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > SCHEMA:
                raise RuntimeError('Knowledge database is newer than this release')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS actors(id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS records(
                id TEXT PRIMARY KEY, band TEXT NOT NULL, kind TEXT NOT NULL,
                parent TEXT REFERENCES records(id), title TEXT NOT NULL, body TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'active', revision INTEGER NOT NULL DEFAULT 1,
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
            CREATE TABLE IF NOT EXISTS links(
                id TEXT PRIMARY KEY, band TEXT NOT NULL, record TEXT NOT NULL REFERENCES records(id),
                kind TEXT NOT NULL, ref TEXT NOT NULL, relation TEXT NOT NULL, note TEXT NOT NULL,
                actor TEXT NOT NULL, ts REAL NOT NULL, auto INTEGER NOT NULL DEFAULT 0,
                retracts TEXT);
            CREATE INDEX IF NOT EXISTS links_record ON links(record,ts);
            CREATE INDEX IF NOT EXISTS links_ref ON links(kind,ref);
            CREATE TABLE IF NOT EXISTS claims(
                id TEXT PRIMARY KEY, band TEXT NOT NULL, task TEXT NOT NULL REFERENCES records(id),
                actor TEXT NOT NULL, host TEXT, client TEXT, dir TEXT, provider_session TEXT,
                started REAL NOT NULL, last_active REAL NOT NULL, released REAL,
                nudged REAL, dirty REAL);
            CREATE INDEX IF NOT EXISTS claims_active ON claims(actor,released,started);
            CREATE INDEX IF NOT EXISTS claims_task ON claims(task,released);
            ''')
            cols = {r[1] for r in db.execute('PRAGMA table_info(records)')}
            if 'slug' not in cols:
                db.execute('ALTER TABLE records ADD COLUMN slug TEXT')
            if 'info' not in {r[1] for r in db.execute('PRAGMA table_info(actors)')}:
                db.execute('ALTER TABLE actors ADD COLUMN info TEXT')
            for row in db.execute('SELECT id,band,title FROM records WHERE slug IS NULL').fetchall():
                db.execute('UPDATE records SET slug=? WHERE id=?', (self._free_slug(db, row['band'], slugify(row['title'])), row['id']))
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS records_slug ON records(band,slug)')
            if version == 1:  # prototype state names → current ones
                for kind, mapping in (('task', {'proposed': 'todo', 'ready': 'todo', 'running': 'in_progress',
                                                'interrupted': 'paused', 'completed': 'done'}),
                                      ('%', {'proposed': 'active', 'ready': 'active', 'completed': 'done'})):
                    for old, new in mapping.items():
                        db.execute('UPDATE records SET state=? WHERE state=? AND kind LIKE ?', (new, old, kind))
            # The 349e3eb prototype's execution-gate tables; never used again.
            for table in ('operations', 'attempts', 'approvals'):
                db.execute('DROP TABLE IF EXISTS ' + table)
            db.execute(f'PRAGMA user_version={SCHEMA}')
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

    # -- lookup ---------------------------------------------------------------

    def _get(self, db, band, rid):
        row = db.execute('SELECT * FROM records WHERE band=? AND (id=? OR slug=?)', (band, rid, rid)).fetchone()
        if row is None:
            raise ValueError(f'Record {rid!r} not found in this band')
        return self.record(row)

    def locate(self, rid, bands):
        """The band holding record id/slug ``rid`` among ``bands``. IDs are
        global; a slug used in several bands needs an explicit band."""
        with self.db(False) as db:
            marks = ','.join('?' * len(bands))
            rows = db.execute(f'SELECT band FROM records WHERE (id=? OR slug=?) AND band IN ({marks})',
                              (rid, rid, *bands)).fetchall()
        if not rows:
            raise ValueError(f'Record {rid!r} not found')
        if len(rows) > 1:
            raise ValueError(f'{rid!r} exists in several bands; pass band=')
        return rows[0]['band']

    def link_band(self, link_id):
        with self.db(False) as db:
            row = db.execute('SELECT band FROM links WHERE id=?', (link_id,)).fetchone()
        if not row:
            raise ValueError('Link not found')
        return row['band']

    @staticmethod
    def _free_slug(db, band, base):
        slug, n = base, 2
        while db.execute('SELECT 1 FROM records WHERE band=? AND slug=?', (band, slug)).fetchone():
            suffix = f'-{n}'
            slug, n = base[:80 - len(suffix)] + suffix, n + 1
        return slug

    def _event(self, db, band, rid, actor, action, data):
        aid = actor_id(actor)
        info = {k: actor.get(k) for k in ('key_id', 'agent_id', 'token', 'client', 'host', 'dir') if actor.get(k)}
        db.execute('INSERT INTO actors(id,kind,label,updated,info) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                   'label=excluded.label,updated=excluded.updated,info=excluded.info',
                   (aid, actor.get('kind', 'agent'), actor.get('label', aid), time.time(), packed(info)))
        db.execute('INSERT INTO events(band,record,actor,action,ts,data) VALUES(?,?,?,?,?,?)',
                   (band, rid, aid, action, time.time(), packed(data)))

    def mutate(self, band, actor, request, operation, data):
        """One idempotent write; receipts cannot be reused with different payloads."""
        aid = actor_id(actor)
        request = text(request or '', 160)
        if not request:
            raise ValueError('request_id is required for writes')
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

    # -- records ----------------------------------------------------------------

    def _validate(self, db, band, kind, parent, attrs, rid=None):
        if kind in ('project', 'task'):
            if not parent:
                raise ValueError('A project needs a parent concept; a task needs a parent project or task')
            p = self._get(db, band, parent)
            expected = ('concept',) if kind == 'project' else ('project', 'task')
            if p['kind'] not in expected or p['id'] == rid:
                raise ValueError('A project needs a parent concept; a task needs a parent project or task')
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
        for key in ('workers', 'dependencies', 'criteria', 'supersedes', 'tags'):
            attrs[key] = strings(attrs.get(key, []))
        for dep in attrs['dependencies']:
            d = self._get(db, band, dep)
            if d['kind'] != 'task' or d['id'] == rid:
                raise ValueError('Dependencies must be other tasks')
        if kind == 'knowledge':
            attrs['verification'] = attrs.get('verification', 'unverified')
            if attrs['verification'] not in VERIFICATION:
                raise ValueError('verification must be unverified, verified or disputed')
            attrs['knowledge_kind'] = attrs.get('knowledge_kind', 'observation')
            if attrs['knowledge_kind'] not in KNOWLEDGE_KINDS:
                raise ValueError('knowledge_kind must be one of ' + ', '.join(KNOWLEDGE_KINDS))
        for key in ('outcome', 'blocked_reason'):
            if key in attrs:
                attrs[key] = text(attrs[key], 8000)
        if len(packed(attrs)) > 30000:
            raise ValueError('Metadata is too large')

    def _op_create(self, db, band, actor, data):
        kind = data.get('kind')
        if kind not in KINDS:
            raise ValueError('kind must be concept, project, task or knowledge')
        title = text(data.get('title', ''), 240)
        if not title:
            raise ValueError('Title is required')
        body = text(data.get('body', ''))
        attrs = dict(data.get('attrs') or {})
        parent = data.get('parent') or None
        if parent:
            parent = self._get(db, band, parent)['id']
        self._validate(db, band, kind, parent, attrs)
        if attrs.get('verification') == 'verified':
            raise ValueError('Create the fact unverified, link traceable evidence, then mark it verified')
        slug = data.get('slug')
        if slug is not None:
            if not isinstance(slug, str) or not SLUG.match(slug):
                raise ValueError('slug must be lowercase letters, digits and dashes (max 80)')
            if db.execute('SELECT 1 FROM records WHERE band=? AND slug=?', (band, slug)).fetchone():
                raise Conflict(f'slug {slug!r} is taken in this band')
        else:
            slug = self._free_slug(db, band, slugify(title))
        rid = kind[:1] + '_' + uuid.uuid4().hex
        now = time.time()
        db.execute('INSERT INTO records(id,band,kind,parent,title,body,state,attrs,created,updated,creator,slug) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (rid, band, kind, parent, title, body, INITIAL[kind], packed(attrs), now, now, actor_id(actor), slug))
        for old_id in attrs['supersedes']:
            old = self._get(db, band, old_id)
            if kind != 'knowledge' or old['kind'] != 'knowledge':
                raise ValueError('Supersession is for knowledge pages')
            db.execute("UPDATE records SET state='superseded',revision=revision+1,updated=? WHERE id=?", (now, old['id']))
            self._link(db, band, rid, actor, 'record', old['id'], 'supersedes', '')
            self._event(db, band, old['id'], actor, 'superseded', {'by': rid})
        r = self._get(db, band, rid)
        self._event(db, band, rid, actor, 'created', {k: r[k] for k in ('kind', 'title', 'slug', 'parent', 'state')})
        return r

    def _op_update(self, db, band, actor, data):
        r = self._get(db, band, data['id'])
        if data.get('revision') != r['revision']:
            raise Conflict('Record changed; get its current revision before updating')
        patch = data.get('patch') or {}
        if not isinstance(patch, dict) or set(patch) - {'title', 'body', 'state', 'attrs'}:
            raise ValueError('patch may contain title, body, state and attrs')
        title = text(patch.get('title', r['title']), 240)
        body = text(patch.get('body', r['body']))
        if not title:
            raise ValueError('Title is required')
        attrs = {**r['attrs'], **(patch.get('attrs') or {})}
        if attrs.get('supersedes') != r['attrs'].get('supersedes'):
            raise ValueError('To supersede, create a new knowledge page with attrs.supersedes')
        self._validate(db, band, r['kind'], r['parent'], attrs, r['id'])
        state = patch.get('state', r['state'])
        if state not in STATES[r['kind']]:
            raise ValueError(f"{r['kind']} state must be one of " + ', '.join(STATES[r['kind']]))
        live = self._live_links(db, r['id'])
        if r['kind'] == 'knowledge' and attrs.get('verification') == 'verified' and r['attrs'].get('verification') != 'verified':
            if not any(l['relation'] == 'evidence' and l['kind'] in TRACEABLE for l in live):
                raise ValueError('Verifying a fact needs at least one evidence link with a traceable id '
                                 '(journal, console, handoff, chat, file, commit, agent or record; a URL is not enough)')
        if r['kind'] == 'task' and state != r['state']:
            self._task_transition(db, r, state, attrs, live)
        db.execute('UPDATE records SET title=?,body=?,attrs=?,state=?,revision=revision+1,updated=? WHERE id=?',
                   (title, body, packed(attrs), state, time.time(), r['id']))
        if r['kind'] == 'task' and state in ('done', 'cancelled', 'archived', 'paused', 'blocked', 'todo'):
            db.execute('UPDATE claims SET released=? WHERE task=? AND released IS NULL', (time.time(), r['id']))
        updated = self._get(db, band, r['id'])
        changed = {k: updated[k] for k in ('title', 'state') if updated[k] != r[k]}
        if body != r['body']:
            changed['body'] = True
        if attrs != r['attrs']:
            changed['attrs'] = sorted(k for k in attrs if attrs.get(k) != r['attrs'].get(k))
        self._event(db, band, r['id'], actor, 'updated', changed)
        return updated

    def _task_transition(self, db, r, state, attrs, live):
        if state == 'done':
            if not attrs.get('outcome'):
                raise ValueError('Marking a task done needs attrs.outcome (what happened)')
            if not any(l['relation'] == 'evidence' for l in live):
                raise ValueError('Marking a task done needs at least one evidence link (rook_task action=link)')
        if state == 'cancelled' and not attrs.get('outcome'):
            raise ValueError('Cancelling a task needs attrs.outcome (why)')
        if state == 'blocked' and not (attrs.get('blocked_reason') or any(l['relation'] == 'blocked_by' for l in live)):
            raise ValueError('Blocking a task needs attrs.blocked_reason or a blocked_by link')
        if r['state'] == 'in_progress' and state in ('paused', 'blocked', 'todo'):
            since = db.execute('SELECT max(started) FROM claims WHERE task=?', (r['id'],)).fetchone()[0] or r['created']
            if not any(l['kind'] == 'handoff' and l['ts'] >= since for l in live):
                raise ValueError('Stopping in-progress work needs a handoff: pass data.handoff '
                                 '{goal,state,next_steps} or link a rook_handoff_save thread first')

    # -- links ------------------------------------------------------------------

    def _link(self, db, band, rid, actor, kind, ref, relation, note, auto=False, retracts=None):
        lid = 'l_' + uuid.uuid4().hex
        db.execute('INSERT INTO links VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (lid, band, rid, kind, ref, relation, note, actor_id(actor), time.time(), int(auto), retracts))
        return lid

    @staticmethod
    def _live_links(db, rid):
        rows = [dict(l) for l in db.execute('SELECT * FROM links WHERE record=? ORDER BY ts', (rid,))]
        gone = {l['retracts'] for l in rows if l['retracts']}
        return [l for l in rows if not l['retracts'] and l['id'] not in gone]

    def _op_link(self, db, band, actor, data):
        r = self._get(db, band, data['id'])
        kind, relation = data.get('kind'), data.get('relation', 'evidence')
        if kind not in LINK_KINDS:
            raise ValueError('kind must be one of ' + ', '.join(LINK_KINDS))
        if relation not in RELATIONS:
            raise ValueError('relation must be one of ' + ', '.join(RELATIONS))
        ref = text(str(data.get('ref', '')), 500)
        if not ref:
            raise ValueError('ref is required: the id, path or URL being linked')
        if kind == 'record':
            ref = self._get(db, band, ref)['id']
        note = text(data.get('note', ''), 2000)
        lid = self._link(db, band, r['id'], actor, kind, ref, relation, note)
        self._event(db, band, r['id'], actor, 'linked', {'link': lid, 'kind': kind, 'ref': ref, 'relation': relation})
        return {'id': lid, 'record': r['id'], 'kind': kind, 'ref': ref, 'relation': relation}

    def _op_retract(self, db, band, actor, data):
        link = db.execute('SELECT * FROM links WHERE id=? AND band=?', (data.get('link'), band)).fetchone()
        if not link or link['retracts']:
            raise ValueError('Link not found')
        reason = text(data.get('note', ''), 2000)
        lid = self._link(db, band, link['record'], actor, link['kind'], link['ref'], link['relation'], reason, retracts=link['id'])
        self._event(db, band, link['record'], actor, 'link_retracted', {'link': link['id'], 'note': reason})
        return {'retracted': link['id'], 'by': lid}

    # -- claims -----------------------------------------------------------------

    def _op_claim(self, db, band, actor, data):
        r = self._get(db, band, data['id'])
        if r['kind'] != 'task':
            raise ValueError('Only tasks can be claimed')
        if r['state'] in ('done', 'cancelled', 'archived'):
            raise Conflict(f"Task is {r['state']}; reopen it (state todo) before claiming")
        aid = actor_id(actor)
        now = time.time()
        mine = db.execute('SELECT id FROM claims WHERE task=? AND actor=? AND released IS NULL', (r['id'], aid)).fetchone()
        if mine:
            db.execute('UPDATE claims SET last_active=?,provider_session=coalesce(?,provider_session) WHERE id=?',
                       (now, data.get('provider_session'), mine['id']))
            cid = mine['id']
        else:
            cid = 'cl_' + uuid.uuid4().hex
            db.execute('INSERT INTO claims(id,band,task,actor,host,client,dir,provider_session,started,last_active) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (cid, band, r['id'], aid, actor.get('host'), actor.get('client'), actor.get('dir'),
                        text(str(data.get('provider_session') or ''), 200) or None, now, now))
            self._event(db, band, r['id'], actor, 'claimed', {'claim': cid})
        if r['state'] != 'in_progress':
            db.execute("UPDATE records SET state='in_progress',revision=revision+1,updated=? WHERE id=?", (now, r['id']))
            self._event(db, band, r['id'], actor, 'updated', {'state': 'in_progress'})
        others = [dict(c) for c in db.execute('SELECT actor,started,last_active FROM claims WHERE task=? AND released IS NULL AND actor<>?', (r['id'], aid))]
        return {'claim': cid, 'task': r['id'], 'state': 'in_progress', 'also_claimed_by': others}

    def _op_release(self, db, band, actor, data):
        r = self._get(db, band, data['id'])
        aid = actor_id(actor)
        mine = db.execute('SELECT * FROM claims WHERE task=? AND actor=? AND released IS NULL', (r['id'], aid)).fetchone()
        if not mine:
            raise ValueError('You have no active claim on this task')
        others = db.execute('SELECT count(*) FROM claims WHERE task=? AND released IS NULL AND actor<>?', (r['id'], aid)).fetchone()[0]
        if not others and r['state'] == 'in_progress':
            live = self._live_links(db, r['id'])
            if not any(l['kind'] == 'handoff' and l['ts'] >= mine['started'] for l in live):
                raise ValueError('Releasing the last claim on in-progress work needs a handoff: pass data.handoff '
                                 '{goal,state,next_steps} or link a rook_handoff_save thread first')
            db.execute("UPDATE records SET state='paused',revision=revision+1,updated=? WHERE id=?", (time.time(), r['id']))
            self._event(db, band, r['id'], actor, 'updated', {'state': 'paused'})
        db.execute('UPDATE claims SET released=? WHERE id=?', (time.time(), mine['id']))
        self._event(db, band, r['id'], actor, 'released', {'claim': mine['id']})
        return {'released': mine['id'], 'task': r['id']}

    def auto_link(self, actor, kind, ref, relation='touched', note=''):
        """Attach an artifact to the actor's most recently claimed open task.
        Called by the hub on the actor's behalf; returns the task id or None."""
        aid = actor.get('id')
        if not aid:
            return None
        with self.db() as db:
            claim = db.execute('SELECT * FROM claims WHERE actor=? AND released IS NULL ORDER BY started DESC LIMIT 1',
                               (aid,)).fetchone()
            if not claim:
                return None
            self._link(db, claim['band'], claim['task'], actor, kind, str(ref)[:500], relation, note, auto=True)
            db.execute('UPDATE claims SET last_active=?,dirty=NULL WHERE id=?', (time.time(), claim['id']))
            return claim['task']

    def active_claims(self):
        with self.db(False) as db:
            return [dict(c) for c in db.execute(
                "SELECT c.*, r.title, r.slug, r.state FROM claims c JOIN records r ON r.id=c.task "
                "WHERE c.released IS NULL AND r.state='in_progress'")]

    def mark_claim(self, claim_id, *, nudged=None, dirty=None, actor=None, note=None, data=None):
        with self.db() as db:
            c = db.execute('SELECT * FROM claims WHERE id=?', (claim_id,)).fetchone()
            if not c:
                return
            if nudged is not None:
                db.execute('UPDATE claims SET nudged=? WHERE id=?', (nudged, claim_id))
            if dirty is not None:
                db.execute('UPDATE claims SET dirty=? WHERE id=?', (dirty, claim_id))
            if actor and note:
                self._event(db, c['band'], c['task'], actor, note, data or {})

    # -- reads ------------------------------------------------------------------

    def get(self, band, rid):
        with self.db(False) as db:
            r = self._get(db, band, rid)
            rid = r['id']
            r['events'] = [{**dict(e), 'data': json.loads(e['data'])} for e in db.execute(
                'SELECT seq,actor,action,ts,data FROM events WHERE band=? AND record=? ORDER BY seq DESC LIMIT 100', (band, rid))]
            r['children'] = [self.brief(self.record(e)) for e in db.execute(
                'SELECT * FROM records WHERE band=? AND parent=? ORDER BY created LIMIT 200', (band, rid))]
            r['links'] = self._live_links(db, rid)
            r['claims'] = [dict(c) for c in db.execute('SELECT * FROM claims WHERE task=? ORDER BY started DESC LIMIT 20', (rid,))]
            r['mentions'] = sorted(set(WIKI.findall(r['body'])))
            back = db.execute('SELECT * FROM records WHERE band=? AND id<>? AND (body LIKE ? OR id IN '
                              "(SELECT record FROM links WHERE kind='record' AND ref=? AND retracts IS NULL))",
                              (band, rid, '%[[' + r['slug'] + ']]%', rid)).fetchall()
            r['backlinks'] = [self.brief(self.record(b)) for b in back]
            if r['state'] == 'superseded':
                sup = db.execute("SELECT r.id,r.slug,r.title FROM links l JOIN records r ON r.id=l.record "
                                 "WHERE l.kind='record' AND l.relation='supersedes' AND l.ref=?", (rid,)).fetchone()
                r['superseded_by'] = dict(sup) if sup else None
            return r

    @staticmethod
    def brief(r):
        return {k: r.get(k) for k in ('id', 'slug', 'kind', 'title', 'state', 'updated', 'creator')} | {
            'excerpt': r['body'][:240],
            **({'verification': r['attrs'].get('verification', 'unverified')} if r['kind'] == 'knowledge' else {})}

    def list(self, band, kind=None, worker=None, parent=None, state=None, limit=50, offset=0, attention=False):
        clauses, params = ['band=?'], [band]
        for field, value in [('kind', kind), ('parent', parent), ('state', state)]:
            if value:
                clauses.append(field + '=?'); params.append(value)
        if attention:
            clauses.append("state IN ('blocked','paused') OR (kind='knowledge' AND json_extract(attrs,'$.verification')='disputed')")
        if worker:
            clauses.append("EXISTS(SELECT 1 FROM json_each(records.attrs,'$.workers') WHERE value=?)"); params.append(worker)
        with self.db(False) as db:
            return [self.record(r) for r in db.execute('SELECT * FROM records WHERE ' + ' AND '.join(clauses) + ' ORDER BY updated DESC LIMIT ? OFFSET ?', params + [max(1, min(int(limit), 200)), max(0, int(offset))])]

    def lexical(self, band, query, limit=30):
        terms = re.findall(r'\w+', text(query, 1000))[:20]
        if not terms:
            return self.list(band, limit=limit)
        match = ' OR '.join('"' + t + '"' for t in terms)
        with self.db(False) as db:
            return [self.record(r) for r in db.execute("SELECT r.* FROM records_fts f JOIN records r ON r.id=f.id WHERE records_fts MATCH ? AND r.band=? AND r.state NOT IN ('archived','superseded') ORDER BY bm25(records_fts) LIMIT ?", (match, band, min(limit, 100)))]

    def context(self, band, worker=None):
        with self.db(False) as db:
            where, params = 'band=?', [band]
            if worker:
                where += " AND EXISTS(SELECT 1 FROM json_each(records.attrs,'$.workers') WHERE value=?)"
                params.append(worker)
            tasks = [self.record(r) for r in db.execute("SELECT * FROM records WHERE " + where + " AND kind='task' AND state IN ('in_progress','blocked','paused','todo') ORDER BY CASE state WHEN 'in_progress' THEN 0 WHEN 'blocked' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END,updated DESC LIMIT 5", params)]
            recent = [self.record(r) for r in db.execute("SELECT * FROM records WHERE " + where + " AND kind='knowledge' AND state='active' ORDER BY updated DESC LIMIT 5", params)]
        return {'band': band, 'worker': worker, 'open_tasks': [self.brief(r) for r in tasks],
                'recent_knowledge': [self.brief(r) for r in recent], 'generated_at': time.time()}

    def deck(self, bands, project=None, done_days=7):
        """What's on deck, per project, across ``bands``: in progress (with
        claimants and latest handoff), blocked, paused, todo, recently done."""
        since = time.time() - done_days * 86400
        out = []
        with self.db(False) as db:
            marks = ','.join('?' * len(bands))
            projects = db.execute(f"SELECT * FROM records WHERE kind='project' AND band IN ({marks}) AND state<>'archived'"
                                  + (' AND (id=? OR slug=?)' if project else '') + ' ORDER BY updated DESC',
                                  (*bands, *((project, project) if project else ()))).fetchall()
            for p in projects:
                tasks, todo = [], [p['id']]
                while todo:
                    kids = db.execute("SELECT * FROM records WHERE parent=? AND kind='task'", (todo.pop(),)).fetchall()
                    tasks += kids
                    todo += [k['id'] for k in kids]
                entry = {'band': p['band'], 'project': self.brief(self.record(p)),
                         'in_progress': [], 'blocked': [], 'paused': [], 'todo': [], 'recently_done': []}
                for t in sorted(tasks, key=lambda t: -t['updated']):
                    rec = self.record(t)
                    item = self.brief(rec)
                    if t['state'] == 'in_progress':
                        item['claimants'] = [dict(c) for c in db.execute(
                            'SELECT actor,started,last_active,dirty FROM claims WHERE task=? AND released IS NULL', (t['id'],))]
                        item['needs_hygiene'] = any(c['dirty'] for c in item['claimants'])
                    if t['state'] in ('in_progress', 'paused', 'blocked'):
                        h = db.execute("SELECT ref,ts FROM links WHERE record=? AND kind='handoff' AND retracts IS NULL "
                                       "ORDER BY ts DESC LIMIT 1", (t['id'],)).fetchone()
                        item['latest_handoff'] = dict(h) if h else None
                    if t['state'] == 'blocked':
                        item['blocked_reason'] = rec['attrs'].get('blocked_reason')
                    if t['state'] in ('done', 'cancelled'):
                        if t['updated'] >= since:
                            item['outcome'] = rec['attrs'].get('outcome')
                            entry['recently_done'].append(item)
                    elif t['state'] in entry:
                        entry[t['state']].append(item)
                out.append(entry)
        return out

    def cursor(self, name, value=None):
        with self.db(value is not None) as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO cursors VALUES(?,?)', (name, str(value)))
            row = db.execute('SELECT value FROM cursors WHERE name=?', (name,)).fetchone()
            return row['value'] if row else None
