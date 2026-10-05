"""Hygiene triggers (rook/hub/plugins/knowledge/hygiene.py, docs/design/hygiene.md):
work-completed signals, idle claims, done without knowledge, ended sessions,
finished projects and stale pages; delivery as ``_hygiene`` and on the deck."""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
import uuid

import anyio
import httpx
import pytest

from rook.hub.plugins.knowledge.hygiene import HygieneEngine, SYSTEM
from rook.hub.plugins.knowledge.service import KnowledgeService
from rook.hub.plugins.knowledge.store import KnowledgeStore

AGENT = {'id': 'codex.codex.gpubox@.home.user.rook', 'kind': 'agent', 'label': 'codex', 'host': 'gpubox',
         'client': 'codex', 'dir': '/home/user/rook'}
OTHER = {'id': 'claude.claudecode.workstation', 'kind': 'agent', 'label': 'claude', 'host': 'workstation',
         'client': 'claudecode'}
H = 3600


def rid():
    return uuid.uuid4().hex


@pytest.fixture
def w(tmp_path):
    s = KnowledgeStore(tmp_path / 'knowledge.db')
    conf = {}
    engine = HygieneEngine(s, conf=lambda: conf)

    def create(kind, title, parent=None, actor=AGENT, body=None, **attrs):
        return s.mutate('default', actor, rid(), 'create',
                        {'kind': kind, 'title': title, 'body': body or title, 'parent': parent, 'attrs': attrs})
    concept = create('concept', 'Shared memory')
    project = create('project', 'Knowledge service', concept['id'])
    task = create('task', 'Restart test service', project['id'])
    return SimpleNamespace(s=s, h=engine, conf=conf, create=create, project=project, task=task)


def claim(w, task, actor=AGENT):
    return w.s.mutate('default', actor, rid(), 'claim', {'id': task['id']})


def idle(w, task_id, seconds):
    with w.s.db() as db:
        db.execute('UPDATE claims SET last_active=? WHERE task=?', (time.time() - seconds, task_id))


def kinds(w, record=None, actor=None):
    return sorted(f['kind'] for f in w.h.open([record] if record else None, actor))


def service(w, actor=AGENT):
    svc = KnowledgeService(w.s.path, lambda: {'actor': actor['id'], 'kind': 'agent'})
    svc.hygiene = w.h
    return svc


def finish(w, task, actor=AGENT):
    """Link the evidence ``done`` needs; returns the task's current version."""
    w.s.mutate('default', actor, rid(), 'link', {'id': task['id'], 'kind': 'commit', 'ref': 'repo@abc1234'})
    return w.s.get('default', task['id'])


# --- work-completed signals ---------------------------------------------------------

def test_commit_link_on_open_task_proposes_done_to_claimant_and_linker_once(w):
    claim(w, w.task)
    assert w.h.on_link(OTHER, w.task['id'], 'commit', 'repo@abc1234')
    assert kinds(w, w.task['id'], AGENT['id']) == ['work_signal']
    assert kinds(w, w.task['id'], OTHER['id']) == ['work_signal']
    assert w.h.on_link(OTHER, w.task['id'], 'commit', 'repo@def5678') == []  # deduped while open
    hint = w.h.take(AGENT['id'])
    assert hint[0]['kind'] == 'work_signal' and hint[0]['id'] == w.task['slug']
    assert 'state done' in hint[0]['say'] and 'repo@abc1234' in hint[0]['say']
    assert w.h.take(AGENT['id']) == []  # delivered once
    assert w.h.on_link(AGENT, w.task['id'], 'url', 'https://example.com/docs') == []  # not a work signal
    events = [e['action'] for e in w.s.get('default', w.task['id'])['events']]
    assert 'hygiene' in events  # every finding is on the record's history


def test_finished_task_gets_no_signal_and_finishing_resolves_open_ones(w):
    svc = service(w)
    claim(w, w.task)
    w.h.on_link(AGENT, w.task['id'], 'commit', 'repo@abc1234')
    cur = finish(w, w.task)
    asyncio.run(svc.dispatch('update', 'default', 'task', w.task['id'], data={
        'revision': cur['revision'], 'patch': {'state': 'done', 'attrs': {'outcome': 'Restarted'}}},
        request_id=rid(), actor=AGENT))
    assert 'work_signal' not in kinds(w, w.task['id'])
    assert w.h.on_link(AGENT, w.task['id'], 'commit', 'repo@later') == []


def links(w, task, kind='commit'):
    return [(l['ref'], l['relation']) for l in w.s.get('default', task['id'])['links'] if l['kind'] == kind]


COMMIT_OUT = {'ok': True, 'result': {'code': 0, 'stdout': '[hygiene-triggers 1a2b3c4] Add triggers\n 2 files changed\n'}}


