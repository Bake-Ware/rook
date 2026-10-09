"""Neighbour stubs on a lean (MCP) knowledge get: structural ``links_out`` /
``backlinks`` and semantic ``related`` (rook/hub/plugins/knowledge/store.py,
search.py, service.py)."""
import uuid

import pytest

from rook.hub.plugins.knowledge import search as search_mod
from rook.hub.plugins.knowledge.service import MCP_GET_STUBS, KnowledgeService
from rook.hub.plugins.knowledge.store import GIST

AGENT = {'id': 'agent.test.worker-a', 'kind': 'agent', 'label': 'agent'}
HUMAN = {'id': 'human:u1', 'kind': 'human', 'label': 'op'}
VOCAB = ('car', 'database', 'branch', 'voice', 'garden')


def embedder(calls):
    """A fake embedding service: one dimension per vocabulary word, so texts
    about different words are near-orthogonal."""
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


def create(svc, title, body, **extra):
    return svc.store.mutate('default', AGENT, uuid.uuid4().hex, 'create',
                            {'kind': 'knowledge', 'title': title, 'body': body, **extra})


def update(svc, rec, **patch):
    rec = svc.store.get('default', rec['id'])
    return svc.store.mutate('default', AGENT, uuid.uuid4().hex, 'update',
                            {'id': rec['id'], 'revision': rec['revision'], 'patch': patch})


async def get(svc, rid, **data):
    return await svc.dispatch('get', rid=rid, data=data, lean=True)


# -- semantic: related ------------------------------------------------------------

@pytest.mark.asyncio
async def test_related_lists_close_unlinked_pages_with_a_score(svc):
    page = create(svc, 'Notes one', 'garden garden. Tomatoes go in the north bed.')
    near = create(svc, 'Notes two', 'garden beds and compost for the garden.')
    mixed = create(svc, 'Notes three', 'garden ideas for the garden, and the car.')
    far = create(svc, 'Notes four', 'car car: oil change every 8000 km.')
    await svc.search.index_batch()
    got = await get(svc, page['slug'])
    related = got['related']
    assert [r['slug'] for r in related] == [near['slug'], mixed['slug']]  # far is below the floor
    assert related[0]['score'] >= related[1]['score'] >= search_mod.RELATED_MIN
    assert set(related[0]) == {'slug', 'title', 'gist', 'score'}
    assert related[0]['gist'] == 'garden beds and compost for the garden.'
    assert far['slug'] not in str(got) and page['slug'] not in [r['slug'] for r in related]
    # Stored vectors only: a get makes no embedding call.
    calls = len(svc.search.calls)
    await get(svc, page['slug'])
    assert len(svc.search.calls) == calls


@pytest.mark.asyncio
async def test_related_leaves_out_structural_neighbours_and_dead_pages(svc):
    folder = create(svc, 'Folder', 'garden garden folder')
    page = create(svc, 'Page', 'garden notes; see [[linked-out]]', parent=folder['id'])
    out = create(svc, 'Linked out', 'garden garden more', slug='linked-out')
    back = create(svc, 'Links here', f'garden, from [[{page["slug"]}]]')
    child = create(svc, 'Child', 'garden child', parent=page['id'])
    old = create(svc, 'Old', 'garden garden archived')
    task = svc.store.mutate('default', AGENT, uuid.uuid4().hex, 'create',
                            {'kind': 'concept', 'title': 'Weed', 'body': 'garden garden weeding'})
    free = create(svc, 'Unlinked', 'garden garden unlinked')
    update(svc, old, state='archived')
    await svc.search.index_batch()
    got = await get(svc, page['id'])
    assert [r['slug'] for r in got['related']] == [free['slug']]
    # The structural ones are listed where they belong.
    assert [s['slug'] for s in got['links_out']] == ['linked-out']
    assert [s['slug'] for s in got['backlinks']] == [back['slug']]
    assert got['parent'] == folder['id'] and [c['slug'] for c in got['children']] == [child['slug']]
    assert task['slug'] not in str(got['related'])
    # Archiving a related page drops it at once (read time, nothing stored).
    update(svc, free, state='superseded')
    assert 'related' not in await get(svc, page['id'])


@pytest.mark.asyncio
async def test_related_count_follows_data_related_and_the_margin(svc):
    page = create(svc, 'P', 'voice voice voice')
    others = [create(svc, f'Q{i}', 'voice ' * (3 + i)) for i in range(8)]
    await svc.search.index_batch()
    assert len((await get(svc, page['id']))['related']) == search_mod.RELATED_MAX
    assert len((await get(svc, page['id'], related=2))['related']) == 2
    assert len((await get(svc, page['id'], related=99))['related']) == len(others)  # capped at MCP_GET_STUBS
    assert MCP_GET_STUBS >= len(others)
    assert 'related' not in await get(svc, page['id'], related=0)
    # One clear neighbour: weaker ones within the floor but outside the margin drop out.
    lone = create(svc, 'Lone', 'branch branch branch database')
    close = create(svc, 'Close', 'branch branch branch database database')
    weaker = create(svc, 'Weaker', 'branch database database database')
    await svc.search.index_batch()
    near = (await get(svc, lone['id']))['related']
    assert [r['slug'] for r in near] == [close['slug']]
    assert svc.search.related('default', lone['id'], (), 5)[0][0] == close['id']
    search_mod.RELATED_MARGIN, margin = 1, search_mod.RELATED_MARGIN
    try:  # weaker passes the floor; only the margin kept it out
        assert weaker['slug'] in [r['slug'] for r in (await get(svc, lone['id']))['related']]
    finally:
        search_mod.RELATED_MARGIN = margin


