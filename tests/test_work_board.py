"""The Work area's project-management reads and writes (docs/web/work-views.md):
planning dates on tasks, the flat ``board`` read behind the board, list and
timeline, the project page's extras, and moving a card through the same
``update`` the agents use."""
import uuid

import pytest

from rook.knowledge.service import KnowledgeService
from rook.knowledge.store import KnowledgeStore

AGENT = {'id': 'codex.codex.gpubox@.home.user.rook', 'kind': 'agent', 'label': 'codex', 'host': 'gpubox'}
HUMAN = {'id': 'human:operator', 'kind': 'human', 'label': 'Operator'}


def rid():
    return uuid.uuid4().hex


@pytest.fixture
def svc(tmp_path):
    saved = []
    s = KnowledgeService(tmp_path / 'k.db', lambda: {'actor': AGENT['id'], 'kind': 'agent'},
                         handoffs=lambda author, h: saved.append((author, h)) or 'thread-' + str(len(saved)))
    s.saved = saved
    return s


async def do(s, action, rkind=None, id=None, actor=None, **data):
    return await s.dispatch(action, None, rkind, id, '', data, rid(), actor)


async def seed(s):
    c = await do(s, 'create', 'concept', title='Home lab')
    p = await do(s, 'create', 'project', title='Backups', parent=c['id'], attrs={'start': '2026-10-01', 'due': '2026-10-30'},
                 body='Nightly backups. See [[restic-notes]].')
    k = await do(s, 'create', 'knowledge', title='Restic notes', slug='restic-notes', body='How restic is set up.')
    await do(s, 'create', 'knowledge', title='Backup log', body='Progress on [[backups]].')
    a = await do(s, 'create', 'task', title='Install restic', parent=p['id'],
                 attrs={'start': '2026-10-02', 'due': '2026-10-05', 'tags': ['infra']})
    b = await do(s, 'create', 'task', title='Schedule nightly run', parent=p['id'],
                 attrs={'due': '2026-10-12', 'dependencies': [a['id']], 'tags': ['infra', 'cron']})
    sub = await do(s, 'create', 'task', title='Write the unit file', parent=b['id'])
    await do(s, 'claim', id=a['id'])
    await do(s, 'link', id=p['id'], kind='chat', ref='room123', relation='discussed_in', note='Huddle')
    return dict(c=c, p=p, k=k, a=a, b=b, sub=sub)


@pytest.mark.asyncio
async def test_dates_are_validated_and_can_be_cleared(svc):
    w = await seed(svc)
    with pytest.raises(ValueError, match='date like'):
        await do(svc, 'create', 'task', title='x', parent=w['p']['id'], attrs={'due': '31/10/2026'})
    with pytest.raises(ValueError, match='before'):
        await do(svc, 'create', 'task', title='x', parent=w['p']['id'], attrs={'start': '2026-10-05', 'due': '2026-10-01'})
    cur = await do(svc, 'get', id=w['b']['id'])
    got = await do(svc, 'update', id=w['b']['id'], revision=cur['revision'], patch={'attrs': {'due': None}})
    assert 'due' not in got['attrs'] and got['attrs']['tags'] == ['infra', 'cron']


@pytest.mark.asyncio
async def test_board_lists_projects_and_tasks_flat(svc):
    w = await seed(svc)
    board = await do(svc, 'board')
    assert [p['title'] for p in board['projects']] == ['Backups']
    proj = board['projects'][0]
    assert proj['concept'] == 'Home lab' and proj['due'] == '2026-10-30'
    assert proj['total'] == 3 and proj['open'] == 3 and proj['progress'] == 0
    assert proj['counts'] == {'todo': 2, 'in_progress': 1}
    tasks = {t['title']: t for t in board['tasks']}
    assert set(tasks) == {'Install restic', 'Schedule nightly run', 'Write the unit file'}
    a, b = tasks['Install restic'], tasks['Schedule nightly run']
    assert a['state'] == 'in_progress' and a['owner'] == [AGENT['id']] and a['claimants'][0]['actor'] == AGENT['id']
    assert a['start'] == '2026-10-02' and a['tags'] == ['infra'] and a['project'] == proj['id']
    assert b['dependencies'][0]['id'] == w['a']['id'] and b['unblocked'] is False
    assert tasks['Write the unit file']['project'] == proj['id'] and tasks['Write the unit file']['parent'] == w['b']['id']
    assert 'start' not in b and b['owner'] == []


@pytest.mark.asyncio
async def test_board_for_one_project_adds_the_page_extras(svc):
    w = await seed(svc)
    page = await do(svc, 'board', id='backups')
    assert len(page['projects']) == 1
    assert [r['room'] for r in page['rooms']] == ['room123']
    assert {k['slug']: k['via'] for k in page['knowledge']} == {'restic-notes': 'linked', 'backup-log': 'links here'}
    actions = [(e['title'], e['action']) for e in page['activity']]
    assert ('Install restic', 'claimed') in actions and ('Backups', 'linked') in actions
    with pytest.raises(ValueError, match='not found'):
        await do(svc, 'board', id=w['a']['id'])


@pytest.mark.asyncio
async def test_finished_work_ages_off_the_board(svc):
    w = await seed(svc)
    await do(svc, 'link', id=w['sub']['id'], kind='commit', ref='rook@abc123')
    cur = await do(svc, 'get', id=w['sub']['id'])
    await do(svc, 'update', id=w['sub']['id'], revision=cur['revision'], patch={'state': 'done', 'attrs': {'outcome': 'Shipped'}})
    with svc.store.db() as db:
        db.execute('UPDATE records SET updated=updated-40*86400 WHERE id=?', (w['sub']['id'],))
    assert 'Write the unit file' not in {t['title'] for t in (await do(svc, 'board'))['tasks']}
    everything = await do(svc, 'board', done_days=None)
    done = next(t for t in everything['tasks'] if t['title'] == 'Write the unit file')
    assert done['outcome'] == 'Shipped'
    assert everything['projects'][0]['progress'] == round(1 / 3, 3)


@pytest.mark.asyncio
async def test_moving_a_card_uses_the_agents_rules(svc):
    """The dashboard moves a card with the same update an agent sends: the
    saving rules apply, claims are released and the change is journaled."""
    w = await seed(svc)
    cur = await do(svc, 'get', id=w['a']['id'])
    with pytest.raises(ValueError, match='handoff'):
        await do(svc, 'update', id=w['a']['id'], actor=HUMAN, revision=cur['revision'],
                 patch={'state': 'blocked', 'attrs': {'blocked_reason': 'Waiting on disks'}})
    moved = await do(svc, 'update', id=w['a']['id'], actor=HUMAN, revision=cur['revision'],
                     patch={'state': 'blocked', 'attrs': {'blocked_reason': 'Waiting on disks'}},
                     handoff={'goal': 'Install restic', 'state': 'Waiting on disks', 'next_steps': ['Resume']})
    assert moved['state'] == 'blocked' and svc.saved[0][0] == HUMAN['id']
    got = await do(svc, 'get', id=w['a']['id'])
    assert all(c['released'] for c in got['claims'])
    assert got['events'][0]['actor'] == HUMAN['id'] and got['events'][0]['data']['state'] == 'blocked'
    board = await do(svc, 'board')
    assert next(t for t in board['tasks'] if t['id'] == w['a']['id'])['claimants'] == []