def test_commit_naming_a_task_the_caller_claimed_is_evidence(w):
    other = w.create('task', 'Write the docs', w.project['id'])
    claim(w, other)
    args = {'cmd': f'git commit -m "Add triggers\n\nrook: {other["slug"]}"'}
    assert w.h.on_call(AGENT, 'shell.exec', args, COMMIT_OUT, 'gpubox') == [other['id']]
    link = [l for l in w.s.get('default', other['id'])['links'] if l['kind'] == 'commit'][0]
    assert (link['ref'], link['relation'], link['auto']) == ('1a2b3c4', 'evidence', 1)
    assert 'on gpubox' in link['note']
    # Other ways to give the message: -F - with a here-document, -am, --message=.
    heredoc = {'cmd': f"cd repo && git commit -F - <<'EOF'\nFix it\n\nrook: {other['id']}\nEOF\n"}
    assert w.h.on_call(AGENT, 'shell.exec', heredoc, COMMIT_OUT) == [other['id']]
    for cmd in (f'git commit -am "fix rook: {other["slug"]}"', f'git commit --message="rook: {other["slug"]}"',
                f'git commit -m "$(cat <<\'EOF\'\nFix\n\nrook: {other["slug"]}\nEOF\n)"'):
        assert w.h.on_call(AGENT, 'shell.exec', {'cmd': cmd}, COMMIT_OUT) == [other['id']], cmd
    assert set(links(w, other)) == {('1a2b3c4', 'evidence')}


def test_commit_without_a_name_goes_to_the_live_claim_as_produced(w):
    claim(w, w.task)
    assert w.h.on_call(AGENT, 'shell.exec', {'cmd': 'git commit -am wip'}, COMMIT_OUT, 'gpubox') == [w.task['id']]
    assert links(w, w.task) == [('1a2b3c4', 'produced')]
    assert 'work_signal' in kinds(w, w.task['id'], AGENT['id'])
    # Nothing to link: no claim; or not a git commit; or a failed call.
    assert w.h.on_call(OTHER, 'shell.exec', {'cmd': 'git commit -am x'}, COMMIT_OUT) == []
    assert w.h.on_call(AGENT, 'shell.exec', {'cmd': 'cat log'}, COMMIT_OUT) == []
    assert w.h.on_call(AGENT, 'shell.exec', {'cmd': 'git log -1'}, COMMIT_OUT) == []
    assert w.h.on_call(AGENT, 'shell.exec', {'cmd': 'git commit'}, {'ok': False, 'error': 'x'}) == []


def test_naming_someone_elses_task_in_a_commit_is_never_evidence(w):
    """Cross-actor spoof: a commit message (or any other arg) naming a task
    the caller has not claimed must not let that task go done."""
    victim = w.create('task', 'Rotate the keys', w.project['id'])
    claim(w, victim, actor=OTHER)
    # Unclaimed by the caller and the caller has no claim: nothing is linked.
    spoof = {'cmd': f'git commit -am "done rook: {victim["slug"]}"'}
    assert w.h.on_call(AGENT, 'shell.exec', spoof, COMMIT_OUT) == []
    assert links(w, victim) == []
    # With a claim of its own, the commit lands there, as produced.
    claim(w, w.task)
    assert w.h.on_call(AGENT, 'shell.exec', spoof, COMMIT_OUT) == [w.task['id']]
    assert links(w, victim) == [] and links(w, w.task) == [('1a2b3c4', 'produced')]
    # A name outside the commit message (other args, other commands) is not read at all.
    claim(w, victim)  # now the caller holds a claim on it too
    for args in ({'cmd': f'echo "rook: {victim["slug"]}" && git commit -am wip'},
                 {'cmd': 'git commit -am wip', 'note': f'rook: {victim["slug"]} {victim["id"]}'},
                 {'cmd': f'git commit -am wip; git tag -m "rook: {victim["slug"]}" v1'}):
        w.h.on_call(AGENT, 'shell.exec', args, COMMIT_OUT)
        assert ('1a2b3c4', 'evidence') not in links(w, victim), args
    # The [branch sha] subject line in the output is the commit message too.
    out = {'ok': True, 'result': {'code': 0, 'stdout': f'[main 7e7e7e7] Rotate rook: {victim["slug"]}\n'}}
    assert w.h.on_call(AGENT, 'shell.exec', {'cmd': 'git commit -F msg.txt'}, out) == [victim['id']]
    assert ('7e7e7e7', 'evidence') in links(w, victim)


