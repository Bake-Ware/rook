"""Secret vault: encrypted at rest, every access logged, placeholders
substituted only on the way to the worker, values masked in replies and the
journal, operator page write-only."""
import json
import os
import sqlite3
import stat
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from rook.band_mcp.vault import Vault, mask, encoded_forms
from rook.band_mcp.vault_web import routes
from rook.band_mcp.server import build_server

SECRET = 'hunter2-Sup3r"secret'
STATIC = 'static-token-0123456789abcdef'


def test_vault_encrypts_logs_and_never_lists_values(tmp_path):
    v = Vault(str(tmp_path / 'vault.db'))
    assert stat.S_IMODE(os.stat(tmp_path / 'vault.key').st_mode) == 0o600
    v.set('starscream-root', SECRET, 'Proxmox root on starscream', 'human:bake')
    assert SECRET.encode() not in (tmp_path / 'vault.db').read_bytes()
    assert SECRET not in json.dumps(v.list())
    assert v.get('starscream-root', 'codex.codex.kaiju', task='t_1') == SECRET
    assert Vault(str(tmp_path / 'vault.db')).get('starscream-root', 'x') == SECRET  # key persists
    log = v.access_log('starscream-root')
    assert [(a['action'], a['actor']) for a in log][:2] == [('get', 'x'), ('get', 'codex.codex.kaiju')]
    assert log[1]['task'] == 't_1' and log[-1]['action'] == 'create'
    with pytest.raises(ValueError):
        v.set('Bad Name', 'x' * 5, '', 'a')
    with pytest.raises(KeyError):
        v.get('missing', 'a')
    assert v.delete('starscream-root', 'human:bake') and not v.list()


def test_substitute_and_mask(tmp_path):
    v = Vault(str(tmp_path / 'vault.db'))
    v.set('pw', SECRET, '', 'a')
    out, used = v.substitute({'argv': ['sshpass', '-p', '{{secret:pw}}'], 'env': {'X': 'a{{secret:pw}}b'}}, 'agent', via='t')
    assert out['argv'][2] == SECRET and out['env']['X'] == f'a{SECRET}b' and used == {'pw': SECRET}
    assert mask({'stdout': f'got {SECRET}!'}, encoded_forms(SECRET)) == {'stdout': 'got ***!'}
    with pytest.raises(KeyError):
        v.substitute('{{secret:nope}}', 'agent', via='t')


class EchoBand:
    def __init__(self):
        self.sent = []
        self.workers = {'w1': {'worker_id': 'w1', 'name': 'kaiju', 'band': 'x', 'caps': ['shell.exec'], 'last_seen': 0}}

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        self.sent.append(args)
        return {'id': 'c-%d' % len(self.sent), 'from': target, 'ok': True,
                'result': {'stdout': 'echo: ' + json.dumps(args)}}


