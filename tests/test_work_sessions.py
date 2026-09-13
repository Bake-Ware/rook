import asyncio
import inspect
import json
import time
from types import SimpleNamespace
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from test_band_management import portal
from rook.remote.work_web import WorkStore, WorkWeb, project
from rook.worker.work_runtime import RuntimeStore, WorkRuntime
from rook.worker.plugins.work import WorkPlugin


class FakeBand:
    def __init__(self):
        self.workers = {'host1': {'worker_id': 'host1', 'name': 'test-host', 'band': 'test',
                        'last_seen': time.time(), 'caps': ['proc.start','proc.read','proc.write']}}
        self.output = ''
        self.calls = []
        self.running = True

    def emit(self, data):
        self.output += json.dumps(data) + '\n'

    async def call(self, cap, args, target, timeout):
        self.calls.append((cap,args))
        if cap == 'proc.start':
            self.running = True
            self.output = ''
            result = {'ok':True, 'handle':'handle1'}
        elif cap == 'proc.signal':
            self.running = False
            result = {'ok':True}
        elif cap == 'proc.read':
            cursor = args['cursor']
            chunk = self.output[cursor:cursor+args['max_bytes']]
            result = {'ok':True, 'chunk':chunk, 'next_cursor':cursor+len(chunk),
                      'running':self.running, 'eof':not self.running, 'dropped':0, 'exit_code':0}
        elif cap == 'proc.write':
            data = json.loads(args['data'])
            method = data.get('method')
            result = {'ok':True}
            if method == 'initialize':
                self.emit({'id':data['id'],'result':{}})
            elif method in ('thread/start','thread/resume'):
                self.emit({'id':data['id'],'result':{'thread':{'id':'thread1'},'model':'test-model'}})
            elif method == 'turn/start':
                self.emit({'id':data['id'],'result':{'turn':{'id':'turn1','status':'inProgress'}}})
                self.emit({'method':'turn/started','params':{'turn':{'id':'turn1'}}})
            elif method == 'turn/interrupt':
                self.emit({'id':data['id'],'result':{}})
                self.emit({'method':'turn/completed','params':{'turn':{'id':'turn1','status':'interrupted'}}})
        else:
            raise AssertionError(cap)
        return {'ok':True,'from':target,'result':result}


class RuntimeBand(FakeBand):
    def __init__(self, path):
        super().__init__()
        self.plugin = WorkPlugin()
        async def local_call(cap, **args):
            return (await super(RuntimeBand, self).call(cap, args, 'host1', 12))['result']
        self.plugin.runtime = WorkRuntime(path, SimpleNamespace(call=local_call))
        self.workers['host1']['caps'] += list(self.plugin.caps())

    async def call(self, cap, args, target, timeout):
        if not cap.startswith('work.'):
            return await super().call(cap, args, target, timeout)
        self.calls.append((cap, args))
        result = self.plugin.caps()[cap](**args)
        if inspect.isawaitable(result):
            result = await result
        return {'ok': True, 'from': target, 'result': result}


@pytest_asyncio.fixture
async def runtime_band(portal, tmp_path):
    band = RuntimeBand(tmp_path / 'worker' / 'work.sqlite3')
    portal.server._band = band
    await band.plugin.start()
    yield band
    await band.plugin.stop()


async def until(test, timeout=8):
    async with asyncio.timeout(timeout):
        while not test():
            await asyncio.sleep(.01)


async def connect(client,p):
    return await client.ws_connect('/account/work/ws',headers=p.headers)


async def create(client,p):
    ws=await connect(client,p)
    await ws.send_json({'op':'create','id':'create-test-123','csrf':p.csrf,
                       'worker':'host1','cwd':'/tmp','title':'Test work'})
    while True:
        msg=await ws.receive_json()
        if msg['type']=='selected':
            return ws,msg['session']


