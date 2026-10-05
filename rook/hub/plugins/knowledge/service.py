"""Service layer for shared knowledge and agent work: one ``dispatch`` used by
the hub caps (``knowledge.*``, ``task.*``), the MCP tools generated from them
and the operator's Knowledge page.

Attribution only: every write records the caller's compound identity
(``token.client.host@dir``, see ``rook.band_mcp.attribution``). Nothing here
sits in the band call path or can deny a call. See
docs/DESIGN-agent-work-system.md.

Bands are the operator's existing enrollment bands. Records are addressed by
id or slug; ids are global, so most calls need no ``band``. The deck covers
all bands.
"""
import asyncio
import logging
import time
from .store import KnowledgeStore
from .search import Search

log = logging.getLogger(__name__)

WRITES = ('create', 'update', 'link', 'retract', 'claim', 'release', 'review', 'note', 'batch')
BATCH_OPS = ('create', 'update', 'link', 'retract', 'claim', 'release', 'note')
BATCH_MAX = 50
CLOSED_TASK = ('done', 'closed', 'cancelled', 'archived')

# MCP replies (not the operator's web page) get lean defaults: agents pay for
# every character. ``data.limit`` and ``data.fields`` override them.
MCP_SEARCH_LIMIT = 5
MCP_LIST_LIMIT = 20
MCP_SEARCH_FIELDS = ('id', 'slug', 'kind', 'title', 'state', 'score', 'excerpt')
MCP_LIST_FIELDS = ('id', 'slug', 'kind', 'title', 'state', 'parent', 'excerpt')
EXCERPT = 240
MCP_GET_EVENTS = 10
MCP_OUTCOME_CHARS = 240


def _fields(value):
    if value is None or value == '':
        return None
    if isinstance(value, str):
        return [f.strip() for f in value.split(',') if f.strip()]
    return [str(f) for f in value]


def project(rows, fields, default):
    """Keep ``fields`` (or ``default``) of each row; ``excerpt`` is derived
    from ``body`` when a row has none; ``fields=['all']`` keeps every field."""
    wanted = _fields(fields) or list(default)
    out = []
    for r in rows:
        r = dict(r)
        if 'excerpt' not in r and 'body' in r:
            r['excerpt'] = r['body'][:EXCERPT]
        out.append(r if 'all' in wanted else {k: r[k] for k in wanted if k in r})
    return out


