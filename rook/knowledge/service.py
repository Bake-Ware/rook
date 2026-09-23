"""MCP tools and human API adapter for shared knowledge and agent work.

Attribution only: every write records the caller's compound identity
(``token.client.host@dir``, see ``rook.band_mcp.attribution``). Nothing here
sits in the band call path or can deny a call. See
docs/DESIGN-agent-work-system.md.

Bands are the operator's existing enrollment bands. Records are addressed by
id or slug; ids are global, so most calls need no ``band``. The deck covers
all bands.
"""
import asyncio
import json
import logging
import time
from .store import KnowledgeStore, LINK_KINDS, RELATIONS
from .search import Search

log = logging.getLogger(__name__)

WRITES = ('create', 'update', 'link', 'retract', 'claim', 'release', 'review')


class KnowledgeService:
    def __init__(self, path, principal, enrollment=None, handoffs=None):
        self.store = KnowledgeStore(path)
        self.principal = principal
        self.enrollment = enrollment
        self.handoffs = handoffs  # callable(author, handoff dict) -> handoff thread_id
        self.search = Search(self.store)
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
                       request_id=None, actor=None):
        data = dict(data or {})
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
            return {'records': [self.store.brief(r) for r in self.store.list(
                b, kind=kind, **{k: v for k, v in data.items() if k in ('worker', 'parent', 'state', 'limit', 'offset', 'attention')})]}
        if action == 'get':
            return self.store.get(b, rid)
        if action == 'search':
            return await self.search.query(b, query, kind, data.get('worker'), int(data.get('limit', 20)))
        if action == 'context':
            return self.store.context(b, data.get('worker'))
        if action == 'status':
            with self.store.db(False) as db:
                counts = {r['kind']: r['n'] for r in db.execute('SELECT kind,count(*) n FROM records WHERE band=? GROUP BY kind', (b,))}
            return {'band': b, 'counts': counts, 'semantic_configured': bool(self.search.url),
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

    def register(self, mcp):
        async def invoke(action, band, kind, rid, query, data, request_id):
            try:
                return json.dumps({'ok': True, 'result': await self.dispatch(action, band, kind, rid, query, data, request_id)})
            except (ValueError, KeyError, TypeError, PermissionError) as error:
                return json.dumps({'ok': False, 'error': str(error), 'code': type(error).__name__})

        links_help = ('link: data {kind, ref, relation?, note?} — kind ' + '|'.join(LINK_KINDS)
                      + '; relation ' + '|'.join(RELATIONS) + ' (default evidence). retract: id=<link id>.')

        @mcp.tool()
        async def rook_knowledge(action: str = 'search', band: str | None = None, id: str | None = None,
                                 query: str = '', data: dict | None = None, request_id: str | None = None) -> str:
            """Shared memory as a wiki: search/get/list/context/status/create/update/link/retract/bands.
            Pages have a slug; reference others in the body with [[slug]] (get shows
            backlinks). get/update/link accept an id or slug. create data {title, body,
            slug?, parent?, attrs:{knowledge_kind, tags, supersedes}}. Pages form a folder
            tree: parent is another knowledge page (list shows each page's parent); file
            new pages under the right section, and move one with update patch {parent}
            (null = top level). To correct a fact,
            create a new page with attrs.supersedes=[old]. To mark a fact verified, first
            link evidence with a traceable id (not just a URL), then update
            attrs.verification='verified'. People also verify or dispute pages on the site
            (attrs.reviewed_by); a disputed page's attrs.dispute_reason says what to fix,
            and editing a person-verified page sends it back to unverified for them to
            re-check. Writes need a unique request_id. Your identity
            is taken from your connection, never from arguments. See rook_task for link kinds.
            """
            return await invoke(action, band, 'knowledge' if action == 'create' else None, id, query, data, request_id)

        @mcp.tool()
        async def rook_concept(action: str = 'search', band: str | None = None, id: str | None = None,
                               query: str = '', data: dict | None = None, request_id: str | None = None) -> str:
            """Concepts (why): search/list/get/create/update/link. create data {title, body, slug?}.
            Projects belong to concepts. Writes need request_id.
            """
            return await invoke(action, band, 'concept', id, query, data, request_id)

        @mcp.tool()
        async def rook_project(action: str = 'list', band: str | None = None, id: str | None = None,
                               query: str = '', data: dict | None = None, request_id: str | None = None) -> str:
            """Projects (what outcome) beneath concepts: list/search/get/create/update/link.
            create data {title, body, parent: concept id or slug, slug?}. States:
            active|paused|done|archived. For what's on deck use rook_task(action="deck").
            """
            return await invoke(action, band, 'project', id, query, data, request_id)

        async def rook_task(action: str = 'deck', band: str | None = None, id: str | None = None,
                            query: str = '', data: dict | None = None, request_id: str | None = None) -> str:
            return await invoke(action, band, 'task', id, query, data, request_id)
        rook_task.__doc__ = """Tasks: the durable record of work done, in progress and to do, with who did what.
deck: what's on deck across all bands (id=project to narrow): in progress with claimants,
last activity and latest handoff; blocked; paused; todo; recently done.
claim id=task (data {provider_session?}): you're on it; your calls, consoles and handoffs
are then linked to it automatically. release: stop working on it (needs a handoff).
create data {title, body, parent: project or task, attrs:{criteria, workers, dependencies}}.
update data {revision, patch:{state?, attrs?, title?, body?}}; states
todo|in_progress|blocked|paused|done|cancelled|archived. done needs attrs.outcome and an
evidence link; blocked needs attrs.blocked_reason or a blocked_by link; stopping
in-progress work needs a handoff: pass data.handoff {goal, state, next_steps}.
""" + links_help + """
Claims never stop others working. Writes need request_id; id accepts a task id or slug."""
        mcp.tool()(rook_task)

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
