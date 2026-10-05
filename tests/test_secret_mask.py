"""Reverse secret masking: known vault values become {{secret:name}} stubs
wherever content crosses or lands on the band through the hub, while the
forward path ({{secret:name}} in args -> real value at the worker) keeps
working."""
import base64
import json
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import quote, quote_plus

import httpx
import pytest

from rook.band_mcp import secret_mask
from rook.band_mcp.chat_rooms import ChatStore
from rook.band_mcp.console_rooms import ConsoleStore
from rook.band_mcp.journal import Journal
from rook.band_mcp.secret_mask import MIN_LEN, Matcher, SecretMasker, StreamMasker, encode, tagged_forms
from rook.band_mcp.server import build_server
from rook.band_mcp.sessions import SessionStore
from rook.band_mcp.vault import Vault

# Made-up test values only.
PW = 'hunter2-Sup3r"secret'
TOKEN = 'tok_EXAMPLE_0123456789abcdef'
STATIC = 'static-token-0123456789abcdef'


@pytest.fixture(autouse=True)
def _no_installed_vault():
    yield
    secret_mask.install(None)


def matcher(**secrets):
    return Matcher(secrets)


# -- matcher ------------------------------------------------------------------

def test_nested_json_bytes_and_keys_are_left_alone():
    m = matcher(pw=PW, tok=TOKEN)
    obj = {'a': [f'x {PW} y', {'deep': (TOKEN, 3, None, True)}],
           TOKEN: 'key position', 'raw': f'..{TOKEN}..'.encode(),
           'inner_json': json.dumps({'cmd': f'login {PW}'})}
    out = m.mask(obj)
    assert out['a'][0] == 'x {{secret:pw}} y'
    assert out['a'][1]['deep'] == ('{{secret:tok}}', 3, None, True)
    # Keys are never masked: a masked key is never substituted back, and two
    # keys could collide into one.
    assert out[TOKEN] == 'key position' and len(out) == len(obj)
    assert out['raw'] == b'..{{secret:tok}}..'
    # The value inside JSON-in-a-string is JSON-escaped (the quote): it is
    # found, and its stub says so, so the string round-trips exactly.
    assert json.loads(out['inner_json']) == {'cmd': 'login {{secret:pw|json}}'}
    assert PW not in json.dumps(out['a']) and PW not in out['inner_json']
    # Twice-encoded: JSON text stored inside JSON text.
    twice = json.dumps(json.dumps({'v': PW}))
    assert PW not in m.sub(twice) and '{{secret:pw|json|json}}' in m.sub(twice)


def test_colliding_keys_are_not_merged():
    m = matcher(a='aaaaaaaa1', b='bbbbbbbb2')
    obj = {'aaaaaaaa1': 1, 'bbbbbbbb2': 2, 'k': 'aaaaaaaa1'}
    assert m.mask(obj) == {'aaaaaaaa1': 1, 'bbbbbbbb2': 2, 'k': '{{secret:a}}'}


def test_overlap_longest_wins_and_same_value_maps_to_one_name():
    m = matcher(short='password1234', long='password1234-and-more', b_dupe=TOKEN, a_dupe=TOKEN)
    assert m.sub('password1234-and-more!') == '{{secret:long}}!'
    assert m.sub('password1234-and-less') == '{{secret:short}}-and-less'
    assert m.sub(f'{TOKEN}') == '{{secret:a_dupe}}'
    # A secret contained in another: the outer one is masked whole.
    m = matcher(inner='abcdefgh12', outer='xx-abcdefgh12-yy')
    assert m.sub('see xx-abcdefgh12-yy and abcdefgh12') == 'see {{secret:outer}} and {{secret:inner}}'


def test_short_values_are_skipped_but_explicit_use_still_masks():
    assert MIN_LEN == 8
    m = matcher(pin='1234567', admin='admin')
    assert not m and m.sub('admin 1234567') == 'admin 1234567'
    assert matcher(ok='12345678').sub('x12345678x') == 'x{{secret:ok}}x'

    class Src:
        def version(self):
            return 1

        def masking_values(self):
            return {'pin': '12345'}
    sm = SecretMasker(Src())
    assert sm.mask('pin 12345') == 'pin 12345'
    assert sm.mask('pin 12345', extra={'pin': '12345'}) == 'pin {{secret:pin}}'


