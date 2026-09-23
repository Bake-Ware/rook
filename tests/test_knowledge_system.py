"""Agent work system: wiki records, links, claims, saving rules, deck, hygiene
and MCP integration (docs/DESIGN-agent-work-system.md). Also pins the 349e3eb
regressions: knowledge never sits in the band call path and mints no bands."""
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import httpx
import pytest
from starlette.applications import Starlette

from rook.knowledge.store import KnowledgeStore, Conflict
from rook.knowledge.service import KnowledgeService
from rook.knowledge.maintenance import import_vault
from rook.knowledge.web import routes
from rook.band_mcp.hygiene import Hygiene

AGENT = {'id': 'codex.codex.kaiju@.home.bake.rook', 'kind': 'agent', 'label': 'codex', 'host': 'kaiju',
         'client': 'codex', 'dir': '/home/bake/rook'}
OTHER = {'id': 'claude.claudecode.cachyrig', 'kind': 'agent', 'label': 'claude', 'host': 'cachyrig', 'client': 'claudecode'}


def rid():
    return uuid.uuid4().hex


@pytest.fixture
def work(tmp_path):
    s = KnowledgeStore(tmp_path / 'knowledge.db')
    def create(kind, title, parent=None, actor=AGENT, band='default', **attrs):
        return s.mutate(band, actor, rid(), 'create',
                        {'kind': kind, 'title': title, 'body': title, 'parent': parent, 'attrs': attrs})
    concept = create('concept', 'Shared memory')
    project = create('project', 'Knowledge service', concept['id'])
    task = create('task', 'Restart test service', project['id'], criteria=['Service responds'], workers=['kaiju'])
    return SimpleNamespace(s=s, create=create, concept=concept, project=project, task=task)


def update(w, record, actor=AGENT, **patch):
    cur = w.s.get(record['band'], record['id'])
    return w.s.mutate(record['band'], actor, rid(), 'update', {'id': cur['id'], 'revision': cur['revision'], 'patch': patch})


def link(w, record, kind, ref, relation='evidence', actor=AGENT):
    return w.s.mutate(record['band'], actor, rid(), 'link', {'id': record['id'], 'kind': kind, 'ref': ref, 'relation': relation})


# --- records and wiki ---------------------------------------------------------

def test_writes_are_idempotent_and_revision_checked(work):
    w = work; data = {'kind': 'concept', 'title': 'Repeat me'}
    r = w.s.mutate('default', AGENT, 'retry', 'create', data)
    assert r == w.s.mutate('default', AGENT, 'retry', 'create', data)
    with pytest.raises(Conflict): w.s.mutate('default', AGENT, 'retry', 'create', data | {'title': 'different'})
    w.s.mutate('default', AGENT, 'e1', 'update', {'id': r['id'], 'revision': r['revision'], 'patch': {'body': 'More'}})
    with pytest.raises(Conflict): w.s.mutate('default', AGENT, 'e2', 'update', {'id': r['id'], 'revision': r['revision'], 'patch': {'body': 'Stale'}})


def test_slugs_wiki_links_and_backlinks(work):
    w = work
    assert w.project['slug'] == 'knowledge-service'
    dup = w.create('knowledge', 'Knowledge service')
    assert dup['slug'] == 'knowledge-service-2'  # unique per band
    page = w.create('knowledge', 'Ports in use')
    assert page['slug'] == 'ports-in-use'
    note = w.s.mutate('default', AGENT, rid(), 'create', {'kind': 'knowledge', 'title': 'Runbook', 'slug': 'runbook',
                                                          'body': 'See [[ports-in-use]] first.'})
    got = w.s.get('default', 'ports-in-use')  # fetch by slug
    assert [b['slug'] for b in got['backlinks']] == ['runbook']
    assert w.s.get('default', 'runbook')['mentions'] == ['ports-in-use']
    with pytest.raises(Conflict):
        w.s.mutate('default', AGENT, rid(), 'create', {'kind': 'knowledge', 'title': 'x', 'slug': 'runbook'})
    assert note['creator'] == AGENT['id']


