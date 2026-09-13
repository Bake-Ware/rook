import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from rook.worker import codex_input as direct

SID = '01a098d3-31a9-7173-ac1e-98e46b8d7af6'
EMPTY = '• Working\n\n› Ask Codex to do anything\n\n  gpt-6-astra medium · ~/Projects/R00K'


@pytest.mark.asyncio
@pytest.mark.parametrize('state,method', [('idle', 'turn/start'), ('active', 'turn/steer'), ('missing', None), ('approval', None)])
async def test_existing_app_server_only(tmp_path, state, method):
    calls = []
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for frame in ws:
            msg = frame.json()
            calls.append(msg)
            name = msg['method']
            if 'id' not in msg:
                continue
            result = {}
            if name == 'thread/loaded/list':
                result = {'data': [] if state == 'missing' else [SID]}
            elif name == 'thread/read':
                result = {'thread': {'canAcceptDirectInput': True, 'status': {
                    'type': 'active' if state == 'approval' else state,
                    'activeFlags': ['waitingOnApproval'] if state == 'approval' else []}}}
            elif name == 'thread/turns/list':
                result = {'data': [{'id': 'turn-123', 'status': 'inProgress'}]}
            await ws.send_json({'id': msg['id'], 'result': result})
        return ws
    app = web.Application()
    app.router.add_get('/', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    path = tmp_path / 'codex.sock'
    await web.UnixSite(runner, str(path)).start()
    try:
        if state == 'approval':
            with pytest.raises(ValueError, match='attention'):
                await direct._app_send(path, SID, 'hello\n💌')
        else:
            result = await direct._app_send(path, SID, 'hello\n💌')
            assert bool(result) == bool(method)
        writes = [c for c in calls if c['method'].startswith('turn/')]
        assert len(writes) == bool(method)
        if method:
            assert writes[0]['method'] == method
            assert writes[0]['params']['input'] == [{'type': 'text', 'text': 'hello\n💌'}]
            if state == 'active':
                assert writes[0]['params']['expectedTurnId'] == 'turn-123'
        assert not any(c['method'] in ('thread/resume', 'thread/start') for c in calls)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_uncertain_app_delivery_never_falls_back(monkeypatch):
    monkeypatch.setattr(direct, 'control_socket', lambda: '/socket')
    monkeypatch.setattr(direct, '_app_send', AsyncMock(side_effect=asyncio.TimeoutError))
    terminal = AsyncMock()
    monkeypatch.setattr(direct, '_terminal_send', terminal)
    with pytest.raises(asyncio.TimeoutError):
        await direct.send(SID, 'hello')
    terminal.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('screen', [EMPTY.replace('Ask Codex to do anything', 'my local draft'),
                                    EMPTY + '\n1. Approve', 'shell $', EMPTY + '\nwrapped draft'])
async def test_terminal_drafts_and_dialogs_refused(monkeypatch, screen):
    monkeypatch.setattr(direct, '_check_terminal', AsyncMock())
    dbus = AsyncMock(return_value=screen)
    monkeypatch.setattr(direct, '_dbus', dbus)
    with pytest.raises(ValueError, match='draft or dialog'):
        await direct._terminal_send(SID, 'hello', {})
    assert all(c.args[1] != 'sendText' for c in dbus.call_args_list)


@pytest.mark.asyncio
async def test_terminal_pastes_literal_then_enter(monkeypatch):
    check = AsyncMock()
    monkeypatch.setattr(direct, '_check_terminal', check)
    dbus = AsyncMock(side_effect=[EMPTY, '', EMPTY.replace('Ask Codex to do anything', 'hello'), ''])
    monkeypatch.setattr(direct, '_dbus', dbus)
    text = 'hello\n💌 $(literal)'
    result = await direct._terminal_send(SID, text, {})
    assert result['delivery'] == 'forwarded'
    assert [c.args[2] for c in dbus.call_args_list if c.args[1] == 'sendText'] == ['\x1b[200~'+text+'\x1b[201~', '\r']
    assert check.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['hello\x1b[201~', 'hello\r/run', '/exit'])
async def test_terminal_control_input_refused(monkeypatch, text):
    dbus = AsyncMock()
    monkeypatch.setattr(direct, '_dbus', dbus)
    with pytest.raises(ValueError):
        await direct._terminal_send(SID, text, {})
    dbus.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_identity_change_refused(monkeypatch):
    monkeypatch.setattr(direct, 'terminal_endpoint', lambda _: None)
    with pytest.raises(ValueError, match='changed'):
        await direct._check_terminal(SID, {'pid': 1})


@pytest.mark.asyncio
@pytest.mark.parametrize('replies', [lambda pid: [str(pid), '999999'],
                                     lambda pid: [str(pid), '123', '[Argument: ai {2}]']])
async def test_terminal_foreground_and_broadcast_refused(monkeypatch, replies):
    import os
    ep = dict(pid=os.getpid(), group=123, service=':1.2')
    monkeypatch.setattr(direct, 'terminal_endpoint', lambda _: ep)
    dbus = AsyncMock(side_effect=replies(os.getpid()))
    monkeypatch.setattr(direct, '_dbus', dbus)
    with pytest.raises(ValueError):
        await direct._check_terminal(SID, ep)
    assert all(c.args[1] != 'sendText' for c in dbus.call_args_list)


@pytest.mark.asyncio
async def test_terminal_ignored_paste_never_sends_enter(monkeypatch):
    monkeypatch.setattr(direct, '_check_terminal', AsyncMock())
    dbus = AsyncMock(side_effect=[EMPTY, '', EMPTY])
    monkeypatch.setattr(direct, '_dbus', dbus)
    with pytest.raises(ValueError, match='pasted message'):
        await direct._terminal_send(SID, 'hello', {})
    assert len([c for c in dbus.call_args_list if c.args[1] == 'sendText']) == 1