def test_encodings_get_tagged_stubs():
    m = matcher(pw=PW)
    raw = PW.encode()
    cases = {base64.b64encode(raw).decode(): '|b64',
             base64.b64encode(raw).decode().rstrip('='): '|b64np',
             quote(PW, safe=''): '|url',
             json.dumps(PW)[1:-1]: '|json'}
    for form, tag in cases.items():
        assert m.sub(f'<{form}>') == '<{{secret:pw%s}}>' % tag, form
    # URL-safe base64 differs only when the value's base64 has + or /.
    m2 = matcher(k='>>>???~~~')
    url_safe = base64.urlsafe_b64encode(b'>>>???~~~').decode()
    assert url_safe != base64.b64encode(b'>>>???~~~').decode()
    assert m2.sub(url_safe) == '{{secret:k|b64url}}'
    # quote_plus and quote(safe='/') differ from quote(safe='') with a space or '/'.
    m3 = matcher(sp='pass word/x1')
    assert m3.sub(quote_plus('pass word/x1')) == '{{secret:sp|urlplus}}'
    assert m3.sub(quote('pass word/x1')) == '{{secret:sp|urlpath}}'
    assert m.sub(f'<{PW}>') == '<{{secret:pw}}>'   # the raw value keeps the plain stub
    # Non-ASCII values: JSON's \\u escapes are covered too.
    m = matcher(u='pässwörd-ünïcode')
    assert m.sub(json.dumps({'v': 'pässwörd-ünïcode'})) == '{"v": "{{secret:u|jsona}}"}'


@pytest.mark.parametrize('value', [PW, TOKEN, 'pässwörd-ünïcode/+?&= x', 'a/b+c==d?e~', '{"k": "v\\n"}'])
def test_every_encoding_round_trips_through_the_vault(tmp_path, value):
    """mask then substitute == the original, for every form we mask: a file
    read, edited and written back keeps its bytes."""
    v = Vault(str(tmp_path / 'vault.db'))
    v.set('s', value, '', 'test')
    m = Matcher({'s': value})
    forms = tagged_forms(value)
    assert len(forms) >= 3 and {'b64'} <= {t for _, chain in forms for t in chain}
    for form, chain in forms:
        assert encode(value, chain) == form
        doc = f'before {form} after'
        masked = m.sub(doc)
        assert masked == 'before {{secret:%s}} after' % '|'.join(('s',) + chain), (chain, masked)
        back, _ = v.substitute({'x': masked}, 'test', via='t')
        assert back == {'x': doc}, chain
    # Plain stubs are unchanged by the tag support.
    assert v.substitute('{{secret:s}}', 'test', via='t')[0] == value
    # An unknown tag is not a placeholder: it is sent as written.
    assert v.substitute('{{secret:s|rot13}}', 'test', via='t')[0] == '{{secret:s|rot13}}'


def test_extra_values_never_nest_stubs():
    """A short value used by a call can occur inside another secret's stub
    name; the stub must stay whole (one pass, existing stubs skipped)."""
    class Src:
        def version(self):
            return 1

        def masking_values(self):
            return {'api-1234': 'longvalue-abcdef'}
    sm = SecretMasker(Src())
    out = sm.mask('key longvalue-abcdef pin 1234', extra={'pin': '1234'})
    assert out == 'key {{secret:api-1234}} pin {{secret:pin}}'
    # Re-masking already-masked text changes nothing.
    assert sm.mask(out, extra={'pin': '1234'}) == out
    assert Matcher({}, extra={'pin': '1234'}).sub('api-{{secret:pin}} 1234') == 'api-{{secret:pin}} {{secret:pin}}'
    # A stub split across stream reads is held whole, not masked inside.
    s = StreamMasker(lambda: sm.matcher_with({'pin': '1234'}))
    out = s.feed('cat: {{secret:api-12') + s.feed('34}} and 12') + s.feed('34\n') + s.flush()
    assert out == 'cat: {{secret:api-1234}} and {{secret:pin}}\n'


