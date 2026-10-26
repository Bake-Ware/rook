"""Whiteboard (agent sessions design item 13, rook/hub/plugins/knowledge/whiteboard.py):
a living page per claim that the hub keeps from the session's calls, the agent's
jots on it, the brief carrying it, and the board standing in as the handoff."""
import json
import time
import uuid
from types import SimpleNamespace

import pytest

from rook.band_mcp import brief
from rook.band_mcp.hygiene import Hygiene
from rook.hub.plugins.knowledge import whiteboard
from rook.hub.plugins.knowledge.service import KnowledgeService
from rook.hub.plugins.knowledge.store import KnowledgeStore
from test_token_envelope import hub

AGENT = {'id': 'claude.claudecode.laptop', 'kind': 'agent', 'label': 'claude', 'host': 'laptop'}
OTHER = {'id': 'codex.codex.gpubox', 'kind': 'agent', 'label': 'codex', 'host': 'gpubox'}


def rid():
    return uuid.uuid4().hex


@pytest.fixture
def w(tmp_path):
    s = KnowledgeStore(tmp_path / 'knowledge.db')

    def create(kind, title, parent=None, actor=AGENT):
        return s.mutate('default', actor, rid(), 'create',
                        {'kind': kind, 'title': title, 'body': title, 'parent': parent, 'attrs': {}})
    concept = create('concept', 'C')
    project = create('project', 'P', concept['id'])
    saved = []

    def handoffs(author, h):
        saved.append((author, h))
        return h.get('thread_id') or f'th{len(saved)}'
    svc = KnowledgeService(s.path, lambda: {'actor': AGENT['id'], 'kind': 'agent'}, handoffs=handoffs)
    return SimpleNamespace(s=svc.store, svc=svc, create=create, project=project, saved=saved,
                           task=lambda title='Ship it': create('task', title, project['id']))


def claim(w, task, actor=AGENT):
    return w.s.mutate('default', actor, rid(), 'claim', {'id': task['id']})


def call(w, why, worker='kaiju', cap='shell.exec', ok=True, actor=AGENT, task=None):
    return w.s.auto_link(actor, 'journal', rid(), note=f'{cap} on {worker}', task=task,
                         board={'text': why, 'worker': worker, 'cap': cap, 'ok': ok})


def board(w, task, actor=AGENT, **kw):
    out = w.s.whiteboards('default', task['id'], actor['id'], **kw)
    return out['boards'][0]


# -- built from calls ----------------------------------------------------------------

def test_calls_become_lines_with_their_why_and_repeats_collapse(w):
    t = w.task()
    claim(w, t)
    assert call(w, 'check the build') == t['id']
    call(w, 'check the build', cap='file.read')
    call(w, 'check the build', ok=False)
    call(w, 'restart the service', worker='bakenetca')
    b = board(w, t)
    assert [x['what'] for x in b['work']] == ['check the build', 'restart the service']
    first = b['work'][0]
    assert first['n'] == 3 and first['cap'] == 'shell.exec, file.read' and first['ok'] is False
    assert b['machines'] == {'kaiju': 3, 'bakenetca': 1}
    assert b['yours'] and b['actor'] == AGENT['id']


def test_no_claim_no_board_and_other_links_have_their_own_lines(w):
    t = w.task()
    assert call(w, 'nothing claimed') is None
    claim(w, t)
    w.s.auto_link(AGENT, 'secret', 'github-pat', note='used in shell.exec on kaiju')
    w.s.auto_link(AGENT, 'console', 'room1', note='install cuda',
                  board={'text': 'opened console: install cuda', 'worker': 'kaiju'})
    w.s.board_console('room1', 'closed console install cuda: driver 560 works')
    work = board(w, t)['work']
    assert [(x.get('kind'), x['what']) for x in work] == [
        ('secret', 'used secret github-pat (used in shell.exec on kaiju)'),
        ('console', 'opened console: install cuda'),
        ('console', 'closed console install cuda: driver 560 works')]