def test_same_title_gets_distinct_slugs_in_a_band(work):
    a = work.create('knowledge', 'Deploy notes'); b = work.create('knowledge', 'Deploy notes')
    assert (a['slug'], b['slug']) == ('deploy-notes', 'deploy-notes-2')


def test_pages_nest_like_folders_and_can_move(work):
    hosts = work.create('knowledge', 'Hosts')
    sojourn = work.create('knowledge', 'Sojourn', hosts['id'])
    gpu = work.create('knowledge', 'GPU notes', sojourn['id'])
    assert sojourn['parent'] == hosts['id']
    assert [c['id'] for c in work.s.get('default', hosts['id'])['children']] == [sojourn['id']]
    with pytest.raises(ValueError):   # pages only nest under pages
        work.create('knowledge', 'Bad', work.task['id'])
    def move(r, parent):
        cur = work.s.get('default', r['id'])
        return work.s.mutate('default', AGENT, rid(), 'update',
                             {'id': r['id'], 'revision': cur['revision'], 'patch': {'parent': parent}})
    with pytest.raises(Conflict):     # no loops
        move(hosts, gpu['slug'])
    assert move(gpu, 'hosts')['parent'] == hosts['id']       # slug accepted
    assert move(gpu, None)['parent'] is None                 # back to top level
    assert work.s.get('default', gpu['id'])['events'][0]['data'] == {'parent': None}
    with pytest.raises(ValueError):   # tasks keep their project structure
        move(work.task, None)


def test_a_person_can_verify_a_page_and_an_agent_edit_undoes_it(work):
    human = {'id': 'human:bake', 'kind': 'human', 'label': 'Bake'}
    page = work.create('knowledge', 'Kaiju has two 3090s')
    def review(verdict, note='', actor=human):
        cur = work.s.get('default', page['id'])
        return work.s.mutate('default', actor, rid(), 'review',
                             {'id': page['id'], 'revision': cur['revision'], 'verdict': verdict, 'note': note})
    with pytest.raises(PermissionError):          # agents can't sign off as a person
        review('verified', actor=AGENT)
    with pytest.raises(ValueError):               # nor forge a 'human' link or review fields
        work.s.mutate('default', AGENT, rid(), 'link', {'id': page['id'], 'kind': 'human', 'ref': 'human:bake'})
    cur = work.s.get('default', page['id'])
    with pytest.raises(ValueError):
        work.s.mutate('default', AGENT, rid(), 'update', {'id': page['id'], 'revision': cur['revision'],
                                                          'patch': {'attrs': {'reviewed_by': 'human:bake'}}})
    r = review('verified')
    assert r['attrs']['verification'] == 'verified' and r['attrs']['reviewed_label'] == 'Bake'
    got = work.s.get('default', page['id'])
    assert any(l['kind'] == 'human' and l['relation'] == 'evidence' for l in got['links'])
    assert got['events'][0]['action'] == 'reviewed'
    # the person can edit their verified page; an agent edit sends it back for review
    upd = lambda actor, body: work.s.mutate('default', actor, rid(), 'update', {
        'id': page['id'], 'revision': work.s.get('default', page['id'])['revision'], 'patch': {'body': body}})
    assert upd(human, 'Dual RTX 3090')['attrs']['verification'] == 'verified'
    r = upd(AGENT, 'Dual RTX 4090')
    assert r['attrs']['verification'] == 'unverified' and 'Bake verified' in r['attrs']['review_note']
    with pytest.raises(ValueError):               # a dispute needs a reason
        review('disputed')
    r = review('disputed', 'They are 3090s, not 4090s')
    assert r['attrs']['dispute_reason'] == 'They are 3090s, not 4090s' and 'review_note' not in r['attrs']
    assert 'reviewed_by' not in review('unverified')['attrs']


