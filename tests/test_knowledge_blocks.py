"""Block-level chunking, per-block embeddings and query-relevant search
excerpts (rook/hub/plugins/knowledge/chunk.py, search.py)."""
import uuid

import pytest

from rook.hub.plugins.knowledge import chunk
from rook.hub.plugins.knowledge.search import PASSAGE
from rook.hub.plugins.knowledge.service import KnowledgeService
from rook.hub.plugins.knowledge.store import KnowledgeStore

AGENT = {'id': 'agent.test.worker-a', 'kind': 'agent', 'label': 'agent'}
VOCAB = ('car', 'database', 'branch', 'voice', 'garden')

PAGE = """This page collects notes about the lab. It is long and covers many things.

## Garden
The garden needs water twice a week in summer; tomatoes go in the north bed.

## Release process
### Branching
Every change starts on a branch cut from beta, and a PR goes back into beta.

## Voice
The voice pipeline uses a wake word and streams audio to the speech service.
"""


def embedder(calls):
    """A fake embedding service: one dimension per vocabulary word."""
    async def embed(texts):
        calls.append(list(texts))
        return [[0.01 + t.lower().count(w) for w in VOCAB] for t in texts]
    return embed


@pytest.fixture
def svc(tmp_path):
    s = KnowledgeService(tmp_path / 'knowledge.db', lambda: AGENT)
    s.search.url = 'http://embedding.test'
    s.search.calls = []
    s.search.embed = embedder(s.search.calls)
    return s


def create(svc, title, body):
    return svc.store.mutate('default', AGENT, uuid.uuid4().hex, 'create',
                            {'kind': 'knowledge', 'title': title, 'body': body})


def edit(svc, rec, body):
    return svc.store.mutate('default', AGENT, uuid.uuid4().hex, 'update',
                            {'id': rec['id'], 'revision': rec['revision'], 'patch': {'body': body}})


# -- chunking -------------------------------------------------------------------

def test_chunks_follow_headings_and_keep_their_path():
    blocks = chunk.chunk(PAGE)
    assert [b.heading for b in blocks] == ['', 'Garden', 'Release process > Branching', 'Voice']
    # 'Release process' has nothing under it but a subheading: merged forward.
    assert chunk.block_text(PAGE, blocks[2]).startswith('Every change starts on a branch')
    assert blocks[0].start == 0 and blocks[-1].end == len(PAGE)
    assert all(a.end == b.start for a, b in zip(blocks, blocks[1:]))


def test_fenced_code_is_not_a_heading_and_long_sections_split():
    body = 'Intro text.\n\n```\n# comment, not a heading\n```\n\n## Big\n' + ('para ' * 100 + '\n\n') * 8
    blocks = chunk.chunk(body)
    assert blocks[0].heading == '' and '# comment' in body[blocks[0].start:blocks[0].end]
    big = [b for b in blocks if b.heading == 'Big']
    assert len(big) > 1 and all(b.end - b.start <= chunk.MAX_BLOCK for b in big)
    assert chunk.chunk('') == [chunk.Block(0, '', 0, 0)]


def test_excerpt_windows_around_the_first_hit():
    text = 'filler ' * 100 + 'the needle is here ' + 'tail ' * 100
    out = chunk.excerpt(text, ['needle'], 120)
    assert 'needle' in out and out.startswith('…') and out.endswith('…') and len(out) <= 120
    assert chunk.excerpt('short text', ['x'], 120) == 'short text'


# -- per-block embeddings ---------------------------------------------------------

@pytest.mark.asyncio
async def test_pages_embed_per_block_and_edits_reembed_only_changed_blocks(svc):
    rec = create(svc, 'Lab notes', PAGE)
    await svc.search.index_batch()
    assert len(svc.search.calls[-1]) == 4 and svc.search.calls[-1][2].startswith('Lab notes\nRelease process > Branching\n')
    svc.search.calls.clear()
    edited = edit(svc, rec, PAGE.replace('twice a week', 'every morning'))
    await svc.search.index_batch()
    assert svc.search.calls == [[chunk.embed_text('Lab notes', edited['body'], chunk.chunk(edited['body'])[1])]]
    with svc.store.db(False) as db:
        assert db.execute('SELECT count(*) FROM block_vectors').fetchone()[0] == 4  # the old garden vector is gone
        assert db.execute('SELECT count(*) FROM blocks WHERE record=?', (rec['id'],)).fetchone()[0] == 4
    svc.search.calls.clear()
    await svc.search.index_batch()
    assert svc.search.calls == []


# -- search excerpts ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_semantic_search_returns_the_matching_block_not_the_head(svc):
    rec = create(svc, 'Lab notes', PAGE)
    await svc.search.index_batch()
    found = await svc.dispatch('search', query='how do I cut a branch for a change', lean=True)
    hit = found['results'][0]
    assert found['semantic'] and hit['id'] == rec['id']
    assert hit['section'] == 'Release process > Branching' and hit['excerpt'].startswith('Every change starts')
    voice = (await svc.dispatch('search', query='voice', lean=True))['results'][0]
    assert voice['section'] == 'Voice' and 'wake word' in voice['excerpt']


@pytest.mark.asyncio
async def test_keyword_search_excerpt_is_the_block_with_the_terms(tmp_path):
    svc = KnowledgeService(tmp_path / 'knowledge.db', lambda: AGENT)
    assert not svc.search.configured
    create(svc, 'Lab notes', PAGE)
    hit = (await svc.dispatch('search', query='tomatoes north bed', lean=True))['results'][0]
    assert hit['section'] == 'Garden' and hit['excerpt'].startswith('The garden needs water')
    intro = (await svc.dispatch('search', query='lab', lean=True))['results'][0]
    assert 'section' not in intro and intro['excerpt'].startswith('This page collects')


@pytest.mark.asyncio
async def test_a_section_deep_in_a_long_page_is_found_and_quoted(svc):
    """The page-level index embedded only the first 3000 chars; blocks reach
    the whole page."""
    filler = ''.join(f'## Part {n}\n' + 'garden ' * 60 + '\n\n' for n in range(12))
    rec = create(svc, 'Long page', filler + '## Storage\nThe database lives on the hub beside the journal.\n')
    create(svc, 'Other page', 'garden garden')
    await svc.search.index_batch()
    hit = (await svc.dispatch('search', query='database', lean=True))['results'][0]
    assert hit['id'] == rec['id'] and hit['section'] == 'Storage' and 'journal' in hit['excerpt']
    assert len(hit['excerpt']) <= PASSAGE


@pytest.mark.asyncio
async def test_unindexed_pages_fall_back_to_page_embeddings(svc):
    """Pages embedded by an older release (embeddings table) stay semantic
    hits until their blocks are indexed."""
    import json
    rec = create(svc, 'Automobile repair', 'Notes.')
    with svc.store.db() as db:
        db.execute('INSERT INTO embeddings VALUES(?,?,?,?)',
                   (rec['id'], rec['revision'], svc.search.model, json.dumps([1.0, 0, 0, 0, 0])))
    found = await svc.search.query('default', 'car')
    assert found['semantic'] and found['results'][0]['id'] == rec['id']
