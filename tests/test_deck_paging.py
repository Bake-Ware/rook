"""The task deck's paging over projects: the totals header, project_fields,
the project filter, offset/limit with next_offset, and loose tasks (open
work under no project). docs/DESIGN-agent-work-system.md 4, "Deck"."""
import sqlite3

import pytest
import pytest_asyncio

from rook.knowledge.service import KnowledgeService

from test_knowledge_system import PRINCIPAL, Enrollment, session

HEADER = ['total_projects', 'total_tasks', 'loose_tasks', 'filters', 'offset', 'limit', 'next_offset', 'deck']


@pytest_asyncio.fixture
async def deck(tmp_path):
    """Three projects (two tasks each, one blocked), oldest first, so the
    deck lists them P3, P2, P1; plus a task made loose under the concept."""
    s = KnowledgeService(tmp_path / 'knowledge.db', lambda: PRINCIPAL, Enrollment())
    n = iter(range(1000))
    async def make(kind, title, parent=None, band='hub'):
        return await s.dispatch('create', band, kind, data={'title': title, 'body': title * 50, 'parent': parent},
                                request_id=f'r{next(n)}')
    c = await make('concept', 'C')
    projects = []
    for i in (1, 2, 3):
        p = await make('project', f'P{i}', c['id'])
        await make('task', f'P{i} todo', p['id'])
        b = await make('task', f'P{i} stuck', p['id'])
        await s.dispatch('update', None, None, b['id'], request_id=f'u{i}', data={
            'revision': b['revision'], 'patch': {'state': 'blocked', 'attrs': {'blocked_reason': 'waiting'}}})
        projects.append(p)
    with sqlite3.connect(s.store.path) as db:  # oldest first: P1 < P2 < P3
        for i, p in enumerate(projects):
            db.execute('UPDATE records SET updated=? WHERE id=?', (1000 + i, p['id']))
    stray = await make('task', 'Stray', projects[0]['id'])
    with sqlite3.connect(s.store.path) as db:  # legacy data: a task under a concept
        db.execute('UPDATE records SET parent=? WHERE id=?', (c['id'], stray['id']))
    s.projects, s.concept, s.stray = projects, c, stray
    return s


@pytest.mark.asyncio
async def test_default_deck_is_unchanged_but_for_the_header_and_loose(deck):
    out = await deck.dispatch('deck', lean=True)
    assert list(out) == HEADER + ['loose']
    assert (out['total_projects'], out['total_tasks'], out['loose_tasks']) == (3, 7, 1)
    assert out['filters'] == {'done_days': 7}
    assert (out['offset'], out['limit'], out['next_offset']) == (0, None, None)
    assert [e['project']['title'] for e in out['deck']] == ['P3', 'P2', 'P1']
    entry = out['deck'][0]
    assert set(entry) == {'band', 'project', 'in_progress', 'blocked', 'paused', 'todo', 'recently_done'}
    assert {'slug', 'excerpt', 'revision', 'creator'} <= set(entry['project'])  # full header by default
    assert entry['blocked'][0]['blocked_reason'] == 'waiting'
    assert [t['title'] for t in out['loose']['todo']] == ['Stray']
    assert deck.store.deck(['26f8c02c'])[0]['project']['title'] == 'P3'  # the store's list view stays a list


@pytest.mark.asyncio
async def test_project_fields_trim_headers_and_fields_trim_rows(deck):
    out = await deck.dispatch('deck', lean=True, data={'project_fields': 'title,state', 'fields': ['title']})
    assert out['deck'][0]['project'] == {'id': deck.projects[2]['id'], 'title': 'P3', 'state': 'active'}
    assert out['deck'][0]['todo'] == [{'id': out['deck'][0]['todo'][0]['id'], 'title': 'P3 todo'}]
    assert set(out['loose']['todo'][0]) == {'id', 'title'}
    assert out['filters'] == {'done_days': 7, 'fields': ['title'], 'project_fields': ['title', 'state']}
    full = await deck.dispatch('deck', lean=True, data={'project_fields': ['all']})
    assert 'excerpt' in full['deck'][0]['project']


