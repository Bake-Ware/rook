import asyncio
import inspect
import json
import time
from types import SimpleNamespace
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

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

    async def call(self, cap, args, target, timeout, identity=None):
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

    async def call(self, cap, args, target, timeout, identity=None):
        if not cap.startswith('work.'):
            return await super().call(cap, args, target, timeout)
        self.calls.append((cap, args))
        result = self.plugin.caps()[cap](**args)
        if inspect.isawaitable(result):
            result = await result
        return {'ok': True, 'from': target, 'result': result}


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


class HistoryBand(FakeBand):
    def __init__(self):
        super().__init__()
        self.workers['host1']['caps'] = ['claude-history.pull', 'claude-history.read',
                                        'codex-history.pull', 'codex-history.read',
                                        'claude-history.read_page', 'codex-history.read_page',
                                        'claude-history.send', 'codex-history.send']
        self.version = 1

    async def call(self, cap, args, target, timeout, identity=None):
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


class FlakyHistoryBand(HistoryBand):
    """host1 pulls fine; host2's pull fails with ``self.fail`` while set."""
    def __init__(self):
        super().__init__()
        self.workers['host2'] = dict(self.workers['host1'], worker_id='host2', name='slow-host',
                                     caps=['claude-history.pull'])
        self.fail = TimeoutError()

    async def call(self, cap, args, target, timeout, identity=None):
        if target == 'host2' and self.fail:
            self.calls.append((cap, args))
            raise self.fail
        return await super().call(cap, args, target, timeout, identity)


