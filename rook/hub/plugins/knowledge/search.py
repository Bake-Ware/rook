"""Bounded hybrid retrieval with an optional embedding service.

The embedding service is either an HTTP endpoint (``POST {"texts": [...]}``
returning ``{"model", "vectors"}``, as ``services/knowledge-embeddings``
serves) or a band capability reached through a ``cap://<worker|any>/<cap>``
resource (for example ``cap://any/embed.text``), called with the same
``{"texts": [...]}`` and returning the same ``{"model", "vectors"}`` (a bare
list of vectors is accepted too). Without one, search is keyword-only.

Pages are embedded per block (:mod:`.chunk`: heading sections), so a long
page that covers a topic in one section ranks on that section, and a result's
``excerpt`` is the block that matched (``section`` names it), not the head of
the page.
"""
import asyncio
import json
import math
import os
import aiohttp

from . import chunk

DEFAULT_MODEL = 'sentence-transformers/all-MiniLM-L6-v2'
# What a failing embedding service may raise; search then falls back to keywords.
EMBED_ERRORS = (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError, KeyError, LookupError, TypeError)

# A page counts as a semantic hit when its best block reaches this cosine.
THRESHOLD = .25
# Search excerpt: the best-matching block, at most this many chars.
PASSAGE = 300
SECTION = 80
# Per-block weight of query terms present (fraction of terms) when picking
# the excerpt block; cosine is the rest.
TERM_BONUS = .15