def test_naming_a_claimed_task_in_another_band_is_not_evidence(w):
    """Cross-band spoof: the call ran on a worker in band ``default``; a task
    in another band is never its evidence, even when the caller claimed it."""
    s = w.s
    c = s.mutate('other', AGENT, rid(), 'create', {'kind': 'concept', 'title': 'Elsewhere'})
    p = s.mutate('other', AGENT, rid(), 'create', {'kind': 'project', 'title': 'Far', 'parent': c['id']})
    far = s.mutate('other', AGENT, rid(), 'create', {'kind': 'task', 'title': 'Far task', 'parent': p['id']})
    s.mutate('other', AGENT, rid(), 'claim', {'id': far['id']})
    args = {'cmd': f'git commit -am "x rook: {far["id"]}"'}
    assert w.h.on_call(AGENT, 'shell.exec', args, COMMIT_OUT, band='default') == []  # no claim in default
    assert s.get('other', far['id'])['links'] == []
    claim(w, w.task)
    assert w.h.on_call(AGENT, 'shell.exec', args, COMMIT_OUT, band='default') == [w.task['id']]
    assert links(w, w.task) == [('1a2b3c4', 'produced')]
    assert [l['relation'] for l in s.get('other', far['id'])['links'] if l['kind'] == 'commit'] == []
    # From a worker in its own band it is evidence.
    assert w.h.on_call(AGENT, 'shell.exec', args, COMMIT_OUT, band='other') == [far['id']]
    assert [l['relation'] for l in s.get('other', far['id'])['links'] if l['kind'] == 'commit'] == ['evidence']


def test_pr_url_from_gh_pr_create_is_linked(w):
    claim(w, w.task)
    out = {'ok': True, 'result': {'code': 0, 'stdout': 'https://github.com/example/repo/pull/37\n'}}
    assert w.h.on_call(AGENT, 'shell.exec', {'cmd': 'gh pr create --fill'}, out) == [w.task['id']]
    assert links(w, w.task, 'url') == [('https://github.com/example/repo/pull/37', 'produced')]
    assert 'PR ' in w.h.take(AGENT['id'])[0]['say']


def test_pr_urls_from_other_gh_commands_are_not_linked(w):
    claim(w, w.task)
    view = {'ok': True, 'result': {'code': 0, 'stdout': (
        'title:\tFix it\nstate:\tOPEN\nurl:\thttps://github.com/example/repo/pull/12\n'
        'https://github.com/example/repo/pull/99\n')}}
    for cmd in ('gh pr view 12', 'gh pr list', 'gh pr view 12 --json url', 'gh api repos/x/y/pulls'):
        assert w.h.on_call(AGENT, 'shell.exec', {'cmd': cmd}, view) == [], cmd
    assert links(w, w.task, 'url') == []
    assert kinds(w, w.task['id']) == []


def test_handoff_on_in_progress_work_asks_for_a_state_and_stopping_resolves_it(w):
    svc = service(w)
    claim(w, w.task)
    assert svc.auto_link(AGENT, 'handoff', 'thread-1', note='half done') == w.task['id']
    assert kinds(w, w.task['id'], AGENT['id']) == ['handoff_saved']
    assert 'release' in w.h.take(AGENT['id'])[0]['say']
    asyncio.run(svc.dispatch('release', 'default', 'task', w.task['id'], data={}, request_id=rid(), actor=AGENT))
    assert kinds(w, w.task['id']) == []


def test_console_closed_on_a_claimed_task_is_a_signal(w):
    claim(w, w.task)
    w.s.auto_link(AGENT, 'console', 'room-42')
    assert w.h.on_console_closed(AGENT, 'room-42')
    assert 'Console room-42 closed' in w.h.take(AGENT['id'])[0]['say']


# --- idle claims ------------------------------------------------------------------------

def test_idle_claim_is_nudged_then_marked_then_release_is_proposed(w):
    claim(w, w.task)
    w.s.auto_link(AGENT, 'journal', 'j1')
    assert w.h.scan() == []  # active
    idle(w, w.task['id'], 45 * 60)
    assert [f['kind'] for f in w.h.scan()] == ['idle_claim']
    assert w.h.scan() == []  # deduped
    deck = w.s.deck(['default'])[0]
    assert not deck['in_progress'][0]['needs_hygiene']
    idle(w, w.task['id'], 5 * H)
    w.h.scan()
    assert w.s.deck(['default'])[0]['in_progress'][0]['needs_hygiene']  # dirty past hygiene_dirty_hours
    idle(w, w.task['id'], 25 * H)
    raised = w.h.scan()
    assert sorted((f['kind'], f['actor']) for f in raised) == [('release_proposed', ''),
                                                               ('release_proposed', AGENT['id'])]
    assert 'rook_task release' in raised[0]['text']
    assert w.s.get('default', w.task['id'])['state'] == 'in_progress'  # a proposal, never a state change
    # The claimant is back (a stale claim collects no links, so it re-claims): idle resolves.
    claim(w, w.task)
    w.h.on_claim(AGENT['id'], w.task['id'])
    w.h.scan()
    assert kinds(w, w.task['id']) == []


def test_idle_claim_with_a_handoff_after_the_work_is_clean(w):
    claim(w, w.task)
    idle(w, w.task['id'], 45 * 60)
    w.s.auto_link(AGENT, 'handoff', 'thread-x')
    idle(w, w.task['id'], 45 * 60)
    with w.s.db() as db:
        db.execute("UPDATE links SET ts=? WHERE ref='thread-x'", (time.time(),))
    assert w.h.scan() == []


def test_repeat_kinds_are_redelivered_after_the_renotify_period(w):
    claim(w, w.task)
    idle(w, w.task['id'], 45 * 60)
    w.h.scan()
    assert w.h.take(AGENT['id'])
    assert w.h.take(AGENT['id']) == []
    assert w.h.take(AGENT['id'], now=time.time() + 7 * H)  # idle_claim repeats


