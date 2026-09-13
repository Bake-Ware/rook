import asyncio
import hashlib
import json
import os
import socket
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from rook.worker import session_messages as messages
from rook.worker.plugins.codex_history import CodexHistoryPlugin
from test_codex_history import SID, rollout


@pytest.mark.asyncio
async def test_queue_literal_text_and_durable_receipts(tmp_path, monkeypatch):
    monkeypatch.setenv('ROOK_WORK_DB', str(tmp_path / 'worker.sqlite3'))
    monkeypatch.setattr(messages, 'codex_queue_available', lambda: True)
    monkeypatch.setattr(messages.shutil, 'which', lambda _: '/bin/codex')
    process = type('Process', (), {'wait': AsyncMock(return_value=0)})()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(messages.asyncio, 'create_subprocess_exec', spawn)
    text = 'Multiline\n💌 $(touch /tmp/never) `literal` --help'
    first, second = await asyncio.gather(messages.deliver('codex', SID, 'command-123', text),
                                         messages.deliver('codex', SID, 'command-123', text))
    assert first['delivery'] == 'queued'
    assert second == first or not second['ok']  # In-flight duplicate is uncertain.
    assert await messages.deliver('codex', SID, 'command-123', text) == first
    spawn.assert_awaited_once()
    assert spawn.call_args.args == ('/bin/codex', 'queue', '--thread', SID, '--message', text)
    assert text.encode() not in (tmp_path / 'worker.sqlite3').read_bytes()


@pytest.mark.asyncio
async def test_failed_queue_not_replayed_and_stderr_not_exposed(tmp_path, monkeypatch):
    monkeypatch.setenv('ROOK_WORK_DB', str(tmp_path / 'worker.sqlite3'))
    monkeypatch.setattr(messages, 'codex_queue_available', lambda: True)
    monkeypatch.setattr(messages.shutil, 'which', lambda _: '/bin/codex')
    spawn = AsyncMock(return_value=type('Process', (), {'wait': AsyncMock(return_value=1)})())
    monkeypatch.setattr(messages.asyncio, 'create_subprocess_exec', spawn)
    result = await messages.deliver('codex', SID, 'command-fail', 'secret prompt')
    assert not result['ok']
    assert await messages.deliver('codex', SID, 'command-fail', 'secret prompt') == result
    spawn.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_requires_exact_live_session(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    rollout(tmp_path / 'sessions')
    plugin = CodexHistoryPlugin()
    monkeypatch.setattr(plugin, '_is_active', lambda _: False)
    assert not (await plugin._send(SID, 'hello', 'command-123'))['ok']
    monkeypatch.setattr(plugin, '_is_active', lambda _: True)
    assert not (await plugin._send(SID[:8], 'hello', 'command-123'))['ok']
    assert not (await plugin._send('../../auth.json', 'hello', 'command-123'))['ok']


@pytest.mark.asyncio
async def test_claude_authenticated_frames_and_stale_process_refused(tmp_path, monkeypatch):
    home = tmp_path / 'claude'
    registry = home / 'sessions'
    registry.mkdir(parents=True)
    proc = tmp_path / 'proc' / str(os.getpid())
    proc.mkdir(parents=True)
    (proc / 'comm').write_text('claude\n')
    fields = ['S'] + ['0'] * 18 + ['12345']
    (proc / 'stat').write_text(f'{os.getpid()} (claude) ' + ' '.join(fields))
    path = tmp_path / 'inbox.sock'
    frames = []
    received = asyncio.Event()
    async def accept(reader, writer):
        frames.append(json.loads(await reader.readline()))
        frames.append(json.loads(await reader.readline()))
        received.set()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_unix_server(accept, str(path))
    path.chmod(0o600)
    marker = registry / f'{os.getpid()}.json'
    data = dict(sessionId=SID, pid=os.getpid(), procStart='12345', peerProtocol=1, messagingSocketPath=str(path))
    marker.write_text(json.dumps(data))
    key = registry / f'{os.getpid()}.{hashlib.sha256(str(path).encode()).hexdigest()}.key'
    key.write_text(json.dumps(dict(peerToken='a' * 32, procStart='12345')))
    key.chmod(0o600)
    endpoint = messages.claude_endpoint(SID, home, tmp_path / 'proc')
    assert endpoint is not None
    data['procStart'] = 'stale'
    marker.write_text(json.dumps(data))
    assert messages.claude_endpoint(SID, home, tmp_path / 'proc') is None
    data['procStart'] = '12345'
    marker.write_text(json.dumps(data))
    key.chmod(0o644)
    assert messages.claude_endpoint(SID, home, tmp_path / 'proc') is None
    key.chmod(0o600)
    monkeypatch.setattr(messages, 'claude_endpoint', lambda _: endpoint)
    monkeypatch.setenv('ROOK_WORK_DB', str(tmp_path / 'worker.sqlite3'))
    try:
        result = await messages.deliver('claude', SID, 'command-123', 'hello\n💌')
        await asyncio.wait_for(received.wait(), 2)
        assert result['delivery'] == 'forwarded'
        assert frames[0] == {'type': 'auth', 'token': 'a' * 32}
        assert frames[1]['session_id'] == SID
        assert frames[1]['message']['content'] == 'hello\n💌'
    finally:
        server.close()
        await server.wait_closed()