def test_old_work_lines_are_pruned_but_jots_stay(w, monkeypatch):
    monkeypatch.setattr(whiteboard, 'MAX_ROWS', 3)
    t = w.task()
    claim(w, t)
    w.s.mutate('default', AGENT, rid(), 'jot', {'id': t['id'], 'decision': 'use sqlite'})
    for i in range(6):
        call(w, f'step {i}')
    b = board(w, t)
    assert [x['what'] for x in b['work']] == ['step 3', 'step 4', 'step 5']
    assert b['decisions'][0]['text'] == 'use sqlite'


# -- per session, not per actor ---------------------------------------------------------

def test_a_sessions_calls_go_to_its_own_claim_not_the_actors_newest(w):
    a, b = w.task('A'), w.task('B')
    sa, sb = {}, {}
    tok = whiteboard.SESSION.set(sa)
    try:
        claim(w, a)
        whiteboard.bind(a['id'])
    finally:
        whiteboard.SESSION.reset(tok)
    tok = whiteboard.SESSION.set(sb)
    try:
        claim(w, b)  # the same actor's newer claim, from another session
        whiteboard.bind(b['id'])
    finally:
        whiteboard.SESSION.reset(tok)
    tok = whiteboard.SESSION.set(sa)
    try:
        assert call(w, 'work on A') == a['id']
    finally:
        whiteboard.SESSION.reset(tok)
    assert call(w, 'no session: newest claim') == b['id']
    assert [x['what'] for x in board(w, a)['work']] == ['work on A']


# -- jots --------------------------------------------------------------------------

def test_jot_adds_decisions_and_open_items_and_done_closes_them(w):
    t = w.task()
    with pytest.raises(ValueError, match='claim the task first'):
        w.s.mutate('default', AGENT, rid(), 'jot', {'id': t['id'], 'note': 'x'})
    claim(w, t)
    got = w.s.mutate('default', AGENT, rid(), 'jot', {
        'id': t['id'], 'goal': 'ship the whiteboard', 'decision': ['per claim, not per MCP session'],
        'open': ['write tests', 'open PR']})
    assert [o['text'] for o in got['open']] == ['write tests', 'open PR']
    seq = got['open'][0]['seq']
    done = w.s.mutate('default', AGENT, rid(), 'jot', {'id': t['id'], 'done': [seq, 'Open PR', 'merged']})
    assert len(done['closed']) == 2 and 'open' not in done
    with pytest.raises(ValueError, match='no open item 999'):
        w.s.mutate('default', AGENT, rid(), 'jot', {'id': t['id'], 'done': 999})
    with pytest.raises(ValueError, match='at least one of'):
        w.s.mutate('default', AGENT, rid(), 'jot', {'id': t['id']})
    b = board(w, t)
    assert b['goal'] == 'ship the whiteboard'
    assert [d['text'] for d in b['done']] == ['write tests', 'open PR', 'merged']
    assert 'open' not in b


@pytest.mark.asyncio
async def test_service_reads_and_jots_your_own_board_without_an_id(w):
    nothing = await w.svc.dispatch('whiteboard', actor=AGENT)
    assert nothing['boards'] == [] and 'Claim one' in nothing['hint']
    t = w.task()
    await w.svc.dispatch('claim', rid=t['id'], request_id=rid(), actor=AGENT)
    await w.svc.dispatch('jot', data={'decision': 'keep it small'}, request_id=rid(), actor=AGENT)
    got = await w.svc.dispatch('whiteboard', actor=AGENT)
    assert got['task']['id'] == t['id'] and got['boards'][0]['decisions'][0]['text'] == 'keep it small'
    other = await w.svc.dispatch('whiteboard', rid=t['id'], actor=OTHER)  # a reader with no claim
    assert other['boards'][0]['actor'] == AGENT['id'] and 'yours' not in other['boards'][0]


# -- the board is the handoff ------------------------------------------------------------

@pytest.mark.asyncio
async def test_release_without_a_handoff_saves_the_board_as_one(w):
    t = w.task()
    await w.svc.dispatch('claim', rid=t['id'], request_id=rid(), actor=AGENT)
    with pytest.raises(ValueError, match='needs a handoff'):
        await w.svc.dispatch('release', rid=t['id'], request_id=rid(), actor=AGENT)  # nothing on the board
    call(w, 'run the tests')
    await w.svc.dispatch('jot', data={'open': 'open the PR', 'decision': 'ship behind no flag'},
                         request_id=rid(), actor=AGENT)
    out = await w.svc.dispatch('release', rid=t['id'], request_id=rid(), actor=AGENT)
    assert out['task'] == t['id']
    (author, h), = w.saved
    assert author == AGENT['id'] and h['goal'] == 'Ship it'
    assert 'run the tests on kaiju' in h['state'] and h['decisions'] == ['ship behind no flag']
    assert h['next_steps'][0] == 'open the PR' and 'whiteboard' in h['next_steps'][-1]


