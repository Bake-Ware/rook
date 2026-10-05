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

from ....core import migrations

KINDS = ('concept', 'project', 'task', 'knowledge')
STATES = {
    'task': ('todo', 'in_progress', 'blocked', 'paused', 'done', 'closed', 'cancelled', 'archived'),
    'project': ('active', 'paused', 'done', 'archived'),
    'concept': ('active', 'paused', 'done', 'archived'),
    'knowledge': ('active', 'superseded', 'archived'),
}
INITIAL = {'task': 'todo', 'project': 'active', 'concept': 'active', 'knowledge': 'active'}
OPEN_TASK = ('todo', 'in_progress', 'blocked', 'paused')
VERIFICATION = ('unverified', 'verified', 'disputed')
KNOWLEDGE_KINDS = ('fact', 'decision', 'procedure', 'observation', 'question', 'summary')
LINK_KINDS = ('journal', 'console', 'handoff', 'chat', 'file', 'commit', 'agent', 'record', 'secret', 'url', 'human')
RELATIONS = ('produced', 'evidence', 'touched', 'discussed_in', 'blocked_by', 'duplicates',
             'supersedes', 'mentions', 'source', 'closed_by')
REVIEW_ATTRS = {'reviewed_by', 'reviewed_label', 'reviewed_at', 'reviewed_revision', 'review_note', 'dispute_reason'}
# A URL is a pointer, not something Rook can trace; it can't verify a fact.
TRACEABLE = tuple(k for k in LINK_KINDS if k != 'url')
SLUG = re.compile(r'^[a-z0-9][a-z0-9-]{0,79}$')
WIKI = re.compile(r'\[\[([a-z0-9][a-z0-9-]{0,79})\]\]')
SCHEMA = 2
#: Plugin migrations (rook.core.migrations), recorded under namespace 'knowledge'.
MIGRATIONS = Path(__file__).resolve().parent / 'migrations'
NAMESPACE = 'knowledge'


# A claim idle longer than this no longer collects auto-links, and another
# actor may release it (docs/DESIGN-agent-work-system.md, grooming).
STALE_CLAIM_SECS = 2 * 3600
CASCADE_STATES = ('paused', 'archived')