@pytest.mark.asyncio
async def test_session_runs_without_browser_and_recovers_server_state(portal, runtime_band):
    p=portal;band=runtime_band
    async with TestClient(TestServer(p.app)) as client:
        ws,sid=await create(client,p)
        work=p.account.work_web
        await until(lambda:work.store.get(sid)['status']=='ready')
        await ws.send_json({'op':'message','id':'message-test-123','session':sid,'csrf':p.csrf,'text':'hello'})
        await until(lambda:work.store.get(sid)['status']=='working')
        await ws.close()
        band.emit({'method':'item/agentMessage/delta','params':{'itemId':'answer','delta':'Still working without a browser.'}})
        band.emit({'method':'item/commandExecution/requestApproval','id':99,'params':{'command':'test approval'}})
        await until(lambda:bool(band.plugin.runtime.store.get(sid)['pending']))
        # Stop only the web collector; the worker process remains available.
        await work.stop(p.app)
        rebuilt=WorkWeb(p.account)
        band.emit({'method':'item/agentMessage/delta','params':{'itemId':'answer','delta':' More output.'}})
        await until(lambda: ' More output.' in band.plugin.runtime.store.get(sid)['items']['answer']['text'])
        s=band.plugin.runtime.store.get(sid)
        assert s['items']['answer']['text']=='Still working without a browser. More output.'
        assert s['pending']['99']['params']['command']=='test approval'
        assert len([c for c,a in band.calls if c=='proc.start'])==1
        assert 'items' not in rebuilt.store.get(sid)
        assert 'pending' not in rebuilt.store.get(sid)
        assert RuntimeStore(band.plugin.runtime.store.path).get(sid)['items']==s['items']
        # Viewing is authenticated, paged, and does not populate web storage.
        response = await client.get('/account/work/view/' + sid, headers=p.headers)
        assert response.status == 200
        payload = await response.json()
        assert json.loads(payload['data'])['pending']['99']['params']['command']=='test approval'
        with rebuilt.store.db() as db:
            assert 'More output' not in db.execute('SELECT state FROM work_sessions WHERE id=?', (sid,)).fetchone()[0]
            assert db.execute('SELECT COUNT(*) FROM work_events').fetchone()[0] == 0



@pytest.mark.asyncio
async def test_auth_csrf_owner_and_command_deduplication(portal, runtime_band):
    p=portal;band=runtime_band
    async with TestClient(TestServer(p.app)) as client:
        r=await client.get('/account/work/bootstrap')
        assert r.status==401
        with pytest.raises(Exception) as error:
            await client.ws_connect('/account/work/ws',headers={**p.headers,'Origin':'https://evil.example'})
        assert error.value.status==403
        ws,sid=await create(client,p)
        work=p.account.work_web
        await until(lambda:work.store.get(sid)['status']=='ready')
        cmd={'op':'message','id':'message-test-123','session':sid,'csrf':p.csrf,'text':'hello'}
        await ws.send_json(cmd);await ws.send_json(cmd)
        await until(lambda:work.store.get(sid)['status']=='working')
        assert len([1 for cap,a in band.calls if cap=='proc.write' and json.loads(a['data']).get('method')=='turn/start'])==1
        bad={**cmd,'id':'bad-message-123','csrf':'wrong'}
        await ws.send_json(bad)
        while True:
            msg=await ws.receive_json()
            if msg['type']=='error':
                assert 'expired' in msg['error'];break
        band.emit({'method':'item/commandExecution/requestApproval','id':88,'params':{'command':'echo approval'}})
        await until(lambda:'88' in band.plugin.runtime.store.get(sid)['pending'])
        answer={'op':'answer','id':'answer-test-123','session':sid,'csrf':p.csrf,'request':'88','decision':'decline'}
        await ws.send_json(answer);await ws.send_json(answer)
        await until(lambda:not band.plugin.runtime.store.get(sid)['pending'])
        sent=[json.loads(a['data']) for cap,a in band.calls if cap=='proc.write']
        assert len([m for m in sent if m.get('id')==88 and 'result' in m])==1
        other=p.store.create_local('other','long enough password')
        with pytest.raises(Exception) as error:work.store.get(sid,other)
        assert error.value.status==404
        await ws.close()