def test_version_check_is_throttled(monkeypatch):
    calls = []

    class Src:
        gen = 0

        def generation(self):
            return self.gen

        def version(self):
            calls.append(1)
            return ('v', self.gen)

        def masking_values(self):
            return {'tok': TOKEN}
    src = Src()
    sm = SecretMasker(src)
    clock = [100.0]
    monkeypatch.setattr(secret_mask.time, 'monotonic', lambda: clock[0])
    for _ in range(50):
        assert sm.mask(TOKEN) == '{{secret:tok}}'
    assert len(calls) == 1
    clock[0] += 1.5          # another process may have changed the vault
    sm.mask(TOKEN)
    assert len(calls) == 2
    src.gen += 1             # an in-process write is seen at once
    sm.mask(TOKEN)
    assert len(calls) == 3


def test_split_chunk_stream_text_and_bytes():
    m = matcher(pw=PW, tok=TOKEN)
    s = StreamMasker(lambda: m)
    chunks = ['first line\nlogin: hun', 'ter2-Sup3', 'r"secret ok\n', 'token=', TOKEN[:5], TOKEN[5:], ' end']
    out = [s.feed(c) for c in chunks]
    # Ordinary text is not held back: only a tail that could start a secret.
    assert out[0] == 'first line\nlogin: '
    assert ''.join(out) + s.flush() == 'first line\nlogin: {{secret:pw}} ok\ntoken={{secret:tok}} end'

    b = StreamMasker(lambda: m)
    data = f'é {TOKEN} ü'.encode()
    pieces = [data[i:i + 3] for i in range(0, len(data), 3)]  # splits é/ü and the token
    got = b''.join(b.feed(p) for p in pieces) + b.feed(b'', final=True)
    assert got.decode() == 'é {{secret:tok}} ü'


def test_stream_holds_back_at_most_longest_minus_one():
    m = matcher(pw=PW)
    s = StreamMasker(lambda: m)
    assert s.feed('x' * 100 + PW[:-1]) == 'x' * 100
    assert s.pending == len(PW) - 1
    assert s.feed(PW[-1] + ' tail') == '{{secret:pw}} tail' and s.pending == 0
    # A held prefix that never completes comes out unchanged at the end.
    assert s.feed('hunter') == '' and s.flush() == 'hunter'


def test_masker_rebuilds_when_the_vault_changes(tmp_path):
    v = Vault(str(tmp_path / 'vault.db'))
    sm = SecretMasker(v)
    assert sm.mask(f'x {TOKEN}') == f'x {TOKEN}'
    v.set('tok', TOKEN, '', 'test')
    assert sm.mask(f'x {TOKEN}') == 'x {{secret:tok}}'
    v.delete('tok', 'test')
    assert sm.mask(f'x {TOKEN}') == f'x {TOKEN}'
    # Masking is not an access: nothing for it in the access log.
    assert [a['action'] for a in v.access_log()] == ['delete', 'create']


@pytest.mark.asyncio
async def test_dashboard_masks_through_the_bridge(tmp_path, caplog):
    """The dashboard holds no vault: it asks the bridge, with the token the
    bridge keeps beside its stores; if it can't, it logs an error."""
    import asyncio
    import logging
    import socket

    import uvicorn
    from starlette.applications import Starlette

    from rook.band_mcp.mask_web import ensure_token, route
    from rook.remote.mask_client import BridgeMask

    caplog.set_level(logging.ERROR, logger='rook.remote.mask_client')
    v = Vault(str(tmp_path / 'vault.db'))
    v.set('tok', TOKEN, '', 'test')
    secret_mask.install(v)
    tok = ensure_token(str(tmp_path))
    assert ensure_token(str(tmp_path)) == tok and (tmp_path / 'mask.token').stat().st_mode & 0o077 == 0
    app = Starlette(routes=[route(tok)])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:
        assert (await c.post('/internal/mask', json={'obj': TOKEN})).status_code == 401
        r = await c.post('/internal/mask', json={'obj': {'text': f'use {TOKEN}'}},
                         headers={'Authorization': 'Bearer ' + tok})
        assert r.json()['obj'] == {'text': 'use {{secret:tok}}'}

    # The client end, against the endpoint served over real HTTP.
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        url = f'http://127.0.0.1:{port}/internal/mask'
        client = BridgeMask([str(tmp_path / 'missing'), str(tmp_path / 'mask.token')], url=url)
        assert await client.mask(f'the key is {TOKEN}') == 'the key is {{secret:tok}}'
        assert not caplog.records
        # A token the bridge does not know: unmasked, and an error is logged.
        (tmp_path / 'stale.token').write_text('0' * 64)
        stale = BridgeMask([str(tmp_path / 'stale.token')], url=url)
        assert await stale.mask(TOKEN) == TOKEN
        assert [r.levelname for r in caplog.records] == ['ERROR']
    finally:
        server.should_exit = True
        await task
    caplog.clear()
    nobody = BridgeMask([str(tmp_path / 'mask.token')], url='http://127.0.0.1:9/internal/mask')
    assert await nobody.mask(TOKEN) == TOKEN
    assert [r.levelname for r in caplog.records] == ['ERROR']
    # The dashboard process installs no vault and never opens the vault key.
    import rook.remote.bootstrap as bootstrap
    src = open(bootstrap.__file__).read()
    assert 'install_from_dir' not in src and 'vault.key' not in src
    assert not hasattr(secret_mask, 'VaultReader')


