"""Builds tests/fixtures/knowledge_v2.db: a knowledge store written by the
pre-plugin code (``rook/knowledge`` at beta b0cd962, schema user_version 2).

The committed .db is the artifact the lossless-migration test opens; this
script documents how it was made. It was run against that older code, so
re-running it on newer code produces a database from *that* code instead
(which is fine for regenerating, but then it no longer proves the upgrade
from the pre-plugin layout). Generic content only: this repo is public.

    python tests/fixtures/make_knowledge_v2_db.py
"""
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from rook.knowledge.store import KnowledgeStore  # noqa: E402
from rook.knowledge.service import KnowledgeService  # noqa: E402

AGENT = {'id': 'claude.claudecode.worker-a@.home.user.proj', 'kind': 'agent', 'label': 'claude',
         'key_id': 'k_1', 'agent_id': 'agent_1', 'token': 'claude', 'client': 'claudecode',
         'host': 'worker-a', 'dir': '/home/user/proj'}
OTHER = {'id': 'codex.codex.gpu-box', 'kind': 'agent', 'label': 'codex', 'host': 'gpu-box', 'client': 'codex'}
HUMAN = {'id': 'human:operator', 'kind': 'human', 'label': 'Operator'}


class Enrollment:
    def bands(self, active_only=False):
        return [{'id': 'band0001', 'name': 'home', 'label': 'aaaa0001', 'is_primary': 1},
                {'id': 'band0002', 'name': 'lab', 'label': 'aaaa0002', 'is_primary': 0}]


async def build(path):
    s = KnowledgeStore(path)
    n = iter(range(1000))

    def m(band, actor, op, data):
        return s.mutate(band, actor, f'req-{next(n)}', op, data)

    b1, b2 = 'band0001', 'band0002'
    concept = m(b1, AGENT, 'create', {'kind': 'concept', 'title': 'Shared memory', 'body': 'Why we keep notes.'})
    project = m(b1, AGENT, 'create', {'kind': 'project', 'title': 'Knowledge service', 'parent': concept['id'],
                                      'body': 'See [[shared-memory]].'})
    task = m(b1, AGENT, 'create', {'kind': 'task', 'title': 'Restart test service', 'parent': project['id'],
                                   'attrs': {'criteria': ['Service responds'], 'workers': ['worker-a']}})
    done = m(b1, OTHER, 'create', {'kind': 'task', 'title': 'Write the runbook', 'parent': project['id']})
    folder = m(b1, AGENT, 'create', {'kind': 'knowledge', 'title': 'Runbooks', 'body': 'Folder page.'})
    fact = m(b1, AGENT, 'create', {'kind': 'knowledge', 'title': 'Service port', 'parent': folder['id'],
                                   'body': 'The test service listens on 8080. See [[runbooks]].',
                                   'attrs': {'knowledge_kind': 'fact', 'tags': ['ops']}})
    newer = m(b1, OTHER, 'create', {'kind': 'knowledge', 'title': 'Service port (moved)',
                                    'body': 'Now 8081.', 'attrs': {'knowledge_kind': 'fact',
                                                                   'supersedes': [fact['id']]}})
    m(b2, OTHER, 'create', {'kind': 'knowledge', 'title': 'Lab note', 'body': 'Lab band page, same slug space.'})
    m(b2, OTHER, 'create', {'kind': 'knowledge', 'title': 'Runbooks', 'body': 'Same slug, other band.'})
    # links: evidence, retraction, a url
    ev = m(b1, AGENT, 'link', {'id': newer['id'], 'kind': 'journal', 'ref': 'call-0001', 'note': 'measured'})
    bad = m(b1, AGENT, 'link', {'id': newer['id'], 'kind': 'url', 'ref': 'https://example.com/x'})
    m(b1, AGENT, 'retract', {'link': bad['link']['id'] if 'link' in bad else bad['id']})
    del ev
    # a person verifies, then an agent edit resets it
    cur = s.get(b1, newer['id'])
    m(b1, HUMAN, 'review', {'id': cur['id'], 'revision': cur['revision'], 'verdict': 'verified', 'note': 'checked'})
    # task lifecycle: claim, activity, paused with a handoff, done with evidence
    svc = KnowledgeService(path, lambda: None, Enrollment(), handoffs=lambda author, h: 'thread-0001')
    await svc.dispatch('claim', None, None, task['id'], request_id='claim-1', actor=AGENT)
    s.auto_link(AGENT, 'journal', 'call-0002', note='shell.exec')
    cur = s.get(b1, task['id'])
    await svc.dispatch('update', None, None, task['id'], request_id='pause-1', actor=AGENT, data={
        'revision': cur['revision'], 'patch': {'state': 'paused'},
        'handoff': {'goal': 'Restart', 'state': 'half done', 'next_steps': ['finish']}})
    await svc.dispatch('claim', None, None, done['id'], request_id='claim-2', actor=OTHER)
    m(b1, OTHER, 'link', {'id': done['id'], 'kind': 'commit', 'ref': 'abc1234'})
    cur = s.get(b1, done['id'])
    m(b1, OTHER, 'update', {'id': done['id'], 'revision': cur['revision'],
                            'patch': {'state': 'done', 'attrs': {'outcome': 'Runbook written.'}}})
    open_task = m(b1, AGENT, 'create', {'kind': 'task', 'title': 'Soak the new build', 'parent': project['id']})
    await svc.dispatch('claim', None, None, open_task['id'], request_id='claim-3', actor=AGENT,
                       data={'provider_session': 'sess-1'})
    claims = s.active_claims()
    if claims:
        s.mark_claim(claims[0]['id'], nudged=1.0, dirty=1.0, actor={'id': 'system:hygiene', 'kind': 'system',
                                                                  'label': 'hygiene'}, note='hygiene_dirty')
    s.cursor('hygiene', 'seq-42')
    with s.db() as db:
        db.execute('INSERT INTO embeddings VALUES(?,?,?,?)',
                   (fact['id'], 1, 'sentence-transformers/all-MiniLM-L6-v2', '[' + ','.join(['0.0'] * 383 + ['1.0']) + ']'))
    # Leave a plain single-file DB (no WAL sidecar) for the fixture.
    db = sqlite3.connect(path)
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    db.execute('PRAGMA journal_mode=DELETE')
    db.execute('VACUUM')
    db.close()


if __name__ == '__main__':
    out = HERE / 'knowledge_v2.db'
    for suffix in ('', '-wal', '-shm'):
        try:
            os.remove(str(out) + suffix)
        except FileNotFoundError:
            pass
    asyncio.run(build(out))
    print(out, out.stat().st_size, 'bytes')