def test_projection_unicode_and_durable_command_claim(tmp_path):
    store=RuntimeStore(tmp_path/'work.sqlite3')
    s=dict(id='1',owner='a',items={},order=[],pending={},status='working')
    project(s,{'method':'item/agentMessage/delta','params':{'itemId':'a','delta':'Hello 🦉'}})
    project(s,{'method':'item/completed','params':{'item':{'id':'a','type':'agentMessage','text':'Hello 🦉 world'}}})
    project(s,{'method':'turn/diff/updated','params':{'diff':'+hello'}})
    store.save(s,{'example':True})
    assert RuntimeStore(store.path).get('1')['items']['a']['text']=='Hello 🦉 world'
    assert store.claim('1','command') and not store.claim('1','command')
    assert len(s['order'])==1

class HistoryBand(FakeBand):
    def __init__(self):
        super().__init__()
        self.workers['host1']['caps'] = ['claude-history.pull', 'claude-history.read',
                                        'codex-history.pull', 'codex-history.read',
                                        'claude-history.read_page', 'codex-history.read_page',
                                        'claude-history.send', 'codex-history.send']
        self.version = 1

    async def call(self, cap, args, target, timeout):
        self.calls.append((cap, args))
        if cap.endswith('.pull'):
            rows = [dict(session_id=f'source-{i}', title=f'Imported {i}', cwd='/tmp',
                         last_modified=self.version, size_bytes=self.version, activity='ready', active=getattr(self, 'active', False),
                         messageable=getattr(self, 'messageable', False)) for i in range(101)]
            offset = args.get('offset', 0)
            result = dict(ok=True, sessions=rows[offset:offset+args['limit']], total=len(rows))
        elif cap.endswith('.send'):
            result = {'ok': False, 'error': 'Host rejected the message.'} if getattr(self, 'fail_send', False) else {
                'ok': True, 'delivery': 'steered', 'note': 'Message delivered to the active turn.'}
        elif cap.endswith('.follow'):
            if args.get('version') == str(self.version):
                result = dict(ok=True, unchanged=True, version=str(self.version))
            else:
                offset = args['offset']
                rows = [dict(index=i, role='assistant', content_offset=0, content=f'Message {i}')
                        for i in range(offset, 501)]
                if self.version > 1:
                    rows.append(dict(index=501, role='assistant', content_offset=0, content='Live update from host'))
                result = dict(ok=True, messages=rows, truncated=False, replace_from=offset, version=str(self.version))
        elif cap.endswith('.read_page'):
            rows = [dict(role='assistant', content=f'Message {i}') for i in range(501)]
            offset = args.get('offset', 0)
            batch = [dict(m, index=offset+i, content_offset=0) for i, m in enumerate(rows[offset:offset+20])]
            result = dict(ok=True, messages=batch, activity='ready', active=getattr(self, 'active', False),
                          truncated=offset+len(batch)<len(rows), next_offset=offset+len(batch), next_content_offset=0)
        else:
            raise AssertionError(cap)
        return {'ok': True, 'from': target, 'result': result}


