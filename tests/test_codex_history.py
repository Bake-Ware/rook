import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from rook.worker.plugins import codex_history as codex
from rook.worker.plugins.claude_history import ClaudeHistoryPlugin

SID = '12345678-1234-1234-1234-123456789abc'


def rollout(root, sid=SID, events_only=False):
    directory = root / '2026/09/09'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'rollout-2026-09-09T00-00-00-{sid}.jsonl'
    rows = [
        ('session_meta', {'id': sid, 'cwd': str(root), 'git': {'branch': 'test'}}),
        ('event_msg', {'type': 'user_message', 'message': 'Design decision: use a queue'}),
        ('event_msg', {'type': 'agent_message', 'message': '```python\nprint(1)\n```'}),
        ('response_item', {'type': 'function_call', 'name': 'exec_command'}),
        ('response_item', {'type': 'reasoning', 'text': 'not a transcript message'}),
    ]
    if not events_only:
        rows += [
            ('response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Design decision: use a queue'}]}),
            ('response_item', {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '```python\nprint(1)\n```'}]}),
        ]
    path.write_text('\n'.join(json.dumps({'type': typ, 'timestamp': '2026-09-09T00:00:00Z', 'payload': payload}) for typ, payload in rows) + '\ninvalid-json\nnull\n')
    return path


def test_history_parity_and_normalization(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    root = tmp_path / 'sessions'
    rollout(root)
    p = codex.CodexHistoryPlugin()
    assert p.available()
    assert {k.split('.', 1)[1] for k in p.caps()} == {k.split('.', 1)[1] for k in ClaudeHistoryPlugin().caps()}
    listed = p._pull()
    assert listed['count'] == 1
    assert listed['sessions'][0]['session_id'] == SID
    assert listed['sessions'][0]['message_count'] == 2
    assert listed['sessions'][0]['git_branch'] == 'test'
    assert p._read(SID[:8])['count'] == 2
    assert p._search('queue')['count'] == 1
    assert p._analyze('tool_usage')['top_tools'] == [('exec_command', 1)]
    assert p._analyze('code_patterns')['total_blocks'] == 1
    assert p._analyze('architectural_decisions')['session_count'] == 1
    for fmt in ('markdown', 'json', 'html'):
        exported = p._export(SID, format=fmt)
        assert exported['ok'] and exported['session_id'] == SID
        assert 'not a transcript message' not in exported['content']
    assert not p._read('../../auth.json')['ok']


def test_event_only_and_ambiguous_prefix(tmp_path):
    rollout(tmp_path, events_only=True)
    p = codex.CodexHistoryPlugin()
    assert p._read(SID, path=str(tmp_path))['count'] == 2
    rollout(tmp_path, sid='12345678-0000-0000-0000-000000000000')
    assert not p._read('12345678', path=str(tmp_path))['ok']
    assert p._read(SID, path=str(tmp_path))['ok']


def test_resume_uses_argv_and_serializes_duplicate_requests(tmp_path, monkeypatch):
    rollout(tmp_path)
    monkeypatch.setattr(codex.shutil, 'which', lambda _: '/usr/bin/codex')
    async def call(cap, **args):
        if cap == 'proc.list':
            return {'sessions': [{'handle': 'h1', 'running': True}]}
        assert args['argv'] == ['/usr/bin/codex', 'resume', SID, '--no-alt-screen']
        assert args['cwd'] == str(tmp_path) and args['pty'] is True
        await asyncio.sleep(.01)
        return {'ok': True, 'handle': 'h1', 'pid': 1}
    registry = SimpleNamespace(has=lambda _: True, call=AsyncMock(side_effect=call))
    p = codex.CodexHistoryPlugin()
    p.bind_worker(SimpleNamespace(registry=registry))
    async def run():
        return await asyncio.gather(p._resume(SID, path=str(tmp_path)), p._resume(SID, path=str(tmp_path)))
    results = asyncio.run(run())
    assert sum(r['ok'] for r in results) == 1
    assert results[1]['error'] == 'session is already running'
    assert sum(c.args[0] == 'proc.start' for c in registry.call.call_args_list) == 1