# --- done without knowledge -----------------------------------------------------------

@pytest.mark.asyncio
async def test_done_without_knowledge_suggests_pages_and_a_linked_page_resolves_it(w):
    svc = service(w)
    page = w.create('knowledge', 'Restart procedure for the test service', actor=OTHER)
    claim(w, w.task)
    cur = finish(w, w.task)
    await svc.dispatch('update', 'default', 'task', w.task['id'], request_id=rid(), actor=AGENT, data={
        'revision': cur['revision'], 'patch': {'state': 'done', 'attrs': {'outcome': 'Restarted'}}})
    hint = w.h.take(AGENT['id'])
    assert hint[0]['kind'] == 'done_without_knowledge' and page['slug'] in hint[0]['suggest']
    assert w.s.get('default', w.task['id'])['state'] == 'done'
    deck = (await svc.dispatch('deck', data={}))['deck'][0]
    assert deck['recently_done'][0]['hygiene'] == ['done_without_knowledge']
    await svc.dispatch('link', 'default', 'task', w.task['id'], request_id=rid(), actor=AGENT,
                       data={'kind': 'record', 'ref': page['slug'], 'relation': 'produced'})
    assert kinds(w, w.task['id']) == []
    w.h.scan()
    assert kinds(w, w.task['id']) == []  # once: not raised again


@pytest.mark.asyncio
async def test_a_page_the_claimant_wrote_or_a_mention_counts_as_knowledge(w):
    svc = service(w)
    claim(w, w.task)
    w.create('knowledge', 'Ports used by the test service')  # written by the claimant during the claim
    cur = finish(w, w.task)
    await svc.dispatch('update', 'default', 'task', w.task['id'], request_id=rid(), actor=AGENT, data={
        'revision': cur['revision'], 'patch': {'state': 'done', 'attrs': {'outcome': 'Restarted'}}})
    assert kinds(w, w.task['id']) == []
    t2 = w.create('task', 'Rotate logs', w.project['id'])
    w.create('knowledge', 'Log rotation', actor=OTHER, body=f'See [[{t2["slug"]}]].')
    cur = finish(w, t2, actor=OTHER)
    await svc.dispatch('update', 'default', 'task', t2['id'], request_id=rid(), actor=OTHER, data={
        'revision': cur['revision'], 'patch': {'state': 'done', 'attrs': {'outcome': 'Rotated'}}})
    assert kinds(w, t2['id']) == []


def test_scan_catches_done_tasks_from_elsewhere_but_not_old_ones(w):
    old = w.create('task', 'Ancient work', w.project['id'])
    for t in (w.task, old):
        cur = finish(w, t)
        w.s.mutate('default', OTHER, rid(), 'update', {'id': t['id'], 'revision': cur['revision'],
                   'patch': {'state': 'done', 'attrs': {'outcome': 'ok'}}})
    with w.s.db() as db:
        db.execute('UPDATE records SET updated=? WHERE id=?', (time.time() - 10 * 86400, old['id']))
    raised = w.h.scan()
    assert {f['record'] for f in raised if f['kind'] == 'done_without_knowledge'} == {w.task['id']}


# --- sessions, projects, stale pages ------------------------------------------------

def test_session_end_with_unhanded_work_asks_for_a_handoff(w):
    claim(w, w.task)
    w.s.auto_link(AGENT, 'journal', 'j1')
    assert w.h.on_session_end(AGENT['id'])
    assert w.s.deck(['default'])[0]['in_progress'][0]['needs_hygiene']
    assert 'rook_handoff_save' in w.h.take(AGENT['id'])[0]['say']
    assert w.h.on_session_end(AGENT['id']) == []  # once per period
    assert w.h.on_session_end(OTHER['id']) == []  # no claim
    assert w.h.on_session_end('unverified') == []


def test_finished_project_gets_a_close_proposal_until_new_work_appears(w):
    cur = finish(w, w.task)
    w.s.mutate('default', AGENT, rid(), 'update', {'id': w.task['id'], 'revision': cur['revision'],
               'patch': {'state': 'done', 'attrs': {'outcome': 'ok'}}})
    assert not [f for f in w.h.scan() if f['kind'] == 'project_complete']  # still fresh
    later = time.time() + 25 * H
    found = [f for f in w.h.scan(now=later) if f['kind'] == 'project_complete']
    assert found and found[0]['actor'] == AGENT['id'] and 'state done' in found[0]['text']
    assert w.s.get('default', w.project['id'])['state'] == 'active'
    w.create('task', 'Next step', w.project['id'])
    w.h.scan(now=later)
    assert 'project_complete' not in kinds(w, w.project['id'])