@pytest.mark.asyncio
async def test_discovery_paginates_both_agents_and_preserves_review_status(portal):
    p = portal
    p.server._band = band = HistoryBand()
    work = p.account.work_web
    host = band.workers['host1']
    for agent in ('claude', 'codex'):
        await work.sync_history(host, agent, [p.uid])
    sessions = work.store.all(p.uid)
    assert len(sessions) == 202
    assert {s['agent'] for s in sessions} == {'claude', 'codex'}
    s = sessions[0]
    sid = s['id']
    assert 'items' not in s and 'order' not in s
    assert all(cap.endswith('.pull') for cap, _ in band.calls)
    await work.command(sid, dict(op='status', status='blocked', id='block-session'))
    band.version += 1
    await work.sync_history(host, s['agent'], [p.uid])
    assert WorkWeb.summary(work.store.get(sid))['status'] == 'blocked'
    await work.command(sid, dict(op='close', id='close-session'))
    assert WorkWeb.snapshot(work.store.get(sid))['status'] == 'closed'
    assert not any(cap.startswith('proc.') for cap, _ in band.calls)
    await work.command(sid, dict(op='status', status='auto', id='auto-session'))
    assert WorkWeb.summary(work.store.get(sid))['status'] == 'ready'
    count = len(band.calls)
    await work.sync_history(host, s['agent'], [p.uid])
    assert all(cap.endswith('.pull') for cap, _ in band.calls[count:])
    assert len(work.store.all(p.uid)) == 202
    assert 'items' not in WorkStore(work.store.path).get(sid)


def test_ready_means_user_interaction_and_manual_status_survives_events():
    s = dict(id='s', status='working', pending={}, review_status='blocked')
    project(s, dict(id=4, method='item/tool/requestUserInput', params={}))
    assert s['status'] == 'ready'
    assert WorkWeb.summary(s)['status'] == 'blocked'
    project(s, dict(method='turn/completed', params={'turn': {}}))
    assert WorkWeb.snapshot(s)['status'] == 'blocked'
    assert WorkWeb.snapshot(s)['activity'] == 'ready'

@pytest.mark.asyncio
async def test_imported_resume_input_close_and_host_isolation(portal):
    p = portal
    band = HistoryBand()
    original = band.call
    async def call(cap, args, target, timeout):
        if cap.endswith('.resume'):
            band.calls.append((cap, args))
            result = {'ok': True, 'handle': 'external', 'note': 'Resume started'}
        elif cap == 'proc.read':
            result = {'ok': True, 'chunk': 'terminal prompt', 'next_cursor': 15, 'eof': False}
        elif cap in ('proc.signal', 'proc.write'):
            band.calls.append((cap, args))
            result = {'ok': True}
        else:
            return await original(cap, args, target, timeout)
        return {'ok': True, 'from': target, 'result': result}
    band.call = call
    p.server._band = band
    work = p.account.work_web
    host = band.workers['host1']
    await work.sync_history(host, 'claude', [p.uid])
    sid = work.store.all()[0]['id']
    await work.command(sid, dict(op='resume', id='resume-imported'))
    assert work.snapshot(work.store.get(sid))['external_running']
    assert 'external_handle' not in work.snapshot(work.store.get(sid))
    await work.drain_external(sid)
    assert work.external_output[sid] == 'terminal prompt'
    assert 'terminal' not in work.store.get(sid)
    await work.command(sid, dict(op='terminal_input', text='hello', id='input-imported'))
    assert ('proc.write', {'handle': 'external', 'data': 'hello', 'newline': True}) in band.calls
    await work.command(sid, dict(op='status', status='closed', id='close-imported'))
    assert ('proc.signal', {'handle': 'external', 'sig': 'TERM'}) in band.calls
    assert work.summary(work.store.get(sid))['status'] == 'closed'
    host2 = dict(host, worker_id='host2')
    band.workers['host2'] = host2
    await work.sync_history(host2, 'claude', [p.uid])
    assert len(work.store.all()) == 202
    band.workers.clear()
    await work.command(sid, dict(op='status', status='pending', id='offline-review'))
    assert work.summary(work.store.get(sid))['status'] == 'pending'