def _block(row):
    return chunk.Block(0, row['heading'], row['start'], row['end'])


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

    async def index_batch(self, calls=8):
        """Chunk records whose blocks are stale, then embed blocks that have
        no vector for this model yet (16 texts per call, the embedding
        service's limit; at most ``calls`` calls)."""
        if not self.configured:
            return
        async with self._lock:
            self._rechunk()
            for _ in range(calls):
                with self.store.db(False) as db:
                    rows = db.execute(
                        'SELECT b.hash,b.heading,b.start,b."end",r.title,r.body FROM blocks b '
                        'JOIN records r ON r.id=b.record AND r.revision=b.revision '
                        'LEFT JOIN block_vectors v ON v.hash=b.hash AND v.model=? '
                        'WHERE v.hash IS NULL GROUP BY b.hash ORDER BY r.updated LIMIT 16',
                        (self.model,)).fetchall()
                if not rows:
                    return
                try:
                    vectors = await self.embed([chunk.embed_text(r['title'], r['body'], _block(r)) for r in rows])
                except EMBED_ERRORS as error:
                    self.last_error = type(error).__name__
                    return
                with self.store.db() as db:
                    for r, vector in zip(rows, vectors):
                        norm = math.sqrt(sum(x*x for x in vector)) or 1
                        db.execute('INSERT OR REPLACE INTO block_vectors VALUES(?,?,?)',
                                   (r['hash'], self.model, json.dumps([round(x / norm, 6) for x in vector])))
                self.last_error = None

    def _rechunk(self, limit=200):
        """Re-chunk up to ``limit`` records edited since their blocks were
        cut, and drop vectors no block uses any more. Local work only."""
        with self.store.db(False) as db:
            rows = db.execute('SELECT r.id,r.revision,r.title,r.body FROM records r WHERE NOT EXISTS'
                              '(SELECT 1 FROM blocks b WHERE b.record=r.id AND b.revision=r.revision) '
                              'ORDER BY r.updated LIMIT ?', (limit,)).fetchall()
        if not rows:
            return
        with self.store.db() as db:
            for r in rows:
                db.execute('DELETE FROM blocks WHERE record=?', (r['id'],))
                db.executemany('INSERT INTO blocks VALUES(?,?,?,?,?,?,?)', [
                    (r['id'], b.ord, r['revision'], b.heading, b.start, b.end, chunk.digest(r['title'], r['body'], b))
                    for b in chunk.chunk(r['body'])])
            db.execute('DELETE FROM block_vectors WHERE hash NOT IN (SELECT hash FROM blocks)')

    def _semantic(self, band, vector):
        """Cosine per block, streamed: memory stays bounded as the corpus
        grows. Returns {record: {ord: cosine}}; records with no block
        vectors yet fall back to their whole-page embedding (ord None)."""
        norm = math.sqrt(sum(x*x for x in vector)) or 1
        vector = [x / norm for x in vector]
        found = {}
        with self.store.db(False) as db:
            for row in db.execute(
                    'SELECT b.record,b.ord,v.vector FROM blocks b JOIN records r ON r.id=b.record AND r.revision=b.revision '
                    'JOIN block_vectors v ON v.hash=b.hash AND v.model=? '
                    "WHERE r.band=? AND r.state NOT IN ('archived','superseded')", (self.model, band)):
                found.setdefault(row['record'], {})[row['ord']] = sum(a*b for a, b in zip(vector, json.loads(row['vector'])))
            for row in db.execute(
                    'SELECT r.id,e.vector FROM records r JOIN embeddings e ON r.id=e.record AND r.revision=e.revision '
                    "WHERE r.band=? AND e.model=? AND r.state NOT IN ('archived','superseded')", (band, self.model)):
                if row['id'] not in found:
                    v = json.loads(row['vector'])
                    found[row['id']] = {None: sum(a*b for a, b in zip(vector, v)) / (math.sqrt(sum(x*x for x in v)) or 1)}
        return found

    async def query(self, band, query, kind=None, worker=None, limit=20):
        lexical = self.store.lexical(band, query, 100)
        scores = {r['id']: 1 / (30 + n) for n, r in enumerate(lexical)}
        records = {r['id']: r for r in lexical}
        semantic, cosines = False, {}
        if self.configured and query.strip():
            try:
                vector = (await self.embed([query[:1000]]))[0]
                cosines = self._semantic(band, vector)
                ranked = sorted(((max(c.values()), rid) for rid, c in cosines.items()
                                 if max(c.values()) >= THRESHOLD), reverse=True)[:100]
                missing = [rid for _, rid in ranked if rid not in records]
                if missing:
                    with self.store.db(False) as db:
                        for row in db.execute('SELECT * FROM records WHERE id IN (%s)' % ','.join('?' * len(missing)), missing):
                            records[row['id']] = self.store.record(row)
                for n, (_, rid) in enumerate(ranked):
                    scores[rid] = scores.get(rid, 0) + 1/(30+n)
                semantic = True
            except EMBED_ERRORS as error:
                self.last_error = type(error).__name__
        result = [records[rid] | {'score': round(score, 5)} for rid, score in sorted(scores.items(), key=lambda x: -x[1])
                  if (not kind or records[rid]['kind'] == kind) and (not worker or worker in records[rid]['attrs'].get('workers', []))]
        result = result[:max(1, min(limit, 50))]
        words = chunk.terms(query)
        stored = {}
        if result:
            with self.store.db(False) as db:
                for b in db.execute('SELECT b.record,b.ord,b.heading,b.start,b."end" FROM blocks b JOIN records r '
                                    'ON r.id=b.record AND r.revision=b.revision WHERE b.record IN (%s) ORDER BY b.ord'
                                    % ','.join('?' * len(result)), [r['id'] for r in result]):
                    stored.setdefault(b['record'], []).append(chunk.Block(b['ord'], b['heading'], b['start'], b['end']))
        for r in result:
            self._passage(r, words, stored.get(r['id']), cosines.get(r['id'], {}))
            r['body'] = r['body'][:800]
        return {'results': result, 'semantic': semantic,
                'model': self.model if semantic else None, 'index_error': self.last_error}

    @staticmethod
    def _passage(r, words, stored, cosines):
        """Set ``excerpt`` to the block that best matches the query (its
        cosine plus a bonus for query terms it contains) and ``section`` to
        its heading path, instead of the head of the body."""
        blocks = stored or chunk.chunk(r['body'])
        if not stored:
            cosines = {}  # the ords would not line up with fresh chunks
        def score(b):
            distinct, total = chunk.term_hits(b.heading + '\n' + chunk.block_text(r['body'], b), words)
            return (cosines.get(b.ord, 0) + TERM_BONUS * distinct / max(1, len(words)), distinct, total, -b.ord)
        best = max(blocks, key=score)
        text = chunk.block_text(r['body'], best) or r['body']
        r['excerpt'] = chunk.excerpt(text, words, PASSAGE)
        if best.heading:  # the nearest two levels are enough to place it
            r['section'] = ' > '.join(best.heading.split(' > ')[-2:])[:SECTION]