# -- stores ---------------------------------------------------------------------

def test_store_writers_mask_what_they_write(tmp_path):
    v = Vault(str(tmp_path / 'vault.db'))
    v.set('pw', PW, '', 'test')
    v.set('tok', TOKEN, '', 'test')
    secret_mask.install(v)

    j = Journal(str(tmp_path / 'journal.db'))
    j.record(cap='shell.exec', worker='w', identity='a', args=None,
             reply={'ok': False, 'error': f'bad {TOKEN}', 'result': {'stdout': PW}})
    with sqlite3.connect(tmp_path / 'journal.db') as db:
        stored = ' '.join(f'{r} {e}' for r, e in db.execute('SELECT reply, error FROM calls'))
    assert TOKEN not in stored and 'secret' in stored and '{{secret:tok}}' in stored

    chat = ChatStore(str(tmp_path / 'chat.db'))
    room = chat.start(f'room {TOKEN}', 'a', ['b'])['room']
    chat.send(room, 'a', f'the key is {TOKEN}', [], False)
    assert chat.read(room, 'b')['messages'][0]['text'] == 'the key is {{secret:tok}}'
    with sqlite3.connect(tmp_path / 'chat.db') as db:
        assert not [r for r in db.execute('SELECT text FROM messages UNION SELECT title FROM rooms') if TOKEN in r[0]]

    sessions = SessionStore(str(tmp_path / 'sessions.db'))
    sessions.save(thread_id='t', author='a', goal=f'use {TOKEN}', state=PW,
                  decisions=[f'd {TOKEN}'], next_steps=[PW], artifacts=[])
    assert TOKEN not in (tmp_path / 'sessions.db').read_bytes().decode('latin-1')
    assert sessions.get('t')['current']['state'] == '{{secret:pw}}'

    console = ConsoleStore(str(tmp_path / 'console.db'))
    rid = console.open(title='login', worker='w', worker_name='w', handle='h', cmd=f'run --token {TOKEN}',
                       pty=True, opened_by='a')['room']
    for chunk in ('Password: hun', 'ter2-Sup3r', '"secret\naccepted\n'):
        console.append(rid, chunk)
    console.append(rid, f'$ {TOKEN}', stream='in', sender='a')
    console.mark_closing(rid, 0)
    text = '\n'.join(line['text'] for line in console.read(rid)['lines'])
    assert 'Password: {{secret:pw}}' in text and '$ {{secret:tok}}' in text
    with sqlite3.connect(tmp_path / 'console.db') as db:
        dump = '\n'.join(str(r) for t in ('lines', 'rooms', 'search') for r in db.execute(f'SELECT * FROM {t}'))
    assert TOKEN not in dump and 'Sup3r' not in dump


# -- through the MCP front door ---------------------------------------------------