def test_supersession_banner_and_safe_import(work, tmp_path):
    w = work; old = w.create('knowledge', 'Old port')
    new = w.create('knowledge', 'New port', supersedes=[old['id']])
    got = w.s.get('default', old['id'])
    assert got['state'] == 'superseded' and got['superseded_by']['id'] == new['id']
    assert [r['id'] for r in w.s.lexical('default', 'port')] == [new['id']]
    vault = tmp_path / 'vault'; vault.mkdir(); (vault / 'note.md').write_text('Existing memory')
    (vault / 'escape.md').symlink_to(tmp_path / 'outside.md'); (tmp_path / 'outside.md').write_text('secret')
    assert import_vault(w.s, 'default', vault) == 1
    assert not w.s.lexical('default', 'secret')


def test_verifying_a_fact_needs_traceable_evidence(work):
    w = work; fact = w.create('knowledge', 'Hub has 1 GB RAM')
    with pytest.raises(ValueError, match='traceable'):
        update(w, fact, attrs={'verification': 'verified'})
    link(w, fact, 'url', 'https://example.com/specs')
    with pytest.raises(ValueError, match='traceable'):
        update(w, fact, attrs={'verification': 'verified'})
    link(w, fact, 'journal', 'c9c799b6')
    assert update(w, fact, attrs={'verification': 'verified'})['attrs']['verification'] == 'verified'
    with pytest.raises(ValueError):
        w.create('knowledge', 'Born verified', verification='verified')


def test_links_are_append_only_with_retraction(work):
    w = work; l = link(w, w.task, 'commit', 'rook@ed6beb8')
    assert [x['ref'] for x in w.s.get('default', w.task['id'])['links']] == ['rook@ed6beb8']
    w.s.mutate('default', AGENT, rid(), 'retract', {'link': l['id'], 'note': 'wrong commit'})
    assert w.s.get('default', w.task['id'])['links'] == []
    with sqlite3.connect(w.s.path) as db:
        assert db.execute('SELECT count(*) FROM links').fetchone()[0] == 2  # nothing deleted
    with pytest.raises(ValueError): link(w, w.task, 'bogus', 'x')


# --- tasks --------------------------------------------------------------------

def test_task_saving_rules(work):
    w = work; t = w.task
    with pytest.raises(ValueError, match='outcome'):
        update(w, t, state='done')
    with pytest.raises(ValueError, match='evidence'):
        update(w, t, state='done', attrs={'outcome': 'Restarted'})
    with pytest.raises(ValueError, match='blocked'):
        update(w, t, state='blocked')
    with pytest.raises(ValueError, match='why'):
        update(w, t, state='cancelled')
    link(w, t, 'journal', 'abc123')
    done = update(w, t, state='done', attrs={'outcome': 'Restarted; health ok'})
    assert done['state'] == 'done'
    with pytest.raises(ValueError, match='state must be'):
        update(w, t, state='completed')


def test_claims_record_but_never_block_and_stopping_needs_a_handoff(work):
    w = work; t = w.task
    c1 = w.s.mutate('default', AGENT, rid(), 'claim', {'id': t['id'], 'provider_session': 'sess-1'})
    assert w.s.get('default', t['id'])['state'] == 'in_progress'
    c2 = w.s.mutate('default', OTHER, rid(), 'claim', {'id': t['slug']})
    assert c2['also_claimed_by'][0]['actor'] == AGENT['id']  # a second agent may claim too
    with pytest.raises(ValueError, match='handoff'):
        update(w, t, state='paused')
    w.s.mutate('default', OTHER, rid(), 'release', {'id': t['id']})  # not the last claimant: fine
    with pytest.raises(ValueError, match='handoff'):
        w.s.mutate('default', AGENT, rid(), 'release', {'id': t['id']})
    assert w.s.auto_link(AGENT, 'handoff', 'thread-9') == t['id']
    w.s.mutate('default', AGENT, rid(), 'release', {'id': t['id']})
    got = w.s.get('default', t['id'])
    assert got['state'] == 'paused' and all(c['released'] for c in got['claims'])
    assert {e['action'] for e in got['events']} >= {'claimed', 'released'}


