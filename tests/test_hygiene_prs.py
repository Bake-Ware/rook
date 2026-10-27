"""Pull requests behind tasks (rook/hub/plugins/knowledge/prs.py): done is
refused while a linked PR is open, a merged PR on in-progress work and red CI
become hygiene findings, the hub's own merge links the merge commit, and
GitHub being unreachable never blocks anything. Plus the hub's argument hint
on a ``bad args`` reply (rook/core/registry.py)."""
import asyncio
import json
import time
from types import SimpleNamespace
import uuid

import pytest

from rook.core.registry import CapabilityRegistry
from rook.hub.plugins.huddle.table import GitHubError
from rook.hub.plugins.knowledge.hygiene import HygieneEngine
from rook.hub.plugins.knowledge.prs import PullRequests, ci_summary, linked_prs
from rook.hub.plugins.knowledge.service import KnowledgeService
from rook.hub.plugins.knowledge.store import KnowledgeStore

AGENT = {'id': 'claude.claudecode.cachyrig', 'kind': 'agent', 'label': 'claude', 'host': 'cachyrig'}
PERSON = {'id': 'human:bake', 'kind': 'human', 'label': 'Bake'}
REPO = 'Bake-Ware/rook'
PR = 'https://github.com/Bake-Ware/rook/pull/{}'
HEAD, HEAD2, MERGE = 'a' * 40, 'c' * 40, 'f' * 40


def rid():
    return uuid.uuid4().hex


class FakeGitHub:
    """pr() and checks() as rook.hub.plugins.huddle.table.GitHub returns them."""

    def __init__(self):
        self.prs = {}
        self.checks_ = {}
        self.calls = []
        self.down = False

    def add(self, n, state='open', merged=False, head=HEAD, draft=False):
        self.prs[n] = {'number': n, 'state': state, 'merged': merged, 'draft': draft, 'head': head,
                       'merge_sha': MERGE if merged else '', 'url': PR.format(n)}

    def pr(self, repo, number):
        self.calls.append(('pr', repo, number))
        if self.down:
            raise GitHubError(0, 'Name or service not known')
        return dict(self.prs[number])

    def checks(self, repo, sha):
        self.calls.append(('checks', repo, sha))
        if self.down:
            raise GitHubError(0, 'Name or service not known')
        return {'runs': [{'name': n, 'status': 'completed' if c else 'in_progress', 'conclusion': c}
                         for n, c in self.checks_.get(sha, [])], 'statuses': [], 'state': None}


@pytest.fixture
def w(tmp_path):
    s = KnowledgeStore(tmp_path / 'knowledge.db')
    conf = {}
    engine = HygieneEngine(s, conf=lambda: conf)
    gh = FakeGitHub()
    clock = [time.time()]
    prs = PullRequests(s, engine, github=lambda: gh, clock=lambda: clock[0])

    def create(kind, title, parent=None, actor=AGENT):
        return s.mutate('default', actor, rid(), 'create',
                        {'kind': kind, 'title': title, 'body': title, 'parent': parent, 'attrs': {}})
    concept = create('concept', 'Hygiene')
    project = create('project', 'PR hygiene', concept['id'])
    task = create('task', 'Ship the PR check', project['id'])
    s.mutate('default', AGENT, rid(), 'claim', {'id': task['id']})

    def svc(actor=AGENT):
        k = KnowledgeService(s.path, lambda: {'actor': actor['id'], 'kind': actor['kind']})
        k.hygiene, k.prs = engine, prs
        return k
    return SimpleNamespace(s=s, h=engine, gh=gh, prs=prs, conf=conf, clock=clock, task=task,
                           project=project, create=create, svc=svc)


def link(w, ref, relation='evidence', kind='url', task=None):
    return w.s.mutate('default', AGENT, rid(), 'link', {'id': (task or w.task)['id'], 'kind': kind,
                                                        'ref': ref, 'relation': relation})


def done(w, actor=AGENT, task=None):
    task = task or w.task
    cur = w.s.get('default', task['id'])
    return asyncio.run(w.svc(actor).dispatch('update', 'default', 'task', task['id'], data={
        'revision': cur['revision'], 'patch': {'state': 'done', 'attrs': {'outcome': 'Shipped'}}},
        request_id=rid(), actor=actor))


def findings(w, kind=None):
    return [f for f in w.h.open([w.task['id']]) if kind is None or f['kind'] == kind]


def later(w, minutes=11):
    w.clock[0] += minutes * 60


# --- the done check -------------------------------------------------------------------

