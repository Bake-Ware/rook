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


def test_history_pagination_and_activity(tmp_path):
    import os
    import time
    path = rollout(tmp_path)
    plugin = codex.CodexHistoryPlugin()
    first = plugin._read(SID, path=str(tmp_path), max_messages=1)
    second = plugin._read(SID, path=str(tmp_path), max_messages=1, offset=first['next_offset'])
    assert first['messages'][0]['role'] == 'user'
    assert second['messages'][0]['role'] == 'assistant'
    assert not second.get('truncated')
    assert first['activity'] == 'working'
    os.utime(path, (time.time()-300, time.time()-300))
    assert plugin._read(SID, path=str(tmp_path))['activity'] == 'pending'
    with path.open('a') as stream:
        stream.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'task_complete'}})+'\n')
    assert plugin._read(SID, path=str(tmp_path))['activity'] == 'ready'
    rollout(tmp_path, sid='12345678-0000-0000-0000-000000000000')
    page1 = plugin._pull(path=str(tmp_path), limit=1)
    page2 = plugin._pull(path=str(tmp_path), limit=1, offset=1)
    assert page1['total'] == 2
    assert page1['sessions'][0]['session_id'] != page2['sessions'][0]['session_id']


def test_default_history_includes_archived_codex_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    rollout(tmp_path / 'sessions')
    archived = '12345678-0000-0000-0000-000000000000'
    rollout(tmp_path / 'archived_sessions', sid=archived)
    plugin = codex.CodexHistoryPlugin()
    assert plugin._pull()['total'] == 2
    assert plugin._read(archived)['ok']


def test_claude_activity_user_input_and_completion(tmp_path):
    path = tmp_path / 'session.jsonl'
    plugin = ClaudeHistoryPlugin()
    path.write_text(json.dumps({'type': 'assistant', 'message': {'stop_reason': 'tool_use',
        'content': [{'type': 'tool_use', 'name': 'AskUserQuestion'}]}}) + '\n')
    assert plugin._activity(path) == 'ready'
    path.write_text(json.dumps({'type': 'assistant', 'message': {'stop_reason': 'tool_use', 'content': None}}))
    assert plugin._activity(path) == 'working'
    path.write_text(json.dumps({'type': 'assistant', 'message': {'stop_reason': 'end_turn'}}))
    assert plugin._activity(path) == 'ready'


def test_bounded_pages_preserve_large_messages_and_unicode(tmp_path):
    content = 'Large 🦉 output\n' * 3000
    rows = [dict(type='user', message={'content': content}),
            dict(type='assistant', message={'content': 'done', 'stop_reason': 'end_turn'})]
    path = tmp_path / 'session.jsonl'
    path.write_text('\n'.join(json.dumps(row) for row in rows))
    plugin = ClaudeHistoryPlugin()
    rebuilt = ['', '']
    offset = content_offset = 0
    calls = 0
    while True:
        page = plugin._read_page('session', path=str(tmp_path), offset=offset, content_offset=content_offset)
        assert len(json.dumps(page).encode()) < 80000
        for message in page['messages']:
            index = message['index']
            assert len(rebuilt[index]) == message['content_offset']
            rebuilt[index] += message['content']
        calls += 1
        if not page.get('truncated'):
            assert page['activity'] == 'ready'
            break
        offset, content_offset = page['next_offset'], page['next_content_offset']
    assert calls > 1
    assert rebuilt == [content, 'done']


def test_active_external_session_blocks_resume_and_updates_metadata(tmp_path, monkeypatch):
    rollout(tmp_path)
    plugin = codex.CodexHistoryPlugin()
    registry = SimpleNamespace(has=lambda _: True, call=AsyncMock())
    plugin.bind_worker(SimpleNamespace(registry=registry))
    monkeypatch.setattr(plugin, '_is_active', lambda *args: True)
    assert plugin._pull(path=str(tmp_path))['sessions'][0]['active'] is True
    result = asyncio.run(plugin._resume(SID, path=str(tmp_path)))
    assert not result['ok'] and 'already active' in result['error']
    registry.call.assert_not_called()


def test_full_read_snapshot_is_stable_unicode_and_session_bound(tmp_path):
    path = rollout(tmp_path)
    with path.open('a') as f:
        f.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': '🦉' * 14000}]}}) + '\n')
    plugin = codex.CodexHistoryPlugin()
    page = plugin._read_page(SID, path=str(tmp_path), snapshot='')
    token = page['snapshot']
    # Log changes after opening must not shift pages or mix two versions.
    path.write_text('')
    messages = {}
    while True:
        assert sum(len(m['content']) for m in page['messages']) <= 6000
        for fragment in page['messages']:
            text = messages.setdefault(fragment['index'], '')
            assert len(text) == fragment['content_offset']
            messages[fragment['index']] = text + fragment['content']
        if not page['truncated']:
            break
        page = plugin._read_page(SID, path=str(tmp_path), snapshot=token,
                                 offset=page['next_offset'], content_offset=page['next_content_offset'])
    assert messages[max(messages)] == '🦉' * 14000
    assert not plugin._read_page('another-session', path=str(tmp_path), snapshot=token)['ok']


def test_follow_checks_version_and_pages_only_new_tail(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    path = rollout(tmp_path / 'sessions')
    plugin = codex.CodexHistoryPlugin()
    first = plugin._follow(SID, offset=1)
    assert first['replace_from'] == 1 and len(first['messages']) == 1
    same = plugin._follow(SID, offset=1, version=first['version'])
    assert same['unchanged'] and 'messages' not in same
    with path.open('a') as stream:
        stream.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': 'New live text 💌' * 1000}]}})+'\n')
    tail = plugin._follow(SID, offset=1, version=first['version'])
    assert tail['replace_from'] == 1 and tail['truncated']
    assert sum(len(m['content']) for m in tail['messages']) <= 6000
    chunks = [m['content'] for m in tail['messages'] if m['index'] == 2]
    while tail['truncated']:
        tail = plugin._read_snapshot(SID, snapshot=tail['snapshot'], offset=tail['next_offset'], content_offset=tail['next_content_offset'])
        chunks += [m['content'] for m in tail['messages'] if m['index'] == 2]
    assert ''.join(chunks) == 'New live text 💌' * 1000
    version = plugin._follow(SID)['version']
    path.write_text('')
    reset = plugin._follow(SID, offset=2, version=version)
    assert reset['replace_from'] == 0 and reset['messages'] == []


def test_title_skips_injected_setup_messages(tmp_path):
    path = rollout(tmp_path)
    records = [json.loads(line) for line in path.read_text().splitlines() if line.startswith('{')]
    setup = [{'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
        'content': [{'type': 'input_text', 'text': text}]}} for text in (
            '# AGENTS.md instructions for /project\n<INSTRUCTIONS>Rules</INSTRUCTIONS>',
            '<environment_context>\n<cwd>/project</cwd>\n</environment_context>')]
    path.write_text('\n'.join(json.dumps(row) for row in setup+records))
    plugin = codex.CodexHistoryPlugin()
    assert plugin._pull(path=str(tmp_path))['sessions'][0]['title'] == 'Design decision: use a queue'
    assert plugin._search('queue', path=str(tmp_path))['hits'][0]['title'] == 'Design decision: use a queue'