@pytest.mark.asyncio
async def test_no_vectors_or_a_failure_never_fails_the_get(svc, tmp_path, monkeypatch):
    page = create(svc, 'Page', 'garden garden')
    create(svc, 'Other', 'garden garden too')
    # Not indexed yet: no related, no error.
    assert 'related' not in await get(svc, page['id'])
    await svc.search.index_batch()
    assert (await get(svc, page['id']))['related']

    # Embedder down: stored vectors still serve related.
    async def down(texts):
        raise RuntimeError('embedder down')
    svc.search.embed = down
    assert (await get(svc, page['id']))['related']

    # Edited since indexing: the page's old vectors are not used.
    update(svc, page, body='car car')
    assert 'related' not in await get(svc, page['id'])

    # Any error in the scan: the get still answers.
    def boom(*a, **k):
        raise RuntimeError('boom')
    monkeypatch.setattr(svc.search, 'related', boom)
    got = await get(svc, page['id'])
    assert got['id'] == page['id'] and 'related' not in got and '_linked' not in got

    # Keyword-only install: no related.
    kw = KnowledgeService(tmp_path / 'knowledge.db', lambda: AGENT)
    kw.search.url = ''
    got = await kw.dispatch('get', rid=page['id'], lean=True)
    assert 'related' not in got and 'links_out' in got


# -- structural: links_out / backlinks --------------------------------------------

@pytest.mark.asyncio
async def test_links_out_and_backlinks_are_capped_stubs(svc):
    targets = [create(svc, f'Target {i}', f'## Heading\nTarget {i} body text.', slug=f't{i:02}') for i in range(13)]
    links = ' '.join(f'[[t{i:02}]]' for i in reversed(range(13)))
    hub = create(svc, 'Hub', f'Intro. {links} [[t03]] [[nowhere]]', slug='hub')
    fans = [create(svc, f'Fan {i}', f'Points at [[hub]]. ' + 'x ' * 200) for i in range(12)]
    task = svc.store.mutate('default', AGENT, uuid.uuid4().hex, 'create',
                            {'kind': 'concept', 'title': 'Do it', 'body': 'see [[hub]]'})
    got = await get(svc, 'hub')
    # Body order, deduplicated, at most MCP_GET_STUBS, the rest counted.
    assert [s['slug'] for s in got['links_out']] == [f't{i:02}' for i in reversed(range(13))][:MCP_GET_STUBS]
    assert got['links_out_more'] == 13 - MCP_GET_STUBS
    assert got['links_missing'] == ['nowhere']
    assert got['links_out'][0] == {'slug': 't12', 'title': 'Target 12', 'gist': 'Target 12 body text.'}
    assert 'mentions' not in got
    # Backlinks: stubs (kind/state shown when not a plain active page), newest first.
    assert len(got['backlinks']) == MCP_GET_STUBS and got['backlinks_more'] == 13 - MCP_GET_STUBS
    assert got['backlinks'][0] == {'slug': task['slug'], 'title': 'Do it', 'gist': 'see [[hub]]',
                                   'kind': 'concept'}
    assert all(len(s['gist']) <= GIST for s in got['backlinks'])
    assert {s['slug'] for s in got['backlinks'][1:]} <= {f['slug'] for f in fans}


@pytest.mark.asyncio
async def test_mutual_link_is_named_once_and_stubs_are_live(svc):
    a = create(svc, 'Alpha', 'Alpha links [[beta]].', slug='alpha')
    b = create(svc, 'Beta', 'Beta links back to [[alpha]].', slug='beta')
    got = await get(svc, 'alpha')
    assert got['links_out'] == [{'slug': 'beta', 'title': 'Beta', 'gist': 'Beta links back to [[alpha]].'}]
    assert got['backlinks'] == [{'slug': 'beta'}]
    # A stub is read from the live row: an edit shows on the next get.
    update(svc, b, title='Beta v2', body='Rewritten. Still [[alpha]].')
    assert (await get(svc, 'alpha'))['links_out'][0] == {
        'slug': 'beta', 'title': 'Beta v2', 'gist': 'Rewritten. Still [[alpha]].'}
    update(svc, b, state='archived')
    assert (await get(svc, 'alpha'))['links_out'][0]['state'] == 'archived'
    assert a['slug'] == 'alpha'


@pytest.mark.asyncio
async def test_operator_web_get_keeps_its_shape(svc):
    create(svc, 'Alpha', 'Alpha links [[beta]].', slug='alpha')
    create(svc, 'Beta', 'Beta links back to [[alpha]].', slug='beta')
    await svc.search.index_batch()
    web = await svc.dispatch('get', rid='alpha', actor=HUMAN)
    assert web['mentions'] == ['beta'] and web['backlinks'][0]['slug'] == 'beta'
    assert {'id', 'kind', 'excerpt'} <= set(web['backlinks'][0])
    assert not {'links_out', 'related', '_linked'} & set(web)


@pytest.mark.asyncio
async def test_parsed_vectors_are_cached_by_digest_and_bounded(svc, monkeypatch):
    page = create(svc, 'Page', 'garden garden')
    for i in range(3):
        create(svc, f'Other {i}', 'garden ' * (i + 1))
    await svc.search.index_batch()
    first = await get(svc, page['id'])
    assert len(svc.search._vectors) == 4  # one block each
    assert await get(svc, page['id']) == first  # same answer from the cache
    monkeypatch.setattr(search_mod, 'VECTOR_CACHE', 2)
    svc.search._vectors.clear()
    assert (await get(svc, page['id']))['related'] == first['related']
    assert len(svc.search._vectors) <= 2