def test_done_is_refused_while_a_linked_pr_is_open_and_goes_through_once_merged(w):
    w.gh.add(242)
    w.gh.checks_[HEAD] = [('pytest', 'failure')]
    lid = link(w, PR.format(242))['id']
    with pytest.raises(ValueError) as e:
        done(w)
    msg = str(e.value)
    assert 'Bake-Ware/rook#242 is still open' in msg and 'CI failing: pytest: failure' in msg
    assert f'rook_task retract id={lid}' in msg and 'merge' in msg
    assert w.s.get('default', w.task['id'])['state'] == 'in_progress'
    w.gh.add(242, state='closed', merged=True)
    w.clock[0] += 120  # past the done check's cache
    out = done(w)
    assert out['state'] == 'done' and 'pr_check' not in out


def test_a_draft_or_a_second_open_pr_blocks_and_retracting_the_link_unblocks(w):
    w.gh.add(1, state='closed', merged=True)
    w.gh.add(2, draft=True)
    link(w, PR.format(1))
    lid = link(w, PR.format(2), relation='produced')['id']
    with pytest.raises(ValueError, match=r'rook#2 is still open \(a draft\)'):
        done(w)
    w.s.mutate('default', AGENT, rid(), 'retract', {'link': lid, 'note': 'not this task'})
    assert done(w)['state'] == 'done'


def test_only_the_tasks_own_pr_links_count(w):
    w.gh.add(9)
    link(w, PR.format(9), relation='mentions')
    link(w, PR.format(9), relation='blocked_by')
    link(w, 'repo@abc1234', kind='commit')
    with w.s.db(False) as db:
        assert linked_prs(w.s, db, w.task['id']) == []
    assert done(w)['state'] == 'done'
    assert w.gh.calls == []  # nothing to ask GitHub


def test_a_person_is_told_not_refused(w):
    w.gh.add(242)
    link(w, PR.format(242))
    out = done(w, actor=PERSON)
    assert out['state'] == 'done' and 'still open' in out['pr_check'][0]


def test_github_unreachable_never_blocks_done(w):
    w.gh.add(242)
    link(w, PR.format(242))
    w.gh.down = True
    out = done(w)
    assert out['state'] == 'done'
    assert 'could not check Bake-Ware/rook#242 on GitHub' in out['pr_check'][0]


def test_a_pr_closed_without_merging_is_a_note(w):
    w.gh.add(5, state='closed')
    link(w, PR.format(5))
    out = done(w)
    assert out['state'] == 'done'
    assert any('closed without merging' in n for n in out['pr_check'])
    assert any('no linked pull request merged' in n for n in out['pr_check'])


def test_batch_updates_get_the_same_check(w):
    w.gh.add(242)
    link(w, PR.format(242))
    cur = w.s.get('default', w.task['id'])
    out = asyncio.run(w.svc().dispatch('batch', 'default', 'task', None, data={'ops': [
        {'action': 'update', 'id': w.task['id'], 'data': {
            'revision': cur['revision'], 'patch': {'state': 'done', 'attrs': {'outcome': 'x'}}}}]},
        request_id=rid(), actor=AGENT))
    assert out['failed'] == 1 and 'still open' in out['results'][0]['error']


# --- the sweep ---------------------------------------------------------------------------

def test_merged_pr_on_in_progress_work_links_the_merge_commit_and_asks_for_done(w):
    w.gh.add(242, state='closed', merged=True)
    link(w, PR.format(242), relation='produced')
    raised = w.prs.sweep()
    assert [f['kind'] for f in raised] == ['pr_merged'] and raised[0]['actor'] == AGENT['id']
    hint = w.h.take(AGENT['id'])[0]
    assert hint['kind'] == 'pr_merged' and 'merge commit fffffff, linked as evidence' in hint['say']
    commits = [l for l in w.s.get('default', w.task['id'], auto_links=True)['links'] if l['kind'] == 'commit']
    assert [(c['ref'], c['relation']) for c in commits] == [(MERGE, 'evidence')]
    # Sweeping again adds nothing and the merged PR is not read again.
    later(w)
    calls = len(w.gh.calls)
    assert w.prs.sweep() == [] and len(w.gh.calls) == calls
    # The merge commit is the evidence done needs: just the outcome now.
    assert done(w)['state'] == 'done'
    assert findings(w, 'pr_merged') == []


def test_no_pr_merged_while_another_linked_pr_is_still_open(w):
    w.gh.add(1, state='closed', merged=True)
    w.gh.add(2)
    link(w, PR.format(1))
    link(w, PR.format(2))
    assert w.prs.sweep() == []
    w.gh.add(2, state='closed', merged=True)
    later(w)
    assert [f['kind'] for f in w.prs.sweep()] == ['pr_merged']


