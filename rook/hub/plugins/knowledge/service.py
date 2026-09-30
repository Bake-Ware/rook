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

WRITES = ('create', 'update', 'link', 'retract', 'claim', 'release', 'review')

# MCP replies (not the operator's web page) get lean defaults: agents pay for
# every character. ``data.limit`` and ``data.fields`` override them.
MCP_SEARCH_LIMIT = 5
MCP_LIST_LIMIT = 20
MCP_SEARCH_FIELDS = ('id', 'slug', 'kind', 'title', 'state', 'score', 'excerpt')
MCP_LIST_FIELDS = ('id', 'slug', 'kind', 'title', 'state', 'parent', 'excerpt')
EXCERPT = 240


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
    def __init__(self, path, principal, enrollment=None, handoffs=None, search=None):
        """``search``: optional ``callable(store) -> Search`` (the plugin passes
        its configured embedder); default reads the legacy env vars."""
        self.store = KnowledgeStore(path)
        self.principal = principal
        self.enrollment = enrollment
        self.handoffs = handoffs  # callable(author, handoff dict) -> handoff thread_id
        self.search = search(self.store) if search else Search(self.store)
        self.last_maintenance = None
        self.last_error = None
        self._band_cache = (0, [])

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
        thread = self.handoffs(actor['id'], h)
        return self.store.mutate(band, actor, f'handoff:{thread}', 'link',
                                 {'id': rid, 'kind': 'handoff', 'ref': thread, 'relation': 'produced',
                                  'note': 'inline handoff'})

    async def dispatch(self, action, band=None, kind=None, rid=None, query='', data=None,
                       request_id=None, actor=None, lean=False):
        """``lean`` (MCP callers): search/list default to fewer rows, excerpts
        and a small field set, overridable with data.limit / data.fields."""
        data = dict(data or {})
        fields = data.pop('fields', None) if action in ('search', 'list') else None
        if action == 'bands':
            return self.bands()
        if action == 'deck':
            return {'deck': self.store.deck([b['id'] for b in self.bands()] if not band else [self.band(band)],
                                            project=rid or data.get('project'))}
        if action == 'review' and (actor or {}).get('kind') != 'human':
            raise PermissionError('review is for people, from the Knowledge page')
        actor = actor or self.actor()
        if action in ('get', 'update', 'link', 'retract', 'claim', 'release', 'review') and not rid:
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
            return self.store.get(b, rid)
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
            raise ValueError('Actions: bands, deck, list, get, search, context, status, '
                             'create, update, link, retract, claim, release')
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
        if action == 'create':
            result = {**result, 'comparable': [
                {'id': r['id'], 'slug': r['slug'], 'title': r['title'], 'kind': r['kind']}
                for r in self.store.lexical(b, result['title'], 5) if r['id'] != result['id']]}
        return result

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