def test_page_leaning_on_a_superseded_page_is_flagged_and_fixing_it_resolves(w):
    old = w.create('knowledge', 'Service port is 8000')
    w.create('knowledge', 'Service port is 9000', supersedes=[old['id']])
    reader = w.create('knowledge', 'How to call the service', actor=OTHER, body=f'Port per [[{old["slug"]}]].')
    raised = [f for f in w.h.scan() if f['kind'] == 'stale_knowledge']
    assert raised[0]['record'] == reader['id'] and raised[0]['actor'] == OTHER['id']
    got = w.s.get('default', reader['id'])
    w.s.mutate('default', OTHER, rid(), 'update', {'id': reader['id'], 'revision': got['revision'],
               'patch': {'body': 'Port per the current page.'}})
    w.h.scan()
    assert kinds(w, reader['id']) == []


# --- settings ----------------------------------------------------------------------

def test_disabled_or_zero_per_reply_delivers_nothing(w):
    claim(w, w.task)
    idle(w, w.task['id'], 45 * 60)
    w.conf['hygiene_hints_per_reply'] = 0
    w.h.scan()
    assert w.h.take(AGENT['id']) == [] and kinds(w, w.task['id']) == ['idle_claim']
    w.conf['hygiene_enabled'] = False
    assert w.h.scan() == [] and w.h.on_link(AGENT, w.task['id'], 'commit', 'x') == []
    w.conf.update(hygiene_enabled=True, hygiene_hints_per_reply=1, hygiene_idle_minutes=120)
    w.h.scan()
    assert kinds(w, w.task['id']) == []  # threshold raised: the idle finding resolves


@pytest.mark.asyncio
async def test_plugin_settings_drive_the_engine_and_notify_people(tmp_path, monkeypatch):
    from rook.hub.node import HubNode
    monkeypatch.setenv('ROOK_KNOWLEDGE', '1')
    monkeypatch.setenv('ROOK_KNOWLEDGE_HYGIENE_NOTIFY_PEOPLE', '1')
    monkeypatch.setenv('ROOK_KNOWLEDGE_HYGIENE_PROJECT_IDLE_HOURS', '0')
    n = HubNode(str(tmp_path), entry_points=False, build_version='1.test.node')
    kb = n.plugin('knowledge')
    engine = kb.service.hygiene
    assert engine.cfg('hygiene_notify_people') is True and engine.cfg('hygiene_idle_minutes') == 30
    sent = []

    class Notify:
        async def send(self, text, channel='all'):
            sent.append(text)
            return {'ok': True}
    real = n.plugin
    monkeypatch.setattr(n, 'plugin', lambda name: Notify() if name == 'notify' else real(name))
    kb.bind_host(n)
    s = kb.service.store
    c = s.mutate('default', AGENT, rid(), 'create', {'kind': 'concept', 'title': 'C'})
    p = s.mutate('default', AGENT, rid(), 'create', {'kind': 'project', 'title': 'P', 'parent': c['id']})
    t = s.mutate('default', AGENT, rid(), 'create', {'kind': 'task', 'title': 'T', 'parent': p['id']})
    s.mutate('default', AGENT, rid(), 'link', {'id': t['id'], 'kind': 'commit', 'ref': 'x@1'})
    s.mutate('default', AGENT, rid(), 'update', {'id': t['id'], 'revision': s.get('default', t['id'])['revision'],
             'patch': {'state': 'done', 'attrs': {'outcome': 'ok'}}})
    raised = await kb.hygiene_tick()
    assert 'project_complete' in {f['kind'] for f in raised}
    assert sent and sent[0].startswith('Rook hygiene: Every task under project p')
    assert SYSTEM['id'] not in {f.get('actor') for f in raised}


# --- MCP delivery -------------------------------------------------------------------

class CommitBand:
    def __init__(self):
        self.workers = {'w1': {'worker_id': 'w1', 'name': 'gpubox', 'band': 'deadbeef', 'caps': ['shell.exec'],
                               'last_seen': 0}}
        self.stdout = '[main 9f8e7d6] Fix it\n'

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        return {'id': 'c-' + uuid.uuid4().hex[:6], 'from': target, 'ok': True,
                'result': {'code': 0, 'stdout': self.stdout}}


@asynccontextmanager
async def mcp_session(tmp_path, monkeypatch):
    from rook.band_mcp.server import build_server
    monkeypatch.setenv('ROOK_KNOWLEDGE', '1')
    band = CommitBand()
    mcp, store = build_server(band, public_url='https://mcp.example.com',
                              persist_path=str(tmp_path / 'tokens.json'),
                              static_token='static-token-0123456789abcdef', journal_path=str(tmp_path / 'journal.db'))
    token = store.mint_api_token('codex')
    app = mcp.streamable_http_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://localhost',
            headers={'Accept': 'application/json, text/event-stream', 'Authorization': 'Bearer ' + token['token'],
                     'X-Rook-Host': 'gpubox', 'X-Rook-Cwd': '/home/user/rook'}) as http:
        async def connect():
            http.headers.pop('mcp-session-id', None)
            r = await http.post('/mcp', json={'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-03-26', 'capabilities': {},
                'clientInfo': {'name': 'codex-mcp-client', 'version': '1'}}})
            http.headers['mcp-session-id'] = r.headers['mcp-session-id']
            await http.post('/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})

        async def tool(name, raw=False, **args):
            r = await http.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                              'params': {'name': name, 'arguments': args}})
            body = r.json() if r.headers['content-type'].startswith('application/json') else json.loads(
                next(l[5:] for l in r.text.splitlines() if l.startswith('data:')))
            text = body['result']['content'][0]['text']
            return text if raw else json.loads(text) if text.startswith('{') else text

        async def close():
            await http.delete('/mcp')
        await connect()
        yield SimpleNamespace(tool=tool, connect=connect, close=close, mcp=mcp, band=band, http=http)