def test_red_ci_is_a_finding_that_follows_the_head_and_clears_when_green(w):
    w.gh.add(242)
    w.gh.checks_[HEAD] = [('pytest', 'failure'), ('docs', 'success')]
    link(w, PR.format(242))
    assert [f['kind'] for f in w.prs.sweep()] == ['pr_ci_failed']
    hint = w.h.take(AGENT['id'])[0]
    assert 'CI is red on Bake-Ware/rook#242 at aaaaaaa (pytest: failure)' in hint['say']
    assert 'gh pr checks 242' in hint['say']
    # A new head, still red: the same finding, rewritten and delivered again.
    w.gh.add(242, head=HEAD2)
    w.gh.checks_[HEAD2] = [('pytest', 'failure')]
    later(w)
    assert w.prs.sweep() == []
    again = w.h.take(AGENT['id'])
    assert again and 'at ccccccc' in again[0]['say']
    # Still running is not a finding; green resolves it.
    w.gh.checks_[HEAD2] = [('pytest', None)]
    later(w)
    w.prs.sweep()
    assert findings(w, 'pr_ci_failed') == []


def test_done_task_with_an_open_pr_is_flagged_until_reopened(w):
    w.gh.add(242)
    link(w, PR.format(242))
    done(w, actor=PERSON)  # a person may; the sweep still says so
    raised = w.prs.sweep()
    assert {(f['kind'], f['actor']) for f in raised} >= {('pr_open_done', PERSON['id'])}
    assert 'still open (not merged)' in raised[0]['text']
    cur = w.s.get('default', w.task['id'])
    asyncio.run(w.svc().dispatch('update', 'default', 'task', w.task['id'], data={
        'revision': cur['revision'], 'patch': {'state': 'in_progress'}}, request_id=rid(), actor=AGENT))
    assert findings(w, 'pr_open_done') == []


def test_an_unreachable_github_changes_nothing(w):
    w.gh.add(242)
    w.gh.checks_[HEAD] = [('pytest', 'failure')]
    link(w, PR.format(242))
    w.prs.sweep()
    assert findings(w, 'pr_ci_failed')
    w.gh.down = True
    later(w)
    assert w.prs.sweep() == []
    assert findings(w, 'pr_ci_failed')  # kept: nothing was read
    assert 'Name or service not known' in w.prs.last_error


def test_leaving_in_progress_or_the_link_going_resolves_the_findings(w):
    w.gh.add(242, state='closed', merged=True)
    lid = link(w, PR.format(242))['id']
    w.prs.sweep()
    assert findings(w, 'pr_merged')
    w.s.mutate('default', AGENT, rid(), 'retract', {'link': lid})
    later(w)
    w.prs.sweep()
    assert findings(w, 'pr_merged') == []


def test_due_follows_the_setting(w):
    assert w.prs.due()
    w.prs.sweep()
    assert not w.prs.due()
    later(w)
    assert w.prs.due()
    w.conf['hygiene_github_minutes'] = 0
    assert not w.prs.due() and w.prs.sweep() == []


def test_the_hubs_own_merge_links_the_commit_and_tells_the_claimant(w):
    link(w, PR.format(242))
    other = w.create('task', 'Unrelated', w.project['id'])
    link(w, PR.format(2420), task=other)  # #2420 is not #242
    assert w.prs.on_merged('bake-ware/rook', 242, MERGE, HEAD, 'merged by the hub, vote x') == [w.task['id']]
    commits = [l for l in w.s.get('default', w.task['id'], auto_links=True)['links'] if l['kind'] == 'commit']
    assert commits[0]['ref'] == MERGE and 'vote x' in commits[0]['note']
    assert [f['kind'] for f in findings(w)] == ['pr_merged']
    assert w.gh.calls == []  # no GitHub read: the hub knows it merged
    assert not [l for l in w.s.get('default', other['id'], auto_links=True)['links'] if l['kind'] == 'commit']
    assert done(w)['state'] == 'done'  # cached as merged: no read, no refusal
    assert w.gh.calls == []


def test_ci_summary():
    assert ci_summary({'runs': [], 'statuses': []})['state'] == 'none'
    s = ci_summary({'runs': [{'name': 'a', 'status': 'completed', 'conclusion': 'skipped'}],
                    'statuses': [{'context': 'b', 'state': 'pending'}]})
    assert s['state'] == 'running' and s['running'] == ['b']
    s = ci_summary({'runs': [{'name': 'a', 'status': 'in_progress', 'conclusion': None}],
                    'statuses': [{'context': 'b', 'state': 'error'}]})
    assert s['state'] == 'failing' and s['failed'] == ['b: error']


