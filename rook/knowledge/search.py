"""Bounded hybrid retrieval with an optional self-hosted embedding endpoint."""
import asyncio
import json
import math
import os
import aiohttp


class Search:
    def __init__(self, store):
        self.store = store
        self.url = os.environ.get('ROOK_EMBED_URL', '')
        self.model = os.environ.get('ROOK_EMBED_MODEL', 'sentence-transformers/all-MiniLM-L6-v2')
        self.last_error = None
        self._lock = asyncio.Lock()

    async def embed(self, texts):
        if not self.url:
            raise RuntimeError('Semantic embedding service is not configured')
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as http:
            async with http.post(self.url, json={'texts': texts}) as response:
                response.raise_for_status()
                data = await response.json()
        if data.get('model') != self.model:
            raise RuntimeError('Embedding model mismatch')
        vectors = data['vectors']
        if len(vectors) != len(texts) or any(len(v) != 384 or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in v) for v in vectors):
            raise RuntimeError('Invalid embedding response')
        return vectors

    async def index_batch(self):
        if not self.url:
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
            except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError, KeyError) as error:
                self.last_error = type(error).__name__

    async def query(self, band, query, kind=None, worker=None, limit=20):
        lexical = self.store.lexical(band, query, 100)
        scores = {r['id']: 1 / (30 + n) for n, r in enumerate(lexical)}
        records = {r['id']: r for r in lexical}
        semantic = False
        if self.url and query.strip():
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
            except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError, KeyError) as error:
                self.last_error = type(error).__name__
        result = [records[rid] | {'score': round(score, 5)} for rid, score in sorted(scores.items(), key=lambda x: -x[1])
                  if (not kind or records[rid]['kind'] == kind) and (not worker or worker in records[rid]['attrs'].get('workers', []))]
        for r in result:
            r['body'] = r['body'][:800]
        return {'results': result[:max(1, min(limit, 50))], 'semantic': semantic,
                'model': self.model if semantic else None, 'index_error': self.last_error}