def test_auto_link_goes_to_latest_active_claim_and_updates_activity(work):
    w = work; t2 = w.create('task', 'Second', w.project['id'])
    assert w.s.auto_link(AGENT, 'journal', 'j0') is None  # no claim → nothing linked
    w.s.mutate('default', AGENT, rid(), 'claim', {'id': w.task['id']})
    time.sleep(0.01)
    w.s.mutate('default', AGENT, rid(), 'claim', {'id': t2['id']})
    assert w.s.auto_link(AGENT, 'journal', 'j1') == t2['id']
    links = w.s.get('default', t2['id'])['links']
    assert links[0]['auto'] == 1 and links[0]['relation'] == 'touched'


def test_deck_covers_all_bands_by_project(work):
    w = work
    other_c = w.create('concept', 'Tablets', band='family')
    other_p = w.create('project', 'Tablet fleet', other_c['id'], band='family')
    w.create('task', 'Charge them', other_p['id'], band='family')
    blocked = w.create('task', 'Blocked one', w.project['id'])
    update(w, blocked, state='blocked', attrs={'blocked_reason': 'waiting on hub RAM'})
    w.s.mutate('default', AGENT, rid(), 'claim', {'id': w.task['id']})
    deck = w.s.deck(['default', 'family'])
    by = {d['project']['title']: d for d in deck}
    assert set(by) == {'Knowledge service', 'Tablet fleet'}
    ks = by['Knowledge service']
    assert ks['in_progress'][0]['claimants'][0]['actor'] == AGENT['id']
    assert ks['blocked'][0]['blocked_reason'] == 'waiting on hub RAM'
    assert by['Tablet fleet']['todo'][0]['title'] == 'Charge them'
    assert [d['project']['title'] for d in w.s.deck(['default', 'family'], project='tablet-fleet')] == ['Tablet fleet']