class KnowledgeService:
    def __init__(self, path, principal, enrollment=None, handoffs=None, search=None,
                 handoff_list=None, handoff_close=None):
        """``search``: optional ``callable(store) -> Search`` (the plugin passes
        its configured embedder); default reads the legacy env vars."""
        self.store = KnowledgeStore(path)
        self.principal = principal
        self.enrollment = enrollment
        self.handoffs = handoffs  # callable(author, handoff dict) -> handoff thread_id
        self.handoff_list = handoff_list    # callable() -> active threads (latest handoff each)
        self.handoff_close = handoff_close  # callable(thread_id, author, reason) -> None
        self.search = search(self.store) if search else Search(self.store)
        self.last_maintenance = None
        self.last_error = None
        self._band_cache = (0, [])
        # HygieneEngine (hygiene.py), set by the knowledge plugin; None = off.
        self.hygiene = None

    def _hook(self, name, *args, **kwargs):
        """Run a hygiene hook. Bookkeeping: never fails the caller."""
        engine = self.hygiene
        if engine is None:
            return None
        try:
            return getattr(engine, name)(*args, **kwargs)
        except Exception:
            log.exception('hygiene hook %s failed', name)
            return None

    def auto_link(self, actor, kind, ref, relation='touched', note='', task=None):
        """The store's auto_link plus its hygiene trigger (a handoff on
        in-progress work asks for a state). Returns the task id or None."""
        linked = self.store.auto_link(actor, kind, ref, relation=relation, note=note, task=task)
        if linked:
            self._hook('on_link', actor, linked, kind, ref, relation, True)
        return linked

    def bands(self):
        """The operator's existing bands (all of them, until per-user band
        access lands). Without an enrollment registry there is one: default."""
        if time.monotonic() - self._band_cache[0] < 5:
            return self._band_cache[1]
        if self.enrollment:
            result = [{'id': b['id'], 'name': b['name'], 'label': b['label'],
                       'primary': bool(b['is_primary'])}
                      for b in self.enrollment.bands(active_only=True)]
        else:
            result = [{'id': 'default', 'name': 'Default', 'label': None, 'primary': True}]
        self._band_cache = (time.monotonic(), result)
        return result

    def actor(self):
        """The attributed caller, by compound identity. Unverified callers may
        still write; their records say so."""
        p = self.principal() or {}
        aid = p.get('actor') or ('unverified' if p.get('kind') == 'unverified' or not p else p.get('identity'))
        return {'id': aid or 'unverified', 'kind': p.get('kind') or 'unverified', 'label': aid or 'unverified',
                **{k: p.get(k) for k in ('key_id', 'agent_id', 'token', 'client', 'host', 'dir') if p.get(k)}}

    def band(self, requested=None):
        """Resolve a band by enrollment ID, name or 8-hex label; omitted means
        the primary band (used for new records)."""
        bands = self.bands()
        if requested:
            match = [b for b in bands if requested in (b['id'], b['name'], b['label'])]
        else:
            match = [b for b in bands if b['primary']] or bands[:1]
        if len(match) != 1:
            raise ValueError('Unknown band; rook_knowledge(action="bands") lists them')
        return match[0]['id']

    def _band_for(self, band, rid):
        if band:
            return self.band(band)
        return self.store.locate(rid, [b['id'] for b in self.bands()])

    def _inline_handoff(self, band, actor, rid, data):
        """Save data.handoff via the handoff store and link it to the task."""
        h = data.pop('handoff', None)
        if not h:
            return None
        if not self.handoffs:
            raise ValueError('Handoff store unavailable; save one with rook_handoff_save and link it')
        if not isinstance(h, dict) or not h.get('goal'):
            raise ValueError('data.handoff needs at least {goal, state, next_steps}')
        if not h.get('thread_id'):
            # Continue the task's own thread; a new one per update piles up
            # duplicates nobody can tell apart.
            with self.store.db(False) as db:
                mine = [l for l in self.store._live_links(db, rid) if l['kind'] == 'handoff']
            if mine:
                h = {**h, 'thread_id': mine[-1]['ref']}
        thread = self.handoffs(actor['id'], h)
        return self.store.mutate(band, actor, f'handoff:{thread}:{time.time_ns()}', 'link',
                                 {'id': rid, 'kind': 'handoff', 'ref': thread, 'relation': 'produced',
                                  'note': 'inline handoff'})

    def _deck(self, band, rid, data, fields, lean):
        bands = [b['id'] for b in self.bands()] if not band else [self.band(band)]
        full = data.get('outcome') == 'full'
        deck = self.store.deck(bands, project=rid or data.get('project'),
                               done_days=data.get('done_days', 7), states=data.get('states'),
                               outcome_chars=None if full or not lean else MCP_OUTCOME_CHARS)
        wanted = _fields(fields)
        if wanted and 'all' not in wanted:
            keep = set(wanted) | {'id'}
            for entry in deck:
                for key, items in entry.items():
                    if isinstance(items, list):
                        entry[key] = [{k: v for k, v in i.items() if k in keep} for i in items]
        out = {'deck': deck}
        flags = self._hook('flags') or {}
        if flags:
            for entry in deck:
                if entry['project']['id'] in flags:
                    entry['project']['hygiene'] = flags[entry['project']['id']]
                for items in entry.values():
                    if isinstance(items, list):
                        for item in items:
                            if item.get('id') in flags:
                                item['hygiene'] = flags[item['id']]
        if data.get('handoffs'):
            if not self.handoff_list:
                raise ValueError('Handoff store unavailable; use rook_handoff_list')
            linked = self.store.handoff_tasks(bands)
            out['handoffs'] = [{k: t.get(k) for k in ('thread_id', 'goal', 'author', 'as_of', 'next_steps')}
                               | {'tasks': linked.get(t['thread_id'], [])} for t in self.handoff_list()]
        return out

    def _close_handoffs(self, band, actor, record):
        """A finished task takes its handoff threads with it, unless another
        open task still uses them. Bookkeeping: never fails the update."""
        if not self.handoff_close:
            return []
        try:
            with self.store.db(False) as db:
                threads = {l['ref'] for l in self.store._live_links(db, record['id']) if l['kind'] == 'handoff'}
            still = self.store.handoff_tasks([b['id'] for b in self.bands()])
            closed = []
            for thread in sorted(threads):
                if not any(t['id'] != record['id'] for t in still.get(thread, [])):
                    if self.handoff_close(thread, actor['id'], f"task {record['slug']} is {record['state']}"):
                        closed.append(thread)
            return closed
        except Exception:
            log.exception('closing handoff threads failed')
            return []

    async def _batch(self, band, kind, data, request_id, actor, lean):
        ops = data.get('ops')
        if not isinstance(ops, list) or not ops or len(ops) > BATCH_MAX:
            raise ValueError(f'batch needs data.ops: 1 to {BATCH_MAX} items of {{action, id?, data?}}')
        if not request_id:
            raise ValueError('request_id is required for writes')
        results = []
        for n, op in enumerate(ops):
            try:
                if not isinstance(op, dict) or op.get('action') not in BATCH_OPS:
                    raise ValueError('op.action must be one of ' + ', '.join(BATCH_OPS))
                results.append({'ok': True, 'result': await self.dispatch(
                    op['action'], op.get('band') or band, kind, op.get('id'), '', op.get('data'),
                    f'{request_id}:{n}', actor, lean)})
            except (ValueError, KeyError, TypeError, PermissionError) as error:
                results.append({'ok': False, 'error': str(error), 'code': type(error).__name__,
                                **({'current_revision': error.revision}
                                   if getattr(error, 'revision', None) is not None else {})})
        return {'results': results, 'failed': sum(1 for r in results if not r['ok'])}

    async def dispatch(self, action, band=None, kind=None, rid=None, query='', data=None,
                       request_id=None, actor=None, lean=False):
        """``lean`` (MCP callers): search/list default to fewer rows, excerpts
        and a small field set, overridable with data.limit / data.fields."""
        data = dict(data or {})
        fields = data.pop('fields', None) if action in ('search', 'list', 'deck') else None
        if action == 'bands':
            return self.bands()
        if action == 'deck':
            return self._deck(band, rid, data, fields, lean)
        if action == 'hygiene':
            if self.hygiene is None:
                return {'findings': [], 'enabled': False}
            who = self.actor()['id'] if data.get('mine') else None
            records = None
            if rid:
                b = self._band_for(band, rid)
                records = [self.store.get(b, rid, events=0)['id']]
            return {'findings': self.hygiene.open(records, who, data.get('limit', 50))}
        if action == 'review' and (actor or {}).get('kind') != 'human':
            raise PermissionError('review is for people, from the Knowledge page')
        actor = actor or self.actor()
        if action == 'batch':
            return await self._batch(band, kind, data, request_id, actor, lean)
        if action in ('get', 'update', 'link', 'retract', 'claim', 'release', 'review', 'note') and not rid:
            raise ValueError(f'{action} needs id (a record id or slug; for retract, the link id)')
        if action == 'retract':
            b = self.store.link_band(rid)
        else:
            b = self._band_for(band, rid) if rid else self.band(band)
        if action == 'list':
            if lean:
                data.setdefault('limit', MCP_LIST_LIMIT)
            records = [self.store.brief(r) for r in self.store.list(
                b, kind=kind, **{k: v for k, v in data.items() if k in ('worker', 'parent', 'state', 'limit', 'offset', 'attention')})]
            if lean or fields:
                records = project(records, fields, MCP_LIST_FIELDS if lean else ('all',))
            return {'records': records}
        if action == 'get':
            if not lean:
                got = self.store.get(b, rid)
            else:
                got = self.store.get(b, rid, auto_links=data.get('links') == 'all',
                                     events=data.get('events', MCP_GET_EVENTS))
            found = self._hook('open', [got['id']])
            if found:
                got['hygiene'] = [{k: f[k] for k in ('kind', 'actor', 'text', 'created')} for f in found]
            return got
        if action == 'search':
            found = await self.search.query(b, query, kind, data.get('worker'),
                                            int(data.get('limit', MCP_SEARCH_LIMIT if lean else 20)))
            if lean or fields:
                found['results'] = project(found['results'], fields, MCP_SEARCH_FIELDS if lean else ('all',))
            if lean:
                found = {k: v for k, v in found.items() if v is not None and v is not False}
            return found
        if action == 'context':
            return self.store.context(b, data.get('worker'))
        if action == 'status':
            with self.store.db(False) as db:
                counts = {r['kind']: r['n'] for r in db.execute('SELECT kind,count(*) n FROM records WHERE band=? GROUP BY kind', (b,))}
            return {'band': b, 'counts': counts, 'semantic_configured': self.search.configured,
                    'semantic_error': self.search.last_error, 'last_maintenance': self.last_maintenance,
                    'maintenance_error': self.last_error}
        if action not in WRITES:
            raise ValueError('Actions: bands, deck, hygiene, list, get, search, context, status, '
                             'create, update, link, retract, claim, release, note, batch')
        if action == 'review':
            data = {k: data.get(k) for k in ('revision', 'verdict', 'note')}
        if action in ('update', 'release'):
            record = self.store.get(b, rid)
            self._inline_handoff(b, actor, record['id'], data)
        if rid:
            data['id'] = rid
        if kind and action == 'create':
            data['kind'] = kind
        if action == 'retract':
            data.setdefault('link', rid)
        result = self.store.mutate(b, actor, request_id, action, data)
        if action == 'update' and result.get('kind') == 'task' and result.get('state') in CLOSED_TASK \
                and record['state'] not in CLOSED_TASK:
            closed = self._close_handoffs(b, actor, result)
            if closed:
                result = {**result, 'closed_handoffs': closed}
        self._after_write(action, actor, data, result, record if action in ('update', 'release') else None)
        if action == 'create':
            result = {**result, 'comparable': [
                {'id': r['id'], 'slug': r['slug'], 'title': r['title'], 'kind': r['kind']}
                for r in self.store.lexical(b, result['title'], 5) if r['id'] != result['id']]}
        return result

    def _after_write(self, action, actor, data, result, before):
        """Hygiene triggers for a write that went through."""
        if self.hygiene is None or not isinstance(result, dict):
            return
        if action == 'link':
            self._hook('on_link', actor, result.get('record'), result.get('kind'), result.get('ref'),
                       result.get('relation'))
        elif action == 'update':
            self._hook('on_record_changed', actor, before, result)
        elif action == 'claim':
            self._hook('on_claim', actor.get('id'), result.get('task'))
        elif action == 'release':
            self._hook('on_release', result.get('task'), data.get('actor') or actor.get('id'))
        if action in ('create', 'update', 'link', 'retract'):
            self._hook('after_knowledge_write')

    async def maintain(self):
        """Background embedding indexing. Failures are recorded, never raised."""
        while True:
            try:
                await self.search.index_batch()
                self.last_maintenance = time.time()
                self.last_error = None
            except Exception as error:
                self.last_error = type(error).__name__
                log.exception('knowledge maintenance failed')
            await asyncio.sleep(30)
