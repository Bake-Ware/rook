"""MCP tools and human API adapter for shared knowledge records.

Attribution only. Every write records the caller's audit identity (from the
band MCP's existing bearer token, see ``rook.band_mcp.attribution``). Nothing
in this module sits in the band call path: ``rook_call`` and every other
execution tool are untouched, and no failure here can deny them.

Bands are the operator's existing enrollment bands, identified by their
enrollment ID (stable across PSK rotation). No band ID is minted here.
"""
import asyncio
import json
import logging
import time
from .store import KnowledgeStore
from .search import Search

log = logging.getLogger(__name__)


class KnowledgeService:
    def __init__(self, path, principal, enrollment=None):
        self.store = KnowledgeStore(path)
        self.principal = principal
        self.enrollment = enrollment
        self.search = Search(self.store)
        self.last_maintenance = None
        self.last_error = None
        self._band_cache = (0, [])

    def bands(self):
        """The operator's existing bands. Without an enrollment registry (a
        single-PSK hub) there is exactly one band, ``default``."""
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
        """The attributed caller. Shared/static and unverified callers may
        still write; their records say so."""
        p = self.principal() or {}
        kind = p.get('kind') or 'unverified'
        aid = p.get('agent_id') or ('shared:static' if kind == 'shared' else 'unverified')
        return {'id': aid, 'kind': kind, 'label': p.get('label') or aid}

    def band(self, requested=None):
        """Resolve a band ID; omitted means the primary band. Accepts the
        enrollment ID, the band name or its 8-hex transport label (the
        ``band`` field ``rook_workers`` shows)."""
        bands = self.bands()
        if requested:
            match = [b for b in bands if requested in (b['id'], b['name'], b['label'])]
        else:
            match = [b for b in bands if b['primary']] or bands[:1]
        if len(match) != 1:
            raise ValueError('Unknown band; rook_knowledge(action="bands") lists them')
        return match[0]['id']

    async def dispatch(self, action, band=None, kind=None, rid=None, query='', data=None,
                       request_id=None, actor=None):
        if action == 'bands':
            return self.bands()
        actor = actor or self.actor()
        band = self.band(band)
        data = dict(data or {})
        if action == 'list':
            return {'records': self.store.list(band, kind=kind, **{k: v for k, v in data.items() if k in ('worker', 'parent', 'state', 'limit', 'offset', 'attention')})}
        if action == 'get':
            return self.store.get(band, rid)
        if action == 'search':
            return await self.search.query(band, query, kind, data.get('worker'), int(data.get('limit', 20)))
        if action == 'context':
            return self.store.context(band, data.get('worker'))
        if action == 'status':
            with self.store.db(False) as db:
                counts = {r['kind']: r['n'] for r in db.execute('SELECT kind,count(*) n FROM records WHERE band=? GROUP BY kind', (band,))}
            return {'band': band, 'counts': counts, 'semantic_configured': bool(self.search.url),
                    'semantic_error': self.search.last_error, 'last_maintenance': self.last_maintenance,
                    'maintenance_error': self.last_error}
        if action not in ('create', 'update'):
            raise ValueError('Actions: bands, list, get, search, context, status, create, update')
        if rid:
            data['id'] = rid
        if kind and action == 'create':
            data['kind'] = kind
        result = self.store.mutate(band, actor, request_id, action, data)
        if action == 'create':
            result = {**result, 'comparable': [
                {'id': r['id'], 'title': r['title'], 'kind': r['kind']}
                for r in self.store.lexical(band, result['title'], 5) if r['id'] != result['id']]}
        return result

    def register(self, mcp):
        async def invoke(action, band, kind, rid, query, data, request_id):
            try:
                return json.dumps({'ok': True, 'result': await self.dispatch(action, band, kind, rid, query, data, request_id)})
            except (ValueError, KeyError, TypeError, PermissionError) as error:
                return json.dumps({'ok': False, 'error': str(error), 'code': type(error).__name__})

        @mcp.tool()
        async def rook_knowledge(action: str = 'search', band: str | None = None, id: str | None = None,
                                 query: str = '', data: dict | None = None, request_id: str | None = None) -> str:
            """Shared memory: bands/search/get/list/context/status/create/update.
            Search before creating comparable records. For writes pass a unique
            request_id and data {title,body,parent?,attrs:{sources,evidence,workers,
            knowledge_kind,verification,related,supersedes}}; update needs data
            {revision,patch}. band is optional (primary band by default) and may be
            a band name, ID or the 8-hex label shown by rook_workers. Records are
            attributed to your API key. Bookkeeping only — nothing here grants or
            restricts what you may run.
            """
            return await invoke(action, band, 'knowledge' if action == 'create' else None, id, query, data, request_id)

        @mcp.tool()
        async def rook_concept(action: str = 'search', band: str | None = None, id: str | None = None,
                               query: str = '', data: dict | None = None, request_id: str | None = None) -> str:
            """Concepts (desired outcomes): search/list/get/create/update.
            create data {title,body,attrs:{sources,related,workers}}; update {revision,patch}.
            Writes need request_id. Projects belong to concepts.
            """
            return await invoke(action, band, 'concept', id, query, data, request_id)

        @mcp.tool()
        async def rook_project(action: str = 'list', band: str | None = None, id: str | None = None,
                               query: str = '', data: dict | None = None, request_id: str | None = None) -> str:
            """Projects beneath concepts: list/search/get/create/update.
            create data {title,body,parent:concept_id,attrs:{criteria,workers,sources,related}}.
            update {revision,patch}. A record of work, not a permission.
            """
            return await invoke(action, band, 'project', id, query, data, request_id)

        @mcp.tool()
        async def rook_task(action: str = 'list', band: str | None = None, id: str | None = None,
                            query: str = '', data: dict | None = None, request_id: str | None = None) -> str:
            """Tasks (and proposals) beneath projects: list/search/get/create/update.
            create data {title,body,parent:project_or_task_id,attrs:{criteria,workers,
            dependencies,sources,related,evidence}}. update {revision,patch:{state?,...}}.
            A record of work, not a permission.
            """
            return await invoke(action, band, 'task', id, query, data, request_id)

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