def test_index_migrates_existing_sessions_without_loading_transcripts(tmp_path):
    store = RuntimeStore(tmp_path/'work.sqlite3')
    state = dict(id='legacy', owner='operator', status='ready', items={'large':'x'*1000000}, order=['large'])
    store.save(state)
    with store.db() as db:
        db.execute('DROP TABLE work_index')
    restored = RuntimeStore(store.path)
    metadata = restored.all('operator', details=False)[0]
    assert 'items' not in metadata and 'order' not in metadata
    assert len(json.dumps(metadata)) < 1000
    assert restored.get('legacy')['items'] == state['items']
    assert restored.revision('legacy', 'operator') == state['revision']
    state['status'] = 'blocked'
    restored.save(state)
    assert restored.all(details=False)[0]['status'] == 'blocked'


@pytest.mark.asyncio
async def test_older_workers_still_get_catalog_entries(portal):
    p = portal
    p.server._band = band = HistoryBand()
    worker = band.workers['host1']
    worker['caps'] = ['claude-history.pull', 'claude-history.read']
    work = p.account.work_web
    await work.sync_history(worker, 'claude', [p.uid])
    assert len(work.store.all()) == 101
    assert all('items' not in s for s in work.store.all())
    assert all(cap.endswith('.pull') for cap, _ in band.calls)


@pytest.mark.asyncio
async def test_history_is_read_on_demand_without_persistence_and_requires_owner(portal):
    p = portal
    p.server._band = band = HistoryBand()
    work = p.account.work_web
    await work.sync_history(band.workers['host1'], 'claude', [p.uid])
    s = work.store.all(p.uid)[0]
    url = '/account/work/history/' + s['id']
    async with TestClient(TestServer(p.app)) as client:
        assert (await client.get(url)).status == 401
        before = work.store.get(s['id'])
        response = await client.get(url, headers=p.headers)
        assert response.status == 200
        assert 'no-store' in response.headers['Cache-Control']
        page = await response.json()
        assert len(page['messages']) == 20
        assert page['messages'][0]['content'] == 'Message 0'
        response = await client.get(url + '?offset=20', headers=p.headers)
        assert (await response.json())['messages'][0]['content'] == 'Message 20'
        assert work.store.get(s['id']) == before
        with work.store.db() as db:
            assert db.execute('SELECT COUNT(*) FROM work_events').fetchone()[0] == 0
        band.workers.clear()
        response = await client.get(url, headers=p.headers)
        assert response.status == 503
        assert 'disconnected' in (await response.json())['error']
        s['owner'] = 'someone-else'
        work.store.save(s)
        assert (await client.get(url, headers=p.headers)).status == 404


def test_old_imported_copies_are_removed_but_native_sessions_survive(tmp_path):
    store = WorkStore(tmp_path / 'work.sqlite3')
    native = dict(id='native', owner='operator', status='ready', items={'a': 'native text'}, order=['a'])
    store.save(native)
    assert 'items' not in store.get('native')
    imported = dict(native, id='imported', imported=True, review_status='blocked',
                    terminal='terminal text', history_loading=False)
    # Simulate a database from the full-copy importer, bypassing the new save filter.
    with store.db() as db:
        for table in ('work_sessions', 'work_index'):
            db.execute('INSERT INTO ' + table + ' VALUES(?,?,?,?)',
                       ('imported', 'operator', 1, json.dumps(imported)))
    restored = WorkStore(store.path)
    for state in (restored.get('imported'), next(s for s in restored.all(details=False) if s['id']=='imported')):
        assert not {'items', 'order', 'terminal', 'history_loading'} & state.keys()
        assert state['review_status'] == 'blocked'
    assert 'items' not in restored.get('native')
    restored.save(imported)
    assert 'items' not in restored.get('imported')