def test_board_handoffs_only_with_news_since_the_last_handoff(w):
    t = w.task()
    claim(w, t)
    assert w.svc.board_handoffs(AGENT['id']) == []  # empty board
    call(w, 'deploy to the test hub')
    assert w.svc.board_handoffs(AGENT['id']) == [t['id']]
    assert w.svc.board_handoffs(AGENT['id']) == []  # nothing new since
    assert 'deploy to the test hub' in w.saved[0][1]['state']
    with w.s.db(False) as db:
        link = db.execute("SELECT * FROM links WHERE kind='handoff'").fetchone()
        c = db.execute('SELECT last_active FROM claims').fetchone()
    assert link['note'] == 'automatic handoff from the whiteboard' and c['last_active'] < link['ts']
    call(w, 'one more thing')
    assert w.svc.board_handoffs(AGENT['id'], tasks=[t['id']]) == [t['id']]
    assert w.saved[1][1]['thread_id'] == 'th1'  # the task's thread continues


@pytest.mark.asyncio
async def test_idle_hygiene_saves_the_board_instead_of_asking(w):
    t = w.task()
    claim(w, t)
    call(w, 'long build')
    with w.s.db() as db:
        db.execute('UPDATE claims SET last_active=?', (time.time() - 3600,))
    loop = Hygiene(w.svc, SimpleNamespace(workers={}), chat=None, prompt=lambda: '', idle=60)
    (done,) = await loop.tick()
    assert done['action'] == 'whiteboard_handoff' and len(w.saved) == 1


# -- the brief carries it ---------------------------------------------------------------

def test_brief_summary_is_compact():
    b = {'goal': 'g', 'decisions': [{'text': f'd{i}'} for i in range(12)], 'open': [{'text': 'o'}],
         'work': [{'what': f'w{i}', 'worker': 'kaiju', 'ts': 0} for i in range(9)] + [
             {'what': 'x', 'kind': 'console', 'ok': False, 'n': 2, 'ts': 0}],
         'machines': {'kaiju': 9}}
    out = whiteboard.brief(b)
    assert out['goal'] == 'g' and len(out['decisions']) == whiteboard.BRIEF_LIST
    assert out['recent'][-1] == 'console: x (x2) [failed]' and len(out['recent']) == whiteboard.BRIEF_WORK
    assert whiteboard.brief({}) is None


def _j(text):
    return json.loads(text)


@pytest.mark.asyncio
async def test_over_mcp_calls_fill_the_sessions_board_and_the_brief_brings_it_back(tmp_path, monkeypatch):
    async with hub(tmp_path, monkeypatch, knowledge=True) as env:
        k = env.mcp._rook_knowledge
        me = {"id": "static", "kind": "shared", "label": "static"}
        c = await k.dispatch("create", kind="concept", data={"title": "C"}, request_id="c", actor=me)
        p = await k.dispatch("create", kind="project", data={"title": "P", "parent": c["id"]},
                             request_id="p", actor=me)
        a = await k.dispatch("create", kind="task", data={"title": "A", "parent": p["id"]},
                             request_id="a", actor=me)
        b = await k.dispatch("create", kind="task", data={"title": "B", "parent": p["id"]},
                             request_id="b", actor=me)
        _, tool_a = await env.connect()
        _, tool_b = await env.connect()  # a second agent on the same token
        receipt = _j((await tool_a("rook_task", action="claim", id=a["id"], request_id="ca"))[0])["_brief"]["receipt"]
        await tool_b("rook_task", action="claim", id=b["id"], request_id="cb")
        await tool_a("rook_call", why="look at the logs", cap="info.host", worker="worker-a", receipt=receipt)
        await tool_a("rook_task", action="jot", data={"open": "fix the parser"}, request_id="j1",
                     receipt=receipt)

        got = _j((await tool_a("rook_task", action="whiteboard", receipt=receipt))[0])["result"]
        assert got["task"]["id"] == a["id"]  # session A's claim, not B's newer one
        mine = got["boards"][0]
        assert mine["work"][0]["what"] == "look at the logs" and mine["work"][0]["worker"] == "worker-a"
        assert mine["open"][0]["text"] == "fix the parser"
        theirs = _j((await tool_b("rook_task", action="whiteboard"))[0])["result"]
        assert theirs["task"]["id"] == b["id"] and "work" not in theirs["boards"][0]

        # Compaction: session A calls without its receipt and gets its board back.
        r = _j((await tool_a("rook_call", why="where was I", cap="info.host", worker="worker-a"))[0])
        assert r["_brief"]["why"] == brief.MISSING
        lead = r["_brief"]["claimed"][0]
        assert lead["id"] == a["id"]
        assert lead["whiteboard"]["open"] == ["fix the parser"]
        assert lead["whiteboard"]["recent"][0] == "look at the logs on worker-a"