def test_prototype_v1_database_migrates(tmp_path):
    path = tmp_path / 'old.db'
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE records(id TEXT PRIMARY KEY, band TEXT NOT NULL, kind TEXT NOT NULL, parent TEXT, title TEXT NOT NULL,
          body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'proposed', revision INTEGER NOT NULL DEFAULT 1,
          scope_revision INTEGER NOT NULL DEFAULT 1, attrs TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, creator TEXT NOT NULL);
        CREATE TABLE approvals(id TEXT);
        INSERT INTO records VALUES('t_1','default','task',NULL,'Old task','',  'running',1,1,'{}',0,0,'x');
        INSERT INTO records VALUES('k_1','default','knowledge',NULL,'Old fact','','proposed',1,1,'{}',0,0,'x');
        PRAGMA user_version=1;''')
    db.close()
    s = KnowledgeStore(path)
    assert (s.get('default', 't_1')['state'], s.get('default', 'old-fact')['state']) == ('in_progress', 'active')
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='approvals'").fetchone()


# --- service ------------------------------------------------------------------

class Enrollment:
    def bands(self, active_only=False):
        return [{'id': '26f8c02c', 'name': 'bakenet', 'label': '7f68c499', 'is_primary': 1},
                {'id': 'c8cbc05b', 'name': 'rooknet', 'label': '6178ba5f', 'is_primary': 0}]


PRINCIPAL = {'kind': 'agent', 'actor': 'codex.codex.kaiju@.home.bake.rook', 'key_id': 'k1', 'agent_id': 'agent_x',
             'token': 'codex', 'client': 'codex', 'host': 'kaiju', 'dir': '/home/bake/rook'}


@pytest.mark.asyncio
async def test_service_finds_records_across_bands_and_attributes_compound_identity(work):
    saved = []
    s = KnowledgeService(work.s.path, lambda: PRINCIPAL, Enrollment(),
                         handoffs=lambda author, h: saved.append((author, h)) or 'thread-1')
    c = await s.dispatch('create', 'rooknet', 'concept', data={'title': 'On rooknet'}, request_id='r1')
    assert c['band'] == 'c8cbc05b' and c['creator'] == PRINCIPAL['actor']
    assert (await s.dispatch('get', None, None, c['slug']))['id'] == c['id']  # no band needed
    p = await s.dispatch('create', 'rooknet', 'project', data={'title': 'P', 'parent': c['slug']}, request_id='r2')
    t = await s.dispatch('create', 'rooknet', 'task', data={'title': 'T', 'parent': p['id']}, request_id='r3')
    await s.dispatch('claim', None, None, t['id'], request_id='r4')
    got = await s.dispatch('update', None, None, t['id'], request_id='r5', data={
        'revision': (await s.dispatch('get', None, None, t['id']))['revision'], 'patch': {'state': 'paused'},
        'handoff': {'goal': 'T', 'state': 'half done', 'next_steps': ['finish']}})
    assert got['state'] == 'paused' and saved[0][0] == PRINCIPAL['actor']
    deck = (await s.dispatch('deck'))['deck']
    assert deck[0]['paused'][0]['latest_handoff']['ref'] == 'thread-1'
    with sqlite3.connect(work.s.path) as db:
        info = json.loads(db.execute('SELECT info FROM actors WHERE id=?', (PRINCIPAL['actor'],)).fetchone()[0])
    assert info['host'] == 'kaiju' and info['dir'] == '/home/bake/rook'


@pytest.mark.asyncio
async def test_search_uses_embeddings_and_falls_back(work):
    w = work; r = w.create('knowledge', 'Automobile repair')
    s = KnowledgeService(w.s.path, lambda: None)
    s.search.url = 'http://embedding.test'
    s.search.embed = AsyncMock(return_value=[[1.0] + [0.0] * 383])
    with w.s.db() as db: db.execute('INSERT INTO embeddings VALUES(?,?,?,?)', (r['id'], r['revision'], s.search.model, json.dumps([1.0] + [0.0] * 383)))
    assert (await s.search.query('default', 'fix a car'))['results'][0]['id'] == r['id']
    s.search.embed.side_effect = RuntimeError('offline')
    res = await s.search.query('default', 'Automobile')
    assert not res['semantic'] and res['results'][0]['id'] == r['id']


@pytest.mark.asyncio
async def test_human_route_requires_operator_login_and_csrf(work):
    s = KnowledgeService(work.s.path, lambda: None)
    accounts = SimpleNamespace(session=lambda c: {'id': 'bake', 'name': 'Bake', 'csrf': 'csrf', 'admin': c == 'admin'} if c else None)
    app = Starlette(routes=routes(s, accounts))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/knowledge/account-api')).status_code == 401
        assert (await client.get('/knowledge/account-api', headers={'Cookie': 'rook_account=member'})).status_code == 403
        h = {'Cookie': 'rook_account=admin'}
        body = {'action': 'create', 'kind': 'concept', 'request_id': 'h1', 'data': {'title': 'Human idea'}}
        assert (await client.post('/knowledge/account-api', headers=h, json=body)).status_code == 403
        r = await client.post('/knowledge/account-api', headers=h, json=body | {'csrf': 'csrf'})
        assert r.status_code == 200 and r.json()['result']['creator'] == 'human:bake'
        r = await client.post('/knowledge/account-api', headers=h, json={'action': 'deck', 'csrf': 'csrf'})
        assert r.status_code == 200 and 'deck' in r.json()['result']


# --- hygiene ------------------------------------------------------------------

class Workers:
    def __init__(self, workers, reply=None):
        self.workers = workers
        self.calls = []
        self.reply = reply or {'ok': True, 'result': {'ok': True}}

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        self.calls.append((cap, target, args, identity))
        return self.reply


class Chat:
    def __init__(self):
        self.sent = []
    def start(self, title, creator, invite):
        return {'ok': True, 'room': 'room1'}
    def send(self, room, sender, text, mentions, expects):
        self.sent.append(text)


def idle(w, task_id, minutes=45):
    with w.s.db() as db:
        db.execute('UPDATE claims SET last_active=? WHERE task=?', (time.time() - minutes * 60, task_id))


def hygiene_for(w, workers):
    svc = SimpleNamespace(store=w.s)
    return Hygiene(svc, workers, Chat(), lambda: 'Idle {idle}m on [[{slug}]] ({id})')


@pytest.mark.asyncio
async def test_hygiene_sends_into_the_agents_session_once_per_idle_period(work):
    w = work
    w.s.mutate('default', AGENT, rid(), 'claim', {'id': w.task['id'], 'provider_session': 'sess-1'})
    w.s.auto_link(AGENT, 'journal', 'j1')
    workers = Workers({'wk': {'worker_id': 'wk', 'name': 'kaiju', 'caps': ['codex-history.send', 'agent.wake']}})
    h = hygiene_for(w, workers)
    assert await h.tick() == []  # not idle yet
    idle(w, w.task['id'])
    out = await h.tick()
    assert out[0]['action'] == 'sent_to_session'
    cap, target, args, identity = workers.calls[0]
    assert (cap, target, args['session_id'], identity) == ('codex-history.send', 'wk', 'sess-1', 'system:hygiene')
    assert args['text'].startswith('Idle 4') and '[[restart-test-service]]' in args['text']
    assert await h.tick() == []  # once per idle period
    events = [e['action'] for e in w.s.get('default', w.task['id'])['events']]
    assert 'hygiene_nudge' in events


@pytest.mark.asyncio
async def test_hygiene_wakes_or_marks_dirty_and_skips_clean_tasks(work):
    w = work
    w.s.mutate('default', AGENT, rid(), 'claim', {'id': w.task['id']})
    idle(w, w.task['id'])
    wake = Workers({'wk': {'worker_id': 'wk', 'name': 'kaiju', 'caps': ['agent.wake']}})
    assert (await hygiene_for(w, wake).tick())[0]['action'] == 'woke_agent'
    assert wake.calls[0][0] == 'agent.wake' and wake.calls[0][2]['room'] == 'room1'

    t2 = w.create('task', 'Offline host task', w.project['id'])
    off = dict(AGENT, id='codex.codex.gone', host='gone')
    w.s.mutate('default', off, rid(), 'claim', {'id': t2['id']})
    idle(w, t2['id'])
    out = await hygiene_for(w, Workers({})).tick()
    assert [o['action'] for o in out if o['task'] == t2['id']] == ['marked_dirty']
    deck = w.s.deck(['default'])[0]
    assert next(t for t in deck['in_progress'] if t['id'] == t2['id'])['needs_hygiene']

    t3 = w.create('task', 'Clean task', w.project['id'])
    clean = dict(AGENT, id='codex.codex.clean', host='kaiju')
    w.s.mutate('default', clean, rid(), 'claim', {'id': t3['id']})
    idle(w, t3['id'])
    w.s.auto_link(clean, 'handoff', 'thread-x')  # handoff after the last activity → clean
    idle(w, t3['id'])
    with w.s.db() as db:
        db.execute("UPDATE links SET ts=? WHERE ref='thread-x'", (time.time(),))
    assert all(o['task'] != t3['id'] for o in await hygiene_for(w, wake).tick())


# --- MCP integration ------------------------------------------------------------

class FakeBand:
    def __init__(self):
        self.calls = []
        self.workers = {'w1': {'worker_id': 'w1', 'name': 'kaiju', 'band': 'deadbeef', 'caps': ['shell.exec', 'file.list'], 'last_seen': 0}}
    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        self.calls.append(cap)
        return {'id': 'c-' + uuid.uuid4().hex[:6], 'from': target, 'ok': True, 'result': {}}


@asynccontextmanager
async def session(tmp_path, monkeypatch, enabled=True, db=None):
    from rook.band_mcp.server import build_server
    monkeypatch.setenv('ROOK_KNOWLEDGE', '1' if enabled else '0')
    if db: monkeypatch.setenv('ROOK_KNOWLEDGE_DB', db)
    band = FakeBand()
    mcp, store = build_server(band, public_url='https://mcp.example.com', persist_path=str(tmp_path / 'tokens.json'),
                              static_token='static-token-0123456789abcdef', journal_path=str(tmp_path / 'journal.db'))
    token = store.mint_api_token('codex')
    app = mcp.streamable_http_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost',
            headers={'Accept': 'application/json, text/event-stream', 'Authorization': 'Bearer ' + token['token'],
                     'X-Rook-Host': 'kaiju', 'X-Rook-Cwd': '/home/bake/rook'}) as http:
        r = await http.post('/mcp', json={'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'codex-mcp-client', 'version': '1'}}})
        http.headers['mcp-session-id'] = r.headers['mcp-session-id']
        await http.post('/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        async def rpc(method, params):
            r = await http.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
            body = r.json() if r.headers['content-type'].startswith('application/json') else json.loads(next(l[5:] for l in r.text.splitlines() if l.startswith('data:')))
            return body['result']
        async def tool(name, **args):
            return json.loads((await rpc('tools/call', {'name': name, 'arguments': args}))['content'][0]['text'])
        yield SimpleNamespace(rpc=rpc, tool=tool, band=band, token=token, mcp=mcp)


@pytest.mark.asyncio
async def test_mcp_claimed_work_builds_its_own_audit_trail(tmp_path, monkeypatch):
    async with session(tmp_path, monkeypatch) as env:
        tools = {t['name'] for t in (await env.rpc('tools/list', {}))['tools']}
        assert {'rook_knowledge', 'rook_concept', 'rook_project', 'rook_task'} <= tools and 'rook_attempt' not in tools
        me = 'codex.codexmcpclient.kaiju@.home.bake.rook'
        c = (await env.tool('rook_concept', action='create', request_id='c1', data={'title': 'Idea'}))['result']
        assert c['creator'] == me
        p = (await env.tool('rook_project', action='create', request_id='p1', data={'title': 'Proj', 'parent': c['slug']}))['result']
        t = (await env.tool('rook_task', action='create', request_id='t1', data={'title': 'Do it', 'parent': p['slug']}))['result']
        assert (await env.tool('rook_task', action='claim', id=t['slug'], request_id='cl1'))['ok']
        reply = await env.tool('rook_call', cap='shell.exec', worker='kaiju')
        assert reply['_task'] == t['id']
        await env.tool('rook_handoff_save', goal='Do it', state='half', next_steps=['rest'])
        got = (await env.tool('rook_task', action='get', id=t['id']))['result']
        assert [(l['kind'], l['auto']) for l in got['links']] == [('journal', 1), ('handoff', 1)]
        assert got['links'][0]['ref'] == reply['_journal_id']
        deck = (await env.tool('rook_task'))['result']['deck']
        assert deck[0]['in_progress'][0]['claimants'][0]['actor'] == me
        assert env.band.calls == ['shell.exec']  # knowledge never touched the band path


@pytest.mark.asyncio
async def test_knowledge_off_by_default_and_broken_store_never_blocks(tmp_path, monkeypatch):
    async with session(tmp_path, monkeypatch, enabled=False) as env:
        assert 'rook_knowledge' not in {t['name'] for t in (await env.rpc('tools/list', {}))['tools']}
        assert '_task' not in await env.tool('rook_call', cap='shell.exec', worker='kaiju')
    bad = tmp_path / 'is-a-directory'; bad.mkdir()
    async with session(tmp_path, monkeypatch, db=str(bad)) as env:
        assert 'rook_knowledge' not in {t['name'] for t in (await env.rpc('tools/list', {}))['tools']}
        assert (await env.tool('rook_call', cap='shell.exec', worker='kaiju'))['ok']