@pytest.mark.asyncio
async def test_worker_view_pages_are_stable_incremental_and_bound_to_session(runtime_band):
    runtime = runtime_band.plugin.runtime
    state = dict(id='paged', owner='host', status='ready', items={'large': {'text': '🦉' * 15000}},
                 order=['large'], pending={}, diff='original')
    runtime.store.save(state)
    plugin = runtime_band.plugin
    page = plugin.view_page('paged')
    assert len(page['data']) <= 6000 and page['truncated']
    with pytest.raises(ValueError, match='expired'):
        plugin.view_page('another', token=page['token'], offset=page['next_offset'])
    original_revision = page['revision']
    # Output changing while a view is in flight must not mix two revisions.
    changed = runtime.store.get('paged')
    changed['items']['second'] = {'text': 'new output'}
    changed['order'].append('second')
    changed['diff'] = 'new diff'
    runtime.store.save(changed)
    text = page['data']
    while page['truncated']:
        page = plugin.view_page('paged', token=page['token'], offset=page['next_offset'])
        assert len(page['data']) <= 6000
        text += page['data']
    assert json.loads(text)['items'] == state['items']
    delta = plugin.view_page('paged', since=original_revision)
    update = json.loads(delta['data'])
    assert update['items'] == {'second': {'text': 'new output'}}
    assert update['diff'] == 'new diff'
    assert 'large' not in update['items']
    assert plugin.status(['paged'])['sessions'][0]['revision'] == changed['revision']


@pytest.mark.asyncio
async def test_legacy_migration_keeps_only_copy_until_worker_acknowledges(portal, runtime_band):
    p = portal
    work = p.account.work_web
    state = dict(id='legacy-native', owner=p.uid, worker_id='host1', band='test',
                 status='closed', agent='codex', title='Legacy', cwd='/tmp', model='',
                 items={'saved': {'text': 'old transcript 🦉'}}, order=['saved'], pending={},
                 rpc={}, partial='', cursor=0, handle=None, turn_id=None, thread_id='thread1',
                 diff='old diff', error='', revision=1)
    with work.store.db() as db:
        db.execute('INSERT INTO work_sessions VALUES(?,?,?,?)',
                   (state['id'], p.uid, 1, json.dumps(state)))
        db.execute('INSERT INTO work_events(session,created,event) VALUES(?,?,?)',
                   (state['id'], 1, json.dumps({'raw': 'old protocol text'})))
    work.store = WorkStore(work.store.path)
    metadata = work.store.get(state['id'])
    assert metadata['legacy_runtime'] and 'items' not in metadata
    original = runtime_band.call
    async def reject(cap, args, target, timeout):
        if cap == 'work.adopt_page':
            raise ValueError('worker unavailable')
        return await original(cap, args, target, timeout)
    runtime_band.call = reject
    with pytest.raises(ValueError):
        await work.migrate(metadata)
    assert work.store.archive(state['id'])['state']['items'] == state['items']
    runtime_band.call = original
    await work.migrate(metadata)
    adopted = runtime_band.plugin.runtime.store.get(state['id'])
    assert adopted['items'] == state['items']
    assert adopted['thread_id'] == 'thread1'
    archive_files = list((runtime_band.plugin.runtime.store.path.parent / 'work-migrations').glob('*.json'))
    assert len(archive_files) == 1
    assert json.loads(archive_files[0].read_text())['events'][0]['event'] == json.dumps({'raw': 'old protocol text'})
    archive = work.store.archive(state['id'])
    assert 'items' not in archive['state'] and not archive['events']
    assert work.store.get(state['id'])['remote_runtime']


@pytest.mark.asyncio
async def test_active_state_changes_without_transcript_modification(portal):
    p = portal
    p.server._band = band = HistoryBand()
    work = p.account.work_web
    await work.sync_history(band.workers['host1'], 'codex', [p.uid])
    before = work.store.all()[0]
    assert not before['active']
    band.active = True
    await work.sync_history(band.workers['host1'], 'codex', [p.uid])
    after = work.store.get(before['id'])
    assert after['source_version'] == before['source_version']
    assert after['active'] and after['revision'] > before['revision']
    band.active = False
    await work.sync_history(band.workers['host1'], 'codex', [p.uid])
    assert not work.store.get(before['id'])['active']