@asynccontextmanager
async def mcp_session(tmp_path, monkeypatch):
    monkeypatch.setenv('ROOK_KNOWLEDGE', '1')
    band = EchoBand()
    mcp, store = build_server(band, public_url='https://mcp.example.com', persist_path=str(tmp_path / 'tokens.json'),
                              static_token=STATIC, journal_path=str(tmp_path / 'journal.db'))
    app = mcp.streamable_http_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://localhost',
            headers={'Accept': 'application/json, text/event-stream', 'Authorization': 'Bearer ' + STATIC,
                     'X-Rook-Host': 'cachyrig'}) as http:
        r = await http.post('/mcp', json={'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'claude-code', 'version': '1'}}})
        http.headers['mcp-session-id'] = r.headers['mcp-session-id']
        await http.post('/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})

        async def tool(tool_name, **args):
            r = await http.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': tool_name, 'arguments': args}})
            body = r.json() if r.headers['content-type'].startswith('application/json') else json.loads(next(l[5:] for l in r.text.splitlines() if l.startswith('data:')))
            return json.loads(body['result']['content'][0]['text'])
        yield SimpleNamespace(tool=tool, band=band, journal=str(tmp_path / 'journal.db'), mcp=mcp)


def journal_text(path):
    with sqlite3.connect(path) as db:
        return '\n'.join(f'{r} {e}' for r, e in db.execute('SELECT reply, error FROM calls'))


def leaks(path):
    text = journal_text(path)
    return [f for f in encoded_forms(SECRET) if f in text]


@pytest.mark.asyncio
async def test_placeholders_never_reach_agent_or_journal(tmp_path, monkeypatch):
    async with mcp_session(tmp_path, monkeypatch) as env:
        # A call that leaked the value before it entered the vault…
        leaked = await env.tool('rook_call', cap='shell.exec', worker='kaiju', args={'cmd': f'echo {SECRET}'})
        assert leaks(env.journal)
        # …is masked in the journal once the secret is stored.
        res = await env.tool('rook_secret', action='set', name='pw', value=SECRET, description='test')
        assert res['ok'] and res['journal_rows_masked'] >= 1
        assert not leaks(env.journal)
        assert SECRET not in json.dumps(await env.tool('rook_secret'))  # list shows no values

        c = (await env.tool('rook_concept', action='create', request_id='c', data={'title': 'C'}))['result']
        p = (await env.tool('rook_project', action='create', request_id='p', data={'title': 'P', 'parent': c['id']}))['result']
        t = (await env.tool('rook_task', action='create', request_id='t', data={'title': 'T', 'parent': p['id']}))['result']
        await env.tool('rook_task', action='claim', id=t['id'], request_id='cl')

        out = await env.tool('rook_call', cap='shell.exec', worker='kaiju', args={'argv': ['login', '{{secret:pw}}']})
        assert env.band.sent[-1] == {'argv': ['login', SECRET]}  # worker got the real value
        assert SECRET not in json.dumps(out) and '***' in out['result']['stdout']  # agent didn't
        assert not leaks(env.journal)

        bad = await env.tool('rook_call', cap='shell.exec', worker='kaiju', args={'cmd': '{{secret:nope}}'})
        assert not bad['ok'] and 'unknown secret' in bad['error']
        assert len(env.band.sent) == 2  # refused before dispatch

        got = await env.tool('rook_secret', action='get', name='pw')
        assert got['value'] == SECRET
        log = (await env.tool('rook_secret', action='log', name='pw'))['access']
        assert [a['action'] for a in log][:2] == ['get', 'use']
        assert log[1]['task'] == t['id'] and log[0]['actor'] == 'static.claudecode.cachyrig'
        links = (await env.tool('rook_task', action='get', id=t['id']))['result']['links']
        assert {(l['kind'], l['ref']) for l in links} >= {('secret', 'pw')}
        assert not leaks(env.journal)


@pytest.mark.asyncio
async def test_operator_page_is_write_only(tmp_path):
    v = Vault(str(tmp_path / 'vault.db'))
    masked = []
    accounts = SimpleNamespace(session=lambda c: {'id': 'u1', 'username': 'bake', 'csrf': 'k', 'admin': c == 'admin'} if c else None)
    app = Starlette(routes=routes(v, lambda val: masked.append(val) or 3, accounts))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:
        api = '/vault/account-api'
        assert (await c.get(api)).status_code == 401
        assert (await c.get(api, headers={'Cookie': 'rook_account=member'})).status_code == 403
        h = {'Cookie': 'rook_account=admin'}
        assert (await c.post(api, headers=h, json={'action': 'set', 'name': 'gh', 'value': SECRET})).status_code == 403
        r = await c.post(api, headers=h, json={'csrf': 'k', 'action': 'set', 'name': 'gh', 'value': SECRET, 'description': 'GitHub PAT'})
        assert r.status_code == 200 and r.json()['journal_rows_masked'] == 3 and masked == [SECRET]
        page = await c.get(api, headers=h)
        assert SECRET not in page.text and page.json()['secrets'][0]['description'] == 'GitHub PAT'
        r = await c.post(api, headers=h, json={'csrf': 'k', 'action': 'describe', 'name': 'gh', 'description': 'PAT for Bake-Ware'})
        assert r.status_code == 200 and v.get('gh', 'x') == SECRET
        assert (await c.post(api, headers=h, json={'csrf': 'k', 'action': 'delete', 'name': 'gh'})).status_code == 200
        assert v.list() == [] and v.access_log()[0]['action'] == 'delete'
