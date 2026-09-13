import asyncio
import json
import time
from types import SimpleNamespace
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from test_band_management import portal
from rook.remote.work_web import WorkStore, WorkWeb, project


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
async def test_session_runs_without_browser_and_recovers_server_state(portal):
    p=portal;p.server._band=band=FakeBand()
    async with TestClient(TestServer(p.app)) as client:
        ws,sid=await create(client,p)
        work=p.account.work_web
        await until(lambda:work.store.get(sid)['status']=='ready')
        await ws.send_json({'op':'message','id':'message-test-123','session':sid,'csrf':p.csrf,'text':'hello'})
        await until(lambda:work.store.get(sid)['status']=='working')
        await ws.close()
        band.emit({'method':'item/agentMessage/delta','params':{'itemId':'answer','delta':'Still working without a browser.'}})
        band.emit({'method':'item/commandExecution/requestApproval','id':99,'params':{'command':'test approval'}})
        await until(lambda:bool(work.store.get(sid)['pending']))
        # Stop only the web collector; the worker process remains available.
        await work.stop(p.app)
        rebuilt=WorkWeb(p.account)
        band.emit({'method':'item/agentMessage/delta','params':{'itemId':'answer','delta':' More output.'}})
        await rebuilt.drain(sid)
        s=rebuilt.store.get(sid)
        assert s['items']['answer']['text']=='Still working without a browser. More output.'
        assert s['pending']['99']['params']['command']=='test approval'
        assert len([c for c,a in band.calls if c=='proc.start'])==1
        # A second read never duplicates committed output.
        await rebuilt.drain(sid)
        assert rebuilt.store.get(sid)['items']['answer']['text']==s['items']['answer']['text']


@pytest.mark.asyncio
async def test_auth_csrf_owner_and_command_deduplication(portal):
    p=portal;p.server._band=band=FakeBand()
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
        await until(lambda:'88' in work.store.get(sid)['pending'])
        answer={'op':'answer','id':'answer-test-123','session':sid,'csrf':p.csrf,'request':'88','decision':'decline'}
        await ws.send_json(answer);await ws.send_json(answer)
        await until(lambda:not work.store.get(sid)['pending'])
        sent=[json.loads(a['data']) for cap,a in band.calls if cap=='proc.write']
        assert len([m for m in sent if m.get('id')==88 and 'result' in m])==1
        other=p.store.create_local('other','long enough password')
        with pytest.raises(Exception) as error:work.store.get(sid,other)
        assert error.value.status==404
        await ws.close()


def test_projection_unicode_and_durable_command_claim(tmp_path):
    store=WorkStore(tmp_path/'work.sqlite3')
    s=dict(id='1',owner='a',items={},order=[],pending={},status='working')
    project(s,{'method':'item/agentMessage/delta','params':{'itemId':'a','delta':'Hello 🦉'}})
    project(s,{'method':'item/completed','params':{'item':{'id':'a','type':'agentMessage','text':'Hello 🦉 world'}}})
    project(s,{'method':'turn/diff/updated','params':{'diff':'+hello'}})
    store.save(s,{'example':True})
    assert WorkStore(store.path).get('1')['items']['a']['text']=='Hello 🦉 world'
    assert store.claim('1','command') and not store.claim('1','command')
    assert len(s['order'])==1

class HistoryBand(FakeBand):
    def __init__(self):
        super().__init__()
        self.workers['host1']['caps'] = ['claude-history.pull', 'claude-history.read',
                                        'codex-history.pull', 'codex-history.read',
                                        'claude-history.read_page', 'codex-history.read_page']
        self.version = 1

    async def call(self, cap, args, target, timeout):
        self.calls.append((cap, args))
        if cap.endswith('.pull'):
            rows = [dict(session_id=f'source-{i}', title=f'Imported {i}', cwd='/tmp',
                         last_modified=self.version, size_bytes=self.version) for i in range(101)]
            offset = args.get('offset', 0)
            result = dict(ok=True, sessions=rows[offset:offset+args['limit']], total=len(rows))
        elif cap.endswith('.read_page'):
            rows = [dict(role='assistant', content=f'Message {i}') for i in range(501)]
            offset = args.get('offset', 0)
            batch = [dict(m, index=offset+i, content_offset=0) for i, m in enumerate(rows[offset:offset+20])]
            result = dict(ok=True, messages=batch, activity='ready',
                          truncated=offset+len(batch)<len(rows), next_offset=offset+len(batch), next_content_offset=0)
        else:
            raise AssertionError(cap)
        return {'ok': True, 'from': target, 'result': result}


@pytest.mark.asyncio
async def test_discovery_paginates_both_agents_and_preserves_review_status(portal):
    p = portal
    p.server._band = band = HistoryBand()
    work = p.account.work_web
    work.import_pause = 0
    host = band.workers['host1']
    for agent in ('claude', 'codex'):
        await work.sync_history(host, agent, [p.uid])
    sessions = work.store.all(p.uid)
    assert len(sessions) == 202
    assert {s['agent'] for s in sessions} == {'claude', 'codex'}
    s = sessions[0]
    sid = s['id']
    assert len(s['order']) == 501
    assert s['items']['500']['text'] == 'Message 500'
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
    assert WorkStore(work.store.path).get(sid)['items']['500']['text'] == 'Message 500'


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
    work.import_pause = 0
    host = band.workers['host1']
    await work.sync_history(host, 'claude', [p.uid])
    sid = work.store.all()[0]['id']
    await work.command(sid, dict(op='resume', id='resume-imported'))
    assert work.snapshot(work.store.get(sid))['external_running']
    assert 'external_handle' not in work.snapshot(work.store.get(sid))
    await work.drain_external(sid)
    assert work.store.get(sid)['terminal'] == 'terminal prompt'
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
    store = WorkStore(tmp_path/'work.sqlite3')
    state = dict(id='legacy', owner='operator', status='ready', items={'large':'x'*1000000}, order=['large'])
    store.save(state)
    with store.db() as db:
        db.execute('DROP TABLE work_index')
    restored = WorkStore(store.path)
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
    assert all(s['history_loading'] for s in work.store.all())
    assert all(cap.endswith('.pull') for cap, _ in band.calls)