@pytest.mark.asyncio
async def test_project_filter_by_id_slug_or_id_arg(deck):
    p2 = deck.projects[1]
    for out in (await deck.dispatch('deck', data={'project': p2['id']}),
                await deck.dispatch('deck', data={'project': p2['slug']}),
                await deck.dispatch('deck', rid=p2['slug'])):
        assert [e['project']['id'] for e in out['deck']] == [p2['id']]
        assert (out['total_projects'], out['total_tasks'], out['loose_tasks']) == (1, 2, 0)
        assert 'loose' not in out and out['filters']['project'] in (p2['id'], p2['slug'])
    with pytest.raises(ValueError, match="Project 'nope' not found"):
        await deck.dispatch('deck', data={'project': 'nope'})


@pytest.mark.asyncio
async def test_offset_and_limit_page_over_projects(deck):
    pages, offset = [], 0
    while offset is not None:
        out = await deck.dispatch('deck', lean=True, data={'limit': 2, 'offset': offset,
                                                           'project_fields': ['title'], 'states': ['todo']})
        assert (out['total_projects'], out['total_tasks'], out['loose_tasks']) == (3, 4, 1)  # before paging
        assert out['limit'] == 2 and out['offset'] == offset
        assert ('loose' in out) == (offset == 0)
        pages.append([e['project']['title'] for e in out['deck']])
        offset = out['next_offset']
    assert pages == [['P3', 'P2'], ['P1']]
    assert out['filters'] == {'done_days': 7, 'states': ['todo'], 'project_fields': ['title']}
    past = await deck.dispatch('deck', data={'offset': 5})
    assert past['deck'] == [] and past['next_offset'] is None and 'loose' not in past
    assert (await deck.dispatch('deck', data={'limit': 1000}))['limit'] == 100


@pytest.mark.asyncio
async def test_loose_tasks_follow_the_state_filters(deck):
    out = await deck.dispatch('deck', data={'states': ['blocked']})
    assert out['loose_tasks'] == 0 and 'loose' not in out and out['total_tasks'] == 3
    with sqlite3.connect(deck.store.path) as db:  # no parent at all is loose too
        db.execute('UPDATE records SET parent=NULL, state=? WHERE id=?', ('blocked', deck.stray['id']))
    out = await deck.dispatch('deck', data={'states': ['blocked']})
    assert out['loose_tasks'] == 1 and list(out['loose']) == ['blocked']
    # A task under an archived project is not loose: it has a project.
    with sqlite3.connect(deck.store.path) as db:
        db.execute("UPDATE records SET state='archived' WHERE id=?", (deck.projects[0]['id'],))
    out = await deck.dispatch('deck', data={'states': ['blocked']})
    assert (out['total_projects'], out['loose_tasks']) == (2, 1)


@pytest.mark.asyncio
async def test_mcp_rook_task_deck_passes_the_paging_args(tmp_path, monkeypatch):
    async with session(tmp_path, monkeypatch) as env:
        c = (await env.tool('rook_concept', action='create', request_id='c1', data={'title': 'Idea'}))['result']
        for i in (1, 2):
            p = (await env.tool('rook_project', action='create', request_id=f'p{i}',
                                data={'title': f'Proj {i}', 'parent': c['slug']}))['result']
            await env.tool('rook_task', action='create', request_id=f't{i}', data={'title': f'T{i}', 'parent': p['slug']})
        got = (await env.tool('rook_task', action='deck', data={'limit': 1, 'project_fields': ['title']}))['result']
        assert list(got)[:7] == HEADER[:7]
        assert (got['total_projects'], got['total_tasks'], got['next_offset']) == (2, 2, 1)
        assert set(got['deck'][0]['project']) == {'id', 'title'}
        last = (await env.tool('rook_task', action='deck', data={'limit': 1, 'offset': 1}))['result']
        assert last['next_offset'] is None and len(last['deck']) == 1
        bad = await env.tool('rook_task', action='deck', data={'project': 'nope'})
        assert not bad['ok'] and "'nope' not found" in bad['error']