@pytest.mark.asyncio
async def test_mcp_replies_carry_hygiene_and_a_closed_session_asks_for_a_handoff(tmp_path, monkeypatch):
    async with mcp_session(tmp_path, monkeypatch) as env:
        c = (await env.tool('rook_concept', action='create', request_id='c1', data={'title': 'Idea'}))['result']
        p = (await env.tool('rook_project', action='create', request_id='p1',
                            data={'title': 'Proj', 'parent': c['slug']}))['result']
        t = (await env.tool('rook_task', action='create', request_id='t1',
                            data={'title': 'Do it', 'parent': p['slug']}))['result']
        assert '_hygiene' not in await env.tool('rook_task', action='claim', id=t['slug'], request_id='cl1')
        reply = await env.tool('rook_call', cap='shell.exec', worker='gpubox', args={'cmd': 'git commit -am fix'})
        assert reply['_task'] == t['id']
        assert reply['_hygiene'][0]['kind'] == 'work_signal' and '9f8e7d6' in reply['_hygiene'][0]['say']
        text = await env.tool('rook_call', cap='shell.exec', worker='gpubox', args={'cmd': 'ls'}, text=True)
        assert '_hygiene' not in text  # delivered once
        got = (await env.tool('rook_task', action='get', id=t['id'], data={'links': 'all'}))['result']
        assert ('commit', '9f8e7d6', 'produced') in [(l['kind'], l['ref'], l['relation']) for l in got['links']]
        open_ = (await env.tool('rook_task', action='hygiene', data={'mine': True}))['result']['findings']
        assert [f['kind'] for f in open_] == ['work_signal']
        # The session closes with work since the last handoff.
        await env.close()
        await env.connect()
        hint = (await env.tool('rook_task', action='deck'))['_hygiene']
        assert hint[0]['kind'] == 'session_ended'
        deck = (await env.tool('rook_task', action='deck'))['result']['deck'][0]
        assert deck['in_progress'][0]['needs_hygiene']
        assert sorted(deck['in_progress'][0]['hygiene']) == ['session_ended', 'work_signal']


def _engine(env):
    return env.mcp._rook_knowledge.hygiene


def _finding(env, actor, text='Write it down', kind='work_signal'):
    """Open a finding for ``actor`` on a fresh task, by hand."""
    k = env.mcp._rook_knowledge
    s = k.store
    c = s.mutate('default', AGENT, rid(), 'create', {'kind': 'concept', 'title': 'C ' + rid()[:6]})
    p = s.mutate('default', AGENT, rid(), 'create', {'kind': 'project', 'title': 'P', 'parent': c['id']})
    t = s.mutate('default', AGENT, rid(), 'create', {'kind': 'task', 'title': 'T ' + rid()[:6], 'parent': p['id']})
    assert _engine(env).raise_('default', t['id'], kind, actor, text)
    return t


@pytest.mark.asyncio
async def test_hygiene_never_edits_worker_output_and_shares_the_rook_line(tmp_path, monkeypatch):
    async with mcp_session(tmp_path, monkeypatch) as env:
        me = (await env.tool('rook_whoami'))['actor']
        # package.json on stdout, text mode: stdout is untouched, the finding
        # is on the one [rook] line.
        pkg = '{"name": "pkg", "version": "1.0.0", "scripts": {"test": "jest"}}'
        env.band.stdout = pkg + '\n'
        _finding(env, me, 'first')
        text = await env.tool('rook_call', raw=True, cap='shell.exec', worker='gpubox',
                              args={'cmd': 'cat package.json'}, text=True)
        lines = text.split('\n')
        assert lines[0] == pkg and json.loads(lines[0]) == json.loads(pkg)
        rook_lines = [l for l in lines if l.startswith('[rook] ')]
        assert len(rook_lines) == 1
        assert json.loads(rook_lines[0][7:])['_hygiene'][0]['say'] == 'first'
        # The same with a reply that already has notices (_tips/_unread_chat...):
        # still one [rook] line, carrying both.
        _finding(env, me, 'second')
        text = await env.tool('rook_call', raw=True, cap='shell.exec', worker='gpubox',
                              args={'cmd': 'cat package.json'}, text=True, hint=True)
        rook_lines = [l for l in text.split('\n') if l.startswith('[rook] ')]
        assert len(rook_lines) == 1
        notice = json.loads(rook_lines[0][7:])
        assert notice['_hygiene'][0]['say'] == 'second' and len(notice) >= 2
        # A JSON array on stdout (compact JSON mode): _hygiene sits on the
        # envelope, the array is the worker's result unchanged.
        env.band.stdout = '[1, 2, {"a": 3}]'
        _finding(env, me, 'third')
        reply = await env.tool('rook_call', cap='shell.exec', worker='gpubox', args={'cmd': 'jq .'})
        assert reply['result']['stdout'] == '[1, 2, {"a": 3}]' and reply['_hygiene'][0]['say'] == 'third'
        text = await env.tool('rook_call', raw=True, cap='shell.exec', worker='gpubox', args={'cmd': 'jq .'},
                              text=True)
        assert text == '[1, 2, {"a": 3}]'  # nothing due: no [rook] line at all