class Conflict(ValueError):
    def __init__(self, message, revision=None):
        super().__init__(message)
        self.revision = revision


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
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > SCHEMA:
                raise RuntimeError('Knowledge database is newer than this release')
            if any(v == 1 for v, _ in migrations.pending(db, MIGRATIONS, NAMESPACE)):
                self._upgrade_legacy(db, version)
            migrations.apply(db, MIGRATIONS, NAMESPACE)
            # Kept at layout 2 so an older release (rollback) can still open
            # the file; a later migration that changes the layout bumps it.
            db.execute(f'PRAGMA user_version={SCHEMA}')
            db.commit()
        finally:
            db.close()
        os.chmod(self.path, 0o600)

    def _upgrade_legacy(self, db, version):
        """Bring a database written by the pre-plugin store (user_version 0-2,
        no ``_rook_migrations`` rows) up to layout 2, in one transaction, so
        migration 001 (all IF NOT EXISTS) only records the baseline. The steps
        need Python (slug backfill), so they can't live in a .sql file. A new
        database has no tables and skips everything here."""
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'records' not in tables:
            return
        if db.in_transaction:
            db.commit()
        db.execute('BEGIN IMMEDIATE')
        try:
            if 'slug' not in {r[1] for r in db.execute('PRAGMA table_info(records)')}:
                db.execute('ALTER TABLE records ADD COLUMN slug TEXT')
            if 'actors' in tables and 'info' not in {r[1] for r in db.execute('PRAGMA table_info(actors)')}:
                db.execute('ALTER TABLE actors ADD COLUMN info TEXT')
            for row in db.execute('SELECT id,band,title FROM records WHERE slug IS NULL').fetchall():
                db.execute('UPDATE records SET slug=? WHERE id=?', (self._free_slug(db, row['band'], slugify(row['title'])), row['id']))
            if version == 1:  # prototype state names → current ones
                for kind, mapping in (('task', {'proposed': 'todo', 'ready': 'todo', 'running': 'in_progress',
                                                'interrupted': 'paused', 'completed': 'done'}),
                                      ('%', {'proposed': 'active', 'ready': 'active', 'completed': 'done'})):
                    for old, new in mapping.items():
                        db.execute('UPDATE records SET state=? WHERE state=? AND kind LIKE ?', (new, old, kind))
            # The 349e3eb prototype's execution-gate tables; never used again.
            for table in ('operations', 'attempts', 'approvals'):
                db.execute('DROP TABLE IF EXISTS ' + table)
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise

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
            p = self._get(db, band, parent)
            if kind == 'knowledge':
                # Pages nest under pages, like folders; no loops.
                seen = {rid}
                while p:
                    if p['kind'] != 'knowledge':
                        raise ValueError('A knowledge page can only be nested under another knowledge page')
                    if p['id'] in seen:
                        raise Conflict('Parent cycle: a page cannot be nested under itself or its own subpages')
                    seen.add(p['id'])
                    p = self._get(db, band, p['parent']) if p['parent'] else None
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
        if set(attrs) & REVIEW_ATTRS:
            raise ValueError('Review fields are set by a person reviewing the page')
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
            raise Conflict(f"Record changed; its current revision is {r['revision']}", r['revision'])
        patch = data.get('patch') or {}
        if not isinstance(patch, dict) or set(patch) - {'title', 'body', 'state', 'attrs', 'parent'}:
            raise ValueError('patch may contain title, body, state, attrs and parent')
        parent = r['parent']
        if 'parent' in patch:
            if r['kind'] != 'knowledge':
                raise ValueError('Only knowledge pages can be moved (patch.parent)')
            parent = self._get(db, band, patch['parent'])['id'] if patch['parent'] else None
        title = text(patch.get('title', r['title']), 240)
        body = text(patch.get('body', r['body']))
        if not title:
            raise ValueError('Title is required')
        if set(patch.get('attrs') or {}) & REVIEW_ATTRS:
            raise ValueError('Review fields are set by a person reviewing the page, not by update')
        if 'closed_by' in (patch.get('attrs') or {}):
            raise ValueError('attrs.closed_by is written by closing a task: state closed + data.closed_by')
        attrs = {**r['attrs'], **(patch.get('attrs') or {})}
        if attrs.get('supersedes') != r['attrs'].get('supersedes'):
            raise ValueError('To supersede, create a new knowledge page with attrs.supersedes')
        self._validate(db, band, r['kind'], parent, attrs, r['id'])
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
            if state == 'closed':
                attrs['closed_by'] = self._closed_by(db, band, r, actor, data.get('closed_by'))
            else:
                attrs.pop('closed_by', None)
        elif data.get('closed_by'):
            raise ValueError('data.closed_by goes with patch.state closed')
        # A person verified what the page said then. If someone else (an agent)
        # changes what it says, that verification no longer covers it.
        reverted = (r['kind'] == 'knowledge' and attrs.get('verification') == 'verified'
                    and r['attrs'].get('reviewed_by') and actor.get('kind') != 'human'
                    and (title != r['title'] or body != r['body']))
        if reverted:
            attrs['verification'] = 'unverified'
            attrs['review_note'] = 'Edited after ' + r['attrs'].get('reviewed_label', r['attrs']['reviewed_by']) + ' verified it; needs another look'
        db.execute('UPDATE records SET title=?,body=?,attrs=?,state=?,parent=?,revision=revision+1,updated=? WHERE id=?',
                   (title, body, packed(attrs), state, parent, time.time(), r['id']))
        if r['kind'] == 'task' and state in ('done', 'closed', 'cancelled', 'archived', 'paused', 'blocked', 'todo'):
            db.execute('UPDATE claims SET released=? WHERE task=? AND released IS NULL', (time.time(), r['id']))
        updated = self._get(db, band, r['id'])
        changed = {k: updated[k] for k in ('title', 'state', 'parent') if updated[k] != r[k]}
        if body != r['body']:
            changed['body'] = True
        if attrs != r['attrs']:
            changed['attrs'] = sorted(k for k in attrs if attrs.get(k) != r['attrs'].get(k))
        self._event(db, band, r['id'], actor, 'updated', changed)
        if data.get('cascade'):
            if r['kind'] != 'project' or state not in CASCADE_STATES:
                raise ValueError('cascade applies to a project going to ' + ' or '.join(CASCADE_STATES))
            updated = {**updated, **self._cascade(db, band, actor, r['id'], state)}
        return updated

    def _cascade(self, db, band, actor, project, state):
        """Give a project's open tasks its new state. Work in progress is left
        alone: stopping it needs a handoff, which only its own update can carry."""
        done, skipped, todo, now = [], [], [project], time.time()
        while todo:
            for t in db.execute("SELECT * FROM records WHERE parent=? AND kind='task'", (todo.pop(),)).fetchall():
                todo.append(t['id'])
                if t['state'] == 'in_progress':
                    skipped.append({'id': t['id'], 'slug': t['slug'], 'reason': 'in progress; stop it with a handoff'})
                elif t['state'] in OPEN_TASK and t['state'] != state:
                    db.execute('UPDATE records SET state=?,revision=revision+1,updated=? WHERE id=?', (state, now, t['id']))
                    db.execute('UPDATE claims SET released=? WHERE task=? AND released IS NULL', (now, t['id']))
                    self._event(db, band, t['id'], actor, 'updated', {'state': state, 'cascade': project})
                    done.append(t['id'])
        return {'cascaded': done, 'cascade_skipped': skipped}

    def _op_note(self, db, band, actor, data):
        """An append-only remark on a record (shown with its events); needs no
        revision. ``evidence`` links are added with the note as their note."""
        r = self._get(db, band, data['id'])
        note = text(data.get('text', ''), 4000)
        if not note:
            raise ValueError('note needs data.text')
        links = []
        for e in (data.get('evidence') or [])[:10]:
            if not isinstance(e, dict) or e.get('kind') not in LINK_KINDS or e.get('kind') == 'human' or not e.get('ref'):
                raise ValueError('evidence items are {kind, ref}; kind as for link')
            ref = self._get(db, band, e['ref'])['id'] if e['kind'] == 'record' else text(str(e['ref']), 500)
            links.append(self._link(db, band, r['id'], actor, e['kind'], ref, 'evidence', note[:2000]))
        self._event(db, band, r['id'], actor, 'note', {'text': note, **({'links': links} if links else {})})
        seq = db.execute('SELECT max(seq) FROM events WHERE record=?', (r['id'],)).fetchone()[0]
        return {'id': r['id'], 'note': seq, 'links': links}

    def _op_review(self, db, band, actor, data):
        """A person's verdict on a knowledge page: verified, disputed or back to
        unverified. Their sign-off is the evidence (a 'human' link), so this is
        for people only; agents verify by linking evidence and updating."""
        if actor.get('kind') != 'human':
            raise PermissionError('Only a signed-in person can review a page; agents link evidence and update instead')
        r = self._get(db, band, data['id'])
        if r['kind'] != 'knowledge':
            raise ValueError('Only knowledge pages are reviewed')
        if data.get('revision') != r['revision']:
            raise Conflict('The page changed while you were reading it; reload and review again')
        verdict = data.get('verdict')
        if verdict not in VERIFICATION:
            raise ValueError('verdict must be verified, disputed or unverified')
        note = text(data.get('note', '') or '', 2000)
        if verdict == 'disputed' and not note:
            raise ValueError("Say what's wrong so an agent can fix it")
        aid = actor_id(actor)
        attrs = {k: v for k, v in r['attrs'].items() if k not in ('review_note', 'dispute_reason')}
        attrs['verification'] = verdict
        if verdict == 'unverified':
            for k in ('reviewed_by', 'reviewed_label', 'reviewed_at', 'reviewed_revision'):
                attrs.pop(k, None)
        else:
            attrs.update(reviewed_by=aid, reviewed_label=actor.get('label') or aid,
                         reviewed_at=time.time(), reviewed_revision=r['revision'] + 1)
        if verdict == 'disputed':
            attrs['dispute_reason'] = note
        if verdict == 'verified':
            self._link(db, band, r['id'], actor, 'human', aid, 'evidence',
                       note or 'Verified by ' + (actor.get('label') or aid))
        db.execute('UPDATE records SET attrs=?,revision=revision+1,updated=? WHERE id=?',
                   (packed(attrs), time.time(), r['id']))
        self._event(db, band, r['id'], actor, 'reviewed', {'verdict': verdict, **({'note': note} if note else {})})
        return self._get(db, band, r['id'])

    def _closed_by(self, db, band, r, actor, said):
        """Closing a task on a person's word, kept apart from ``done`` (which
        needs evidence of the work): who said so, their words, and the session
        they said it in, recorded on the task and as a ``closed_by`` link."""
        said = said if isinstance(said, dict) else {}
        who, quote, session = (text(str(said.get(k) or ''), n) for k, n in (('who', 120), ('quote', 1000), ('session', 500)))
        if not (who and quote and session):
            raise ValueError('Closing a task needs data.closed_by {who, quote, session}: the person who said to '
                             'close it, their words, and the session (URL or id) they said it in. '
                             'For finished work with evidence use state done')
        kind = 'url' if session.startswith(('http://', 'https://')) else 'agent'
        lid = self._link(db, band, r['id'], actor, kind, session, 'closed_by', f'{who}: "{quote}"')
        self._event(db, band, r['id'], actor, 'linked', {'link': lid, 'kind': kind, 'ref': session, 'relation': 'closed_by'})
        return {'who': who, 'quote': quote, 'session': session, 'recorded_by': actor_id(actor), 'at': time.time()}

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
        if kind not in LINK_KINDS or kind == 'human':
            raise ValueError('kind must be one of ' + ', '.join(k for k in LINK_KINDS if k != 'human')
                             + " ('human' links come only from a person's review)")
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
        if r['state'] in ('done', 'closed', 'cancelled', 'archived'):
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
        me = actor_id(actor)
        aid = text(str(data.get('actor') or ''), 200) or me
        mine = db.execute('SELECT * FROM claims WHERE task=? AND actor=? AND released IS NULL', (r['id'], aid)).fetchone()
        if not mine:
            raise ValueError('You have no active claim on this task' if aid == me
                             else f'{aid} has no active claim on this task')
        if aid != me and time.time() - mine['last_active'] < STALE_CLAIM_SECS:
            raise ValueError(f"That claim was active {int((time.time() - mine['last_active']) // 60)} min ago; "
                             f"another actor's claim can be released after {STALE_CLAIM_SECS // 3600} h idle")
        others = db.execute('SELECT count(*) FROM claims WHERE task=? AND released IS NULL AND actor<>?', (r['id'], aid)).fetchone()[0]
        if not others and r['state'] == 'in_progress':
            live = self._live_links(db, r['id'])
            if not any(l['kind'] == 'handoff' and l['ts'] >= mine['started'] for l in live):
                raise ValueError('Releasing the last claim on in-progress work needs a handoff: pass data.handoff '
                                 '{goal,state,next_steps} or link a rook_handoff_save thread first')
            db.execute("UPDATE records SET state='paused',revision=revision+1,updated=? WHERE id=?", (time.time(), r['id']))
            self._event(db, band, r['id'], actor, 'updated', {'state': 'paused'})
        db.execute('UPDATE claims SET released=? WHERE id=?', (time.time(), mine['id']))
        self._event(db, band, r['id'], actor, 'released',
                    {'claim': mine['id'], **({'of': aid, 'stale': True} if aid != me else {})})
        return {'released': mine['id'], 'task': r['id']}

    def auto_link(self, actor, kind, ref, relation='touched', note='', task=None, band=None):
        """Attach an artifact to the actor's most recently claimed open task,
        or to ``task`` (id or slug) when the caller names one. A claim idle
        longer than STALE_CLAIM_SECS is skipped: the actor has moved on, and
        its later work belongs to something else. ``band`` limits the claim
        lookup to one band. Called by the hub on the actor's behalf; returns
        the task id or None."""
        aid = actor.get('id')
        if not aid:
            return None
        with self.db() as db:
            if task:
                row = db.execute("SELECT * FROM records WHERE (id=? OR slug=?) AND kind='task'", (task, task)).fetchall()
                if len(row) != 1:
                    raise ValueError(f'task {task!r} not found' if not row else f'task slug {task!r} is in several bands; use its id')
                self._link(db, row[0]['band'], row[0]['id'], actor, kind, str(ref)[:500], relation, note, auto=True)
                db.execute('UPDATE claims SET last_active=?,dirty=NULL WHERE task=? AND actor=? AND released IS NULL',
                           (time.time(), row[0]['id'], aid))
                return row[0]['id']
            claim = db.execute('SELECT * FROM claims WHERE actor=? AND released IS NULL AND last_active>=? '
                               + ('AND band=? ' if band else '') + 'ORDER BY started DESC LIMIT 1',
                               (aid, time.time() - STALE_CLAIM_SECS, *([band] if band else []))).fetchone()
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

    def _dependencies(self, db, band, attrs):
        """A task's dependencies with their states, and whether all are done."""
        deps = []  # only done counts: closed on someone's word is not proof the work exists
        for dep in attrs.get('dependencies') or []:
            d = db.execute('SELECT id,slug,state FROM records WHERE band=? AND (id=? OR slug=?)', (band, dep, dep)).fetchone()
            deps.append(dict(d) if d else {'id': dep, 'slug': None, 'state': 'missing'})
        return deps, all(d['state'] == 'done' for d in deps)

    def get(self, band, rid, auto_links=True, events=100):
        """``auto_links=False`` returns only the links someone made by hand,
        with ``auto_links`` = a count of the automatic ones per kind."""
        with self.db(False) as db:
            r = self._get(db, band, rid)
            rid = r['id']
            r['events'] = [{**dict(e), 'data': json.loads(e['data'])} for e in db.execute(
                'SELECT seq,actor,action,ts,data FROM events WHERE band=? AND record=? ORDER BY seq DESC LIMIT ?',
                (band, rid, max(0, min(int(events), 1000))))]
            r['children'] = [self.brief(self.record(e)) for e in db.execute(
                'SELECT * FROM records WHERE band=? AND parent=? ORDER BY created LIMIT 200', (band, rid))]
            r['links'] = self._live_links(db, rid)
            if not auto_links:
                counts = {}
                for l in r['links']:
                    if l['auto']:
                        counts[l['kind']] = counts.get(l['kind'], 0) + 1
                r['links'] = [l for l in r['links'] if not l['auto']]
                r['auto_links'] = counts
            if r['kind'] == 'task' and r['attrs'].get('dependencies'):
                r['dependencies'], r['unblocked'] = self._dependencies(db, band, r['attrs'])
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
        return {k: r.get(k) for k in ('id', 'slug', 'kind', 'title', 'state', 'revision', 'updated', 'creator', 'parent', 'band')} | {
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

    def deck(self, bands, project=None, done_days=7, states=None, outcome_chars=None):
        """What's on deck, per project, across ``bands``: in progress (with
        claimants and latest handoff), blocked, paused, todo, recently done.
        ``states`` keeps only those lists (``done`` = recently done);
        ``done_days=0`` drops finished work; ``outcome_chars`` shortens outcomes."""
        since = time.time() - float(done_days) * 86400
        wanted = {'recently_done' if s == 'done' else s for s in states} if states else None
        lists = ('in_progress', 'blocked', 'paused', 'todo', 'recently_done')
        if wanted is not None and wanted - set(lists):
            raise ValueError('states are in_progress, blocked, paused, todo and done')
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
                         **{k: [] for k in lists if wanted is None or k in wanted}}
                for t in sorted(tasks, key=lambda t: -t['updated']):
                    key = 'recently_done' if t['state'] in ('done', 'closed', 'cancelled') else t['state']
                    if key not in entry or (key == 'recently_done' and t['updated'] < since):
                        continue
                    rec = self.record(t)
                    item = self.brief(rec)
                    if t['state'] == 'in_progress':
                        item['claimants'] = [dict(c) for c in db.execute(
                            'SELECT actor,started,last_active,dirty FROM claims WHERE task=? AND released IS NULL', (t['id'],))]
                        item['needs_hygiene'] = any(c['dirty'] for c in item['claimants'])
                    if t['state'] in ('in_progress', 'paused', 'blocked'):
                        handoffs = [l for l in self._live_links(db, t['id']) if l['kind'] == 'handoff']
                        item['latest_handoff'] = {'ref': handoffs[-1]['ref'], 'ts': handoffs[-1]['ts']} if handoffs else None
                        if item.get('needs_hygiene'):
                            # Why: who went quiet, since when, and the handoff they last left.
                            item['hygiene'] = [{'actor': c['actor'], 'idle_since': c['last_active'], 'marked': c['dirty'],
                                                'last_handoff': item['latest_handoff'] and item['latest_handoff']['ts']}
                                               for c in item['claimants'] if c['dirty']]
                    if t['state'] in OPEN_TASK and rec['attrs'].get('dependencies'):
                        item['dependencies'], item['unblocked'] = self._dependencies(db, t['band'], rec['attrs'])
                    if t['state'] == 'blocked':
                        item['blocked_reason'] = rec['attrs'].get('blocked_reason')
                    if key == 'recently_done':
                        outcome = rec['attrs'].get('outcome')
                        if outcome_chars and outcome and len(outcome) > outcome_chars:
                            outcome = outcome[:outcome_chars] + '…'
                        item['outcome'] = outcome
                        if rec['attrs'].get('closed_by'):
                            item['closed_by'] = {k: rec['attrs']['closed_by'].get(k) for k in ('who', 'quote', 'session')}
                    entry[key].append(item)
                out.append(entry)
        return out

    def handoff_tasks(self, bands):
        """Handoff thread id -> the open tasks linked to it (id, slug, state)."""
        out = {}
        with self.db(False) as db:
            marks = ','.join('?' * len(bands))
            rows = db.execute(f"SELECT l.id lid,l.ref,l.retracts,r.id,r.slug,r.state FROM links l JOIN records r "
                              f"ON r.id=l.record WHERE l.kind='handoff' AND r.kind='task' AND r.band IN ({marks})",
                              tuple(bands)).fetchall()
            gone = {r['retracts'] for r in rows if r['retracts']}
            for r in rows:
                if r['retracts'] or r['lid'] in gone or r['state'] not in OPEN_TASK:
                    continue
                tasks = out.setdefault(r['ref'], [])
                if all(t['id'] != r['id'] for t in tasks):
                    tasks.append({'id': r['id'], 'slug': r['slug'], 'state': r['state']})
        return out

    def cursor(self, name, value=None):
        with self.db(value is not None) as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO cursors VALUES(?,?)', (name, str(value)))
            row = db.execute('SELECT value FROM cursors WHERE name=?', (name,)).fetchone()
            return row['value'] if row else None