class FakeBand:
    def __init__(self):
        self.sent = []
        self.workers = {'w1': {'worker_id': 'w1', 'name': 'gpu-box', 'band': 'x', 'last_seen': 0,
                               'caps': ['shell.exec', 'proc.start', 'proc.write', 'settings.fetch']}}

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        self.sent.append((cap, args))
        if cap == 'proc.start':
            return {'ok': True, 'result': {'ok': True, 'handle': 'h1', 'cmd': args.get('cmd', ''), 'pid': 1}}
        if cap == 'proc.write':
            return {'ok': True, 'result': {'ok': True}}
        if cap == 'settings.fetch':
            return {'id': 'f', 'ok': True, 'result': {'password': PW, 'note': f'b64 {base64.b64encode(TOKEN.encode()).decode()}'}}
        # A careless cap: echoes its args and leaks a secret it found on disk.
        return {'id': 'c-%d' % len(self.sent), 'from': target, 'ok': True,
                'result': {'stdout': 'echo: ' + json.dumps(args),
                           'file': base64.b64encode(TOKEN.encode()).decode()}}


@asynccontextmanager
async def mcp_session(tmp_path, monkeypatch):
    monkeypatch.setenv('ROOK_KNOWLEDGE', '1')
    band = FakeBand()
    mcp, _ = build_server(band, public_url='https://mcp.example.com', persist_path=str(tmp_path / 'tokens.json'),
                          static_token=STATIC, journal_path=str(tmp_path / 'journal.db'))
    app = mcp.streamable_http_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://localhost',
            headers={'Accept': 'application/json, text/event-stream', 'Authorization': 'Bearer ' + STATIC,
                     'X-Rook-Host': 'workstation'}) as http:
        r = await http.post('/mcp', json={'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'claude-code', 'version': '1'}}})
        http.headers['mcp-session-id'] = r.headers['mcp-session-id']
        await http.post('/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})

        async def tool(tool_name, **args):
            r = await http.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                              'params': {'name': tool_name, 'arguments': args}})
            body = r.json() if r.headers['content-type'].startswith('application/json') else json.loads(
                next(l[5:] for l in r.text.splitlines() if l.startswith('data:')))
            text = body['result']['content'][0]['text']
            try:
                return json.loads(text)
            except ValueError:
                return text
        yield SimpleNamespace(tool=tool, band=band, mcp=mcp)


def leaked(tmp_path):
    """Any test value in plain text in any of the hub's stores."""
    found = []
    for db in ('journal.db', 'chat.db', 'sessions.db', 'console.db', 'knowledge.db'):
        for path in tmp_path.rglob(db):
            blob = path.read_bytes()
            for value in (PW, TOKEN, json.dumps(PW)[1:-1]):
                if value.encode() in blob:
                    found.append((path.name, value[:3]))
    return found


@pytest.mark.asyncio
async def test_forward_path_still_works_and_everything_else_is_masked(tmp_path, monkeypatch):
    async with mcp_session(tmp_path, monkeypatch) as env:
        assert (await env.tool('rook_secret', action='set', name='pw', value=PW))['ok']
        assert (await env.tool('rook_secret', action='set', name='tok', value=TOKEN))['ok']

        # Forward: the placeholder resolves for the worker...
        out = await env.tool('rook_call', cap='shell.exec', worker='gpu-box', args={'argv': ['login', '{{secret:pw}}']})
        assert env.band.sent[-1] == ('shell.exec', {'argv': ['login', PW]})
        # ...and the reply comes back with stubs, including a base64 leak of another secret.
        assert out['result']['file'] == '{{secret:tok|b64}}' and '{{secret:pw|json}}' in out['result']['stdout']
        assert PW not in json.dumps(out) and TOKEN not in json.dumps(out)

        # A stub the agent got back is usable as-is and re-encodes: round trip.
        await env.tool('rook_call', cap='shell.exec', worker='gpu-box', args={'cmd': out['result']['file']})
        assert env.band.sent[-1] == ('shell.exec', {'cmd': base64.b64encode(TOKEN.encode()).decode()})
        await env.tool('rook_call', cap='shell.exec', worker='gpu-box', args={'cmd': '{{secret:tok}}'})
        assert env.band.sent[-1] == ('shell.exec', {'cmd': TOKEN})

        # An agent that pasted a raw value: the call is not broken (the worker
        # gets what was sent), the reply and the journal are masked.
        raw = await env.tool('rook_call', cap='shell.exec', worker='gpu-box', args={'cmd': f'echo {TOKEN}'})
        assert env.band.sent[-1] == ('shell.exec', {'cmd': f'echo {TOKEN}'})
        assert TOKEN not in json.dumps(raw)
        jr = await env.tool('rook_journal', call_id=raw['id'])
        assert TOKEN not in json.dumps(jr) and '{{secret:tok}}' in json.dumps(jr)

        # The audited raw read still returns the value.
        assert (await env.tool('rook_secret', action='get', name='tok'))['value'] == TOKEN

        # Chat, handoffs, knowledge and task attrs written by an agent.
        room = (await env.tool('rook_chat_start', title='ops'))['room']
        await env.tool('rook_chat_send', room=room, text=f'use {TOKEN} for the API')
        msgs = (await env.tool('rook_chat_read', room=room))['messages']
        assert msgs[-1]['text'] == 'use {{secret:tok}} for the API'
        h = await env.tool('rook_handoff_save', goal='deploy', state=f'logged in with {PW}', next_steps=[TOKEN])
        got = await env.tool('rook_handoff_get', thread_id=h['thread_id'])
        assert TOKEN not in json.dumps(got) and '{{secret:tok}}' in json.dumps(got)
        c = (await env.tool('rook_concept', action='create', request_id='c',
                            data={'title': 'Creds', 'body': f'the token is {TOKEN}'}))['result']
        assert c['body'] == 'the token is {{secret:tok}}'
        p = (await env.tool('rook_project', action='create', request_id='p', data={'title': 'P', 'parent': c['id']}))['result']
        t = (await env.tool('rook_task', action='create', request_id='t',
                            data={'title': 'T', 'parent': p['id'], 'attrs': {'notes': f'pw={PW}'}}))['result']
        assert TOKEN not in json.dumps(t) and PW not in json.dumps(t)

        # Console: output split across reads, the stdin echo, and a placeholder typed at a prompt.
        opened = await env.tool('rook_console_open', worker='gpu-box', task='log in', cmd='login', pty=True)
        console = env.mcp._rook_console
        console.append(opened['room'], f'Password for {TOKEN[:9]}')
        console.append(opened['room'], f'{TOKEN[9:]}: \n')
        w = await env.tool('rook_console_write', room=opened['room'], text='{{secret:pw}}')
        assert w['ok'] and env.band.sent[-1] == ('proc.write', {'data': PW, 'newline': True, 'handle': 'h1'})
        read = await env.tool('rook_console_read', room=opened['room'])
        lines = [l['text'] for l in read['lines']]
        assert 'Password for {{secret:tok}}: ' in lines and '$ {{secret:pw}}' in lines

        assert leaked(tmp_path) == []


@pytest.mark.asyncio
async def test_sensitive_hub_cap_replies_are_not_masked(tmp_path, monkeypatch):
    """settings.fetch exists to hand vault values to a service: its reply
    reaches the caller whole (both in rook_call and in the reply wrapper),
    while the journal keeps nothing of it."""
    async with mcp_session(tmp_path, monkeypatch) as env:
        assert (await env.tool('rook_secret', action='set', name='pw', value=PW))['ok']
        assert (await env.tool('rook_secret', action='set', name='tok', value=TOKEN))['ok']
        tags = {'settings.fetch': ('sensitive',)}
        env.mcp._rook_hub = SimpleNamespace(worker_id='w1', host=SimpleNamespace(registry=SimpleNamespace(
            meta=lambda cap: SimpleNamespace(tags=tags.get(cap, ())))))
        got = await env.tool('rook_call', cap='settings.fetch', worker='gpu-box', args={'namespace': 'svc'})
        assert got['result']['password'] == PW
        assert got['result']['note'] == 'b64 ' + base64.b64encode(TOKEN.encode()).decode()
        # Everything else is still masked, in the same session.
        other = await env.tool('rook_call', cap='shell.exec', worker='gpu-box', args={'cmd': 'x'})
        assert other['result']['file'] == '{{secret:tok|b64}}'
        row = env.mcp._rook_journal.query(cap_prefix='settings.fetch', include_reply=True)[0]
        assert row['reply']['result'] == '[sensitive: not journaled]'
        assert leaked(tmp_path) == []


@pytest.mark.asyncio
async def test_console_masks_short_values_typed_into_that_room(tmp_path, monkeypatch):
    async with mcp_session(tmp_path, monkeypatch) as env:
        assert (await env.tool('rook_secret', action='set', name='pin', value='4321'))['ok']
        a = (await env.tool('rook_console_open', worker='gpu-box', task='a', cmd='login', pty=True))['room']
        b = (await env.tool('rook_console_open', worker='gpu-box', task='b', cmd='other', pty=True))['room']
        w = await env.tool('rook_console_write', room=a, text='{{secret:pin}}')
        assert w['ok'] and env.band.sent[-1][1]['data'] == '4321'
        console = env.mcp._rook_console
        console.append(a, 'PIN: 43')         # the terminal echoes it, split across reads
        console.append(a, '21\nok 4321\n')
        console.append(b, 'port 4321\n')    # another room: an ordinary number
        lines = [l['text'] for l in (await env.tool('rook_console_read', room=a))['lines']]
        assert '$ {{secret:pin}}' in lines and 'PIN: {{secret:pin}}' in lines and 'ok {{secret:pin}}' in lines
        assert not [l for l in lines if '4321' in l]
        assert [l['text'] for l in (await env.tool('rook_console_read', room=b))['lines']] == ['port 4321']


@pytest.mark.asyncio
async def test_console_write_literal_sends_placeholders_as_text(tmp_path, monkeypatch):
    async with mcp_session(tmp_path, monkeypatch) as env:
        room = (await env.tool('rook_console_open', worker='gpu-box', task='t', cmd='cat', pty=True))['room']
        bad = await env.tool('rook_console_write', room=room, text='token: {{secret:nope}}')
        assert not bad['ok'] and 'unknown secret' in bad['error']
        ok = await env.tool('rook_console_write', room=room, text='token: {{secret:nope}}', literal=True)
        assert ok['ok'] and env.band.sent[-1][1]['data'] == 'token: {{secret:nope}}'


def test_console_sanitize_keeps_stubs_whole():
    from rook.band_mcp.console_rooms import sanitize
    assert sanitize('{{secret:github-token}}') == '{{secret:github-token}}'
    assert sanitize('export TOKEN={{secret:gh|b64}} x') == 'export TOKEN={{secret:gh|b64}} x'
    # Ordinary announced secrets are still redacted around a stub.
    assert sanitize('password: hunter2xyz {{secret:pw}}') == 'password: «redacted» {{secret:pw}}'


@pytest.mark.parametrize('value', ['true', '"ok":true', 'null}]'])
def test_journal_redact_never_touches_json_syntax(tmp_path, value):
    j = Journal(str(tmp_path / 'journal.db'))
    reply = {'ok': True, 'result': {'flag': True, 'none': None, 'list': [1, 2], 'text': 'all good'}}
    j.record(cap='x', worker='w', identity='a', args=None, reply=reply)
    j.redact_secret('s', value)
    assert j.query(include_reply=True)[0]['reply'] == reply


def test_journal_redact_masks_string_content_with_tagged_stubs(tmp_path):
    j = Journal(str(tmp_path / 'journal.db'))
    j.record(cap='x', worker='w', identity='a', args=None,
             reply={'ok': True, 'result': {'stdout': f'pw {PW}', 'b': base64.b64encode(PW.encode()).decode()}})
    j.record(cap='x', worker='w', identity='a', args=None, reply={'ok': False, 'error': f'bad {PW}'})
    j.record(cap='y', worker='w', identity='a', args=None, reply={'ok': True, 'result': 'clean'})
    assert j.redact_secret('pw', PW) == 2
    rows = {r['cap'] + str(r['ok']): r for r in j.query(include_reply=True)}
    assert rows['xTrue']['reply']['result'] == {'stdout': 'pw {{secret:pw}}', 'b': '{{secret:pw|b64}}'}
    assert rows['xFalse']['error'] == 'bad {{secret:pw}}'
    with sqlite3.connect(tmp_path / 'journal.db') as db:
        dump = ' '.join(f'{r} {e}' for r, e in db.execute('SELECT reply, error FROM calls'))
    assert PW not in dump and json.dumps(PW)[1:-1] not in dump
    # Below the masking floor nothing is redacted (as everywhere else).
    assert j.redact_secret('short', 'pw') == 0