@pytest.mark.asyncio
async def test_a_reply_that_cannot_carry_hygiene_leaves_it_queued(tmp_path, monkeypatch):
    async with mcp_session(tmp_path, monkeypatch) as env:
        me = (await env.tool('rook_whoami'))['actor']
        _finding(env, me, 'keep me')
        # rook_chat_rooms / rook_handoff_list reply with data (a list, other
        # people's text): never edited, the finding stays due.
        rooms = await env.tool('rook_chat_rooms', raw=True)
        assert '_hygiene' not in rooms and 'keep me' not in rooms
        assert '_hygiene' not in await env.tool('rook_handoff_list', raw=True)
        assert [h['say'] for h in _engine(env).due(me)] == ['keep me']
        # The next reply that can carry it does, once.
        assert (await env.tool('rook_task', action='deck'))['_hygiene'][0]['say'] == 'keep me'
        assert '_hygiene' not in await env.tool('rook_task', action='deck')
        assert _engine(env).due(me) == []


def test_add_notice_only_merges_into_a_json_object():
    from rook.band_mcp.envelope import add_notice
    out, ok = add_notice('{"ok":true,"result":{}}', '_hygiene', [{'kind': 'x'}])
    assert ok and json.loads(out)['_hygiene'] == [{'kind': 'x'}]
    for text in ('[1,2,3]', 'plain text', '{"name": "pkg"', 'hello\n[rook] {"_task":"t"}', '"{"'):
        assert add_notice(text, '_hygiene', [1]) == (text, False), text
    out, ok = add_notice('{"_hygiene":[0]}', '_hygiene', [1])  # never overwrite one already there
    assert not ok and out == '{"_hygiene":[0]}'


@pytest.mark.asyncio
async def test_session_end_still_fires_after_another_session_timed_out(tmp_path, monkeypatch):
    async with mcp_session(tmp_path, monkeypatch) as env:
        mgr = env.mcp._session_manager
        c = (await env.tool('rook_concept', action='create', request_id='c1', data={'title': 'Idea'}))['result']
        p = (await env.tool('rook_project', action='create', request_id='p1',
                            data={'title': 'Proj', 'parent': c['slug']}))['result']
        t = (await env.tool('rook_task', action='create', request_id='t1',
                            data={'title': 'Do it', 'parent': p['slug']}))['result']
        await env.tool('rook_task', action='claim', id=t['slug'], request_id='cl1')
        await env.tool('rook_call', cap='shell.exec', worker='gpubox', args={'cmd': 'ls'})
        # This session is abandoned (no DELETE) and times out.
        old = env.http.headers['mcp-session-id']
        await env.connect()
        await env.tool('rook_whoami')
        mgr._owners[old]['scope'].deadline = anyio.current_time()  # its idle deadline passes
        for _ in range(100):
            if old not in mgr._server_instances:
                break
            await asyncio.sleep(0.02)
        assert old not in mgr._server_instances, 'the abandoned session did not time out'
        # Now the live session closes: session_ended fires despite the timed-out one.
        await env.close()
        assert [f['kind'] for f in _engine(env).open([t['id']])] == ['session_ended']


# --- batch, band scope, dedupe, scan transactions --------------------------------

@pytest.mark.asyncio
async def test_batch_writes_run_the_hooks_per_op(w):
    svc = service(w)
    claim(w, w.task)
    cur = finish(w, w.task)
    out = await svc.dispatch('batch', 'default', 'task', data={'ops': [
        {'action': 'update', 'id': w.task['id'], 'data': {
            'revision': cur['revision'], 'patch': {'state': 'done', 'attrs': {'outcome': 'ok'}}}}]},
        request_id=rid(), actor=AGENT)
    assert out['failed'] == 0
    assert kinds(w, w.task['id']) == ['done_without_knowledge']
    page = w.create('knowledge', 'What we learned')
    out = await svc.dispatch('batch', 'default', 'task', data={'ops': [
        {'action': 'link', 'id': w.task['id'], 'data': {'kind': 'record', 'ref': page['id'], 'relation': 'produced'}}]},
        request_id=rid(), actor=AGENT)
    assert out['failed'] == 0 and kinds(w, w.task['id']) == []