@pytest.mark.asyncio
async def test_stopping_by_update_uses_the_board_too_even_from_the_dashboard(w):
    t = w.task()
    await w.svc.dispatch('claim', rid=t['id'], request_id=rid(), actor=AGENT)
    call(w, 'bisect the regression')
    person = {'id': 'human:bake', 'kind': 'human', 'label': 'Bake'}
    rec = w.s.get('default', t['id'])
    out = await w.svc.dispatch('update', rid=t['id'], request_id=rid(), actor=person,
                               data={'revision': rec['revision'], 'patch': {'state': 'paused'}})
    assert out['state'] == 'paused'
    (author, h), = w.saved
    assert author == person['id'] and 'set paused' in h['state'] and 'bisect the regression' in h['state']


@pytest.mark.asyncio
async def test_a_reconnected_session_points_itself_again_by_jotting(w):
    a, b = w.task('A'), w.task('B')
    await w.svc.dispatch('claim', rid=a['id'], request_id=rid(), actor=AGENT)
    await w.svc.dispatch('claim', rid=b['id'], request_id=rid(), actor=AGENT)  # newest
    tok = whiteboard.SESSION.set({})  # a new MCP session after the idle timeout
    try:
        assert call(w, 'before') == b['id']
        await w.svc.dispatch('jot', rid=a['id'], data={'note': 'back on A'}, request_id=rid(), actor=AGENT)
        assert call(w, 'after') == a['id']
        await w.svc.dispatch('note', rid=w.project['id'], data={'text': 'x'}, request_id=rid(), actor=AGENT)
        assert call(w, 'still A') == a['id']  # a note on something not claimed doesn't move it
    finally:
        whiteboard.SESSION.reset(tok)


@pytest.mark.asyncio
async def test_closing_the_session_saves_the_board_as_the_handoff(tmp_path, monkeypatch):
    async with hub(tmp_path, monkeypatch, knowledge=True) as env:
        k = env.mcp._rook_knowledge
        me = {"id": "static", "kind": "shared", "label": "static"}
        c = await k.dispatch("create", kind="concept", data={"title": "C"}, request_id="c", actor=me)
        p = await k.dispatch("create", kind="project", data={"title": "P", "parent": c["id"]},
                             request_id="p", actor=me)
        t = await k.dispatch("create", kind="task", data={"title": "T", "parent": p["id"]},
                             request_id="t", actor=me)
        _, tool = await env.connect()
        await tool("rook_task", action="claim", id=t["id"], request_id="c1")
        await tool("rook_call", why="rebuild the index", cap="info.host", worker="worker-a")
        manager = env.mcp._session_manager
        (sid,) = list(manager._server_instances)
        manager.on_session_end(sid)  # what an HTTP DELETE from the client runs
        got = await k.dispatch("get", rid=t["id"], actor=me)
        (link,) = [l for l in got["links"] if l["kind"] == "handoff"]
        assert link["note"] == "automatic handoff from the whiteboard"
        h = json.loads((await tool("rook_handoff_get", thread_id=link["ref"]))[0])["current"]
        assert h["goal"] == "T" and "rebuild the index on worker-a" in h["state"]
        assert "session ended" in h["state"]