# --- the plugin ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plugin_wires_the_sweep_and_reads_the_hub_credential_once(tmp_path, monkeypatch):
    from rook.hub.node import HubNode
    monkeypatch.setenv('ROOK_KNOWLEDGE', '1')
    n = HubNode(str(tmp_path), entry_points=False, build_version='1.test.node')
    kb = n.plugin('knowledge')
    assert kb.service.prs.engine.cfg('hygiene_github_minutes') == 10
    reads = []

    class Vault:
        def get(self, name, actor, via='get', task=None):
            reads.append((name, actor))
            return 'ghp_' + 'T' * 36
    real = n.plugin
    monkeypatch.setattr(n, 'plugin', lambda name: SimpleNamespace(settings={'vote_github_secret': 'hub-gh'})
                        if name == 'huddle' else real(name))
    monkeypatch.setattr(n, '_vault', Vault(), raising=False)
    kb.bind_host(n)
    client = kb._github()
    assert client._token() == 'ghp_' + 'T' * 36 and client._token() and reads == [('hub-gh', 'system:hygiene')]
    # The tick runs the sweep when it is due.
    gh = FakeGitHub()
    gh.add(7, state='closed', merged=True)
    kb.service.prs.github = lambda: gh
    s = kb.service.store
    c = s.mutate('default', AGENT, rid(), 'create', {'kind': 'concept', 'title': 'C'})
    p = s.mutate('default', AGENT, rid(), 'create', {'kind': 'project', 'title': 'P', 'parent': c['id']})
    t = s.mutate('default', AGENT, rid(), 'create', {'kind': 'task', 'title': 'T', 'parent': p['id']})
    s.mutate('default', AGENT, rid(), 'claim', {'id': t['id']})
    s.mutate('default', AGENT, rid(), 'link', {'id': t['id'], 'kind': 'url', 'ref': PR.format(7)})
    raised = await kb.hygiene_tick()
    assert 'pr_merged' in {f['kind'] for f in raised}
    assert await kb.hygiene_tick() == []  # not due again yet
    assert kb.pr_merged('Bake-Ware/rook', 7, MERGE) == [t['id']]


# --- bad args: say what the cap takes --------------------------------------------------------

def test_bad_args_say_what_the_cap_takes_and_the_closest_name():
    r = CapabilityRegistry()

    async def read(room: str, since_seq: int = 0):
        return {}

    async def boom(x: int = 0):
        raise TypeError('inside the cap')
    r.register('huddle.read', read)
    r.register('t.boom', boom)
    hint = r.arg_hint('huddle.read', {'huddle': 'h', 'since': 3})
    assert hint == " (huddle.read takes: room, since_seq?; did you mean 'since_seq' for 'since'?)"
    assert r.arg_hint('huddle.read', {'room': 'h'}) == ''
    assert r.arg_hint('t.boom', {'x': 1}) == ''  # a TypeError from inside is left alone
    assert r.arg_hint('huddle.read', {'room': 'h', 'limit': 5}) == ''  # stripped by call()

    from rook.core.host import PluginHost
    host = PluginHost.__new__(PluginHost)
    host.registry = r
    out = asyncio.run(host.dispatch('huddle.read', {'since': 3}))
    assert out['ok'] is False and out['error'].startswith('bad args: ')
    assert "did you mean 'since_seq' for 'since'?" in out['error']
    # Python's own did-you-mean is not repeated.
    r.register('t.read', read)
    out = asyncio.run(host.dispatch('t.read', {'rooom': 'h'}))
    # (Python 3.13+ suggests 'room' itself; older ones get ours.)
    assert out['error'].count('id you mean') == 1 and '(t.read takes: room, since_seq?' in out['error']
    out = asyncio.run(host.dispatch('t.boom', {}))
    assert out['error'] == 'bad args: inside the cap'


def test_hint_text_is_plain_json():
    assert json.dumps(CapabilityRegistry().arg_hint('nope', {})) == '""'


def test_linking_a_pr_says_keep_it_in_progress_until_it_merges(w):
    assert w.h.on_link(AGENT, w.task['id'], 'url', PR.format(242), 'produced')
    say = w.h.take(AGENT['id'])[0]['say']
    assert 'Keep the task in_progress until the PR merges' in say and 'If the work is finished' not in say


def test_a_pr_on_a_done_task_and_an_in_progress_one_still_gets_its_checks(w):
    other = w.create('task', 'Second task', w.project['id'])
    w.s.mutate('default', AGENT, rid(), 'claim', {'id': other['id']})
    w.gh.add(242)
    w.gh.checks_[HEAD] = [('pytest', 'failure')]
    link(w, PR.format(242))
    link(w, PR.format(242), task=other)
    done(w, actor=PERSON)
    raised = w.prs.sweep()
    assert {f['kind'] for f in raised} == {'pr_open_done', 'pr_ci_failed'}
