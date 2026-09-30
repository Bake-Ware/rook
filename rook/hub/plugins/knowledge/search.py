"""Bounded hybrid retrieval with an optional embedding service.

The embedding service is either an HTTP endpoint (``POST {"texts": [...]}``
returning ``{"model", "vectors"}``, as ``services/knowledge-embeddings``
serves) or a band capability reached through a ``cap://<worker|any>/<cap>``
resource (for example ``cap://any/embed.text``), called with the same
``{"texts": [...]}`` and returning the same ``{"model", "vectors"}`` (a bare
list of vectors is accepted too). Without one, search is keyword-only.
"""
import asyncio
import json
import math
import os
import aiohttp

DEFAULT_MODEL = 'sentence-transformers/all-MiniLM-L6-v2'
# What a failing embedding service may raise; search then falls back to keywords.
EMBED_ERRORS = (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError, KeyError, LookupError, TypeError)


class Search:
    def __init__(self, store, url=None, model=None, resource=None, semantic=True):
        """``url``: HTTP embedding endpoint; ``resource``: a callable
        ``cap://`` :class:`rook.core.plugin.Resource`. With neither given, the
        legacy ``ROOK_EMBED_URL`` / ``ROOK_EMBED_MODEL`` env vars apply.
        ``semantic=False`` turns embeddings off (keyword search only)."""
        self.store = store
        if url is None and resource is None:
            url = os.environ.get('ROOK_EMBED_URL', '')
        self.url = url or ''
        self.resource = resource
        self.model = model or os.environ.get('ROOK_EMBED_MODEL', DEFAULT_MODEL)
        self.semantic = semantic
        self.last_error = None
        self._lock = asyncio.Lock()

    @property
    def configured(self):
        return bool(self.semantic and (self.url or self.resource is not None))

    @property
    def endpoint(self):
        """Where embeddings come from, for status (no credentials in it)."""
        if not self.configured:
            return None
        return self.resource.url if self.resource is not None else self.url.split('?')[0]

    async def embed(self, texts):
        if not self.configured:
            raise RuntimeError('Semantic embedding service is not configured')
        if self.resource is not None:
            data = await self.resource.call({'texts': texts}, timeout=8)
            if isinstance(data, list):
                data = {'model': self.model, 'vectors': data}
        else:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as http:
                async with http.post(self.url, json={'texts': texts}) as response:
                    response.raise_for_status()
                    data = await response.json()
        if not isinstance(data, dict):
            raise RuntimeError('Invalid embedding response')
        if data.get('model', self.model) != self.model:
            raise RuntimeError('Embedding model mismatch')
        vectors = data['vectors']
        dims = {len(v) for v in vectors} if isinstance(vectors, list) else set()
        if (len(vectors) != len(texts) or len(dims) != 1 or 0 in dims
                or any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x)
                       for v in vectors for x in v)):
            raise RuntimeError('Invalid embedding response')
        return vectors

    async def index_batch(self):
        if not self.configured:
            return
        async with self._lock:
            with self.store.db(False) as db:
                rows = db.execute('SELECT r.* FROM records r LEFT JOIN embeddings e ON r.id=e.record WHERE e.record IS NULL OR e.revision<>r.revision OR e.model<>? ORDER BY r.updated LIMIT 16', (self.model,)).fetchall()
            if not rows:
                return
            try:
                vectors = await self.embed([r['title'] + '\n' + r['body'][:3000] for r in rows])
                with self.store.db() as db:
                    for r, vector in zip(rows, vectors):
                        db.execute('INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?)', (r['id'], r['revision'], self.model, json.dumps(vector)))
                self.last_error = None
            except EMBED_ERRORS as error:
                self.last_error = type(error).__name__

    async def query(self, band, query, kind=None, worker=None, limit=20):
        lexical = self.store.lexical(band, query, 100)
        scores = {r['id']: 1 / (30 + n) for n, r in enumerate(lexical)}
        records = {r['id']: r for r in lexical}
        semantic = False
        if self.configured and query.strip():
            try:
                vector = (await self.embed([query[:1000]]))[0]
                norm = math.sqrt(sum(x*x for x in vector)) or 1
                ranked = []
                # Stream vectors: memory use stays bounded as the corpus grows.
                with self.store.db(False) as db:
                    rows = db.execute("SELECT r.*,e.vector FROM records r JOIN embeddings e ON r.id=e.record AND r.revision=e.revision WHERE r.band=? AND e.model=? AND r.state NOT IN ('archived','superseded')", (band, self.model))
                    for row in rows:
                        v = json.loads(row['vector'])
                        cosine = sum(a*b for a, b in zip(vector, v)) / norm / (math.sqrt(sum(x*x for x in v)) or 1)
                        if cosine >= .25:
                            ranked.append((cosine, self.store.record(row)))
                            if len(ranked) > 200:
                                ranked = sorted(ranked, key=lambda x: -x[0])[:100]
                for n, (cosine, r) in enumerate(sorted(ranked, key=lambda x: -x[0])[:100]):
                    r.pop('vector', None)
                    records[r['id']] = r
                    scores[r['id']] = scores.get(r['id'], 0) + 1/(30+n)
                semantic = True
            except EMBED_ERRORS as error:
                self.last_error = type(error).__name__
        result = [records[rid] | {'score': round(score, 5)} for rid, score in sorted(scores.items(), key=lambda x: -x[1])
                  if (not kind or records[rid]['kind'] == kind) and (not worker or worker in records[rid]['attrs'].get('workers', []))]
        for r in result:
            r['body'] = r['body'][:800]
        return {'results': result[:max(1, min(limit, 50))], 'semantic': semantic,
                'model': self.model if semantic else None, 'index_error': self.last_error}