class Bands:
    def bands(self, active_only=True):
        return [{'id': 'default', 'name': 'home', 'label': 'aaaa1111', 'is_primary': 1},
                {'id': 'other', 'name': 'lab', 'label': 'bbbb2222', 'is_primary': 0}]


@pytest.mark.asyncio
async def test_hygiene_action_and_deck_flags_are_band_scoped(w):
    svc = service(w)
    svc.enrollment = Bands()
    w.h.raise_('default', w.task['id'], 'work_signal', AGENT['id'], 'here')
    w.h.raise_('other', 't_' + 'a' * 32, 'work_signal', AGENT['id'], 'there')
    w.h.raise_('ghost', 't_' + 'b' * 32, 'work_signal', AGENT['id'], 'a band nobody has')
    said = lambda r: sorted(f['text'] for f in r['findings'])  # noqa: E731
    assert said(await svc.dispatch('hygiene', 'default')) == ['here']
    assert said(await svc.dispatch('hygiene', 'lab')) == ['there']
    assert said(await svc.dispatch('hygiene')) == ['here', 'there']  # the caller's bands only
    assert w.h.flags(['other']) == {'t_' + 'a' * 32: ['work_signal']}


def test_idle_claim_is_not_raised_after_the_band_side_nudge_and_vice_versa(w):
    from rook.band_mcp.hygiene import Hygiene, SYSTEM as LOOP

    claim(w, w.task)
    w.s.auto_link(AGENT, 'journal', 'j1')
    idle(w, w.task['id'], 45 * 60)
    # The band-side loop nudged this idle period: the scan stays quiet.
    c = w.s.active_claims()[0]
    w.s.mark_claim(c['id'], nudged=time.time(), actor=LOOP, note='hygiene_nudge', data={'action': 'woke_agent'})
    assert w.h.scan() == [] and kinds(w, w.task['id']) == []
    # A "marked_dirty" outcome reached nobody: the finding is still raised.
    t2 = w.create('task', 'Second', w.project['id'])
    claim(w, t2)
    w.s.auto_link(AGENT, 'journal', 'j2', task=t2['id'])
    idle(w, t2['id'], 45 * 60)
    c2 = [c for c in w.s.active_claims() if c['task'] == t2['id']][0]
    now = time.time()
    w.s.mark_claim(c2['id'], nudged=now, dirty=now, actor=LOOP, note='hygiene_dirty', data={})
    assert [f['kind'] for f in w.h.scan()] == ['idle_claim']
    # ...and once that finding was delivered, the loop does not nudge again.
    assert w.h.take(AGENT['id'])
    w.s.mark_claim(c2['id'], nudged=0)
    loop = Hygiene(SimpleNamespace(store=w.s, hygiene=w.h), SimpleNamespace(workers={}), None, lambda: '')
    assert asyncio.run(loop.tick()) == []
    # Undelivered: the loop nudges (here: marks dirty, the worker is offline).
    t3 = w.create('task', 'Third', w.project['id'])
    claim(w, t3)
    w.s.auto_link(AGENT, 'journal', 'j3', task=t3['id'])
    idle(w, t3['id'], 45 * 60)
    assert [d['task'] for d in asyncio.run(loop.tick())] == [t3['id']]


def test_scan_reads_in_a_read_transaction_and_writes_briefly(w, monkeypatch):
    claim(w, w.task)
    w.s.auto_link(AGENT, 'journal', 'j1')
    idle(w, w.task['id'], 5 * H)
    real = w.s.db
    opened = []

    def db(write=True):
        opened.append(write)
        return real(write)
    monkeypatch.setattr(w.s, 'db', db)
    assert [f['kind'] for f in w.h.scan()] == ['idle_claim']
    assert opened == [False, True]  # evaluate, then one short write
    opened.clear()
    assert w.h.scan() == []  # nothing new: no write transaction at all
    assert True not in opened
    with real(False) as raw:
        idx = {r['name'] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert 'events_actor' in idx


def test_after_knowledge_write_runs_only_for_pages_and_links(w):
    svc = service(w)
    calls = []
    real = w.h.after_knowledge_write
    w.h.after_knowledge_write = lambda *a, **k: calls.append(1) or real(*a, **k)
    asyncio.run(svc.dispatch('create', 'default', 'task', data={'title': 'A task', 'parent': w.project['id']},
                             request_id=rid(), actor=AGENT))
    asyncio.run(svc.dispatch('claim', 'default', 'task', w.task['id'], request_id=rid(), actor=AGENT))
    assert calls == []
    asyncio.run(svc.dispatch('create', 'default', 'knowledge', data={'title': 'A page', 'body': 'x'},
                             request_id=rid(), actor=AGENT))
    assert calls == [1]
    asyncio.run(svc.dispatch('link', 'default', 'task', w.task['id'], request_id=rid(), actor=AGENT,
                             data={'kind': 'url', 'ref': 'https://example.com', 'relation': 'touched'}))
    assert calls == [1, 1]
