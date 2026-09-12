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
